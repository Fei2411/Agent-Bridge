"""A direct capability surface over the existing worker lifecycle."""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from agent_bridge.models import NativeCapability, Task, TaskStatus
from agent_bridge.output_files import checked_path

if TYPE_CHECKING:
    from agent_bridge.registry import Registry

SUPPORTED = {
    "codex": {"search", "image"},
    "grok": {"search", "image", "video"},
    "minimax": {"search"},
}


def validate_request(agent: str, capability: str, duration_sec: int | None) -> None:
    if capability not in SUPPORTED.get(agent, set()):
        raise ValueError(f"{agent} does not have a supported {capability} integration")
    if capability == "video":
        if isinstance(duration_sec, bool) or not isinstance(duration_sec, int) or not 1 <= duration_sec <= 60:
            raise ValueError("video requires duration_sec between 1 and 60")
    elif duration_sec is not None:
        raise ValueError("duration_sec applies only to video")


_SEARCH_SUFFIX = (
    "\nDo not generate media, delegate, edit project source, or use shell/network scripts "
    "as a substitute for the native capability. If the capability is unavailable or its "
    "result is uncertain, report the condition and stop.\nRequest:\n"
)
_MEDIA_SUFFIX = (
    "\nDo not retry, start another generation/search, delegate, edit project source, "
    "or use shell/network scripts as a substitute for the native capability. "
    "If the capability is unavailable or its result is uncertain, report the condition "
    "and stop. This request authorizes only this one operation.\nRequest:\n"
)


def compose_message(
    capability: str,
    prompt: str,
    duration_sec: int | None,
    *,
    previous_call_id: str | None = None,
    input_images: list[str] | None = None,
) -> str:
    instructions = {
        "search": (
            "Perform a native web search and use native page retrieval when needed "
            "to verify the findings. Return findings and native sources."
        ),
        "image": "Generate exactly one image using the native image tool. Return its local file.",
        "video": (
            f"Generate exactly one {duration_sec}-second animation using a native video tool. "
            "Use only explicitly supplied input files. If an input image is required but none "
            "was supplied, report that condition without generating an extra image. "
            "Return the completed local video file."
        ),
    }
    message = instructions[capability] + (_SEARCH_SUFFIX if capability == "search" else _MEDIA_SUFFIX) + prompt
    if input_images:
        message += (
            "\nAuthorized reference images (workspace-relative paths):\n"
            + json.dumps(input_images, ensure_ascii=False)
        )
    if previous_call_id:
        message += "\nContinue the operation identified by previous_call_id: " + previous_call_id
    return message


def _require_call(registry: Registry, call_id: str) -> Task:
    task = registry.tasks.get(call_id)
    if task is None or task.capability is None:
        raise KeyError(f"unknown capability call {call_id}")
    return task


async def _public_result(registry: Registry, task: Task, *, cursor: int, max_chars: int) -> dict[str, Any]:
    result = await registry.get_result(task.task_id, cursor=cursor, max_chars=max_chars)
    # Provider job IDs remain available for recovery, but the caller never
    # needs a Bridge session, a task tree, or a follow-up conversation.
    fields = (
        "status", "stop_reason", "error", "failure", "persisted", "warnings",
        "model", "effort", "observed_model", "observed_effort",
        "created_at", "started_at", "finished_at", "elapsed_sec",
        "sources", "output_files", "job", "usage", "result_text", "result_offset",
        "result_total_chars", "next_cursor", "has_more", "result_complete",
    )
    payload = {key: result[key] for key in fields if key in result}
    payload.update(
        call_id=task.task_id, agent=task.agent, capability=task.capability,
        request_id=task.request_id, cwd=task.cwd,
        native_calls=task.native_calls,
        pending=not registry._done[task.task_id].is_set(),
    )
    if task.capability_duration_sec is not None:
        payload["requested_duration_sec"] = task.capability_duration_sec
    return payload


async def call_capability(
    registry: Registry, *, agent: str, capability: NativeCapability, prompt: str,
    cwd: str, request_id: str, duration_sec: int | None = None,
    model: str | None = None, effort: str | None = None, user_requested: bool = False,
    previous_call_id: str | None = None, input_images: list[str] | None = None,
) -> dict[str, Any]:
    validate_request(agent, capability, duration_sec)
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id is required on the first capability call")
    if not prompt.strip() or len(prompt) > 16000:
        raise ValueError("prompt must contain between 1 and 16000 characters")
    if previous_call_id is not None and (not isinstance(previous_call_id, str) or not previous_call_id.strip()):
        raise ValueError("previous_call_id must be a capability call id")
    if input_images is not None and (
        not isinstance(input_images, list) or any(not isinstance(item, str) for item in input_images)
    ):
        raise ValueError("input_images must be a list of paths")
    message = compose_message(capability, prompt, duration_sec)
    result = await registry.dispatch_task(
        agent=agent, message=message, cwd=cwd, request_id=request_id, _capability_prompt=prompt,
        model=model, effort=effort, user_requested=user_requested,
        operation="image" if agent == "codex" and capability == "image" else None,
        _capability=capability, _capability_duration_sec=duration_sec,
        _previous_call_id=previous_call_id, _input_images=input_images,
    )
    task = _require_call(registry, result["task_id"])
    return {
        "call_id": task.task_id, "agent": agent, "capability": capability,
        "request_id": task.request_id, "status": task.status.value,
        "reused": result.get("reused", False), "persisted": result["persisted"],
    }


async def get_capability_result(
    registry: Registry, call_id: str, *, wait_sec: float = 0,
    cursor: int = 0, max_chars: int = 60000,
) -> dict[str, Any]:
    if not math.isfinite(wait_sec) or not 0 <= wait_sec <= 60:
        raise ValueError("wait_sec must be between 0 and 60")
    task = _require_call(registry, call_id)
    if cursor < 0 or not 1 <= max_chars <= 60000:
        raise ValueError("invalid result page bounds")
    waited = await registry.wait_task(task.task_id, timeout_sec=wait_sec)
    registry._validate_native_files(task)
    fail_invalid_result(task)
    registry.save()
    payload = await _public_result(registry, task, cursor=cursor, max_chars=max_chars)
    payload["pending"] = waited["timed_out"]
    return payload


async def cancel_capability(registry: Registry, call_id: str) -> dict[str, Any]:
    task = _require_call(registry, call_id)
    await registry.cancel_task(task.task_id)
    return await _public_result(registry, task, cursor=0, max_chars=60000)


def _boxes(handle: BinaryIO, start: int, end: int):
    offset = start
    while offset < end:
        handle.seek(offset)
        header = handle.read(8)
        if len(header) != 8:
            raise ValueError("truncated MP4 box")
        size, kind = struct.unpack(">I4s", header)
        prefix = 8
        if size == 1:
            extended = handle.read(8)
            if len(extended) != 8:
                raise ValueError("truncated MP4 extended box")
            size = struct.unpack(">Q", extended)[0]
            prefix = 16
        if size == 0:
            size = end - offset
        if size < prefix or offset + size > end:
            raise ValueError("invalid MP4 duration container")
        yield kind, offset + prefix, offset + size
        offset += size


def mp4_duration(path: Path) -> float:
    """Read the movie timeline from a bounded, verified MP4 container."""
    with path.open("rb") as handle:
        for kind, start, end in _boxes(handle, 0, path.stat().st_size):
            if kind != b"moov":
                continue
            for child, data, child_end in _boxes(handle, start, end):
                if child != b"mvhd":
                    continue
                handle.seek(data)
                header = handle.read(min(32, child_end - data))
                if not header:
                    raise ValueError("missing MP4 movie header")
                if header[0] == 0 and len(header) >= 20:
                    timescale, duration = struct.unpack(">II", header[12:20])
                    unknown = 0xFFFFFFFF
                elif header[0] == 1 and len(header) >= 32:
                    timescale, duration = struct.unpack(">IQ", header[20:32])
                    unknown = 0xFFFFFFFFFFFFFFFF
                else:
                    raise ValueError("invalid MP4 movie header")
                if not timescale or duration == unknown:
                    raise ValueError("MP4 duration is unavailable")
                return duration / timescale
    raise ValueError("MP4 duration is unavailable")


def validate_capability_output(task: Task) -> None:
    if task.checkpoint_error:
        raise ValueError("Native metadata persistence failed; do not resubmit generation.")
    allowed = {
        "search": {"web_search", "web_fetch"}, "image": {"image_gen", "image_edit"},
        "video": {"image_to_video", "reference_to_video"},
    }
    names = set(task.native_calls.values())
    if task.capability == "search":
        if not task.native_calls or "web_search" not in names or not names <= allowed["search"]:
            raise ValueError(
                "Search requires native web_search evidence and only search/page-retrieval operations."
            )
    elif task.capability and (len(task.native_calls) != 1 or not names <= allowed[task.capability]):
        raise ValueError("Capability requires exactly one matching native operation; do not resubmit.")
    if task.capability == "search":
        if not task.sources or task.output_files or task.job or any(
            source.tool_call_id not in task.native_calls for source in task.sources
        ):
            raise ValueError("Search requires native sources and no media generation.")
    elif task.capability in {"image", "video"}:
        expected = task.capability + "/"
        if (
            len(task.output_files) != 1 or not task.output_files[0].mime.startswith(expected)
            or task.job is None or task.job.status != "completed"
        ):
            raise ValueError("Capability requires exactly one completed native media file; do not resubmit.")
        if task.capability == "video":
            path = checked_path(Path(task.cwd) / task.output_files[0].path, Path(task.cwd))
            duration = mp4_duration(path)
            if task.capability_duration_sec is None or abs(duration - task.capability_duration_sec) > 0.05:
                raise ValueError("Video duration differs from the requested duration; do not resubmit.")
    elif task.capability is not None:
        raise ValueError("Unsupported capability result.")


def fail_invalid_result(task: Task) -> None:
    if task.capability and task.status == TaskStatus.completed:
        try:
            validate_capability_output(task)
        except (OSError, ValueError) as exc:
            task.status = TaskStatus.failed
            task.stop_reason = "error"
            task.error = str(exc)
