"""Bounded native evidence and per-task checkpoints, without provider retries."""

from __future__ import annotations

import ipaddress
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from agent_bridge.models import OutputFile, ProviderJob, SourceReference, Task, iso
from agent_bridge.persist import atomic_write_json, read_json_strict

MAX_SOURCES = 64
MAX_RAW_CHARS = 64 * 1024
MAX_CHECKPOINT_BYTES = 2 * 1024 * 1024
MEDIA_TOOLS = frozenset({"image_gen", "image_edit", "image_to_video", "reference_to_video"})
SEARCH_TOOLS = frozenset({"web_search", "web_fetch"})


def observe_call(task: Task, home: Path, call_id: str, name: str) -> None:
    """Record operation identities, not a provider-side execution budget."""
    if task.capability is None or name not in SEARCH_TOOLS | MEDIA_TOOLS:
        return
    if not call_id or len(call_id) > 128:
        raise ValueError("invalid native capability call identity")
    if call_id in task.native_calls:
        if task.native_calls[call_id] != name:
            raise ValueError("native capability tool identity changed")
        return
    if len(task.native_calls) >= 64:
        raise ValueError("native capability call evidence limit exceeded")
    task.native_calls[call_id] = name
    checkpoint(home, task)


def safe_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        if parsed.hostname.lower() == "localhost":
            return None
        try:
            if not ipaddress.ip_address(parsed.hostname).is_global:
                return None
        except ValueError:
            pass
        if any(
            any(mark in key.lower() for mark in ("token", "signature", "credential", "password", "authorization"))
            or key.lower() in {"sig", "key", "api_key", "expires"}
            or key.lower().startswith("x-amz-")
            for key, _ in parse_qsl(parsed.query)
        ):
            return None
    except ValueError:
        return None
    return value


def add_source(into: list[SourceReference], value: Any, call_id: str) -> None:
    if not isinstance(value, dict) or not call_id or len(call_id) > 128 or len(into) >= MAX_SOURCES:
        return
    url = safe_url(value.get("url"))
    if url is None or any(s.url == url and s.tool_call_id == call_id for s in into):
        return
    title = value.get("title")
    snippet = value.get("snippet")
    into.append(SourceReference(
        title=title[:500] if isinstance(title, str) and title else url,
        url=url, snippet=snippet[:2000] if isinstance(snippet, str) else None,
        tool_call_id=call_id,
    ))


def extract_codex_sources(item: dict[str, Any], into: list[SourceReference]) -> None:
    call_id = item.get("id")
    action = item.get("action")
    if not isinstance(call_id, str):
        return
    results = item.get("results")
    if isinstance(results, list):
        for result in results[:MAX_SOURCES]:
            if isinstance(result, dict) and result.get("type") == "text_result":
                add_source(into, result, call_id)
    if isinstance(action, dict) and action.get("type") in {"open_page", "find_in_page", "openPage", "findInPage"}:
        add_source(into, {"url": action.get("url")}, call_id)


def bounded_output(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        if len(value) > MAX_RAW_CHARS:
            return None
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    pending: list[tuple[Any, int]] = [(value, 0)]
    budget = MAX_RAW_CHARS
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > 4096 or depth > 12:
            return None
        if isinstance(current, str):
            budget -= len(current) * 6 + 2
        elif isinstance(current, dict):
            if len(current) > 4096:
                return None
            budget -= 2 + len(current)
            for key, nested in current.items():
                if not isinstance(key, str):
                    return None
                pending.extend(((key, depth + 1), (nested, depth + 1)))
        elif isinstance(current, list):
            if len(current) > 4096:
                return None
            budget -= 2 + len(current)
            pending.extend((nested, depth + 1) for nested in current)
        elif current is None or isinstance(current, (int, float, bool)):
            budget -= 32
        else:
            return None
        if budget < 0:
            return None
    return value


def journal_path(home: Path, task_id: str) -> Path:
    if not task_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in task_id):
        raise ValueError("invalid native checkpoint task identifier")
    return home / "native-results" / f"{task_id}.json"


def checkpoint(home: Path, task: Task) -> None:
    row = {
        "task_id": task.task_id,
        "sources": [s.model_dump(mode="json") for s in task.sources[:MAX_SOURCES]],
        "output_files": [f.model_dump(mode="json") for f in task.output_files[:16]],
        "job": task.job.model_dump(mode="json") if task.job else None,
        "native_calls": task.native_calls,
    }
    if len((json.dumps(row, ensure_ascii=False, indent=2) + "\n").encode("utf-8")) > MAX_CHECKPOINT_BYTES:
        task.checkpoint_error = True
        if task.job:
            task.job.status = "uncertain"
            task.job.reason = "Native metadata exceeded checkpoint byte limit; do not resubmit."
        raise ValueError("native checkpoint exceeded size limit")
    for attempt in range(3):
        try:
            atomic_write_json(journal_path(home, task.task_id), row)
            task.checkpoint_error = False
            return
        except OSError as exc:
            if isinstance(exc, PermissionError) and attempt < 2:
                time.sleep(0.02 * (attempt + 1))
                continue
            task.checkpoint_error = True
            if task.job:
                task.job.status = "uncertain"
                task.job.reason = "Native metadata persistence failed; do not resubmit generation."
            if "Native metadata persistence failed." not in task.warnings:
                task.warnings.append("Native metadata persistence failed.")
            raise


def load_checkpoint(home: Path, task: Task, *, interrupted: bool = False) -> None:
    if task.checkpoint_error:
        return
    path = journal_path(home, task.task_id)
    if not path.exists():
        return
    if path.stat().st_size > MAX_CHECKPOINT_BYTES:
        raise ValueError("native checkpoint exceeded size limit")
    row = read_json_strict(path, {})
    if not isinstance(row, dict) or row.get("task_id") != task.task_id:
        raise ValueError("native checkpoint task mismatch")
    task.sources = [SourceReference.model_validate(s) for s in row.get("sources", [])[:MAX_SOURCES]]
    task.output_files = [OutputFile.model_validate(f) for f in row.get("output_files", [])[:16]]
    calls = row.get("native_calls", {})
    if not isinstance(calls, dict) or len(calls) > 64 or any(
        not isinstance(key, str) or not key or len(key) > 128
        or not isinstance(value, str) or value not in SEARCH_TOOLS | MEDIA_TOOLS for key, value in calls.items()
    ):
        raise ValueError("invalid native capability call evidence")
    task.native_calls = calls
    raw_job = row.get("job")
    task.job = ProviderJob.model_validate(raw_job) if raw_job else None
    if interrupted and task.job and task.job.status in {"submitted", "running"}:
        task.job.status = "uncertain"
        task.job.reason = "Local execution ended without native completion; do not resubmit automatically."
        checkpoint(home, task)


def start_job(task: Task, home: Path, *, provider: str, native_id: str | None = None) -> None:
    if task.job is None:
        task.job = ProviderJob(
            provider=provider, native_session_id=native_id, status="running", submitted_at=iso(),
        )
    elif task.job.status == "completed":
        task.job.status = "running"
    # Persist before submitting a dedicated image turn. ACP starts are observations:
    # if the provider already started, a crash must still never trigger a replay.
    checkpoint(home, task)


def finish_job(task: Task, home: Path, *, status: str, reason: str | None = None) -> None:
    if task.job is not None:
        task.job = task.job.model_copy(update={"status": status, "reason": reason})
        checkpoint(home, task)
