"""One fresh official app-server image turn, with no submission retry."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_bridge.codex_exec import AUTH_STORE_OVERRIDE, codex_effort, default_codex_home
from agent_bridge.models import Session, Task, TurnResult
from agent_bridge.native_results import checkpoint, extract_codex_sources, finish_job, observe_call, start_job
from agent_bridge.output_files import MAX_OUTPUT_FILES, deliver_file, existing_files
from agent_bridge.processes import drop_pid, process_create_time, process_image_name, reap_subprocess, record_pid
from agent_bridge.transcript import append_event

READ_LIMIT = 32 * 1024 * 1024
RPC_TIMEOUT = 60.0


def image_component(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value) or "generated_image"


class RpcRejected(RuntimeError):
    pass


class CodexImageTurn:
    def __init__(self, home: Path, session: Session, task: Task, env: dict[str, str]) -> None:
        self.home, self.session, self.task, self.env = home, session, task, env
        self.proc: asyncio.subprocess.Process | None = None
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.image_root: Path | None = None
        self.previous: set[Path] = set()
        self.pending: list[dict[str, Any]] = []
        self.seen: set[str] = set()
        self.text = ""
        self.cancelled = False
        self.native_failed = False
        self.delivery_attempted = False
        self.rpc_id = 0
        self.usage: dict[str, int] = {}
        self.media_calls: dict[str, str] = {}

    async def _send(self, message: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("Codex image process has no stdin")
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def _read(self) -> dict[str, Any]:
        assert self.proc is not None and self.proc.stdout is not None
        line = await self.proc.stdout.readline()
        if not line:
            raise RuntimeError("Codex image process disconnected before native completion")
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            raise RuntimeError("Codex image process emitted invalid JSON") from None
        if not isinstance(value, dict):
            raise RuntimeError("Codex image process emitted an invalid event")
        # The official item embeds base64 alongside savedPath. Discard it at
        # the transport boundary, before buffering, logging or persistence.
        params = value.get("params")
        if isinstance(params, dict) and isinstance(params.get("item"), dict):
            self._strip_image(params["item"])
        for envelope in (params, value.get("result")):
            if isinstance(envelope, dict):
                turn = envelope.get("turn")
                if isinstance(turn, dict) and isinstance(turn.get("items"), list):
                    for item in turn["items"]:
                        if isinstance(item, dict):
                            self._strip_image(item)
        return value

    @staticmethod
    def _strip_image(item: dict[str, Any]) -> None:
        if item.get("type") == "imageGeneration":
            item.pop("result", None)
            item.pop("revisedPrompt", None)

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.rpc_id += 1
        wanted = self.rpc_id
        await self._send({"id": wanted, "method": method, "params": params})
        async with asyncio.timeout(RPC_TIMEOUT):
            while True:
                value = await self._read()
                if value.get("id") == wanted and not value.get("method"):
                    if "error" in value:
                        raise RpcRejected(f"Codex app-server rejected {method}; check official login and capabilities")
                    result = value.get("result")
                    return result if isinstance(result, dict) else {}
                await self._queue_or_reject(value)

    async def _queue_or_reject(self, value: dict[str, Any]) -> None:
        if value.get("method") and "id" in value:
            # A media operation does not authorize new permission grants.
            await self._send({"id": value["id"], "error": {"code": -32601, "message": "Unsupported client request"}})
        elif value.get("method") in {"item/started", "item/completed", "turn/completed", "thread/tokenUsage/updated", "error"}:
            if len(self.pending) >= 64:
                raise RuntimeError("Codex image pending notification limit exceeded")
            self.pending.append(value)

    def consume(self, value: dict[str, Any]) -> str | None:
        params = value.get("params")
        if not isinstance(params, dict) or params.get("threadId") != self.thread_id:
            return None
        method = value.get("method")
        if method == "turn/completed":
            turn = params.get("turn")
            if isinstance(turn, dict) and turn.get("id") == self.turn_id:
                status = turn.get("status")
                return status if status in {"completed", "failed", "interrupted"} else None
            return None
        if params.get("turnId") != self.turn_id:
            return None
        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage")
            total = usage.get("total") if isinstance(usage, dict) else None
            if isinstance(total, dict):
                for src, dst in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"), ("cachedInputTokens", "cached_input_tokens")):
                    number = total.get(src)
                    if isinstance(number, int) and number >= 0:
                        self.usage[dst] = number
            return None
        if method == "error":
            if not params.get("willRetry", False):
                self.native_failed = True
            return None
        if method not in {"item/started", "item/completed"}:
            return None
        item = params.get("item")
        if not isinstance(item, dict):
            return None
        call_id = item.get("id")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 128:
            return None
        if item.get("type") == "imageGeneration":
            observe_call(self.task, self.home, call_id, "image_gen")
        elif item.get("type") == "webSearch":
            action = item.get("action")
            if isinstance(action, dict) or method == "item/completed":
                name = "web_search" if isinstance(action, dict) and action.get("type") == "search" else "web_fetch"
                observe_call(self.task, self.home, call_id, name)
        if method == "item/started":
            if item.get("type") == "imageGeneration" and call_id not in self.media_calls:
                if len(self.media_calls) >= MAX_OUTPUT_FILES:
                    raise ValueError("Codex native image call limit exceeded")
                self.media_calls[call_id] = "running"
                self._aggregate()
            return None
        if call_id in self.seen:
            return None
        self.seen.add(call_id)
        if len(self.seen) > 256:
            raise ValueError("Codex native item limit exceeded")
        if item.get("type") == "agentMessage":
            # Dedicated images return files only. Model prose is neither a
            # completion receipt nor a safe place for embedded image bytes.
            return None
        elif item.get("type") == "webSearch":
            extract_codex_sources(item, self.task.sources)
        elif item.get("type") == "imageGeneration":
            append_event(self.session.session_id, "native_media", {
                "tool_call_id": call_id, "status": str(item.get("status", ""))[:32],
            }, self.home)
            if item.get("status") != "completed" or not isinstance(item.get("savedPath"), str):
                self.native_failed = True
                self.media_calls[call_id] = "failed"
                self._aggregate()
                return None
            assert self.image_root is not None
            self.delivery_attempted = True
            try:
                source = Path(item["savedPath"])
                expected = self.image_root / f"{image_component(call_id)}.png"
                if source.absolute() != expected.absolute():
                    raise ValueError("Codex image path does not match this native item")
                if len(self.task.output_files) >= MAX_OUTPUT_FILES:
                    raise ValueError("Codex output file limit exceeded")
                output = deliver_file(
                    source, allowed_root=self.image_root, cwd=Path(self.task.cwd),
                    task_id=self.task.task_id, tool_call_id=call_id,
                    previous=self.previous, expected="image",
                )
            except (OSError, ValueError):
                self.native_failed = True
                self.media_calls[call_id] = "failed"
                self._aggregate()
                raise
            self.task.output_files.append(output)
            self.media_calls[call_id] = "completed"
            # Native completion is durable even if the later turn response is lost.
            self._aggregate()
        checkpoint(self.home, self.task)
        return None

    def _aggregate(self, *, ended: bool = False) -> None:
        states = set(self.media_calls.values())
        if "running" in states and ended:
            status = "uncertain"
        elif "failed" in states:
            status = "failed"
        elif "running" in states:
            status = "running"
        elif states:
            status = "completed"
        else:
            return
        finish_job(
            self.task, self.home, status=status,
            reason="Native image calls failed or remain unconfirmed; do not resubmit." if status in {"failed", "uncertain"} else None,
        )

    async def run(self, command: list[str], overrides: list[str]) -> TurnResult:
        if self.cancelled:
            return self.result("cancelled")
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        argv = [*command, "app-server", "-c", AUTH_STORE_OVERRIDE]
        for override in overrides:
            argv += ["-c", override]
        self.proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, cwd=self.task.cwd, env=self.env,
            limit=READ_LIMIT, **kwargs,
        )
        try:
            if self.proc.pid:
                record_pid(
                    self.home, self.session.session_id, self.proc.pid,
                    process_create_time(self.proc.pid), process_image_name(self.proc.pid),
                )
            initialized = await self._rpc("initialize", {
                "clientInfo": {"name": "agent-bridge", "title": "Agent Bridge", "version": "1"},
            })
            raw_home = initialized.get("codexHome")
            expected_home = default_codex_home(self.env)
            if not isinstance(raw_home, str) or Path(raw_home).resolve() != expected_home.resolve():
                raise ValueError("Codex initialize returned an unexpected output home")
            await self._send({"method": "initialized", "params": {}})
            params: dict[str, Any] = {
                "cwd": self.task.cwd, "ephemeral": True,
                "approvalPolicy": "never", "sandbox": "workspace-write",
            }
            if self.task.model:
                params["model"] = self.task.model
            thread = await self._rpc("thread/start", params)
            thread_id = (thread.get("thread") or {}).get("id")
            if not isinstance(thread_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", thread_id):
                raise ValueError("Codex returned an invalid native thread ID")
            self.thread_id = thread_id
            self.image_root = expected_home / "generated_images" / thread_id
            self.previous = existing_files(self.image_root)
            if self.cancelled:
                return self.result("cancelled")
            start_job(self.task, self.home, provider="codex", native_id=thread_id)
            turn_input: list[dict[str, Any]] = [{"type": "text", "text": self.task.message, "text_elements": []}]
            turn_input.extend(
                {"type": "localImage", "path": str((Path(self.task.cwd) / relative).resolve())}
                for relative in self.task.input_images
            )
            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "input": turn_input,
            }
            if mapped := codex_effort(self.task.effort):
                turn_params["effort"] = mapped
            turn = await self._rpc("turn/start", turn_params)
            turn_id = (turn.get("turn") or {}).get("id")
            if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 128:
                raise ValueError("Codex returned an invalid native turn ID")
            self.turn_id = turn_id
            assert self.task.job is not None
            self.task.job.native_turn_id = turn_id
            checkpoint(self.home, self.task)
            terminal = None
            for value in self.pending:
                terminal = self.consume(value) or terminal
            self.pending.clear()
            while terminal is None:
                value = await self._read()
                if "id" in value and value.get("method"):
                    await self._queue_or_reject(value)
                    continue
                terminal = self.consume(value)
            if terminal == "interrupted" or self.cancelled:
                self._aggregate(ended=True)
                if self.task.job and self.task.job.status == "running":
                    finish_job(self.task, self.home, status="uncertain", reason="Local turn interrupted; remote generation cancellation is unconfirmed.")
                return self.result("cancelled")
            if terminal == "failed" or self.native_failed:
                self._aggregate(ended=True)
                if self.task.job and self.task.job.status == "running":
                    finish_job(self.task, self.home, status="uncertain", reason="Turn failed without a definitive native image outcome; do not resubmit generation.")
                return self.result("error", "Codex native image generation failed")
            if any(status == "running" for status in self.media_calls.values()):
                self._aggregate(ended=True)
                return self.result("error", "Codex turn completed with unfinished native image calls")
            if not self.task.output_files:
                finish_job(self.task, self.home, status="failed", reason="Turn completed without a verified native image.")
                return self.result("error", "Codex turn completed without a verified native image")
            return self.result("end_turn")
        except asyncio.CancelledError:
            await self.cancel()
            if not self.task.checkpoint_error:
                self._aggregate(ended=True)
            if self.task.job and self.task.job.status == "running":
                finish_job(self.task, self.home, status="uncertain", reason="Local turn cancelled; remote generation and billing are unconfirmed.")
            return self.result("cancelled")
        except Exception as exc:
            if not self.task.checkpoint_error:
                self._aggregate(ended=True)
            if self.task.job and self.task.job.status == "running" and not self.task.checkpoint_error:
                finish_job(
                    self.task, self.home,
                    status="failed" if isinstance(exc, RpcRejected) or self.delivery_attempted else "uncertain",
                    reason="Native execution could not be confirmed; do not resubmit generation.",
                )
            return self.result(
                "cancelled" if self.cancelled else "error",
                None if self.cancelled else "Codex image execution failed: " + type(exc).__name__,
            )
        finally:
            cleanup = asyncio.create_task(reap_subprocess(self.proc, timeout=2.0))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
            finally:
                if self.proc.returncode is None:
                    raise RuntimeError("Codex image worker exit is unconfirmed; cleanup remains pending.")
                drop_pid(self.home, self.session.session_id)

    def result(self, stop: str, error: str | None = None) -> TurnResult:
        return TurnResult(
            text=self.text, stop_reason=stop, error=error,
            sources=self.task.sources, output_files=self.task.output_files, job=self.task.job,
            usage=self.usage,
            observed_model=self.task.model, observed_effort=codex_effort(self.task.effort),
        )

    async def cancel(self) -> None:
        self.cancelled = True
        if self.proc is not None and self.thread_id and self.turn_id:
            with contextlib.suppress(OSError, RuntimeError):
                await self._send({
                    "id": "bridge-interrupt", "method": "turn/interrupt",
                    "params": {"threadId": self.thread_id, "turnId": self.turn_id},
                })
        if self.proc is not None:
            await reap_subprocess(self.proc, timeout=2.0)
            if self.proc.returncode is None:
                raise RuntimeError("Codex image worker exit is unconfirmed; cleanup remains pending.")
            drop_pid(self.home, self.session.session_id)
