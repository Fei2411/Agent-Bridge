from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from agent_bridge.adapters.base import STDIO_LIMIT, Adapter
from agent_bridge.config import AgentConfig
from agent_bridge.diagnostics import redact_diagnostic
from agent_bridge.model_discovery_exec import (
    parse_agy_models_output,
    read_agy_models,
)
from agent_bridge.models import Session, Task, TurnResult, agy_effort
from agent_bridge.processes import (
    drop_pid,
    process_create_time,
    process_image_name,
    reap_subprocess,
    record_pid,
    resolve_command,
)
from agent_bridge.transcript import append_event
from agent_bridge.worker_env import build_worker_env

log = logging.getLogger(__name__)

STDERR_TAIL_LIMIT = 16000
_RESULT_STATUSES = {"SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING"}
_TOOL_SCHEMA_MARKERS = (
    "invalid tool call error",
    "invalid_signature",
    "codecontent is a required parameter",
    "convert tool call for permissions",
)


def _scoped_usage(usage: dict[str, Any], resumed: bool) -> dict[str, Any]:
    if not usage:
        return usage
    return {**usage, "scope": "conversation" if resumed else "turn"}


def conversation_id_of(obj: dict[str, Any]) -> str | None:
    for key in ("conversation_id", "conversationId"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for nested_key in ("init", "step_update", "result"):
        nested = obj.get(nested_key)
        if isinstance(nested, dict):
            found = conversation_id_of(nested)
            if found:
                return found
    return None


def is_result_event(obj: dict[str, Any]) -> bool:
    if obj.get("event") == "result":
        return True
    if obj.get("type") in {"result", "final", "turn_complete", "completed"}:
        return True
    return obj.get("status") in _RESULT_STATUSES and ("response" in obj or "error" in obj)


def unwrap_result(obj: dict[str, Any]) -> dict[str, Any]:
    nested = obj.get("result")
    if isinstance(nested, dict) and (nested.get("status") or nested.get("response") or nested.get("error")):
        return nested
    return obj


def text_delta_of(obj: dict[str, Any]) -> str:
    step = obj.get("step_update")
    if isinstance(step, dict):
        delta = step.get("text_delta")
        if isinstance(delta, str) and delta:
            return delta
    for key in ("text_delta", "delta"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def result_text_of(obj: dict[str, Any]) -> str:
    payload = unwrap_result(obj)
    for key in ("response", "result", "text", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            nested = result_text_of(value)
            if nested:
                return nested
    content = payload.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def result_error_of(obj: dict[str, Any]) -> str | None:
    payload = unwrap_result(obj)
    status = str(payload.get("status") or "")
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error
    if status and status != "SUCCESS":
        return status
    return None


def is_agy_tool_schema_error(error: str | None) -> bool:
    """agy/cortex rejected a model tool call while converting it for permissions.

    This is not a Bridge write adapter. Headless agy executes tools itself; the
    model sometimes omits ``CodeContent`` on ``write_to_file``. The run can still
    recover, write files, and exit 0 with a full response.
    """
    if not error:
        return False
    text = error.lower()
    return any(marker in text for marker in _TOOL_SCHEMA_MARKERS)


def stream_json_prompt(message: str) -> bytes:
    return (
        json.dumps(
            {"event": "user", "message": {"role": "user", "content": message}},
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def recovered_agy_tool_error(
    result: dict[str, Any],
    response: str,
    exit_code: int | None,
) -> bool:
    error = result_error_of(result)
    if not is_agy_tool_schema_error(error):
        return False
    if not (response or "").strip():
        return False
    return exit_code in (0, None)


# Tool names that can write to the workspace. Read-only tools (view_file,
# list_dir, grep_search, ...) also carry path params; counting those would
# report files the agent merely looked at. Under-matching is safe because the
# disk snapshot diff in merge_files_changed still catches every real write.
_MUTATING_TOOL_MARKERS = (
    "write",
    "edit",
    "replace",
    "delete",
    "remove",
    "move",
    "rename",
    "patch",
    "create",
)


def agy_projects_dir() -> Path:
    return Path.home() / ".gemini" / "config" / "projects"


def _uri_path(value: str) -> str:
    p = unquote(value.removeprefix("file:")).replace("\\", "/")
    p = p.lstrip("/") if re.match(r"^/*[A-Za-z]:", p) else "/" + p.lstrip("/")
    return os.path.normcase(os.path.normpath(p))


def find_agy_project(cwd: str, projects_dir: Path) -> str | None:
    """Id of the agy project whose folderUri points at ``cwd``, if one exists."""
    if not projects_dir.is_dir():
        return None
    target = _uri_path(cwd)
    for file in sorted(projects_dir.glob("*.json")):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        project_id = data.get("id")
        resources = data.get("projectResources")
        entries = resources.get("resources") if isinstance(resources, dict) else None
        if not isinstance(project_id, str) or not isinstance(entries, list):
            continue
        for resource in entries:
            if not isinstance(resource, dict):
                continue
            candidates = [resource.get("folderUri")]
            git = resource.get("gitFolder")
            if isinstance(git, dict):
                candidates.append(git.get("folderUri"))
            if any(isinstance(uri, str) and _uri_path(uri) == target for uri in candidates):
                return project_id
    return None


def collect_tool_paths(obj: dict[str, Any], into: set[str]) -> None:
    step = obj.get("step_update") if isinstance(obj.get("step_update"), dict) else obj
    if not isinstance(step, dict):
        return
    info = step.get("tool_info")
    if not isinstance(info, dict):
        return
    name = str(info.get("name") or step.get("tool_name") or "").lower()
    if not any(marker in name for marker in _MUTATING_TOOL_MARKERS):
        return
    params = info.get("parameters")
    if not isinstance(params, dict):
        return
    for key in ("Path", "path", "file", "filePath", "TargetFile", "AbsolutePath"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            into.add(value)


class AgyAdapter(Adapter):
    resident = False

    def __init__(self, agent: AgentConfig, home: Path, env_config=None) -> None:
        super().__init__(agent, home, env_config)
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        # Session ids whose current turn was cancelled via cancel()/shutdown().
        # Stopping the process makes agy stdout hit EOF, which is otherwise
        # indistinguishable from a normal end of stream.
        self._cancelled: set[str] = set()

    async def ensure_session(self, session: Session) -> None:
        return None

    async def list_models(self, cwd: str) -> dict[str, Any]:
        """Read the catalog from ``agy models`` in the project directory."""
        source = "agy models"
        cmd = resolve_command(self.agent.command, self.agent.fallback_commands)
        env = self._enforce_spawn_env(
            build_worker_env(self.agent.env, config=self.env_config, worker_context=True, home=self.home)
        )
        stdout = await read_agy_models(cmd, env=env, cwd=cwd)
        return {"source": source, "models": parse_agy_models_output(stdout),
                "default_model": None, "warnings": []}

    def _build_cmd(self, session: Session, task: Task) -> list[str]:
        cmd = resolve_command(self.agent.command, self.agent.fallback_commands)
        model = task.model or session.model
        effort = agy_effort(task.effort or session.effort)
        # --model/--effort before -p: print mode can swallow later flags.
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        # Process cwd is the workspace, but default-cli-project can keep a
        # stale scratch root. Pin this turn to the requested folder before -p.
        if session.cwd:
            cmd += ["--add-dir", session.cwd]
        if session.native_session_id:
            cmd += ["--conversation", session.native_session_id]
        else:
            # Reuse the cwd's existing project so each dispatch does not add a
            # duplicate to Antigravity's project list; --new-project only when none exists.
            project_id = find_agy_project(session.cwd, agy_projects_dir()) if session.cwd else None
            cmd += ["--project", project_id] if project_id else ["--new-project"]
        cmd += [
            "-p",
            "",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--print-timeout",
            self.agent.print_timeout or "120m",
        ]
        return cmd

    async def _drain_stderr(self, proc: asyncio.subprocess.Process, session_id: str) -> str:
        if proc.stderr is None:
            return ""
        tail = ""
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return tail
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    tail = f"{tail}\n{text}"[-STDERR_TAIL_LIMIT:]
        except (ValueError, OSError):
            log.warning(
                "stderr drain aborted for %s (line over %s MiB or stream closed); "
                "later stderr evidence is lost",
                session_id,
                STDIO_LIMIT // (1024 * 1024),
                exc_info=True,
            )
            return tail

    async def _write_prompt(self, proc: asyncio.subprocess.Process, message: str) -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(stream_json_prompt(message))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            log.debug("agy closed stdin before reading the prompt for pid %s: %s", proc.pid, exc)
        finally:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                proc.stdin.close()

    async def run_turn(self, session: Session, task: Task) -> TurnResult:
        """Drive one agy turn.

        ``usage.scope`` is ``conversation`` when this turn resumes an existing
        conversation (those counters cover every turn so far); otherwise ``turn``.
        """
        cmd = self._build_cmd(session, task)
        env = self._enforce_spawn_env(
            build_worker_env(self.agent.env, config=self.env_config, worker_context=True, home=self.home)
        )
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        append_event(session.session_id, "prompt_sent", {"text": task.message, "cmd": cmd[:6]}, self.home)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=task.cwd or session.cwd,
            limit=STDIO_LIMIT,
            **kwargs,
        )
        self._procs[session.session_id] = proc
        self._cancelled.discard(session.session_id)
        if proc.pid:
            record_pid(
                self.home,
                session.session_id,
                proc.pid,
                process_create_time(proc.pid),
                process_image_name(proc.pid),
            )
        stderr_task = asyncio.create_task(self._drain_stderr(proc, session.session_id))
        await self._write_prompt(proc, task.message)
        text_parts: list[str] = []
        conversation_id = session.native_session_id
        resumed = bool(session.native_session_id)
        usage: dict[str, Any] = {}
        files: set[str] = set()
        last_result: dict[str, Any] | None = None
        try:
            assert proc.stdout is not None
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError as exc:
                    raise RuntimeError(
                        f"antigravity emitted a stdout line longer than "
                        f"{STDIO_LIMIT // (1024 * 1024)} MiB (asyncio stream limit); "
                        "the turn cannot continue"
                    ) from exc
                if not line:
                    break
                raw = line.decode("utf-8", errors="replace").rstrip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    append_event(session.session_id, "raw", {"text": raw[:2000]}, self.home)
                    text_parts.append(raw)
                    continue
                if not isinstance(obj, dict):
                    append_event(session.session_id, "raw", {"value": obj}, self.home)
                    continue
                cid = conversation_id_of(obj)
                if cid:
                    conversation_id = cid
                event = str(obj.get("event") or obj.get("type") or "")
                raw_step = obj.get("step_update")
                step: dict[str, Any] = raw_step if isinstance(raw_step, dict) else {}
                step_type = str(step.get("step_type") or "")
                chunk = text_delta_of(obj)
                event_type = "raw"
                if event == "init":
                    event_type = "raw"
                elif step_type == "agent_response" or chunk:
                    event_type = "message_chunk"
                    if chunk:
                        text_parts.append(chunk)
                elif step_type in {"tool"} or event in {"tool", "tool_call"}:
                    event_type = "tool_call"
                    collect_tool_paths(obj, files)
                elif step_type == "checkpoint" or event in {"thought", "reasoning"}:
                    event_type = "thought_chunk"
                append_event(
                    session.session_id,
                    event_type,
                    {"payload": obj} if len(raw) < 4000 else {"truncated": True},
                    self.home,
                )
                payload = unwrap_result(obj) if is_result_event(obj) else obj
                if isinstance(payload.get("usage"), dict):
                    usage = payload["usage"]
                elif isinstance(step.get("usage"), dict):
                    usage = step["usage"]
                if is_result_event(obj):
                    last_result = unwrap_result(obj)
            try:
                await asyncio.wait_for(proc.wait(), timeout=60)
            except TimeoutError:
                log.warning("agy stdout closed but process lingered; killing %s", proc.pid)
                await reap_subprocess(proc)
            stderr_tail = await stderr_task
            if session.session_id in self._cancelled:
                append_event(session.session_id, "turn_end", {"stop_reason": "cancelled"}, self.home)
                return TurnResult(
                    text="".join(text_parts),
                    files_changed=sorted(files),
                    stop_reason="cancelled",
                    native_session_id=conversation_id,
                )
            exit_warnings: list[str] = []
            if proc.returncode not in (0, None):
                exit_warnings.append(f"agy exited with code {proc.returncode}")
            if last_result is not None:
                err = result_error_of(last_result)
                result_text = result_text_of(last_result) or "".join(text_parts)
                cid = conversation_id_of(last_result) or conversation_id
                if isinstance(last_result.get("usage"), dict):
                    usage = last_result["usage"]
                session.native_session_id = cid
                if err and last_result.get("status") != "SUCCESS":
                    if recovered_agy_tool_error(last_result, result_text, proc.returncode):
                        append_event(
                            session.session_id,
                            "warning",
                            {"error": err, "code": proc.returncode, "treated_as": "recovered_tool_schema"},
                            self.home,
                        )
                        append_event(session.session_id, "turn_end", {"stop_reason": "end_turn"}, self.home)
                        return TurnResult(
                            text=result_text,
                            files_changed=sorted(files),
                            stop_reason="end_turn",
                            usage=_scoped_usage(usage, resumed),
                            native_session_id=cid,
                            warnings=[err, *exit_warnings],
                        )
                    append_event(
                        session.session_id,
                        "error",
                        {"error": redact_diagnostic(err), "code": proc.returncode},
                        self.home,
                    )
                    return TurnResult(
                        text=result_text,
                        files_changed=sorted(files),
                        stop_reason="error",
                        error=err,
                        usage=_scoped_usage(usage, resumed),
                        native_session_id=cid,
                    )
                append_event(session.session_id, "turn_end", {"stop_reason": "end_turn"}, self.home)
                return TurnResult(
                    text=result_text,
                    files_changed=sorted(files),
                    stop_reason="end_turn",
                    usage=_scoped_usage(usage, resumed),
                    native_session_id=cid,
                    warnings=exit_warnings,
                )
            if proc.returncode not in (0, None) and not text_parts:
                detail = stderr_tail.strip()
                error = f"agy exit {proc.returncode}"
                if detail:
                    error += f": {detail}"
                return TurnResult(text="", stop_reason="error", error=error)
            session.native_session_id = conversation_id
            append_event(session.session_id, "turn_end", {"stop_reason": "end_turn"}, self.home)
            return TurnResult(
                text="".join(text_parts),
                files_changed=sorted(files),
                stop_reason="end_turn",
                usage=_scoped_usage(usage, resumed),
                native_session_id=conversation_id,
                warnings=exit_warnings,
            )
        except asyncio.CancelledError:
            await reap_subprocess(proc)
            append_event(session.session_id, "turn_end", {"stop_reason": "cancelled"}, self.home)
            return TurnResult(
                text="".join(text_parts),
                files_changed=sorted(files),
                stop_reason="cancelled",
                native_session_id=conversation_id,
            )
        except Exception:
            # e.g. readline() past STDIO_LIMIT. Without this, agy would keep
            # running with nobody reading its stdout and never get reaped.
            await reap_subprocess(proc)
            raise
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            self._procs.pop(session.session_id, None)
            self._cancelled.discard(session.session_id)
            drop_pid(self.home, session.session_id)

    async def cancel(self, session: Session) -> None:
        self._cancelled.add(session.session_id)
        proc = self._procs.get(session.session_id)
        if proc is not None:
            await reap_subprocess(proc)

    async def shutdown(self, session: Session) -> None:
        await self.cancel(session)
        self._procs.pop(session.session_id, None)
