from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import psutil

from agent_bridge.adapters import build_adapter
from agent_bridge.adapters.acp import AcpError
from agent_bridge.adapters.base import Adapter
from agent_bridge.config import (
    COORDINATOR_MODE_HINTS,
    AgentConfig,
    AppConfig,
    load_config,
    normalize_coordinator_mode,
    write_coordinator_overlay,
)
from agent_bridge.diagnostics import redact_diagnostic
from agent_bridge.grok_observe import observe_grok_session
from agent_bridge.kimi_observe import observe_kimi_session
from agent_bridge.model_catalog import ModelCatalog, model_discovery_capability
from agent_bridge.models import (
    DEFAULT_WAIT_SEC,
    TERMINAL_STATUSES,
    FilesChangedState,
    ProcState,
    Session,
    Task,
    TaskStatus,
    iso,
    normalize_effort,
)
from agent_bridge.paths import ensure_home, result_path, state_path, transcript_path
from agent_bridge.persist import (
    atomic_write_json,
    atomic_write_text,
    file_lock,
    read_json_strict,
)
from agent_bridge.probes import probe_agent
from agent_bridge.processes import count_sibling_servers, owner_alive, process_create_time, reap_orphans
from agent_bridge.quota import QuotaCache, fetch_quota, looks_like_quota_error, provider_table, unknown_quota
from agent_bridge.transcript import (
    append_event,
    flush_pending,
    flush_session,
    forget_session,
    forget_worker_activity,
    mark_worker_activity,
    page_events,
    read_events,
    read_events_tail,
    recent_activity,
    worker_silence_sec,
)
from agent_bridge.worker_env import build_worker_env, describe_env, install_host_env, is_worker_context
from agent_bridge.workspace import merge_files_changed, snapshot_workspace

log = logging.getLogger(__name__)

RuntimeContext = Literal["coordinator", "worker"]
NESTED_DISPATCH_ERROR = (
    "nested dispatch is disabled: this Agent Bridge instance was inherited inside a worker process"
)
NESTED_PREFERENCES_ERROR = (
    "preference updates are disabled: this Agent Bridge instance was inherited inside a worker process"
)
NESTED_CANCEL_ERROR = (
    "task cancellation is disabled: this Agent Bridge instance was inherited inside a worker process"
)
NESTED_END_SESSION_ERROR = (
    "session shutdown is disabled: this Agent Bridge instance was inherited inside a worker process"
)

RESULT_TAIL = 6000
# Explicit get_result calls can read up to this many characters per page.
RESULT_PAGE_MAX_CHARS = 60000
# Retained only as a fallback if the one-time result artifact write fails.
RESULT_STORE_MAX = 30000
# Terminal tasks kept per session; older ones are pruned so state.json does
# not grow without bound over a long-lived Bridge.
TASK_KEEP_PER_SESSION = 20
FILES_CHANGED_MAX = 200
SESSION_KEEP_INACTIVE = 50
SESSION_RETAIN_SEC = 14 * 86400
TASK_KEEP_TOTAL = 200
STOP_TASK_GRACE_SEC = 15
STALL_POLL_SEC = 30
STALL_CANCEL_GRACE_SEC = 15
STATE_WRITE_ATTEMPTS = 5
STATE_WRITE_RETRY_BASE_SEC = 0.05
STATE_LOCK_TIMEOUT_SEC = 5.0
# flush_state() waits for the saves issued at call time and returns whether
# that generation reached disk; start()/stop() raise on False, wait_task
# reports it as ``persisted``.
STATE_FLUSH_ATTEMPTS = 3
# Poll cadence while flush_state() waits on the shared flusher; small so a
# terminal wait_task does not add up to a full second under save traffic.
STATE_FLUSH_POLL_SEC = 0.05
# state.json is an index, not a data store: task message/result bodies are
# persisted truncated so a long-lived Bridge keeps each write bounded.
STATE_MESSAGE_MAX = 2000
# A sibling may write a result artifact before its task row lands in
# state.json; the sweep lock does not cover the artifact write, so only
# delete orphans old enough that a live write cannot still be in flight.
ORPHAN_RESULT_MIN_AGE_SEC = 3600
# Persisted request_id bindings kept; bindings also drop with their task.
REQUEST_KEEP_MAX = 1000
MODEL_CACHE_MAX = 128


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _resolve_runtime_context(runtime_context: RuntimeContext | None) -> RuntimeContext:
    if runtime_context is None:
        return "worker" if is_worker_context() else "coordinator"
    if runtime_context not in ("coordinator", "worker"):
        raise ValueError(f"unknown runtime_context {runtime_context!r}; use coordinator or worker")
    return runtime_context


def _session_last_active_ts(last_active_at: str) -> float:
    try:
        return datetime.fromisoformat(last_active_at).timestamp()
    except (ValueError, TypeError, OSError, OverflowError):
        return 0.0


def _tail(text: str, limit: int = RESULT_TAIL) -> str:
    if len(text.encode("utf-8")) <= limit:
        return text
    encoded = text.encode("utf-8")
    return encoded[-limit:].decode("utf-8", errors="ignore")


def _request_fingerprint(request: tuple) -> str:
    blob = json.dumps(list(request), ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Registry:
    def __init__(
        self,
        home: Path,
        config: AppConfig,
        *,
        owner_pid: int | None = None,
        owner_create_time: float | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> None:
        self.home = home
        self.config = config
        self.sessions: dict[str, Session] = {}
        self.tasks: dict[str, Task] = {}
        # request_id -> (request fingerprint, task_id). Fingerprints instead
        # of request tuples so bindings can persist to state.json compactly.
        self._requests: dict[str, tuple[str, str]] = {}
        self._adapters: dict[str, Adapter] = {}
        self._model_cache: dict[tuple[str, str], tuple[float, dict]] = {}
        self._model_locks: dict[str, asyncio.Lock] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._idle: dict[str, asyncio.Task[None]] = {}
        self._bg: dict[str, asyncio.Task[None]] = {}
        self._cancel_requested: set[str] = set()
        self._lock = asyncio.Lock()
        self._last_activity = time.monotonic()
        self._watchdog: asyncio.Task[None] | None = None
        self._owner_pid = os.getpid() if owner_pid is None else owner_pid
        self._owner_create_time = (
            process_create_time(os.getpid()) if owner_create_time is None else owner_create_time
        )
        self.runtime_context = _resolve_runtime_context(runtime_context)
        self.dispatch_enabled = self.runtime_context == "coordinator"
        self._sibling_cache: tuple[float, int] | None = None
        self._quota_cache = QuotaCache(config.quota.cache_sec)
        self._stopping = False
        self._pending_state: dict[str, list[dict]] | None = None
        self._pending_gen = 0
        self._flush_task: asyncio.Task[None] | None = None
        self._save_gen = 0
        self._flushed_gen = 0
        self._state_error: Exception | None = None
        self._state_quarantined = False

    @classmethod
    def create(
        cls,
        home: Path | None = None,
        config: AppConfig | None = None,
        *,
        owner_pid: int | None = None,
        owner_create_time: float | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> Registry:
        resolved = ensure_home(home)
        return cls(
            resolved,
            config or load_config(resolved),
            owner_pid=owner_pid,
            owner_create_time=owner_create_time,
            runtime_context=runtime_context,
        )

    def _stamp_owner(self, record: Session | Task) -> None:
        record.owner_pid = self._owner_pid
        record.owner_create_time = self._owner_create_time

    def _is_mine(self, owner_pid: int | None, owner_create_time: float | None) -> bool:
        return owner_pid == self._owner_pid and owner_create_time == self._owner_create_time

    def _foreign_live(self, owner_pid: int | None, owner_create_time: float | None) -> bool:
        if owner_pid is None and owner_create_time is None:
            return False
        if self._is_mine(owner_pid, owner_create_time):
            return False
        return owner_alive(owner_pid, owner_create_time)

    def _merge_owned(
        self,
        disk_rows: list,
        mine: dict[str, dict],
        id_key: str,
    ) -> list[dict]:
        merged: dict[str, dict] = {}
        for raw in disk_rows:
            if not isinstance(raw, dict):
                continue
            key = raw.get(id_key)
            if not isinstance(key, str):
                continue
            owner_pid = raw.get("owner_pid")
            owner_create_time = raw.get("owner_create_time")
            if self._is_mine(owner_pid, owner_create_time):
                continue
            if self._foreign_live(owner_pid, owner_create_time) or key not in mine:
                merged[key] = raw
        merged.update(mine)
        return list(merged.values())

    @staticmethod
    def _state_task_row(task: Task) -> dict:
        row = task.model_dump(mode="json")
        # Full bodies already live in result files and transcripts; writing
        # them into state.json again grew the payload without bound. Keep
        # result_text whole: when the artifact write failed it is the only
        # copy (RESULT_STORE_MAX), and trimming it would lose the tail.
        if len(row["message"]) > STATE_MESSAGE_MAX:
            row["message"] = row["message"][:STATE_MESSAGE_MAX]
        return row

    def _own_rows(self) -> dict[str, list[dict]]:
        request_rows = [
            {
                "request_id": rid,
                "task_id": tid,
                "fingerprint": fp,
                # Owner stamps let _merge_owned drop rows this instance pruned;
                # without them pruned bindings linger on disk forever.
                "owner_pid": self._owner_pid,
                "owner_create_time": self._owner_create_time,
            }
            for rid, (fp, tid) in self._requests.items()
        ][-REQUEST_KEEP_MAX:]
        return {
            "sessions": [s.model_dump(mode="json") for s in self.sessions.values()],
            "tasks": [self._state_task_row(t) for t in self.tasks.values()],
            "requests": request_rows,
        }

    def _write_state(self, own: dict[str, list[dict]]) -> None:
        # The lock covers the complete read-merge-write transaction. Atomic
        # replacement alone prevents partial JSON, but not sibling lost updates.
        path = state_path(self.home)
        lock_path = path.with_name(f"{path.name}.lock")
        for attempt in range(STATE_WRITE_ATTEMPTS):
            try:
                with file_lock(lock_path, timeout_sec=STATE_LOCK_TIMEOUT_SEC):
                    disk = read_json_strict(path, {})
                    if not isinstance(disk, dict):
                        disk = {}
                    atomic_write_json(
                        path,
                        {
                            "sessions": self._merge_owned(
                                disk.get("sessions") or [],
                                {row["session_id"]: row for row in own["sessions"]},
                                "session_id",
                            ),
                            "tasks": self._merge_owned(
                                disk.get("tasks") or [],
                                {row["task_id"]: row for row in own["tasks"]},
                                "task_id",
                            ),
                            "requests": self._merge_owned(
                                disk.get("requests") or [],
                                {row["request_id"]: row for row in own["requests"]},
                                "request_id",
                            ),
                        },
                    )
                return
            except PermissionError:
                if attempt + 1 == STATE_WRITE_ATTEMPTS:
                    raise
                time.sleep(STATE_WRITE_RETRY_BASE_SEC * (2**attempt))

    def save(self) -> None:
        self._pending_state = self._own_rows()
        self._save_gen += 1
        self._pending_gen = self._save_gen
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            own, self._pending_state = self._pending_state, None
            try:
                self._write_state(own)
            except Exception as exc:
                self._pending_state = own
                self._pending_gen = self._save_gen
                self._state_error = exc
                raise
            self._flushed_gen = self._save_gen
            self._state_error = None
            return
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = loop.create_task(self._flush_state_loop(), name="state-flush")

    async def _flush_state_loop(self) -> None:
        # No await between seeing _pending_state is None and returning, so
        # save() cannot observe a still-running flush task and skip creating
        # a new one after this loop has already decided to exit.
        while self._pending_state is not None:
            own, gen = self._pending_state, self._pending_gen
            self._pending_state = None
            try:
                await asyncio.to_thread(self._write_state, own)
            except Exception as exc:
                # Keep the newest snapshot. A later save or flush can retry it.
                if self._pending_state is None:
                    self._pending_state = own
                    self._pending_gen = gen
                self._state_error = exc
                log.exception("could not write state.json")
                return
            self._flushed_gen = gen
            self._state_error = None

    async def flush_state(self) -> bool:
        """Wait until every save() issued so far is on disk.

        Generation-scoped and returns True iff that generation landed on
        disk: a save() issued *after* this call does not block it, so
        concurrent saves can no longer trigger the spurious "could not
        persist state.json" raise. Persistent write failures degrade to a
        logged warning — a broken disk must not kill coordination; the next
        save() retries the same rows because snapshots are cumulative.
        Callers that cannot proceed on stale disk (start, stop) check the
        return value; wait_task reports it as ``persisted``.
        """
        target = self._save_gen
        for _ in range(STATE_FLUSH_ATTEMPTS):
            while self._flushed_gen < target:
                task = self._flush_task
                fresh = False
                if task is None or task.done():
                    if self._pending_state is None:
                        break
                    task = asyncio.create_task(self._flush_state_loop(), name="state-flush")
                    self._flush_task = task
                    fresh = True
                # Poll the shared flusher instead of awaiting it to the end:
                # under steady save() traffic the loop keeps draining newer
                # snapshots, but our target generation is already on disk.
                await asyncio.wait({task}, timeout=STATE_FLUSH_POLL_SEC)
                if fresh and task.done():
                    break  # this attempt's run finished; retry or give up
            if self._flushed_gen >= target:
                return True
            if self._pending_state is None:
                break
        if self._flushed_gen < target:
            log.warning(
                "state.json persistence degraded (flushed %s of %s saves): %s",
                self._flushed_gen,
                target,
                self._state_error,
            )
        return False

    def touch_activity(self) -> None:
        self._last_activity = time.monotonic()

    def idle_exit_due(self) -> bool:
        idle_sec = self.config.server.idle_exit_sec
        if idle_sec <= 0:
            return False
        if time.monotonic() - self._last_activity < idle_sec:
            return False
        return all(
            task.status not in {TaskStatus.queued, TaskStatus.running} for task in self.tasks.values()
        )

    async def _idle_exit_watchdog(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                if not self.idle_exit_due():
                    continue
                idle_sec = self.config.server.idle_exit_sec
                log.info(
                    "no MCP activity for %s seconds and no queued/running tasks; self-exiting",
                    idle_sec,
                )
                # Clear before stop() so a CancelledError from stop cancelling
                # this task cannot skip os._exit once the exit decision is made.
                self._watchdog = None
                try:
                    await self.stop()
                finally:
                    os._exit(0)
        except asyncio.CancelledError:
            raise

    def _load_state(self) -> dict[str, Any]:
        """Read state.json, isolating unparsable data instead of deadlocking on it.

        _write_state refuses to treat invalid JSON as empty, so a corrupt file is
        never overwritten. Left in place it also fails every later write, so the
        process would never start again without deleting the file by hand.
        Renaming keeps the evidence and lets startup continue from empty state.
        """
        path = state_path(self.home)
        lock_path = path.with_name(f"{path.name}.lock")
        with file_lock(lock_path, timeout_sec=STATE_LOCK_TIMEOUT_SEC):
            try:
                payload = read_json_strict(path, {})
            except (json.JSONDecodeError, UnicodeDecodeError):
                # The lock only serialises concurrent writers, not a later start
                # that finds a fresh state.json corrupt again within the same
                # second, so the name needs a unique suffix of its own.
                stamp = time.strftime("%Y%m%d-%H%M%S")
                quarantine = path.with_name(
                    f"{path.name}.corrupt-{stamp}-{uuid.uuid4().hex[:8]}"
                )
                os.replace(path, quarantine)
                log.warning("moved unreadable %s to %s; starting from empty state", path, quarantine)
                self._state_quarantined = True
                return {}
        if not isinstance(payload, dict):
            log.warning("ignoring non-object %s; starting from empty state", path)
            return {}
        return payload

    async def start(self) -> None:
        self._stopping = False
        install_host_env(self.config.env)
        reap_orphans(self.home)
        payload = self._load_state()
        foreign_task_ids = {
            raw["task_id"]
            for raw in payload.get("tasks") or []
            if isinstance(raw, dict)
            and isinstance(raw.get("task_id"), str)
            and self._foreign_live(raw.get("owner_pid"), raw.get("owner_create_time"))
        }
        for raw in payload.get("sessions") or []:
            session = Session.model_validate(raw)
            if self._foreign_live(session.owner_pid, session.owner_create_time):
                continue
            self._stamp_owner(session)
            if session.proc_state in {ProcState.busy, ProcState.spawning, ProcState.ready}:
                session.proc_state = ProcState.idle_unloaded
            session.pid = None
            self.sessions[session.session_id] = session
        for raw in payload.get("tasks") or []:
            task = Task.model_validate(raw)
            if self._foreign_live(task.owner_pid, task.owner_create_time):
                continue
            self._stamp_owner(task)
            if task.status in {TaskStatus.queued, TaskStatus.running}:
                task.status = TaskStatus.failed
                task.error = "bridge_restarted"
                task.finished_at = iso()
            if task.files_changed_state == FilesChangedState.pending and task.status in TERMINAL_STATUSES:
                task.files_changed_state = (
                    FilesChangedState.collected
                    if task.files_changed or task.files_changed_total > 0
                    else FilesChangedState.unavailable
                )
            self.tasks[task.task_id] = task
            done = asyncio.Event()
            done.set()
            self._done[task.task_id] = done
        for raw in payload.get("requests") or []:
            if not isinstance(raw, dict):
                continue
            rid = raw.get("request_id")
            tid = raw.get("task_id")
            fp = raw.get("fingerprint")
            if rid and fp and tid in self.tasks:
                self._requests[rid] = (fp, tid)
        if self._state_quarantined:
            # The tasks map is empty only because state was quarantined, not
            # because the tasks are gone: sweeping now would delete result
            # files whose rows live in the corrupt file (review).
            log.warning("state.json was quarantined; skipping orphan result sweep")
        else:
            self._sweep_orphan_results(foreign_task_ids)
        # Stamp adopted owners onto disk first so a following prune is not
        # undone by _merge_owned treating the old unowned rows as foreign.
        self.save()
        if not await self.flush_state():
            raise RuntimeError("could not persist state.json") from self._state_error
        self._prune()
        self.save()
        if not await self.flush_state():
            raise RuntimeError("could not persist state.json") from self._state_error
        self.touch_activity()
        if self.config.server.idle_exit_sec > 0:
            self._watchdog = asyncio.create_task(
                self._idle_exit_watchdog(),
                name="idle-exit-watchdog",
            )

    async def stop(self) -> None:
        self._stopping = True
        await self._quota_cache.close()
        watchdog = self._watchdog
        self._watchdog = None
        if watchdog is not None:
            watchdog.cancel()
        idles = list(self._idle.values())
        for idle in idles:
            idle.cancel()
        self._idle.clear()
        if idles:
            # Await cancellation so an in-flight idle unload finishes
            # unwinding before adapter shutdown — otherwise the loop may close
            # with a worker half-reaped.
            await asyncio.wait(idles, timeout=STOP_TASK_GRACE_SEC)
        bgs = [task for task in self._bg.values() if not task.done()]
        for bg in bgs:
            bg.cancel()
        if bgs:
            _done, pending = await asyncio.wait(bgs, timeout=STOP_TASK_GRACE_SEC)
            if pending:
                log.warning(
                    "%d task(s) did not finish cancelling within %ss",
                    len(pending),
                    STOP_TASK_GRACE_SEC,
                )
        for session_id, adapter in list(self._adapters.items()):
            session = self.sessions.get(session_id)
            if session is not None:
                try:
                    await adapter.shutdown(session)
                except Exception:
                    log.exception("shutdown failed for %s", session_id)
                if session.proc_state != ProcState.dead:
                    session.proc_state = ProcState.idle_unloaded
        self._adapters.clear()
        try:
            flush_pending(self.home)
        except OSError:
            log.exception("could not flush transcripts during shutdown")
        self.save()
        if not await self.flush_state():
            raise RuntimeError("could not persist state.json") from self._state_error

    def _adapter_for(self, session: Session) -> Adapter:
        existing = self._adapters.get(session.session_id)
        if existing is not None:
            return existing
        adapter = build_adapter(self.config.get(session.agent), self.home, self.config.env)
        self._adapters[session.session_id] = adapter
        return adapter

    def _busy_task(self, session_id: str) -> Task | None:
        for task in self.tasks.values():
            if task.session_id == session_id and task.status in {TaskStatus.queued, TaskStatus.running}:
                return task
        return None

    async def list_agents(self) -> list[dict]:
        async def describe(cfg: AgentConfig) -> dict:
            row = await probe_agent(cfg, self.config.env)
            row["quota"] = await self._quota_for(cfg, available=bool(row.get("available")))
            row["model_discovery"] = model_discovery_capability(cfg)
            return row

        return list(await asyncio.gather(*(describe(cfg) for cfg in self.config.agents.values())))

    async def list_models(self, agent: str, cwd: str, *, refresh: bool = False) -> dict:
        """Discover and cache a worker's model catalog."""
        if not self.dispatch_enabled:
            raise RuntimeError("model discovery is disabled inside worker-inherited Bridge instances")
        cfg = self.config.get(agent)
        if not model_discovery_capability(cfg)["supported"]:
            raise NotImplementedError(f"model discovery is not implemented for {agent}")
        path = Path(cwd)
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("cwd must be an existing absolute project directory")
        resolved = str(path.resolve())
        key = (agent, resolved)
        previous = self._model_cache.get(key)
        if not refresh and previous and time.monotonic() - previous[0] < self.config.models.cache_sec:
            return {**copy.deepcopy(previous[1]), "cached": True}
        lock = self._model_locks.setdefault(agent, asyncio.Lock())
        try:
            async with asyncio.timeout(self.config.models.timeout_sec), lock:
                previous = self._model_cache.get(key)
                if not refresh and previous and time.monotonic() - previous[0] < self.config.models.cache_sec:
                    return {**copy.deepcopy(previous[1]), "cached": True}
                adapter = build_adapter(cfg, self.home, self.config.env)
                raw = await adapter.list_models(resolved)
                catalog = ModelCatalog.model_validate(raw)
                result = {
                    **catalog.model_dump(mode="json"),
                    "agent": agent,
                    "cwd": resolved,
                    "fetched_at": iso(),
                    "cached": False,
                    "availability": "advertised",
                    "invocation_verified": False,
                    "effort_vocabulary": "worker_native",
                }
                result["warnings"] = [redact_diagnostic(w) for w in result["warnings"]]
                if key not in self._model_cache and len(self._model_cache) >= MODEL_CACHE_MAX:
                    oldest = min(self._model_cache, key=lambda k: self._model_cache[k][0])
                    self._model_cache.pop(oldest)
                self._model_cache[key] = (time.monotonic(), copy.deepcopy(result))
                return result
        except Exception as exc:
            raise RuntimeError(redact_diagnostic(str(exc)) or type(exc).__name__) from None

    async def _quota_for(self, cfg: AgentConfig, *, available: bool) -> dict:
        """The ``quota`` block for one ``list_agents`` row. Never raises, never gates ``available``."""
        quota_cfg = self.config.quota
        if not quota_cfg.enabled:
            return unknown_quota("quota lookup is disabled ([quota] enabled = false)").model_dump(mode="json")
        if not available:
            return unknown_quota("worker command not found").model_dump(mode="json")
        try:
            env = await asyncio.to_thread(build_worker_env, cfg.env, config=self.config.env, log_fill=False)
        except Exception as exc:
            log.warning("could not build the worker env for %s quota lookup: %s", cfg.name, exc)
            return unknown_quota(f"worker environment unavailable: {type(exc).__name__}").model_dump(mode="json")
        return await fetch_quota(
            cfg,
            env,
            cache=self._quota_cache,
            timeout_sec=quota_cfg.timeout_sec,
            providers=provider_table(self.config),
        )

    SIBLING_CACHE_SEC = 60

    async def _sibling_count(self, *, refresh: bool = False) -> int:
        now = time.monotonic()
        if not refresh and self._sibling_cache is not None:
            cached_at, count = self._sibling_cache
            if now - cached_at < self.SIBLING_CACHE_SEC:
                return count
        try:
            count = await asyncio.to_thread(count_sibling_servers)
        except (psutil.Error, OSError) as exc:
            log.warning("could not count sibling agent-bridge servers: %s", exc)
            count = 0
        self._sibling_cache = (time.monotonic(), count)
        return count

    async def env_status(self) -> dict:
        status = describe_env(self.config.env)
        if self.config.warnings:
            status.setdefault("warnings", []).extend(self.config.warnings)
        siblings = await self._sibling_count()
        if siblings > 0:
            warnings = status.setdefault("warnings", [])
            warnings.append(
                f"{siblings} other agent-bridge server instance(s) running on this machine "
                "(coordinator threads may each hold an instance; "
                "counts alone do not imply orphaned processes)"
            )
        return status

    def coordinator_status(self) -> dict:
        cfg = self.config.coordinator
        return {
            "mode": cfg.mode,
            "hint": COORDINATOR_MODE_HINTS.get(cfg.mode, COORDINATOR_MODE_HINTS["auto"]),
            "instructions": cfg.instructions or None,
            "runtime_context": self.runtime_context,
            "dispatch_enabled": self.dispatch_enabled,
        }

    def set_preferences(
        self,
        *,
        mode: str | None = None,
        instructions: str | None = None,
    ) -> dict:
        if not self.dispatch_enabled:
            raise RuntimeError(NESTED_PREFERENCES_ERROR)
        if mode is None and instructions is None:
            raise ValueError("provide mode and/or instructions")
        if mode is not None:
            mode = normalize_coordinator_mode(mode, strict=True)
        path = write_coordinator_overlay(self.home, mode=mode, instructions=instructions)
        # The running instance applies the change immediately; the file makes
        # it stick for every Bridge instance started after this.
        if mode is not None:
            self.config.coordinator.mode = mode
        if instructions is not None:
            self.config.coordinator.instructions = instructions.strip()
        notes = [
            "active in this Bridge instance now; other running instances pick it up at their next start"
        ]
        if mode is not None and os.environ.get("AGENT_BRIDGE_MODE"):
            notes.append(
                "this host pins mode via AGENT_BRIDGE_MODE, which outranks the file "
                "after a restart; the saved mode applies to hosts without that pin"
            )
        return {"coordinator": self.coordinator_status(), "path": str(path), "notes": notes}

    async def dispatch_task(
        self,
        agent: str,
        message: str,
        cwd: str,
        session_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        title: str | None = None,
        user_requested: bool = False,
        request_id: str | None = None,
    ) -> dict:
        if not self.dispatch_enabled:
            raise RuntimeError(NESTED_DISPATCH_ERROR)
        if self.config.coordinator.mode == "manual" and not user_requested:
            raise RuntimeError(
                "coordinator mode is manual: dispatch only when the user explicitly "
                "asked for a worker on this task. If they did, retry with "
                "user_requested=true; otherwise do the work yourself."
            )
        if request_id is not None:
            try:
                request_id = str(uuid.UUID(request_id))
            except ValueError:
                raise ValueError("request_id must be a UUID") from None
        cwd_path = Path(cwd)
        if not cwd_path.is_absolute():
            raise ValueError("cwd must be an absolute path")
        if not cwd_path.exists():
            raise ValueError(f"cwd does not exist: {cwd_path}")
        if not cwd_path.is_dir():
            raise ValueError(f"cwd is not a directory: {cwd_path}")
        effort = normalize_effort(effort)
        self.config.get(agent)
        # Compare supplied arguments, not selections inherited from a mutable session.
        request = (agent, message, str(cwd_path.resolve()), session_id, model, effort, title, user_requested)
        async with self._lock:
            if request_id is not None and request_id in self._requests:
                previous_fp, task_id = self._requests[request_id]
                if previous_fp != _request_fingerprint(request):
                    raise ValueError("request_id is already bound to a different dispatch request")
                task = self.tasks[task_id]
                return {
                    "task_id": task.task_id,
                    "session_id": task.session_id,
                    "agent": task.agent,
                    "model": task.model,
                    "effort": task.effort,
                    "request_id": request_id,
                    "reused": True,
                    "persisted": await self.flush_state(),
                }
            if session_id:
                session = self.sessions.get(session_id)
                if session is None:
                    raise KeyError(f"unknown session {session_id}")
                if session.agent != agent:
                    raise ValueError(f"session {session_id} belongs to agent {session.agent}, not {agent}")
                if Path(session.cwd).resolve() != cwd_path.resolve():
                    raise ValueError(
                        f"session {session.session_id} is bound to {session.cwd}; "
                        "follow-up cwd must be the same project folder"
                    )
                busy = self._busy_task(session.session_id)
                if busy is not None:
                    raise RuntimeError(
                        f"session {session.session_id} is busy with {busy.task_id}; call wait_task first"
                    )
            else:
                session = Session(
                    session_id=_new_id("sess"),
                    agent=agent,
                    cwd=str(cwd_path.resolve()),
                    model=model,
                    effort=effort,
                    title=title,
                    proc_state=ProcState.spawning,
                )
                self._stamp_owner(session)
                self.sessions[session.session_id] = session
            if model:
                session.model = model
            if effort:
                session.effort = effort
            if title:
                session.title = title
            if session_id is None:
                session.cwd = str(cwd_path.resolve())
            session.last_active_at = iso()
            task = Task(
                task_id=_new_id("task"),
                session_id=session.session_id,
                agent=agent,
                message=message,
                cwd=session.cwd,
                model=model or session.model,
                effort=effort or session.effort,
                status=TaskStatus.queued,
            )
            self._stamp_owner(task)
            self.tasks[task.task_id] = task
            if request_id is not None:
                self._requests[request_id] = (_request_fingerprint(request), task.task_id)
            self._done[task.task_id] = asyncio.Event()
            self._cancel_idle(session.session_id)
            self._prune()
            self.save()
            # The task row and request_id binding must reach disk before the
            # worker starts: a crash between them would otherwise replay the
            # dispatch as a duplicate. Failure degrades to persisted=False,
            # never a rejected dispatch.
            log.info(
                "task_dispatched task_id=%s session_id=%s agent=%s",
                task.task_id,
                session.session_id,
                agent,
            )
            try:
                persisted = await self.flush_state()
            finally:
                # Even when the caller cancels mid-flush, the registered task
                # must still start — a request_id retry would otherwise reuse
                # a row that never runs.
                self._bg[task.task_id] = asyncio.create_task(self._run_task(task.task_id), name=f"task-{task.task_id}")
        return {
            "task_id": task.task_id,
            "session_id": session.session_id,
            "agent": agent,
            "model": session.model,
            "effort": session.effort,
            "persisted": persisted,
            **({"request_id": request_id, "reused": False} if request_id is not None else {}),
        }

    async def _run_task(self, task_id: str) -> None:
        started_monotonic = time.monotonic()
        task = self.tasks[task_id]
        session = self.sessions[task.session_id]
        adapter = self._adapter_for(session)
        task.status = TaskStatus.running
        task.started_at = iso()
        session.proc_state = ProcState.busy
        session.last_active_at = iso()
        self.save()
        watch = None
        before: dict[str, tuple[int, int]] | None = None
        try:
            mark_worker_activity(session.session_id, self.home)
            before = await asyncio.to_thread(snapshot_workspace, task.cwd)
            agent_cfg = self.config.get(session.agent)
            limit = agent_cfg.stall_timeout_sec
            turn_cap = agent_cfg.turn_timeout_sec
            watch = (
                asyncio.create_task(
                    self._stall_watch(task, session, adapter, limit, turn_cap),
                    name=f"stall-{task_id}",
                )
                if limit > 0 or turn_cap > 0
                else None
            )
            result = await adapter.run_turn(session, task)
            if result.native_session_id:
                session.native_session_id = result.native_session_id
            task.result_chars = len(result.text)
            # Worker-derived text crosses the trust boundary here: scrub
            # before it lands in state.json/transcripts/MCP replies.
            task.warnings = [redact_diagnostic(w) for w in result.warnings]
            try:
                atomic_write_text(result_path(task.task_id, self.home), result.text)
                task.result_text = _tail(result.text)
            except OSError as exc:
                task.result_text = _tail(result.text, RESULT_STORE_MAX)
                task.warnings.append(
                    f"full result persistence failed: {type(exc).__name__}: {exc}"
                )
                log.exception("could not persist full result for task %s", task.task_id)
            inside_changed, outside_changed = await asyncio.to_thread(
                merge_files_changed, task.cwd, result.files_changed, before
            )
            self._set_files_changed(task, inside_changed, outside_changed)
            task.usage = result.usage
            if session.agent == "grok":
                observed = await asyncio.to_thread(observe_grok_session, session.cwd, session.native_session_id)
                task.observed_model = observed["model"]
                task.observed_effort = observed["effort"]
            elif session.agent == "kimi":
                observed = await asyncio.to_thread(observe_kimi_session, session.native_session_id)
                task.observed_model = observed["model"]
                task.observed_effort = observed["effort"]
                if observed["failure"]:
                    # Kimi answered end_turn, so nothing above this line knows
                    # the turn failed. Say so where the coordinator looks.
                    task.warnings.append(
                        "kimi reported end_turn but the turn failed: "
                        + redact_diagnostic(str(observed["failure"]))
                    )
            else:
                # OpenCode (and any later ACP worker) has no on-disk sampler
                # log. Report the last model/effort the adapter applied.
                task.observed_model = result.observed_model
                task.observed_effort = result.observed_effort
            # cancel_task's timeout path may already have finalized this task
            # as cancelled; a late turn result must not overwrite that.
            if task.status not in TERMINAL_STATUSES:
                task.stop_reason = result.stop_reason
                if result.error:
                    if task_id in self._cancel_requested:
                        task.status = TaskStatus.cancelled
                        task.stop_reason = "cancelled"
                    else:
                        task.status = TaskStatus.failed
                        task.error = redact_diagnostic(result.error)
                elif result.stop_reason == "cancelled":
                    task.status = TaskStatus.cancelled
                else:
                    task.status = TaskStatus.completed
            session.turns += 1
        except asyncio.CancelledError:
            if task.status not in TERMINAL_STATUSES:
                task.status = TaskStatus.cancelled
                task.stop_reason = "cancelled"
        except Exception as exc:
            if isinstance(exc, AcpError):
                log.warning("task %s failed: %s; failure=%s", task_id, exc, exc.failure.model_dump())
            else:
                log.exception("task %s failed", task_id)
            if task.status not in TERMINAL_STATUSES:
                if task_id in self._cancel_requested:
                    task.status = TaskStatus.cancelled
                    task.stop_reason = "cancelled"
                    task.error = None
                    task.failure = None
                else:
                    task.status = TaskStatus.failed
                    task.error = redact_diagnostic(str(exc))
                    task.failure = exc.failure if isinstance(exc, AcpError) else None
                    task.stop_reason = "error"
        finally:
            self._cancel_requested.discard(task_id)
            if watch is not None:
                watch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watch
            if task.files_changed_state == FilesChangedState.pending:
                if before is None:
                    task.files_changed_state = FilesChangedState.unavailable
                else:
                    try:
                        inside_changed, outside_changed = await asyncio.to_thread(
                            merge_files_changed, task.cwd, [], before
                        )
                        self._set_files_changed(task, inside_changed, outside_changed)
                    except Exception as exc:
                        task.files_changed_state = FilesChangedState.unavailable
                        task.warnings.append(
                            f"final files_changed collection failed: {type(exc).__name__}: {exc}"
                        )
                        log.exception(
                            "could not collect final workspace changes for task %s",
                            task.task_id,
                        )
            if task.finished_at is None:
                task.finished_at = iso()
            # A Kimi quota failure arrives as a warning on a "completed" turn.
            if (task.status == TaskStatus.failed and looks_like_quota_error(task.error)) or any(
                looks_like_quota_error(w) for w in task.warnings
            ):
                # The cached "ok" is now a lie; make the next list_agents re-read it.
                self._quota_cache.invalidate(session.agent)
            session.last_active_at = iso()
            if session.proc_state != ProcState.dead:
                session.proc_state = ProcState.ready if adapter.resident else ProcState.idle_unloaded
            try:
                flush_session(session.session_id, self.home)
            except OSError:
                log.exception("could not flush transcript for task %s", task.task_id)
            self._done[task_id].set()
            self._bg.pop(task_id, None)
            self.save()
            duration_ms = int((time.monotonic() - started_monotonic) * 1000)
            log_method = log.warning if task.status == TaskStatus.failed else log.info
            log_method(
                "task_finished task_id=%s session_id=%s agent=%s status=%s "
                "duration_ms=%s stop_reason=%s error=%r",
                task.task_id,
                task.session_id,
                task.agent,
                task.status.value,
                duration_ms,
                task.stop_reason,
                (task.error or "")[:500],
            )
            self._schedule_idle(session.session_id)

    @staticmethod
    def _set_files_changed(task: Task, paths: list[str], outside: list[str] | None = None) -> None:
        task.files_changed_total = len(paths)
        task.files_changed = paths[:FILES_CHANGED_MAX]
        task.files_changed_truncated = len(paths) > FILES_CHANGED_MAX
        outside = outside or []
        task.files_outside_cwd_total = len(outside)
        task.files_outside_cwd = outside[:FILES_CHANGED_MAX]
        task.files_changed_state = FilesChangedState.collected

    async def _stall_watch(
        self,
        task: Task,
        session: Session,
        adapter: Adapter,
        limit: int,
        turn_cap: int = 0,
    ) -> None:
        """Dual watchdog: ``limit`` bounds worker silence, ``turn_cap`` bounds
        total turn wall-clock. Silence only counts worker-originated transcript
        events, so a heartbeat-spamming worker still needs the cap (H-16)."""
        started = time.monotonic()
        while True:
            silence = worker_silence_sec(session.session_id, self.home) or 0.0
            elapsed = time.monotonic() - started
            if limit > 0 and silence >= limit:
                reason = "stalled"
                break
            if turn_cap > 0 and elapsed >= turn_cap:
                reason = "timed_out"
                break
            waits: list[float] = [STALL_POLL_SEC]
            if limit > 0:
                waits.append(limit - silence)
            if turn_cap > 0:
                waits.append(turn_cap - elapsed)
            await asyncio.sleep(max(min(waits), 0.05))
        if task.status in TERMINAL_STATUSES:
            return
        log.warning(
            "task %s %s: cancelling the turn (silence=%.0fs, elapsed=%.0fs)",
            task.task_id,
            reason,
            silence,
            elapsed,
        )
        task.status = TaskStatus.failed
        task.stop_reason = reason
        task.error = (
            f"worker produced no output for {limit}s (stall_timeout_sec); "
            "Bridge cancelled the turn"
            if reason == "stalled"
            else f"turn exceeded turn_timeout_sec={turn_cap}s; Bridge cancelled the turn"
        )
        append_event(
            session.session_id,
            "error",
            {
                "error": task.error,
                "stalled": reason == "stalled",
                "timed_out": reason == "timed_out",
                "stall_timeout_sec": limit,
                "turn_timeout_sec": turn_cap,
            },
            self.home,
        )
        # The turn normally returns (and _run_task tears this watch down)
        # while cancel() is still reaping the worker; shield so that
        # cleanup runs to completion instead of dying with the watch.
        await asyncio.shield(self._cancel_stalled(adapter, session, task.task_id))
        try:
            await asyncio.wait_for(self._done[task.task_id].wait(), timeout=STALL_CANCEL_GRACE_SEC)
        except TimeoutError:
            bg = self._bg.get(task.task_id)
            if bg is not None:
                bg.cancel()

    async def _cancel_stalled(self, adapter: Adapter, session: Session, task_id: str) -> None:
        try:
            await adapter.cancel(session)
        except Exception:
            log.exception("stall cancel failed for task %s", task_id)

    def _sweep_orphan_results(self, foreign_task_ids: set[str]) -> None:
        """Remove result files whose task never reached state.json.

        A crash between the result write and the final save() leaves a file
        no task row ever references (A1). Rows owned by a live sibling
        instance are not in self.tasks, so their files are kept via
        foreign_task_ids.
        """
        results_dir = self.home / "results"
        if not results_dir.is_dir():
            return
        # Re-read state under the same lock writers use: a sibling may have
        # written a task row between the startup snapshot and this sweep —
        # deleting its result file would destroy a live artifact (review).
        path = state_path(self.home)
        lock_path = path.with_name(f"{path.name}.lock")
        try:
            with file_lock(lock_path, timeout_sec=STATE_LOCK_TIMEOUT_SEC):
                disk = read_json_strict(path, {})
        except (OSError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
            # Unknown task set — deleting on a stale view risks live results.
            # FileLockTimeout is a TimeoutError: a slow sibling writer must not
            # abort startup over an optional sweep.
            log.warning("could not re-read %s; skipping orphan result sweep", path)
            return
        known = set(self.tasks) | foreign_task_ids
        if isinstance(disk, dict):
            for row in disk.get("tasks") or []:
                if isinstance(row, dict) and isinstance(row.get("task_id"), str):
                    known.add(row["task_id"])
        # task ids are "task_<hex>" (_new_id); narrowing the glob avoids
        # touching unrelated .txt files dropped in results/.
        for orphan in results_dir.glob("task_*.txt"):
            task_id = orphan.stem
            if task_id in known:
                continue
            try:
                if time.time() - orphan.stat().st_mtime < ORPHAN_RESULT_MIN_AGE_SEC:
                    # Recent file: a sibling may have written the artifact
                    # before its task row reached disk (the write lock does
                    # not cover it), so keep it for the next boot's sweep.
                    continue
                orphan.unlink()
            except OSError:
                log.warning("could not remove orphan result %s", orphan)
            else:
                log.info("removed orphan result %s (no task row)", orphan.name)

    def _drop_task(self, task_id: str) -> None:
        self.tasks.pop(task_id, None)
        self._requests = {
            key: binding for key, binding in self._requests.items() if binding[1] != task_id
        }
        self._done.pop(task_id, None)
        try:
            result_path(task_id, self.home).unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove pruned result for task %s", task_id)

    def _prune_sessions(self) -> None:
        now = time.time()
        candidates = [
            session
            for session in self.sessions.values()
            if session.proc_state in {ProcState.dead, ProcState.idle_unloaded}
            and session.session_id not in self._adapters
            and self._busy_task(session.session_id) is None
        ]
        candidates.sort(key=lambda item: _session_last_active_ts(item.last_active_at), reverse=True)
        drop = [
            session
            for index, session in enumerate(candidates)
            if index >= SESSION_KEEP_INACTIVE
            or now - _session_last_active_ts(session.last_active_at) > SESSION_RETAIN_SEC
        ]
        for session in drop:
            session_id = session.session_id
            self.sessions.pop(session_id, None)
            # Result files die with their tasks; the per-session transcript
            # would otherwise grow unbounded on disk (H-02).
            forget_session(session_id, self.home)
            self._cancel_idle(session_id)
            for task in [item for item in self.tasks.values() if item.session_id == session_id]:
                self._drop_task(task.task_id)
            log.info(
                "pruned session %s (%s, last active %s)",
                session_id,
                session.proc_state.value,
                session.last_active_at,
            )

    def _prune_tasks(self) -> None:
        by_session: dict[str, list[Task]] = {}
        for task in self.tasks.values():
            if task.status in TERMINAL_STATUSES:
                by_session.setdefault(task.session_id, []).append(task)
        for terminal in by_session.values():
            if len(terminal) <= TASK_KEEP_PER_SESSION:
                continue
            terminal.sort(key=lambda item: item.created_at)
            for old in terminal[: len(terminal) - TASK_KEEP_PER_SESSION]:
                self._drop_task(old.task_id)
        terminal_all = [task for task in self.tasks.values() if task.status in TERMINAL_STATUSES]
        if len(terminal_all) <= TASK_KEEP_TOTAL:
            return
        terminal_all.sort(key=lambda item: item.created_at)
        for old in terminal_all[: len(terminal_all) - TASK_KEEP_TOTAL]:
            self._drop_task(old.task_id)

    def _prune(self) -> None:
        self._prune_sessions()
        self._prune_tasks()

    def _cancel_idle(self, session_id: str) -> None:
        idle = self._idle.pop(session_id, None)
        if idle:
            idle.cancel()

    def _schedule_idle(self, session_id: str) -> None:
        if self._stopping:
            return
        session = self.sessions.get(session_id)
        if session is None:
            return
        adapter = self._adapters.get(session_id)
        if adapter is not None and not adapter.resident:
            return
        try:
            cfg = self.config.get(session.agent)
        except KeyError:
            return
        if cfg.idle_unload_sec <= 0:
            return
        self._cancel_idle(session_id)

        async def _idle() -> None:
            await asyncio.sleep(cfg.idle_unload_sec)
            current = self.sessions.get(session_id)
            if current is None or self._busy_task(session_id):
                return
            adapter = self._adapters.get(session_id)
            if adapter is not None:
                try:
                    await adapter.shutdown(current)
                except Exception:
                    log.exception("idle unload failed for %s", session_id)
                else:
                    # Drop the dead adapter mapping: keeping it excludes the
                    # session from inactive pruning (the filter requires
                    # `not in _adapters`); revival rebuilds via _adapter_for.
                    self._adapters.pop(session_id, None)
            current.proc_state = ProcState.idle_unloaded
            self.save()

        self._idle[session_id] = asyncio.create_task(_idle(), name=f"idle-{session_id}")

    def _task_from_disk(self, task_id: str) -> Task | None:
        """Last-persisted row for a task this instance does not own.

        _merge_owned keeps sibling instances' rows in state.json, so a task
        unknown in memory can still answer read calls from its persisted
        state (F5). Lifecycle calls deliberately stay memory-only.
        """
        try:
            rows = self._load_state().get("tasks") or []
        except Exception:
            return None
        for raw in rows:
            if isinstance(raw, dict) and raw.get("task_id") == task_id:
                try:
                    return Task.model_validate(raw)
                except ValueError:
                    return None
        return None

    def _require_task(self, task_id: str, *, allow_disk: bool = False) -> Task:
        task = self.tasks.get(task_id)
        if task is None and allow_disk:
            task = self._task_from_disk(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id}")
        return task

    @staticmethod
    def _result_hint(task: Task, prefix: str) -> str:
        hint = prefix
        if task.agent == "grok":
            hint += (
                " Grok system-prompt identity is not the selected model; "
                "use observed_model from this payload."
            )
        if task.agent == "kimi":
            hint += (
                " Kimi reports a failed turn as end_turn with empty text; "
                "an empty result is only clean if warnings is empty."
            )
        if task.agent == "opencode":
            hint += (
                " OpenCode observed_model/effort are the last values Bridge "
                "successfully set on the session after mapping, not a live sampler."
            )
        if task.agent == "claude":
            hint += (
                " Claude Code observed_model/effort are the last values Bridge "
                "successfully set on the session after mapping, not a live sampler."
            )
        if task.agent == "cursor":
            hint += (
                " Cursor observed_model is the requested ID after Cursor confirmed "
                "its mapped ACP options; observed_effort is Cursor's confirmed thought "
                "level. Neither is a live sampler."
            )
        if task.agent == "devin":
            hint += (
                " Devin observed_model is the last id Bridge set on the session; "
                "observed_effort is always null because the level is part of the model id."
            )
        if task.agent == "zcode":
            hint += (
                " ZCode observed_model is the slug Bridge matched to an advertised "
                "provider\\model option; observed_effort is the thought level it set."
            )
        if task.agent == "minimax":
            hint += (
                " MiniMax Code observed_model is the provider/model slug Bridge matched; "
                "observed_effort is the thinkingEffort it set. Neither is a live sampler."
            )
        if task.files_changed_truncated:
            hint += (
                f" files_changed lists the first {FILES_CHANGED_MAX} of "
                f"{task.files_changed_total} paths; run git status in cwd for the full set."
            )
        if task.stop_reason == "stalled":
            hint += (
                " The worker produced no output for stall_timeout_sec and Bridge cancelled the turn; "
                "read get_transcript for its last activity, then either dispatch a narrower "
                f"task on the same session_id or raise [agents.{task.agent}] stall_timeout_sec "
                "if that step was legitimately long."
            )
        if task.stop_reason == "timed_out":
            hint += (
                " The turn hit turn_timeout_sec and Bridge cancelled it; "
                f"raise [agents.{task.agent}] turn_timeout_sec if turns legitimately "
                "need more wall-clock time."
            )
        return hint

    def _task_snapshot(self, task: Task, include_result: bool = False) -> dict:
        events = read_events_tail(task.session_id, self.home)
        payload: dict[str, Any] = {
            "task_id": task.task_id,
            "session_id": task.session_id,
            "agent": task.agent,
            "status": task.status.value,
            "stop_reason": task.stop_reason,
            "error": task.error,
            "failure": task.failure.model_dump(mode="json") if task.failure else None,
            "warnings": task.warnings,
            "files_changed": task.files_changed,
            "files_changed_total": task.files_changed_total,
            "files_changed_truncated": task.files_changed_truncated,
            "files_changed_state": task.files_changed_state.value,
            "files_outside_cwd": task.files_outside_cwd,
            "files_outside_cwd_total": task.files_outside_cwd_total,
            "model": task.model,
            "effort": task.effort,
            "observed_model": task.observed_model,
            "observed_effort": task.observed_effort,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "recent_activity": recent_activity(events),
        }
        if task.task_id not in self.tasks:
            # Read via the disk fallback: values are the other instance's
            # last-persisted state, not live data.
            payload["from_state"] = True
        if task.started_at:
            start = datetime.fromisoformat(task.started_at)
            end = datetime.fromisoformat(task.finished_at) if task.finished_at else datetime.fromisoformat(iso())
            payload["elapsed_sec"] = max(0, int((end - start).total_seconds()))
        else:
            payload["elapsed_sec"] = 0
        if include_result:
            preview = _tail(task.result_text)
            total_chars = task.result_chars or len(task.result_text)
            payload["result_text"] = preview
            payload["result_total_chars"] = total_chars
            payload["result_truncated"] = total_chars > len(preview)
            payload["usage"] = task.usage
            payload["hint"] = self._result_hint(
                task,
                "Use get_result for the complete final result and get_transcript "
                "for the detailed turn log.",
            )
        payload["silent_for_sec"] = (
            int(worker_silence_sec(task.session_id, self.home) or 0)
            if task.status == TaskStatus.running and task.task_id in self.tasks
            else None
        )
        cfg = self.config.agents.get(task.agent)
        payload["stall_timeout_sec"] = cfg.stall_timeout_sec if cfg is not None else None
        return payload

    async def _persisted(self, task: Task) -> bool | None:
        """Persistence verdict for terminal answers; None while non-terminal.

        A disk-fallback row belongs to another instance and is already on
        disk by definition, so it answers True without a local flush.
        """
        if task.status not in TERMINAL_STATUSES:
            return None
        if task.task_id not in self.tasks:
            return True
        done = self._done.get(task.task_id)
        if done is None or not done.is_set():
            # _run_task marks the status terminal in-memory, then collects
            # final files_changed and flushes the transcript before its
            # closing save. Done set means that save has been issued (it
            # runs synchronously right after set()); before that, issue the
            # save here so a read cannot answer persisted=True while disk
            # still says running.
            self.save()
        return await self.flush_state()

    async def _with_persisted(self, task: Task, payload: dict) -> dict:
        persisted = await self._persisted(task)
        if persisted is not None:
            payload["persisted"] = persisted
        return payload

    async def wait_task(self, task_id: str, timeout_sec: float = DEFAULT_WAIT_SEC) -> dict:
        task = self._require_task(task_id)
        event = self._done.setdefault(task_id, asyncio.Event())
        if task.status not in TERMINAL_STATUSES:
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout_sec)
            except TimeoutError:
                return {"timed_out": True, **self._task_snapshot(self.tasks[task_id])}
        # A terminal answer must also be on disk, not merely queued
        # behind the background flusher.
        return {
            "timed_out": False,
            "persisted": (await self._persisted(task)) is True,
            **self._task_snapshot(self.tasks[task_id], include_result=True),
        }

    async def check_task(self, task_id: str) -> dict:
        task = self._require_task(task_id, allow_disk=True)
        return await self._with_persisted(task, self._task_snapshot(task))

    async def get_result(
        self,
        task_id: str,
        cursor: int = 0,
        max_chars: int = RESULT_PAGE_MAX_CHARS,
    ) -> dict:
        if cursor < 0:
            raise ValueError("cursor must be non-negative")
        if not 1 <= max_chars <= RESULT_PAGE_MAX_CHARS:
            raise ValueError(f"max_chars must be between 1 and {RESULT_PAGE_MAX_CHARS}")
        task = self._require_task(task_id, allow_disk=True)
        path = result_path(task.task_id, self.home)
        artifact = path.is_file()
        try:
            text = path.read_text(encoding="utf-8") if artifact else task.result_text
        except OSError as exc:
            log.warning("could not read full result for task %s: %s", task.task_id, exc)
            artifact = False
            text = task.result_text
        if cursor > len(text):
            raise ValueError(f"cursor exceeds result length ({len(text)})")
        end = min(len(text), cursor + max_chars)
        has_more = end < len(text)
        payload = self._task_snapshot(task)
        payload.update(
            {
                "result_text": text[cursor:end],
                "result_offset": cursor,
                "result_total_chars": len(text) if artifact else (task.result_chars or len(text)),
                "next_cursor": end if has_more else None,
                "has_more": has_more,
                "result_truncated": has_more,
                "result_complete": artifact,
                "result_source": "artifact" if artifact else "legacy_state",
                "usage": task.usage,
                "hint": self._result_hint(
                    task,
                    "Continue with next_cursor while has_more is true. "
                    "Use get_transcript for the detailed turn log.",
                ),
            }
        )
        return await self._with_persisted(task, payload)

    def get_transcript(self, session_id: str, offset: int = 0, limit: int = 50, kinds: list[str] | None = None) -> dict:
        if session_id not in self.sessions and not transcript_path(session_id, self.home).is_file():
            raise KeyError(f"unknown session {session_id}")
        events = read_events(session_id, self.home)
        return page_events(events, offset=offset, limit=limit, kinds=kinds)

    async def cancel_task(self, task_id: str) -> dict:
        if not self.dispatch_enabled:
            raise RuntimeError(NESTED_CANCEL_ERROR)
        task = self._require_task(task_id)
        if task.status in TERMINAL_STATUSES:
            return await self._with_persisted(task, self._task_snapshot(task))
        self._cancel_requested.add(task_id)
        session = self.sessions[task.session_id]
        adapter = self._adapters.get(session.session_id)
        if adapter is not None:
            await adapter.cancel(session)
        bg = self._bg.get(task_id)
        if bg is not None:
            bg.cancel()
            await asyncio.wait({bg}, timeout=15)
        else:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._done[task_id].wait(), timeout=15)
        if not self._done[task_id].is_set():
            # _run_task never ran (cancelled before its first step) or did not
            # finish in time; close the record here.
            task.status = TaskStatus.cancelled
            task.stop_reason = "cancelled"
            task.finished_at = iso()
            task.files_changed_state = FilesChangedState.unavailable
            if task.started_at is None and session.proc_state == ProcState.spawning:
                session.proc_state = ProcState.idle_unloaded
            self._bg.pop(task_id, None)
            self._done[task_id].set()
            self.save()
        task = self.tasks[task_id]
        return await self._with_persisted(task, self._task_snapshot(task))

    def list_sessions(self, active_only: bool = False) -> list[dict]:
        rows = []
        for session in self.sessions.values():
            if active_only and session.proc_state in {ProcState.dead, ProcState.idle_unloaded}:
                continue
            rows.append(
                {
                    "session_id": session.session_id,
                    "agent": session.agent,
                    "cwd": session.cwd,
                    "native_session_id": session.native_session_id,
                    "proc_state": session.proc_state.value,
                    "turns": session.turns,
                    "title": session.title,
                    "model": session.model,
                    "effort": session.effort,
                    "last_active_at": session.last_active_at,
                }
            )
        return rows

    async def session_scope(self) -> dict:
        return {
            "scope": "current_instance",
            "other_live_instances": await self._sibling_count(refresh=True),
        }

    async def end_session(self, session_id: str) -> dict:
        if not self.dispatch_enabled:
            raise RuntimeError(NESTED_END_SESSION_ERROR)
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown session {session_id}")
        busy = self._busy_task(session_id)
        if busy is not None:
            await self.cancel_task(busy.task_id)
        adapter = self._adapters.pop(session_id, None)
        if adapter is not None:
            await adapter.shutdown(session)
        self._cancel_idle(session_id)
        forget_worker_activity(session_id, self.home)
        session.proc_state = ProcState.dead
        session.pid = None
        self.save()
        return {"session_id": session_id, "proc_state": session.proc_state.value}
