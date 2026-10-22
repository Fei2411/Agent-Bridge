from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_bridge.adapters.base import STDIO_LIMIT, Adapter
from agent_bridge.codex_exec import (
    CodexTurnState,
    apply_codex_event,
    build_codex_exec_argv,
    codex_effort,
    finalize_codex_turn,
    resolve_codex_command,
    yolo_requested,
)
from agent_bridge.diagnostics import redact_diagnostic
from agent_bridge.model_discovery_exec import (
    codex_app_server_list_models,
    parse_codex_models,
)
from agent_bridge.models import Session, Task, TurnResult
from agent_bridge.processes import (
    drop_pid,
    interrupt_then_reap,
    process_create_time,
    process_image_name,
    reap_subprocess,
    record_pid,
)
from agent_bridge.transcript import append_event
from agent_bridge.worker_env import build_worker_env

log = logging.getLogger(__name__)

STDERR_TAIL_LIMIT = 16000


class CodexAdapter(Adapter):
    resident = False

    def __init__(self, agent, home: Path, env_config=None) -> None:
        super().__init__(agent, home, env_config)
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set[str] = set()

    async def ensure_session(self, session: Session) -> None:
        return None

    async def list_models(self, cwd: str) -> dict[str, Any]:
        """Query app-server model/list using its configuration and config_overrides."""
        env = self._enforce_spawn_env(self._worker_env())
        command = await asyncio.to_thread(
            resolve_codex_command, self.agent.command, self.agent.fallback_commands, env=env
        )
        argv = [*command]
        for item in self.agent.config_overrides:
            argv += ["-c", item]
        argv.append("app-server")
        entries = await codex_app_server_list_models(argv, env=env, cwd=cwd)
        return parse_codex_models(entries)

    def _worker_env(self) -> dict[str, str]:
        return build_worker_env(self.agent.env, config=self.env_config, worker_context=True, home=self.home)

    def _base_cmd(self, env: dict[str, str] | None = None) -> list[str]:
        return resolve_codex_command(self.agent.command, self.agent.fallback_commands, env=env)

    def _build_cmd(self, session: Session, task: Task, env: dict[str, str] | None = None) -> list[str]:
        return build_codex_exec_argv(
            self._base_cmd(env),
            cwd=task.cwd or session.cwd,
            model=task.model or session.model,
            effort=task.effort or session.effort,
            resume_id=session.native_session_id,
            yolo=yolo_requested(self.agent.session_meta),
            config_overrides=self.agent.config_overrides,
        )

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
        if proc.stdin is None:
            return
        try:
            proc.stdin.write(message.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            log.debug(
                "codex closed stdin before reading the prompt for pid %s: %s",
                proc.pid,
                exc,
            )
        finally:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                proc.stdin.close()

    async def run_turn(self, session: Session, task: Task) -> TurnResult:
        env = self._worker_env()
        cmd = self._build_cmd(session, task, env)
        env = self._enforce_spawn_env(env)
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        append_event(
            session.session_id,
            "prompt_sent",
            {"text": task.message, "cmd": cmd[:8]},
            self.home,
        )
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
        state = CodexTurnState()
        if session.native_session_id:
            state.thread_id = session.native_session_id
        observed_model = task.model or session.model
        observed_effort = codex_effort(task.effort or session.effort)
        try:
            await self._write_prompt(proc, task.message)
            assert proc.stdout is not None
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError as exc:
                    raise RuntimeError(
                        f"codex emitted a stdout line longer than "
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
                    continue
                if not isinstance(obj, dict):
                    append_event(session.session_id, "raw", {"value": obj}, self.home)
                    continue
                apply_codex_event(state, obj)
                event = str(obj.get("type") or "")
                event_type = "raw"
                if event == "item.completed":
                    raw_item = obj.get("item")
                    item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else {}
                    item_type = str(item.get("type") or "")
                    if item_type == "agent_message":
                        event_type = "message_chunk"
                    elif item_type == "file_change":
                        event_type = "tool_call"
                elif event in {"turn.failed", "error"}:
                    event_type = "error"
                append_event(
                    session.session_id,
                    event_type,
                    {"payload": obj} if len(raw) < 4000 else {"truncated": True},
                    self.home,
                )
            try:
                await asyncio.wait_for(proc.wait(), timeout=60)
            except TimeoutError:
                log.warning("codex stdout closed but process lingered; killing %s", proc.pid)
                await reap_subprocess(proc)
            stderr_tail = await stderr_task
            stop_reason, error, warnings = finalize_codex_turn(
                state,
                cancelled=session.session_id in self._cancelled,
                returncode=proc.returncode,
                stderr=stderr_tail,
            )
            if stop_reason == "cancelled":
                append_event(session.session_id, "turn_end", {"stop_reason": "cancelled"}, self.home)
                return TurnResult(
                    text=state.text,
                    files_changed=sorted(state.files),
                    stop_reason="cancelled",
                    native_session_id=state.thread_id,
                    usage=state.usage,
                    observed_model=observed_model,
                    observed_effort=observed_effort,
                )
            if stop_reason == "error":
                append_event(
                    session.session_id, "error", {"error": redact_diagnostic(error or "")}, self.home
                )
                return TurnResult(
                    text=state.text,
                    files_changed=sorted(state.files),
                    stop_reason="error",
                    error=error,
                    usage=state.usage,
                    native_session_id=state.thread_id,
                    warnings=warnings,
                    observed_model=observed_model,
                    observed_effort=observed_effort,
                )
            session.native_session_id = state.thread_id
            append_event(session.session_id, "turn_end", {"stop_reason": "end_turn"}, self.home)
            return TurnResult(
                text=state.text,
                files_changed=sorted(state.files),
                stop_reason="end_turn",
                usage=state.usage,
                native_session_id=state.thread_id,
                warnings=warnings,
                observed_model=observed_model,
                observed_effort=observed_effort,
            )
        except asyncio.CancelledError:
            await interrupt_then_reap(proc)
            append_event(session.session_id, "turn_end", {"stop_reason": "cancelled"}, self.home)
            return TurnResult(
                text=state.text,
                files_changed=sorted(state.files),
                stop_reason="cancelled",
                native_session_id=state.thread_id,
                usage=state.usage,
                observed_model=observed_model,
                observed_effort=observed_effort,
            )
        except Exception:
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
            await interrupt_then_reap(proc)

    async def shutdown(self, session: Session) -> None:
        await self.cancel(session)
        self._procs.pop(session.session_id, None)
