from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, NoReturn

from acp import PROTOCOL_VERSION, connect_to_agent, text_block
from acp.exceptions import RequestError
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    DeniedOutcome,
    Implementation,
    NewSessionResponse,
    PermissionOption,
    RequestPermissionResponse,
)

from agent_bridge.acp_config import config_option_values
from agent_bridge.adapters.base import STDIO_LIMIT, Adapter
from agent_bridge.claude_meta import (
    CLAUDE_MODE_BYPASS,
    apply_claude_gateway_env,
    resolve_claude_effort,
)
from agent_bridge.config import AgentConfig
from agent_bridge.devin_meta import DEVIN_MODE_BYPASS, apply_devin_env
from agent_bridge.diagnostics import redact_diagnostic as _redact_diagnostic
from agent_bridge.dsh_home import command_has_native_acp, prepare_dsh_launch, resolve_dsh_command
from agent_bridge.kimi_meta import KIMI_MODE_YOLO, resolve_kimi_thinking
from agent_bridge.minimax_meta import (
    MINIMAX_PERMISSION_BYPASS,
    minimax_model_labels,
    resolve_minimax_effort,
    resolve_minimax_model,
)
from agent_bridge.models import Session, Task, TaskFailure, TurnResult, dsh_effort, grok_effort
from agent_bridge.opencode_meta import resolve_opencode_effort
from agent_bridge.processes import (
    drop_pid,
    process_create_time,
    process_image_name,
    reap_subprocess,
    record_pid,
    resolve_command,
)
from agent_bridge.transcript import append_event
from agent_bridge.worker_env import build_worker_env, denied_by_policy
from agent_bridge.workspace import collect_update_paths
from agent_bridge.zcode_meta import (
    ZCODE_MODE_YOLO,
    resolve_zcode_effort,
    resolve_zcode_model,
    zcode_model_candidates,
)

STDERR_TAIL_LIMIT = 16000

log = logging.getLogger(__name__)

GROK_SET_MODEL_METHODS = ("session/setModel", "session/set_model")
# These workers' session/load replays persisted history as session/update
# notifications. session/resume is advertised and skips that replay.
_RESUME_AGENTS = frozenset({"kimi", "opencode", "claude", "zcode", "minimax"})
_CONFIG_OPTION_AGENTS = frozenset(
    {"kimi", "opencode", "claude", "devin", "cursor", "dsh", "zcode", "minimax"}
)
_MODEL_EFFORT_AGENTS = frozenset(
    {"grok", "dsh", "kimi", "opencode", "claude", "devin", "cursor", "zcode", "minimax"}
)

# Handshake-style RPCs (initialize, session/new, session/load, setModel)
# normally answer in seconds. A worker that wedges before the prompt would
# otherwise hang dispatch forever; prompt itself stays unbounded by design.
RPC_TIMEOUT_SEC = 60.0
CURSOR_MODEL_LIST_TIMEOUT_SEC = 30.0
PROMPT_CANCEL_GRACE_SEC = 10.0
EXIT_OBSERVE_SEC = 1.0

# ACP ToolKind values that can change the workspace. Read-only kinds (read,
# search, fetch, think, ...) also carry locations; counting them would report
# files the agent merely opened. Under-matching is safe: the disk snapshot
# diff in merge_files_changed still catches every real write.
MUTATING_TOOL_KINDS = {"edit", "delete", "move", "write", "create"}
_TOOL_IO_LIMIT = 500


def _tool_io_summary(value: Any) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= _TOOL_IO_LIMIT else text[:_TOOL_IO_LIMIT] + "…"


def _stderr_summary(text: str) -> str | None:
    hints = re.findall(
        r"\b(?:authentication required|not logged in|unauthorized|permission denied|"
        r"unsupported model|invalid model|model not found|connection (?:closed|refused|reset)|"
        r"timed out|timeout|quota exceeded|rate limit exceeded)\b",
        text, re.IGNORECASE,
    )
    return "; ".join(dict.fromkeys(hint.lower() for hint in hints))[:2048] or None


class AcpError(RuntimeError):
    def __init__(self, message: str, failure: TaskFailure) -> None:
        super().__init__(_redact_diagnostic(message))
        self.failure = failure


class AcpUnavailableError(AcpError):
    pass


class RpcTimeoutError(AcpUnavailableError):
    pass


def grok_session_meta(
    base: dict[str, Any] | None,
    model: str | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    """Hints for Grok ``session/new`` ``_meta`` (yolo + optional model/effort).

    Official pager/tests inject ``modelId`` and ``reasoningEffort`` there.
    ``yoloMode`` must stay explicit (absent is not false). Those hints are not
    sticky: Grok ``/new`` still lands on the campaign default (currently
    grok-4.6 xhigh). Bridge must ``session/setModel`` after the session exists.
    """
    meta = dict(base or {})
    if model:
        meta["modelId"] = model
    mapped = grok_effort(effort)
    if mapped:
        meta["reasoningEffort"] = mapped
    return meta


def with_grok_cli_selection(
    command: list[str],
    model: str | None,
    effort: str | None,
) -> list[str]:
    """Stamp Grok ``agent --model/--effort`` before ``stdio``.

    Process flags only seed the spawn default. Grok ``/new`` still resets to
    the campaign model; a later ``session/setModel`` is what sticks.
    """
    extras: list[str] = []
    if model:
        extras += ["--model", model]
    mapped = grok_effort(effort)
    if mapped:
        extras += ["--effort", mapped]
    if not extras:
        return list(command)
    cmd = list(command)
    for token in ("stdio", "headless", "serve"):
        if token in cmd:
            index = cmd.index(token)
            return cmd[:index] + extras + cmd[index:]
    return cmd + extras


def with_cursor_cli_model(command: list[str], model: str | None) -> list[str]:
    """Pin Cursor's model before its ACP subcommand."""
    cmd = list(command)
    if not model:
        return cmd
    try:
        index = cmd.index("acp")
    except ValueError:
        raise RuntimeError(
            "cursor model selection requires an 'acp' token in the configured command"
        ) from None
    return [*cmd[:index], "--model", model, *cmd[index:]]


def cursor_list_models_command(command: list[str]) -> list[str]:
    """Build the matching Cursor CLI model-discovery command."""
    cmd = list(command)
    try:
        index = cmd.index("acp")
    except ValueError:
        raise RuntimeError(
            "cursor model discovery requires an 'acp' token in the configured command"
        ) from None
    executable = Path(cmd[0]).name.lower()
    if executable in {"agent", "agent.exe"}:
        return [*cmd[:index], "models"]
    return [*cmd[:index], "--list-models"]


def parse_cursor_models(output: str) -> dict[str, str]:
    """Parse the stable ``<id> - <label>`` rows from ``--list-models``."""
    models: dict[str, str] = {}
    for raw_line in output.splitlines():
        model, separator, label = raw_line.strip().partition(" - ")
        if separator and model and label and not any(char.isspace() for char in model):
            models[model] = label
    return models


def _cursor_words(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", value.casefold()))


def _cursor_without_base(words: tuple[str, ...], base: str) -> tuple[str, ...]:
    remaining = list(words)
    for word in _cursor_words(base):
        if word in remaining:
            remaining.remove(word)
    return tuple(remaining)


def _cursor_choices(config_options: list[Any], option_id: str) -> list[tuple[str, str]]:
    for option in config_options:
        dumped = _dump(option)
        if not isinstance(dumped, dict) or dumped.get("id") != option_id:
            continue
        raw = dumped.get("options")
        if not isinstance(raw, list):
            return []
        choices: list[tuple[str, str]] = []
        pending = list(raw)
        while pending:
            entry = _dump(pending.pop(0))
            if not isinstance(entry, dict):
                continue
            nested = entry.get("options")
            if isinstance(nested, list) and "value" not in entry:
                pending[0:0] = nested
                continue
            value = entry.get("value")
            if isinstance(value, str):
                name = entry.get("name")
                choices.append((value, name if isinstance(name, str) else value))
        return choices
    return []


def _cursor_base_model(model: str, label: str, config_options: list[Any]) -> tuple[str, str]:
    choices = _cursor_choices(config_options, "model")
    for value, name in choices:
        if model == value:
            return value, name
    label_words = _cursor_words(label)
    matches = [
        (len(_cursor_words(name)), value, name)
        for value, name in choices
        if label_words[: len(_cursor_words(name))] == _cursor_words(name)
    ]
    if not matches:
        raise ValueError(
            f"cursor model {model!r} does not match ACP models "
            f"{[value for value, _name in choices] or '(none)'}"
        )
    _length, value, name = max(matches)
    return value, name


def _cursor_has_phrase(words: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    return bool(phrase) and any(
        words[index : index + len(phrase)] == phrase
        for index in range(len(words) - len(phrase) + 1)
    )


def _cursor_effort_option_ids(config_options: list[Any]) -> list[str]:
    ids: list[str] = []
    for option in config_options:
        dumped = _dump(option)
        if not isinstance(dumped, dict):
            continue
        option_id = dumped.get("id")
        offered = [value for value, _name in _cursor_choices(config_options, str(option_id))]
        if (
            isinstance(option_id, str)
            and set(offered) != {"true", "false"}
            and (
                dumped.get("category") == "thought_level"
                or option_id in {"effort", "reasoning", "reasoning_effort"}
            )
        ):
            ids.append(option_id)
    return ids


def _resolve_cursor_effort(effort: str, offered: list[str]) -> str | None:
    preferences = {
        "off": ("off", "none", "minimal", "low"),
        "low": ("low", "minimal"),
        "medium": ("medium", "high", "low"),
        "high": ("high", "xhigh", "medium"),
        "max": ("max", "xhigh", "high"),
    }
    return next(
        (candidate for candidate in preferences.get(effort, (effort,)) if candidate in offered),
        None,
    )


def _cursor_parameter_targets(
    model: str,
    label: str,
    base: str,
    base_name: str,
    effort: str | None,
    config_options: list[Any],
) -> dict[str, str]:
    label_words = _cursor_words(label)
    base_words = _cursor_words(base_name)
    suffix = label_words[len(base_words) :] if label_words[: len(base_words)] == base_words else label_words
    suffix = _cursor_without_base(suffix, base)
    model_words = _cursor_without_base(_cursor_words(model), base)
    targets: dict[str, str] = {}
    for option in config_options:
        dumped = _dump(option)
        if not isinstance(dumped, dict):
            continue
        option_id = dumped.get("id")
        if not isinstance(option_id, str) or option_id in {"mode", "model"}:
            continue
        choices = _cursor_choices(config_options, option_id)
        offered = [value for value, _name in choices]
        category = str(dumped.get("category") or "")
        if (
            effort
            and set(offered) != {"true", "false"}
            and (
                category == "thought_level"
                or option_id in {"effort", "reasoning", "reasoning_effort"}
            )
        ):
            target = _resolve_cursor_effort(effort, offered)
            if target:
                targets[option_id] = target
            continue
        if set(offered) == {"true", "false"}:
            enabled = _cursor_has_phrase(model_words, _cursor_words(option_id)) or any(
                value == "true"
                and (
                    _cursor_has_phrase(model_words, _cursor_words(name))
                    or _cursor_has_phrase(suffix, _cursor_words(name))
                )
                for value, name in choices
            )
            targets[option_id] = "true" if enabled else "false"
            continue
        matches = [
            (max(len(_cursor_words(value)), len(_cursor_words(name))), value)
            for value, name in choices
            if _cursor_has_phrase(model_words, _cursor_words(value))
            or _cursor_has_phrase(suffix, _cursor_words(name))
        ]
        if matches:
            targets[option_id] = max(matches)[1]
    return targets


def dsh_needs_respawn(
    applied_model: str | None,
    applied_effort: str | None,
    model: str | None,
    effort: str | None,
) -> bool:
    return applied_model != model or applied_effort != dsh_effort(effort)


def _dsh_native_pair(value: str) -> list[Any] | None:
    """Decode one native dsh model option value: ``["provider","model"]`` JSON."""
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, list) and len(parsed) == 2 else None


def dsh_native_model_value(model: str, config_options: list[Any]) -> str | None:
    """Map a coordinator ``provider/model`` or bare model onto an advertised value."""
    _current, offered = config_option_values(config_options, "model")
    provider = None
    wanted = model
    if "/" in model and not model.startswith("@"):
        provider, _, wanted = model.partition("/")
    parsed = [
        (value, str(pair[0]), str(pair[1]))
        for value in offered
        if (pair := _dsh_native_pair(value)) is not None
    ]
    if provider is not None:
        return next((value for value, p, m in parsed if p == provider and m == wanted), None)
    return next((value for value, _p, m in parsed if m == wanted), None)


def _dsh_native_model_label(value: str | None) -> str | None:
    pair = _dsh_native_pair(value) if value else None
    return f"{pair[0]}/{pair[1]}" if pair else None


def _dsh_native_model_labels(config_options: list[Any]) -> list[str]:
    _current, offered = config_option_values(config_options, "model")
    return [_dsh_native_model_label(value) or value for value in offered]


def _dump(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(obj, (dict, list, str, int, float, bool)):
        return obj
    return str(obj)


def _collect_paths(obj: Any, into: set[str]) -> None:
    collect_update_paths(obj, into)


def _has_diff_content(dumped: dict[str, Any]) -> bool:
    content = dumped.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(item, dict) and item.get("type") == "diff" for item in content)


def should_collect_tool_paths(
    type_name: str,
    dumped: Any,
    known_kinds: dict[str, str],
) -> bool:
    """Decide whether a session update may contribute to files_changed.

    Only tool calls with a mutating ``kind`` (or an explicit diff content)
    count. ``known_kinds`` maps toolCallId -> kind so that progress updates,
    which often omit ``kind``, inherit it from their start event.
    """
    if not isinstance(dumped, dict):
        return False
    if type_name not in {"ToolCallStart", "ToolCallProgress", "ToolCallUpdate"}:
        return False
    call_id = str(dumped.get("toolCallId") or "")
    kind = str(dumped.get("kind") or "").lower()
    if type_name == "ToolCallStart" and call_id:
        known_kinds[call_id] = kind
    if not kind and call_id:
        kind = known_kinds.get(call_id, "")
    return kind in MUTATING_TOOL_KINDS or _has_diff_content(dumped)


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if hasattr(content, "text") and isinstance(content.text, str):
        return content.text
    dumped = _dump(content)
    if isinstance(dumped, dict):
        if isinstance(dumped.get("text"), str):
            return dumped["text"]
        return json.dumps(dumped, ensure_ascii=False)
    if isinstance(dumped, list):
        return "".join(_text_of(item) for item in dumped)
    return str(dumped)


def _pick_permission_option(
    options: Iterable[PermissionOption],
    policy: str = "allow_once",
) -> PermissionOption | None:
    """Pick the permission option matching the agent's policy.

    Bridge is the only approver in the loop — there is no human to click
    "allow". Default policy ``allow_once`` grants each request for this turn
    only; ``allow_always`` additionally persists the grant inside the worker;
    ``deny`` cancels every request.
    """
    if policy == "deny":
        return None
    allow_always_first = policy == "allow_always"
    ranked: list[tuple[int, PermissionOption]] = []
    for option in options:
        kind = str(getattr(option, "kind", "")).lower().replace("_", "-")
        option_id = str(getattr(option, "option_id", "")).lower().replace("_", "-")
        blob = f"{kind} {option_id}"
        if "allow-always" in blob or kind == "allow-always":
            # Under any non-allow_always policy a persistent grant must not be
            # picked — falling back to it would make a one-time policy lie.
            if not allow_always_first:
                continue
            score = 0
        elif "allow-once" in blob or kind == "allow-once" or blob.strip().startswith("allow"):
            score = 1 if allow_always_first else 0
        else:
            score = 50 if allow_always_first else 90
        ranked.append((score, option))
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1] if ranked else None


async def _observe_exit(proc: asyncio.subprocess.Process | None) -> None:
    if proc is not None and proc.returncode is None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=EXIT_OBSERVE_SEC)


class _BridgeClient:
    def __init__(self, session_id: str, home: Path, permission_policy: str = "allow_once") -> None:
        self.session_id = session_id
        self.home = home
        self.permission_policy = permission_policy
        self.text_parts: list[str] = []
        self.files: set[str] = set()
        self.usage: dict[str, Any] = {}
        self.tool_kinds: dict[str, str] = {}

    def reset_turn(self) -> None:
        self.text_parts = []
        self.files = set()
        self.usage = {}
        self.tool_kinds = {}

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[Any] | None,
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        option = _pick_permission_option(options or [], self.permission_policy)
        dumped = _dump(tool_call)
        if option is None:
            append_event(
                self.session_id,
                "permission",
                {"decision": "cancelled", "tool_call": dumped},
                self.home,
            )
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        append_event(
            self.session_id,
            "permission",
            {"decision": "selected", "option_id": option.option_id, "tool_call": dumped},
            self.home,
        )
        return RequestPermissionResponse(
            outcome=AllowedOutcome(option_id=option.option_id, outcome="selected")
        )

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        dumped = _dump(update)
        type_name = type(update).__name__
        text = ""
        event_type = "raw"
        if type_name == "AgentMessageChunk":
            event_type = "message_chunk"
            text = _text_of(getattr(update, "content", dumped))
            if text:
                self.text_parts.append(text)
        elif type_name == "AgentThoughtChunk":
            event_type = "thought_chunk"
            text = _text_of(getattr(update, "content", dumped))
        elif type_name == "UserMessageChunk":
            event_type = "raw"
        elif type_name == "ToolCallStart":
            event_type = "tool_call"
        elif type_name in {"ToolCallProgress", "ToolCallUpdate"}:
            event_type = "tool_call_update"
        elif type_name in {"AgentPlanUpdate", "AgentPlanContentUpdate"}:
            event_type = "plan"
        elif type_name == "UsageUpdate":
            event_type = "raw"
            self.usage = dumped if isinstance(dumped, dict) else {"raw": dumped}
        if should_collect_tool_paths(type_name, dumped, self.tool_kinds):
            _collect_paths(dumped, self.files)
        data: dict[str, Any] = {"update_type": type_name}
        if text:
            data["text"] = text
        if isinstance(dumped, dict):
            title = dumped.get("title")
            if title:
                data["title"] = title
            if dumped.get("path"):
                data["path"] = dumped["path"]
        if event_type in {"tool_call", "tool_call_update"} and isinstance(dumped, dict):
            for src, dst in (("toolCallId", "tool_call_id"), ("kind", "kind"), ("status", "status")):
                if dumped.get(src) is not None:
                    data[dst] = dumped[src]
            locations = dumped.get("locations")
            if isinstance(locations, list):
                paths = [loc.get("path") for loc in locations if isinstance(loc, dict) and loc.get("path")]
                if paths:
                    data["locations"] = paths[:20]
            if event_type == "tool_call":
                summary = _tool_io_summary(dumped.get("rawInput"))
                if summary is not None:
                    data["input"] = summary
            elif dumped.get("status") == "failed":
                summary = _tool_io_summary(dumped.get("rawOutput"))
                if summary is not None:
                    data["output"] = summary
        append_event(self.session_id, event_type, data, self.home)

    async def write_text_file(self, session_id: str, path: str, content: str, **kwargs: Any) -> NoReturn:
        raise RuntimeError("filesystem client methods are disabled")

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> NoReturn:
        raise RuntimeError("filesystem client methods are disabled")

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[Any] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> NoReturn:
        raise RuntimeError("terminal client methods are disabled")

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> NoReturn:
        raise RuntimeError("terminal client methods are disabled")

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> None:
        return None

    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs: Any) -> NoReturn:
        raise RuntimeError("terminal client methods are disabled")

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> None:
        return None

    async def create_elicitation(self, message: str, mode: Any, **kwargs: Any) -> Any:
        from acp.schema import DeclineElicitationResponse

        return DeclineElicitationResponse(action="decline")

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        return None

    async def ext_method(self, method: str, params: Any) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: Any) -> None:
        return None

    def on_connect(self, conn: Any) -> None:
        return None


class _Live:
    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.conn: Any = None
        self.client: _BridgeClient | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.stderr_tail = ""
        self.prompt_task: asyncio.Task[Any] | None = None
        self.applied_model: str | None = None
        self.applied_effort: str | None = None
        self.applied_mode: str | None = None
        # Latest `configOptions` snapshot from a supported ACP lifecycle or
        # set_config_option response: the only place model-specific parameters
        # such as thinking level and fast mode are published.
        self.config_options: list[Any] = []
        # A resolved dsh launch on `--profile acp` selects model/effort through
        # config options; the legacy demo read DSH_ACP_* spawn env instead.
        self.dsh_native_acp = False
        self.dsh_env_model: str | None = None
        # Warnings raised outside a turn (e.g. during ensure_session);
        # drained into the next TurnResult.
        self.pending_warnings: list[str] = []


class _DiscoveryClient(_BridgeClient):
    """ACP discovery client that denies permission requests and discards updates."""

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[Any] | None,
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        return None


def _acp_model_catalog(response: NewSessionResponse, agent_name: str) -> dict[str, Any]:
    """Extract model choices and current-model effort levels from an ACP snapshot."""
    options = response.config_options or []
    model_option_id = next(
        (option.id for option in options if option.id == "model" or option.category == "model"),
        "model",
    )
    current, _offered = config_option_values(options, model_option_id)

    def model_id(value: str) -> str:
        return (_dsh_native_model_label(value) or value) if agent_name == "dsh" else value

    default = model_id(current) if current else None
    choices = [(model_id(value), name) for value, name in _cursor_choices(options, model_option_id)]
    efforts: list[str] | None = None
    for option in options:
        if option.category == "thought_level" or option.id in {
            "thinking", "reasoning_effort",
        }:
            _current_effort, offered = config_option_values(options, option.id)
            # A boolean switch describes thinking enablement, not effort levels.
            if offered and set(offered) != {"true", "false"}:
                efforts = offered
                break
    return {
        "source": "acp",
        "models": [
            {
                "id": value,
                "name": name,
                "is_default": value == default,
                "supported_efforts": efforts if value == default else None,
            }
            for value, name in choices
        ],
        "default_model": default,
        "warnings": [] if choices else [
            "Model discovery is unavailable: the ACP session did not advertise a model catalog."
        ],
    }


async def _cleanup_discovery(live: _Live) -> None:
    try:
        if live.conn is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(live.conn.close(), timeout=2)
    finally:
        try:
            if live.proc is not None:
                await reap_subprocess(live.proc)
        finally:
            if live.stderr_task is not None:
                live.stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await live.stderr_task


class AcpAdapter(Adapter):
    def __init__(self, agent: AgentConfig, home: Path, env_config=None) -> None:
        super().__init__(agent, home, env_config)
        self._live: dict[str, _Live] = {}
        self._cursor_models_cache: dict[str, str] | None = None

    async def list_models(self, cwd: str) -> dict[str, Any]:
        """Read a catalog through a separate ACP initialize/session-new handshake.

        The caller controls the deadline; this method closes the process on exit.
        """
        if self.agent.name == "dsh":
            cmd = resolve_dsh_command(self.agent.command, self.agent.fallback_commands)
        else:
            cmd = resolve_command(self.agent.command, self.agent.fallback_commands)
        env = self._env()
        if self.agent.name == "dsh":
            cmd, env = prepare_dsh_launch(cmd, env)
        env = self._enforce_spawn_env(env)
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        live = _Live()
        try:
            live.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.agent.cwd or cwd or None,
                limit=STDIO_LIMIT,
                **kwargs,
            )
            proc = live.proc
            client = _DiscoveryClient("model-discovery", self.home, permission_policy="deny")
            live.conn = connect_to_agent(client, proc.stdin, proc.stdout)
            live.stderr_task = asyncio.create_task(self._drain_stderr(proc, "model-discovery", live))
            await live.conn.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_info=Implementation(name="agent-bridge", version="0.1.0"),
                client_capabilities=ClientCapabilities(),
            )
            created = await live.conn.new_session(cwd=cwd, mcp_servers=[])
            return _acp_model_catalog(created, self.agent.name)
        except Exception as exc:
            raise self._failure(
                exc, "model_discovery", proc=live.proc, stderr=live.stderr_tail,
            ) from None
        finally:
            # The outer deadline can fire during close(), after the handshake
            # already succeeded. Keep cleanup alive and await the reap before
            # propagating that cancellation to the registry.
            cleanup = asyncio.create_task(_cleanup_discovery(live))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise

    def _env(self) -> dict[str, str]:
        env = build_worker_env(self.agent.env, config=self.env_config, worker_context=True, home=self.home)
        if self.agent.name == "claude":
            # The gateway rewrite exists only to install ANTHROPIC_AUTH_TOKEN;
            # when the user denies that token, skip the rewrite entirely so a
            # direct ANTHROPIC_API_KEY is not blanked for a token the gate
            # would strip anyway.
            if not denied_by_policy(
                "ANTHROPIC_AUTH_TOKEN",
                self.env_config,
                set(self.env_config.set) | set(self.agent.env),
            ):
                env = apply_claude_gateway_env(env)
        elif self.agent.name == "devin":
            env = apply_devin_env(env)
        # Gateway derivation above can recreate a denied key; the deny gate
        # runs on the rewritten env, not on build_worker_env's output.
        return self._enforce_spawn_env(env)

    async def _drain_stderr(
        self,
        proc: asyncio.subprocess.Process,
        session_id: str,
        live: _Live,
    ) -> None:
        if proc.stderr is None:
            return
        try:
            while True:
                chunk = await proc.stderr.read(65536)
                if not chunk:
                    return
                live.stderr_tail = (
                    live.stderr_tail + chunk.decode("utf-8", errors="replace")
                )[-STDERR_TAIL_LIMIT:]
        except (ValueError, OSError):
            log.warning("stderr drain aborted for %s", session_id, exc_info=True)

    def _failure(
        self, exc: Exception, operation: str, session: Session | None = None, *,
        proc: asyncio.subprocess.Process | None = None, stderr: str | None = None,
        prompt_may_have_been_sent: bool = False, kind: str | None = None,
    ) -> AcpError:
        if isinstance(exc, AcpError):
            return exc
        if isinstance(exc.__cause__, AcpError):
            return type(exc.__cause__)(str(exc), exc.__cause__.failure)
        live = self._live.get(session.session_id) if session else None
        proc = proc if proc is not None else live.proc if live else None
        exit_code = proc.returncode if proc is not None else None
        if kind is None:
            if isinstance(exc, TimeoutError):
                kind = "timeout"
            elif isinstance(exc, RequestError) and exc.code == -32000:
                kind = "auth_required"
            elif isinstance(exc, RequestError):
                kind = "protocol_error"
            elif exit_code is not None:
                kind = "worker_exit"
            elif isinstance(exc, (ConnectionError, EOFError)):
                kind = "connection_closed"
            else:
                kind = "unknown"
        failure = TaskFailure(
            operation=operation, kind=kind, exit_code=exit_code,
            prompt_may_have_been_sent=prompt_may_have_been_sent,
            stderr_summary=_stderr_summary(stderr if stderr is not None else live.stderr_tail if live else ""),
        )
        error_type = (
            RpcTimeoutError if kind == "timeout" else
            AcpUnavailableError if kind in {"auth_required", "connection_closed", "worker_exit"} else AcpError
        )
        return error_type(str(exc) or type(exc).__name__, failure)

    async def _rpc(
        self,
        coro: Any,
        what: str,
        session: Session,
        timeout: float = RPC_TIMEOUT_SEC,
    ) -> Any:
        """Await a handshake RPC with a timeout; kill the worker on hang."""
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                exc = TimeoutError(f"{self.agent.name} {what} timed out after {int(timeout)}s")
            if isinstance(exc, (ConnectionError, EOFError)):
                live = self._live.get(session.session_id)
                await _observe_exit(live.proc if live else None)
            error = self._failure(exc, what, session)
            if (
                isinstance(error, RpcTimeoutError)
                or what in {"initialize", "session/new"}
                or (what in {"session/load", "session/resume"} and isinstance(error, AcpUnavailableError))
            ):
                log.error("%s; stderr_summary=%r", error, error.failure.stderr_summary)
                await self.shutdown(session)
            raise error from None

    async def _cursor_models(
        self,
        command: list[str],
        env: dict[str, str],
        cwd: str | None,
    ) -> dict[str, str]:
        if self._cursor_models_cache is not None:
            return self._cursor_models_cache
        env = self._enforce_spawn_env(env)
        try:
            model_cmd = cursor_list_models_command(command)
            proc = await asyncio.create_subprocess_exec(
                *model_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
            )
        except Exception as exc:
            raise self._failure(exc, "model_discovery") from None
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=CURSOR_MODEL_LIST_TIMEOUT_SEC
            )
        except asyncio.CancelledError:
            await reap_subprocess(proc)
            raise
        except TimeoutError:
            error = self._failure(
                TimeoutError("cursor model discovery timed out; run 'cursor-agent --list-models' to check the current account"),
                "model_discovery", proc=proc,
            )
            await reap_subprocess(proc)
            raise error from None
        if proc.returncode:
            raise self._failure(
                RuntimeError("cursor model discovery failed"), "model_discovery",
                proc=proc, stderr=stderr.decode("utf-8", errors="replace"),
            )
        models = parse_cursor_models(stdout.decode("utf-8", errors="replace"))
        if not models:
            raise self._failure(
                RuntimeError("cursor model discovery returned no model IDs; run 'cursor-agent --list-models' to check the current account"),
                "model_discovery", proc=proc, stderr=stderr.decode("utf-8", errors="replace"),
                kind="unknown",
            )
        self._cursor_models_cache = models
        return models

    async def _spawn(self, session: Session) -> _Live:
        await self.shutdown(session)
        if self.agent.name == "dsh":
            cmd = resolve_dsh_command(self.agent.command, self.agent.fallback_commands)
        else:
            cmd = resolve_command(self.agent.command, self.agent.fallback_commands)
        env = self._env()
        if self.agent.name == "dsh":
            cmd, env = prepare_dsh_launch(
                cmd,
                env,
                session_id=session.session_id,
                model=session.model,
                effort=session.effort,
            )
        elif self.agent.name == "grok":
            cmd = with_grok_cli_selection(cmd, session.model, session.effort)
        elif self.agent.name == "cursor" and session.model:
            models = await self._cursor_models(
                cmd,
                env,
                self.agent.cwd or session.cwd or None,
            )
            if session.model not in models:
                raise self._failure(
                    ValueError(f"cursor model {session.model!r} is not available for the current account; "
                               f"available model IDs: {', '.join(models)}"),
                    "model_discovery", session, kind="unsupported_model",
                )
            cmd = with_cursor_cli_model(cmd, session.model)
        env = self._enforce_spawn_env(env)
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.agent.cwd or session.cwd or None,
            limit=STDIO_LIMIT,
            **kwargs,
        )
        if proc.stdin is None or proc.stdout is None:
            raise RuntimeError(f"{self.agent.name} did not expose stdio")
        live = _Live()
        live.proc = proc
        live.client = _BridgeClient(
            session.session_id, self.home, self.agent.permission_policy
        )
        live.conn = connect_to_agent(live.client, proc.stdin, proc.stdout)
        live.stderr_task = asyncio.create_task(
            self._drain_stderr(proc, session.session_id, live)
        )
        self._live[session.session_id] = live
        session.pid = proc.pid
        session.pid_create_time = process_create_time(proc.pid) if proc.pid else None
        session.image_name = process_image_name(proc.pid) if proc.pid else None
        if proc.pid:
            record_pid(self.home, session.session_id, proc.pid, session.pid_create_time, session.image_name)
        capabilities = ClientCapabilities(
            field_meta={"parameterizedModelPicker": True}
            if self.agent.name == "cursor"
            else None
        )
        await self._rpc(
            live.conn.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_info=Implementation(name="agent-bridge", version="0.1.0"),
                client_capabilities=capabilities,
            ),
            "initialize",
            session,
        )
        if self.agent.name == "dsh":
            live.dsh_native_acp = command_has_native_acp(cmd)
            if live.dsh_native_acp:
                provider = env.get("DSH_ACP_PROVIDER")
                model_name = env.get("DSH_ACP_MODEL")
                live.dsh_env_model = (
                    f"{provider}/{model_name}" if provider and model_name else model_name
                )
            else:
                live.applied_model = session.model
                live.applied_effort = dsh_effort(session.effort)
        return live

    def _new_session_meta(self, session: Session) -> dict[str, Any]:
        base = self.agent.session_meta or None
        if self.agent.name != "grok":
            return dict(base or {})
        return grok_session_meta(base, session.model, session.effort)

    async def _call_new_session(self, conn: Any, cwd: str, meta: dict[str, Any] | None) -> Any:
        extra = dict(meta or {})
        return await conn.new_session(cwd=cwd, mcp_servers=[], **extra)

    async def _call_load_session(
        self, conn: Any, cwd: str, native_id: str, *, use_resume: bool | None = None
    ) -> Any:
        if use_resume is None:
            use_resume = self.agent.name in _RESUME_AGENTS
        if use_resume:
            # kimi/opencode/claude/zcode/minimax replay persisted history as
            # session/update on load; resume skips it. Native dsh implements
            # only resume. ZCode replays on resume only for its own TUI client.
            return await conn.resume_session(
                session_id=native_id,
                cwd=cwd,
                mcp_servers=[],
            )
        return await conn.load_session(
            cwd=cwd,
            session_id=native_id,
            mcp_servers=[],
            noReplay=True,
        )

    async def _call_grok_set_model(
        self,
        conn: Any,
        native_id: str,
        model: str,
        effort: str | None,
    ) -> None:
        params: dict[str, Any] = {"sessionId": native_id, "modelId": model}
        if effort:
            params["_meta"] = {"reasoningEffort": effort}
        sender = getattr(conn, "_conn", None)
        send = getattr(sender, "send_request", None)
        if send is None:
            raise RuntimeError("ACP connection cannot send session/setModel")
        last: Exception | None = None
        for method in GROK_SET_MODEL_METHODS:
            try:
                await send(method, params)
                return
            except Exception as exc:
                last = exc
                code = getattr(exc, "code", None)
                if code != -32601:
                    raise
        if last is not None:
            raise last

    async def _sync_grok_model(self, live: _Live, session: Session) -> None:
        if self.agent.name != "grok" or live.conn is None or not session.native_session_id:
            return
        effort = grok_effort(session.effort)
        model = session.model
        if live.applied_model == model and live.applied_effort == effort:
            return
        if not model:
            if effort and live.applied_effort != effort:
                message = (
                    f"grok effort={effort} ignored on live session "
                    f"{session.session_id} without a model; "
                    "Grok session/setModel needs a modelId"
                )
                log.warning("%s", message)
                live.pending_warnings.append(message)
            return
        await self._rpc(
            self._call_grok_set_model(live.conn, session.native_session_id, model, effort),
            "session/setModel",
            session,
        )
        live.applied_model = model
        live.applied_effort = effort

    def _remember_config_options(self, live: _Live, response: Any) -> None:
        """Cache the `configOptions` snapshot a lifecycle response carries."""
        if self.agent.name not in _CONFIG_OPTION_AGENTS:
            return
        options = getattr(response, "config_options", None)
        if isinstance(options, (list, tuple)):
            live.config_options = list(options)

    async def _set_config_option(
        self,
        live: _Live,
        session: Session,
        config_id: str,
        value: str,
    ) -> None:
        assert live.conn is not None
        response = await self._rpc(
            live.conn.set_config_option(
                config_id=config_id,
                session_id=session.native_session_id,
                value=value,
            ),
            f"session/set_config_option {config_id}",
            session,
        )
        self._remember_config_options(live, response)

    def _remember_applied_model(self, live: _Live, model: str) -> None:
        """Record the live model and drop a stale per-model effort.

        OpenCode (and Kimi thinking) reset the variant when the model
        changes. Keeping ``applied_effort`` from the previous model makes the
        next sync skip the RPC and leave the new model's default in place.
        """
        if live.applied_model != model:
            live.applied_effort = None
        live.applied_model = model

    async def _sync_model_option(self, live: _Live, session: Session) -> None:
        """Apply ``session.model`` through the typed ``model`` config option.

        A model the coordinator named explicitly and that this session does
        not offer must fail the turn, not silently run on the default. The
        error names the real options.
        """
        if not session.model or session.model == live.applied_model:
            return
        current_model, _ = config_option_values(live.config_options, "model")
        if session.model == current_model:
            # Already the session's model (commonly the default a coordinator
            # names explicitly).
            self._remember_applied_model(live, session.model)
            return
        try:
            await self._set_config_option(live, session, "model", session.model)
        except AcpUnavailableError:
            raise
        except Exception as exc:
            _, offered = config_option_values(live.config_options, "model")
            raise RuntimeError(
                f"{self.agent.name} rejected model {session.model!r}; "
                f"session advertises {offered or 'no models'}"
            ) from exc
        self._remember_applied_model(live, session.model)

    async def _sync_cursor_selection(self, live: _Live, session: Session) -> None:
        if self.agent.name != "cursor" or live.conn is None or not session.native_session_id:
            return
        if session.model:
            command = resolve_command(self.agent.command, self.agent.fallback_commands)
            catalog = await self._cursor_models(
                command,
                self._env(),
                self.agent.cwd or session.cwd or None,
            )
            label = catalog.get(session.model)
            if label is None:
                raise self._failure(
                    ValueError(f"cursor model {session.model!r} is not available for the current account; "
                               f"available model IDs: {', '.join(catalog)}"),
                    "model_discovery", session, kind="unsupported_model",
                )
            base, base_name = _cursor_base_model(session.model, label, live.config_options)
            current, _offered = config_option_values(live.config_options, "model")
            if current != base:
                await self._set_config_option(live, session, "model", base)
                current, _offered = config_option_values(live.config_options, "model")
                if current != base:
                    raise RuntimeError(
                        f"cursor confirmed model {current!r} after selecting {base!r}"
                    )
            targets = _cursor_parameter_targets(
                session.model,
                label,
                base,
                base_name,
                session.effort,
                live.config_options,
            )
        else:
            targets = _cursor_parameter_targets(
                "",
                "",
                "",
                "",
                session.effort,
                live.config_options,
            )
        effort_ids = _cursor_effort_option_ids(live.config_options)
        effort_mapped = not session.effort or any(
            option_id in targets for option_id in effort_ids
        )
        for option_id, target in targets.items():
            current, offered = config_option_values(live.config_options, option_id)
            if target not in offered:
                raise RuntimeError(
                    f"cursor parameter {option_id}={target!r} is not advertised; "
                    f"session offers {offered or '(none)'}"
                )
            if current != target:
                await self._set_config_option(live, session, option_id, target)
                current, _offered = config_option_values(live.config_options, option_id)
                if current != target:
                    raise RuntimeError(
                        f"cursor confirmed {option_id}={current!r} after selecting {target!r}"
                    )
        live.applied_model = session.model
        applied_effort_id = next(
            (option_id for option_id in effort_ids if option_id in targets),
            effort_ids[0] if effort_ids else None,
        )
        live.applied_effort = (
            config_option_values(live.config_options, applied_effort_id)[0]
            if applied_effort_id
            else None
        )
        if not effort_mapped:
            message = (
                f"cursor effort={session.effort} has no counterpart on "
                f"{session.model or 'the current model'}"
            )
            log.warning("%s", message)
            live.pending_warnings.append(message)

    async def _sync_bypass_mode(self, live: _Live, session: Session, mode_id: str) -> None:
        """Switch a session that starts in a manual mode to ``mode_id``.

        A missing or rejected mode only warns: ACP ``requestPermission``
        still auto-allows every tool call, so the turn runs either way.
        """
        if live.applied_mode == mode_id:
            return
        current_mode, offered_modes = config_option_values(live.config_options, "mode")
        if current_mode == mode_id:
            live.applied_mode = mode_id
            return
        if offered_modes and mode_id not in offered_modes:
            message = (
                f"{self.agent.name} {mode_id} is not advertised "
                f"(session offers mode {offered_modes}); "
                "ACP requestPermission will auto-allow instead"
            )
            log.warning("%s", message)
            live.pending_warnings.append(message)
            return
        try:
            await self._rpc(
                live.conn.set_session_mode(
                    session_id=session.native_session_id,
                    mode_id=mode_id,
                ),
                "session/set_mode",
                session,
            )
        except AcpUnavailableError:
            raise
        except Exception as exc:
            message = (
                f"{self.agent.name} rejected mode={mode_id}: {exc}; "
                "ACP requestPermission will auto-allow instead"
            )
            log.warning("%s", message)
            live.pending_warnings.append(message)
            return
        live.applied_mode = mode_id

    async def _sync_kimi_selection(self, live: _Live, session: Session) -> None:
        """Force yolo mode, then apply model and thinking for a Kimi session.

        Kimi accepts no ``session/new`` hints: a fresh session lands in
        ``default`` mode (approval per tool call) on the configured default
        model. Mode, model and thinking are all set afterwards through the
        typed ACP config surface, whose responses carry the refreshed option
        snapshot — that snapshot is how the next turn knows it has nothing to
        change.
        """
        if self.agent.name != "kimi" or live.conn is None or not session.native_session_id:
            return
        if live.applied_mode != KIMI_MODE_YOLO:
            await self._rpc(
                live.conn.set_session_mode(
                    session_id=session.native_session_id,
                    mode_id=KIMI_MODE_YOLO,
                ),
                "session/set_mode",
                session,
            )
            live.applied_mode = KIMI_MODE_YOLO
        await self._sync_model_option(live, session)
        # Order matters: thinking vocabularies are per model, and the set above
        # refreshed the snapshot this reads.
        await self._sync_kimi_thinking(live, session)

    async def _sync_kimi_thinking(self, live: _Live, session: Session) -> None:
        if not session.effort:
            return
        current, offered = config_option_values(live.config_options, "thinking")
        level = resolve_kimi_thinking(session.effort, offered)
        if level is None:
            message = (
                f"kimi effort={session.effort} has no counterpart on "
                f"{live.applied_model or 'the current model'}; "
                f"session advertises thinking {offered or '(none)'}"
            )
            log.warning("%s", message)
            live.pending_warnings.append(message)
            return
        if level == current:
            live.applied_effort = level
            return
        try:
            await self._set_config_option(live, session, "thinking", level)
        except AcpUnavailableError:
            raise
        except Exception as exc:
            # Bridge chose this level by mapping, so a rejection is Bridge's
            # problem to report; the turn still runs on the model's default.
            message = f"kimi rejected thinking={level} for effort={session.effort}: {exc}"
            log.warning("%s", message)
            live.pending_warnings.append(message)
            return
        live.applied_effort = level

    async def _sync_opencode_selection(self, live: _Live, session: Session) -> None:
        """Apply model and variant for an OpenCode ACP session.

        OpenCode has no product login and no yolo mode on the ACP path.
        Permissions go through requestPermission (Bridge auto-picks
        allow-always). Model is ``provider/model``; effort is the current
        model's variants and may be absent entirely.
        """
        if self.agent.name != "opencode" or live.conn is None or not session.native_session_id:
            return
        await self._sync_model_option(live, session)
        await self._sync_opencode_effort(live, session)

    async def _sync_opencode_effort(self, live: _Live, session: Session) -> None:
        if not session.effort:
            return
        current, offered = config_option_values(live.config_options, "effort")
        level = resolve_opencode_effort(session.effort, offered)
        if level is None:
            message = (
                f"opencode effort={session.effort} has no counterpart on "
                f"{live.applied_model or 'the current model'}; "
                f"session advertises effort {offered or '(none)'}"
            )
            log.warning("%s", message)
            live.pending_warnings.append(message)
            return
        if level == current:
            live.applied_effort = level
            return
        try:
            await self._set_config_option(live, session, "effort", level)
        except AcpUnavailableError:
            raise
        except Exception as exc:
            message = f"opencode rejected effort={level} for effort={session.effort}: {exc}"
            log.warning("%s", message)
            live.pending_warnings.append(message)
            return
        live.applied_effort = level

    async def _sync_claude_selection(self, live: _Live, session: Session) -> None:
        """Force bypassPermissions, then apply model and effort for Claude Code.

        Product ``claude`` is not an ACP server. The worker is
        ``claude-agent-acp``, which starts in ``default`` (manual) mode.
        Mode, model and effort are set afterwards through the typed ACP
        surface. ``session/load`` replays history; Bridge uses resume.
        """
        if self.agent.name != "claude" or live.conn is None or not session.native_session_id:
            return
        await self._sync_bypass_mode(live, session, CLAUDE_MODE_BYPASS)
        await self._sync_model_option(live, session)
        await self._sync_claude_effort(live, session)

    async def _sync_devin_selection(self, live: _Live, session: Session) -> None:
        """Force bypass, then apply the model for a Devin CLI session.

        ``devin acp`` starts every session in ``accept-edits`` regardless of
        ``DEVIN_PERMISSION_MODE``. Model ids already carry the level
        (``swe-1-7-medium``, ``claude-opus-5-high``); there is no effort
        option, so a Bridge effort is reported as ignored once per value.
        """
        if self.agent.name != "devin" or live.conn is None or not session.native_session_id:
            return
        await self._sync_bypass_mode(live, session, DEVIN_MODE_BYPASS)
        await self._sync_model_option(live, session)
        if session.effort and live.applied_effort != session.effort:
            message = (
                f"devin has no effort option; effort={session.effort} ignored. "
                "Pick a model id that carries the level instead "
                "(e.g. swe-1-7-medium, claude-opus-5-high)"
            )
            log.warning("%s", message)
            live.pending_warnings.append(message)
            live.applied_effort = session.effort

    async def _sync_zcode_selection(self, live: _Live, session: Session) -> None:
        """Force yolo, then apply model and thought level for a ZCode session.

        The adapter's default create mode is yolo, but ``ZCODE_ACP_MODE`` and
        the user's saved session mode can land elsewhere. Model values are
        ``provider\\model``; a bare id is accepted only when one advertised
        option has that id. Thought level is per model.
        """
        if self.agent.name != "zcode" or live.conn is None or not session.native_session_id:
            return
        await self._sync_named_option(
            live,
            session,
            "mode",
            ZCODE_MODE_YOLO,
            missing=(
                f"zcode mode {ZCODE_MODE_YOLO} is not advertised; "
                "ACP requestPermission will auto-allow instead"
            ),
        )
        await self._sync_zcode_model(live, session)
        await self._sync_mapped_effort(
            live,
            session,
            "thought",
            resolve_zcode_effort,
            noun="thought",
        )

    async def _sync_zcode_model(self, live: _Live, session: Session) -> None:
        if not session.model or session.model == live.applied_model:
            return
        current, offered = config_option_values(live.config_options, "model")
        target = resolve_zcode_model(session.model, offered)
        if target is None:
            matches = zcode_model_candidates(session.model, offered)
            if len(matches) > 1:
                raise RuntimeError(
                    f"zcode model {session.model!r} matches more than one advertised option: "
                    f"{matches}"
                )
            raise RuntimeError(
                f"zcode rejected model {session.model!r}; "
                f"session advertises {offered or 'no models'}"
            )
        if current != target:
            try:
                await self._set_config_option(live, session, "model", target)
            except AcpUnavailableError:
                raise
            except Exception as exc:
                _, refreshed = config_option_values(live.config_options, "model")
                raise RuntimeError(
                    f"zcode rejected model {session.model!r}; "
                    f"session advertises {refreshed or offered or 'no models'}"
                ) from exc
        self._remember_applied_model(live, session.model)

    async def _sync_minimax_selection(self, live: _Live, session: Session) -> None:
        """Force bypassPermissions, then apply model and thinking effort.

        ``permissionMode`` is not the ACP session mode (that one is only
        ``default`` / ``plan``). Model slugs are ``provider/model`` or
        ``provider/model#variant``; the wire value is the CLI's ``m:``
        encoding. ``thinkingEffort`` is per model and may be absent.
        """
        if self.agent.name != "minimax" or live.conn is None or not session.native_session_id:
            return
        await self._sync_named_option(
            live,
            session,
            "permissionMode",
            MINIMAX_PERMISSION_BYPASS,
            missing=(
                "minimax permissionMode=bypassPermissions is not advertised; "
                "ACP requestPermission will auto-allow instead"
            ),
        )
        await self._sync_minimax_model(live, session)
        await self._sync_mapped_effort(
            live,
            session,
            "thinkingEffort",
            resolve_minimax_effort,
            noun="thinkingEffort",
        )

    async def _sync_minimax_model(self, live: _Live, session: Session) -> None:
        if not session.model or session.model == live.applied_model:
            return
        current, offered = config_option_values(live.config_options, "model")
        target = resolve_minimax_model(session.model, offered)
        if target is None:
            raise RuntimeError(
                f"minimax rejected model {session.model!r}; "
                f"session advertises {minimax_model_labels(offered) or 'no models'}"
            )
        if current != target:
            try:
                await self._set_config_option(live, session, "model", target)
            except AcpUnavailableError:
                raise
            except Exception as exc:
                _, refreshed = config_option_values(live.config_options, "model")
                labels = minimax_model_labels(refreshed or offered)
                raise RuntimeError(
                    f"minimax rejected model {session.model!r}; "
                    f"session advertises {labels or 'no models'}"
                ) from exc
        self._remember_applied_model(live, session.model)

    async def _sync_named_option(
        self,
        live: _Live,
        session: Session,
        option_id: str,
        value: str,
        *,
        missing: str,
    ) -> None:
        """Set one select option, warning instead of failing when it will not stick.

        Used for permission/mode switches. A missing or rejected value still
        leaves ACP ``requestPermission`` to auto-allow tool calls.
        """
        if live.applied_mode == value:
            return
        current, offered = config_option_values(live.config_options, option_id)
        if current == value:
            live.applied_mode = value
            return
        if value not in offered:
            log.warning("%s", missing)
            live.pending_warnings.append(missing)
            return
        try:
            await self._set_config_option(live, session, option_id, value)
        except AcpUnavailableError:
            raise
        except Exception as exc:
            message = (
                f"{self.agent.name} rejected {option_id}={value}: {exc}; "
                "ACP requestPermission will auto-allow instead"
            )
            log.warning("%s", message)
            live.pending_warnings.append(message)
            return
        live.applied_mode = value

    async def _sync_mapped_effort(
        self,
        live: _Live,
        session: Session,
        option_id: str,
        resolve: Callable[[str | None, Any], str | None],
        *,
        noun: str,
    ) -> None:
        if not session.effort:
            return
        current, offered = config_option_values(live.config_options, option_id)
        level = resolve(session.effort, offered)
        if level is None:
            message = (
                f"{self.agent.name} effort={session.effort} has no counterpart on "
                f"{live.applied_model or 'the current model'}; "
                f"session advertises {noun} {offered or '(none)'}"
            )
            log.warning("%s", message)
            live.pending_warnings.append(message)
            return
        if level == current:
            live.applied_effort = level
            return
        try:
            await self._set_config_option(live, session, option_id, level)
        except AcpUnavailableError:
            raise
        except Exception as exc:
            message = (
                f"{self.agent.name} rejected {noun}={level} for effort={session.effort}: {exc}"
            )
            log.warning("%s", message)
            live.pending_warnings.append(message)
            return
        live.applied_effort = level

    async def _sync_selection(self, live: _Live, session: Session) -> None:
        try:
            await self._sync_grok_model(live, session)
            await self._sync_cursor_selection(live, session)
            await self._sync_kimi_selection(live, session)
            await self._sync_opencode_selection(live, session)
            await self._sync_claude_selection(live, session)
            await self._sync_devin_selection(live, session)
            await self._sync_dsh_selection(live, session)
            await self._sync_zcode_selection(live, session)
            await self._sync_minimax_selection(live, session)
        except Exception as exc:
            raise self._failure(exc, "session/configure", session) from None

    async def _sync_claude_effort(self, live: _Live, session: Session) -> None:
        if not session.effort:
            return
        current, offered = config_option_values(live.config_options, "effort")
        level = resolve_claude_effort(session.effort, offered)
        if level is None:
            message = (
                f"claude effort={session.effort} has no counterpart on "
                f"{live.applied_model or 'the current model'}; "
                f"session advertises effort {offered or '(none)'}"
            )
            log.warning("%s", message)
            live.pending_warnings.append(message)
            return
        if level == current:
            live.applied_effort = level
            return
        try:
            await self._set_config_option(live, session, "effort", level)
        except AcpUnavailableError:
            raise
        except Exception as exc:
            message = f"claude rejected effort={level} for effort={session.effort}: {exc}"
            log.warning("%s", message)
            live.pending_warnings.append(message)
            return
        live.applied_effort = level

    async def _sync_dsh_selection(self, live: _Live, session: Session) -> None:
        """Apply model and effort on the native ``dsh --profile acp`` app.

        The legacy demo reads ``DSH_ACP_*`` spawn env, so a selection change
        respawns the process. The native profile ignores that env and
        publishes the standard ``model`` / ``reasoning_effort`` config options
        instead. Without an explicit coordinator model Bridge applies the
        ``settings.yaml`` default the env would have carried.
        """
        if self.agent.name != "dsh" or not live.dsh_native_acp:
            return
        requested = session.model or live.dsh_env_model
        if requested and requested != live.applied_model:
            target = dsh_native_model_value(requested, live.config_options)
            if target is None:
                if session.model:
                    raise RuntimeError(
                        f"dsh model {session.model!r} is not advertised by the native ACP "
                        f"profile; session offers "
                        f"{_dsh_native_model_labels(live.config_options) or 'no models'}"
                    )
                # Consumed: warn once, then let the bundle default stand.
                live.dsh_env_model = None
                message = (
                    f"dsh settings default model {requested!r} is not advertised by the "
                    "native ACP profile; the session keeps its bundle default"
                )
                log.warning("%s", message)
                live.pending_warnings.append(message)
            else:
                current, _offered = config_option_values(live.config_options, "model")
                if current != target:
                    await self._set_config_option(live, session, "model", target)
                live.applied_model = requested
        if live.applied_model is None:
            current, _offered = config_option_values(live.config_options, "model")
            live.applied_model = _dsh_native_model_label(current)
        mapped = dsh_effort(session.effort)
        if mapped and mapped != live.applied_effort:
            current_effort, offered_effort = config_option_values(
                live.config_options, "reasoning_effort"
            )
            if mapped in offered_effort:
                if current_effort != mapped:
                    await self._set_config_option(live, session, "reasoning_effort", mapped)
            else:
                message = (
                    f"dsh effort={session.effort} (native id {mapped!r}) is not advertised "
                    f"for the current model; session offers "
                    f"{offered_effort or 'no reasoning options'}"
                )
                log.warning("%s", message)
                live.pending_warnings.append(message)
            live.applied_effort = mapped
        elif live.applied_effort is None:
            current_effort, _offered = config_option_values(live.config_options, "reasoning_effort")
            live.applied_effort = current_effort or None

    async def ensure_session(self, session: Session) -> None:
        live = self._live.get(session.session_id)
        if (
            live
            and live.proc
            and live.proc.returncode is None
            and live.conn is not None
            and session.native_session_id
        ):
            if self.agent.name == "dsh" and not live.dsh_native_acp and dsh_needs_respawn(
                live.applied_model,
                live.applied_effort,
                session.model,
                session.effort,
            ):
                log.info(
                    "dsh model/effort changed (%s/%s -> %s/%s); respawning ACP process",
                    live.applied_model,
                    live.applied_effort,
                    session.model,
                    dsh_effort(session.effort),
                )
                session.native_session_id = None
                await self.shutdown(session)
            else:
                await self._sync_selection(live, session)
                return
        live = await self._spawn(session)
        native = session.native_session_id
        load_failure: Exception | None = None
        if native and self.can_revive():
            use_resume = self.agent.name in _RESUME_AGENTS or (
                self.agent.name == "dsh" and live.dsh_native_acp
            )
            try:
                revived = await self._rpc(
                    self._call_load_session(live.conn, session.cwd, native, use_resume=use_resume),
                    "session/resume" if use_resume else "session/load",
                    session,
                )
            except RpcTimeoutError:
                # The worker is already dead; a new_session on the same
                # connection cannot succeed.
                raise
            except AcpUnavailableError:
                raise
            except Exception as exc:
                log.warning("session/load failed for %s; creating a new session: %s", session.session_id, _redact_diagnostic(str(exc)))
                load_failure = exc
            else:
                self._remember_config_options(live, revived)
                await self._sync_selection(live, session)
                return
        created = await self._rpc(
            self._call_new_session(live.conn, session.cwd, self._new_session_meta(session)),
            "session/new",
            session,
        )
        # Adopt the new id before any fallible bookkeeping: a transcript write
        # failure must not leave the session pointing at the rejected id.
        session.native_session_id = created.session_id
        if load_failure is not None:
            # H-05: the coordinator must see that its session lost context.
            # pending_warnings lands on the next turn's task.warnings; the
            # transcript event keeps the switch auditable after the fact. Both
            # are emitted only once session/new has actually succeeded — a
            # failed fallback is a failed task, not a recreation.
            live.pending_warnings.append(
                f"session/load failed ({type(load_failure).__name__}); started a new worker "
                "session — prior conversation context is lost"
            )
            append_event(
                session.session_id,
                "session_recreated",
                {
                    "old_native_session_id": native,
                    "reason": _redact_diagnostic(str(load_failure)),
                },
                self.home,
            )
        self._remember_config_options(live, created)
        if self.agent.name == "grok":
            # Grok /new applies the _meta reasoningEffort but always lands on
            # the campaign default model. Record the effort as applied so an
            # effort-only dispatch does not raise a spurious warning, and let
            # _sync_grok_model switch the model via session/setModel.
            live.applied_effort = grok_effort(session.effort)
        await self._sync_selection(live, session)

    async def run_turn(self, session: Session, task: Task) -> TurnResult:
        live = self._live.get(session.session_id)
        if live is not None:
            live.stderr_tail = ""
        try:
            await self.ensure_session(session)
        except Exception as exc:
            raise self._failure(exc, "process_start", session) from None
        live = self._live[session.session_id]
        assert live.conn is not None and live.client is not None
        live.client.reset_turn()
        warnings: list[str] = live.pending_warnings
        live.pending_warnings = []
        if self.agent.name not in _MODEL_EFFORT_AGENTS and (task.model or task.effort):
            warnings.append(
                f"{self.agent.name} has no model/effort selection; "
                f"model={task.model!r} effort={task.effort!r} were ignored"
            )
        append_event(session.session_id, "prompt_sent", {"text": task.message}, self.home)
        try:
            prompt = live.conn.prompt(
                session_id=session.native_session_id,
                prompt=[text_block(task.message)],
            )
            live.prompt_task = asyncio.ensure_future(prompt)
            response = await live.prompt_task
        except asyncio.CancelledError:
            append_event(session.session_id, "turn_end", {"stop_reason": "cancelled"}, self.home)
            return TurnResult(
                text="".join(live.client.text_parts),
                files_changed=sorted(live.client.files),
                stop_reason="cancelled",
                warnings=warnings,
                observed_model=live.applied_model,
                observed_effort=live.applied_effort,
            )
        except Exception as exc:
            if isinstance(exc, (ConnectionError, EOFError)):
                await _observe_exit(live.proc)
            raise self._failure(
                exc, "session/prompt", session, prompt_may_have_been_sent=True,
            ) from None
        finally:
            live.prompt_task = None
        stop = getattr(response, "stop_reason", None) or "end_turn"
        if hasattr(stop, "value"):
            stop = stop.value
        stop = str(stop)
        usage = live.client.usage
        if not usage:
            dumped_usage = _dump(getattr(response, "usage", None))
            if isinstance(dumped_usage, dict):
                usage = dumped_usage
        append_event(session.session_id, "turn_end", {"stop_reason": stop}, self.home)
        return TurnResult(
            text="".join(live.client.text_parts),
            files_changed=sorted(live.client.files),
            stop_reason=stop,
            usage=usage,
            native_session_id=session.native_session_id,
            warnings=warnings,
            observed_model=live.applied_model,
            observed_effort=live.applied_effort,
        )

    async def cancel(self, session: Session) -> None:
        live = self._live.get(session.session_id)
        if live is None or live.conn is None:
            return
        if session.native_session_id:
            try:
                await asyncio.wait_for(live.conn.cancel(session_id=session.native_session_id), timeout=5)
            except Exception:
                log.warning("ACP cancel failed for %s", session.session_id, exc_info=True)
        if live.prompt_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(live.prompt_task), timeout=PROMPT_CANCEL_GRACE_SEC)
                return
            except (asyncio.CancelledError, Exception):
                # CancelledError derives from BaseException and must be
                # listed explicitly; a cancelled prompt is the normal case.
                pass
        if live.proc is not None:
            await reap_subprocess(live.proc)
            await self.shutdown(session)

    async def shutdown(self, session: Session) -> None:
        live = self._live.pop(session.session_id, None)
        if live is None:
            drop_pid(self.home, session.session_id)
            session.pid = None
            return
        if live.conn is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(live.conn.close(), timeout=2)
        if live.proc is not None:
            await reap_subprocess(live.proc)
        if live.stderr_task is not None:
            if not live.stderr_task.done():
                live.stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await live.stderr_task
        drop_pid(self.home, session.session_id)
        session.pid = None
