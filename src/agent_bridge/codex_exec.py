from __future__ import annotations

import logging
import os
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_bridge.diagnostics import redact_diagnostic
from agent_bridge.native_results import extract_codex_sources
from agent_bridge.processes import resolve_command

log = logging.getLogger(__name__)

REQUIRED_EXEC_FLAGS = (
    "--json",
    "--ignore-user-config",
    "--approve-for-me",
    "--skip-git-repo-check",
    "--thread-source",
)
AUTH_STORE_OVERRIDE = 'cli_auth_credentials_store="auto"'
THREAD_SOURCE = "subagent"
_HELP_TIMEOUT_SEC = 15.0
_capability_cache: dict[tuple[str, int, int], str | None] = {}


def default_codex_home(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    raw = str(source.get("CODEX_HOME", "")).strip()
    if raw:
        return Path(raw)
    return Path.home() / ".codex"


def local_app_data(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    raw = str(source.get("LOCALAPPDATA", "")).strip()
    if raw:
        return Path(raw)
    return Path.home() / "AppData" / "Local"


def _walk_cli_path(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "CODEX_CLI_PATH" and isinstance(nested, str) and nested.strip():
                return nested.strip()
            found = _walk_cli_path(nested)
            if found:
                return found
    return None


def read_codex_cli_path(home: Path | None = None, env: Mapping[str, str] | None = None) -> str | None:
    source = os.environ if env is None else env
    explicit = str(source.get("CODEX_CLI_PATH", "")).strip()
    if explicit:
        return explicit
    config = (home or default_codex_home(source)) / "config.toml"
    try:
        raw = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return _walk_cli_path(raw)


def _codex_exe_name() -> str:
    return "codex.exe" if os.name == "nt" else "codex"


def desktop_codex_binaries(env: Mapping[str, str] | None = None) -> list[Path]:
    root = local_app_data(env) / "OpenAI" / "Codex" / "bin"
    if not root.is_dir():
        return []
    found: list[Path] = []
    exe_name = _codex_exe_name()
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    for child in children:
        try:
            if child.is_dir():
                candidate = child / exe_name
                if candidate.is_file():
                    found.append(candidate)
        except OSError:
            continue
    root_exe = root / exe_name
    try:
        if root_exe.is_file():
            found.append(root_exe)
    except OSError:
        pass
    found.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    return found


def _run_text(args: list[str], timeout: float = _HELP_TIMEOUT_SEC) -> str:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    out = (completed.stdout or b"").decode("utf-8", errors="replace")
    err = (completed.stderr or b"").decode("utf-8", errors="replace")
    return (out or err).strip()


def looks_like_codex_version(text: str) -> bool:
    return "codex" in text.lower()


def _identity(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), int(stat.st_mtime_ns), int(stat.st_size))


def exec_help_problem(help_text: str) -> str | None:
    missing = [flag for flag in REQUIRED_EXEC_FLAGS if flag not in help_text]
    if missing:
        return "exec lacks " + ", ".join(missing)
    return None


def codex_command_problem(command: list[str]) -> str | None:
    if not command:
        return "empty command"
    exe = Path(command[0])
    key = _identity(exe)
    if key and key in _capability_cache:
        return _capability_cache[key]
    version = _run_text([command[0], "--version"])
    if not looks_like_codex_version(version):
        problem = f"{command[0]} is not a Codex CLI"
        if key:
            _capability_cache[key] = problem
        return problem
    help_text = _run_text([command[0], "exec", "--help"])
    help_problem = exec_help_problem(help_text)
    if help_problem:
        help_problem = f"{command[0]} {help_problem}"
    if key:
        _capability_cache[key] = help_problem
    return help_problem


def discovered_codex_commands(env: Mapping[str, str] | None = None) -> list[list[str]]:
    found: list[list[str]] = []
    seen: set[str] = set()

    def add(raw: str | Path | None) -> None:
        if not raw:
            return
        path = Path(raw)
        try:
            if not path.is_file():
                return
            resolved = str(path.resolve())
        except OSError:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        found.append([resolved])

    add(read_codex_cli_path(env=env))
    for binary in desktop_codex_binaries(env):
        add(binary)
    return found


def resolve_codex_command(
    command: list[str],
    fallbacks: list[list[str]] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    extra: Sequence[list[str]] | None = None,
) -> list[str]:
    discovered = list(extra) if extra is not None else discovered_codex_commands(env)
    pinned: list[list[str]] = []
    if command and Path(command[0]).is_file():
        pinned.append(list(command))
    return resolve_command(
        command or ["codex"],
        fallbacks,
        extra=[*pinned, *discovered],
        validate=codex_command_problem,
    )


def codex_effort(effort: str | None) -> str | None:
    if effort in {None, "low", "medium", "high", "max"}:
        return effort
    if effort == "off":
        return "none"
    return None


def yolo_requested(session_meta: Mapping[str, Any] | None) -> bool:
    if not session_meta:
        return False
    value = session_meta.get("yolo")
    return value is True or value == "true" or value == 1


def build_codex_exec_argv(
    command: list[str],
    *,
    cwd: str,
    model: str | None = None,
    effort: str | None = None,
    resume_id: str | None = None,
    yolo: bool = False,
    config_overrides: Sequence[str] = (),
) -> list[str]:
    cmd = [
        *command,
        "exec",
        "--json",
        "--ignore-user-config",
        "-c",
        AUTH_STORE_OVERRIDE,
    ]
    if yolo:
        cmd.append("--yolo")
    else:
        cmd.append("--approve-for-me")
    cmd += [
        "--skip-git-repo-check",
        "--thread-source",
        THREAD_SOURCE,
        "-C",
        cwd,
    ]
    for item in config_overrides:
        # Earlier than the task's -m / effort -c so per-dispatch values win.
        cmd += ["-c", item]
    if model:
        cmd += ["-m", model]
    mapped = codex_effort(effort)
    if mapped:
        cmd += ["-c", f'model_reasoning_effort="{mapped}"']
    if resume_id:
        cmd += ["resume", resume_id]
    cmd.append("-")
    return cmd


class CodexTurnState:
    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.agent_messages: list[str] = []
        self.files: set[str] = set()
        self.usage: dict[str, Any] = {}
        self.turn_completed = False
        self.turn_failed = False
        self.errors: list[str] = []
        self.sources: list[Any] = []

    @property
    def text(self) -> str:
        return self.agent_messages[-1] if self.agent_messages else ""

    @property
    def error(self) -> str | None:
        return self.errors[-1] if self.errors else None


def finalize_codex_turn(
    state: CodexTurnState,
    *,
    cancelled: bool = False,
    returncode: int | None = 0,
    stderr: str | None = None,
) -> tuple[str, str | None, list[str]]:
    """Map Codex JSONL plus process status onto Bridge stop_reason.

    Codex can emit a top-level ``error`` event and keep running
    (``will_retry``). ``turn.failed`` is the hard failure; ``turn.completed``
    wins even if an ``error`` arrived earlier.
    """
    if cancelled:
        return "cancelled", None, []
    warnings: list[str] = []
    if returncode not in (0, None):
        warnings.append(f"codex exited with code {returncode}")
    if state.turn_failed:
        return "error", redact_diagnostic(state.error or "codex turn failed"), warnings
    if state.turn_completed:
        warnings.extend(redact_diagnostic(err) for err in state.errors)
        return "end_turn", None, warnings
    if state.errors:
        return "error", redact_diagnostic(state.error or "codex turn failed"), warnings
    # Codex stderr is raw worker output (may echo env secrets it read);
    # sanitize before it becomes the task's error text.
    stderr_error = redact_diagnostic((stderr or "").strip())
    if stderr_error:
        return "error", stderr_error, warnings
    error = (
        f"codex exit {returncode}"
        if returncode not in (0, None)
        else "codex produced no turn.completed"
    )
    return "error", error, warnings


def _error_message(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, dict):
        message = value.get("message")
        if isinstance(message, str) and message.strip():
            return message
    return None


def apply_codex_event(state: CodexTurnState, obj: Mapping[str, Any]) -> None:
    event_type = str(obj.get("type") or "")
    if event_type == "thread.started":
        thread_id = obj.get("thread_id")
        if isinstance(thread_id, str) and thread_id.strip():
            state.thread_id = thread_id
        return
    if event_type == "item.completed":
        item = obj.get("item")
        if not isinstance(item, dict):
            return
        item_type = str(item.get("type") or "")
        if item_type == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text:
                state.agent_messages.append(text)
            return
        if item_type == "file_change":
            for change in item.get("changes") or []:
                if isinstance(change, dict):
                    path = change.get("path")
                    if isinstance(path, str) and path:
                        state.files.add(path)
        if item_type == "web_search":
            extract_codex_sources(item, state.sources)
        return
    if event_type == "turn.completed":
        state.turn_completed = True
        usage = obj.get("usage")
        if isinstance(usage, dict):
            state.usage = dict(usage)
        return
    if event_type == "turn.failed":
        state.turn_failed = True
        failed_message = _error_message(obj.get("error")) or "turn.failed"
        state.errors.append(failed_message)
        return
    if event_type == "error":
        message = _error_message(obj.get("message")) or _error_message(obj.get("error"))
        if message:
            state.errors.append(message)
