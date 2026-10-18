from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat()


class TaskStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class FilesChangedState(StrEnum):
    pending = "pending"
    collected = "collected"
    unavailable = "unavailable"


class ProcState(StrEnum):
    spawning = "spawning"
    ready = "ready"
    busy = "busy"
    idle_unloaded = "idle_unloaded"
    dead = "dead"


TERMINAL_STATUSES = {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}
DEFAULT_WAIT_SEC = 180.0
NativeCapability = Literal["search", "image", "video"]
EFFORTS = ("off", "low", "medium", "high", "max")


def normalize_effort(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip().lower()
    aliases = {"l": "low", "m": "medium", "med": "medium", "h": "high"}
    value = aliases.get(value, value)
    if value not in EFFORTS:
        raise ValueError(f"effort must be one of off, low, medium, high, max (got {raw!r})")
    return value


def agy_effort(effort: str | None) -> str | None:
    if effort in {None, "low", "medium", "high"}:
        return effort
    if effort == "off":
        return "low"
    if effort == "max":
        return "high"
    return None


def dsh_effort(effort: str | None) -> str | None:
    if effort in {None, "off", "low", "high", "max"}:
        return effort
    if effort == "medium":
        return "high"
    return None


def grok_effort(effort: str | None) -> str | None:
    """Map Bridge effort onto Grok `_meta.reasoningEffort` wire values.

    Grok Build (`ReasoningEffort::from_str`) accepts
    ``none|minimal|low|medium|high|xhigh|max``. Bridge ``off`` is Grok
    ``none``. Bridge ``max`` maps to ``xhigh``: that is the top tier the
    grok-4.6 catalog actually advertises, whereas ``max`` is not listed for
    any current model and gets clamped by the worker.
    """
    if effort in {None, "low", "medium", "high"}:
        return effort
    if effort == "max":
        return "xhigh"
    if effort == "off":
        return "none"
    return None


class Session(BaseModel):
    session_id: str
    agent: str
    cwd: str
    native_session_id: str | None = None
    internal: bool = False
    proc_state: ProcState = ProcState.idle_unloaded
    model: str | None = None
    effort: str | None = None
    title: str | None = None
    turns: int = 0
    created_at: str = Field(default_factory=iso)
    last_active_at: str = Field(default_factory=iso)
    pid: int | None = None
    pid_create_time: float | None = None
    image_name: str | None = None
    owner_pid: int | None = None
    owner_create_time: float | None = None


class TaskFailure(BaseModel):
    operation: str
    kind: str
    prompt_may_have_been_sent: bool
    exit_code: int | None = None
    stderr_summary: str | None = None


class SourceReference(BaseModel):
    title: str
    url: str
    snippet: str | None = None
    tool_call_id: str | None = None


class OutputFile(BaseModel):
    path: str
    mime: str
    bytes: int = Field(gt=0)
    sha256: str
    tool_call_id: str | None = None


class ProviderJob(BaseModel):
    provider: str
    task_id: str | None = None
    native_session_id: str | None = None
    native_turn_id: str | None = None
    status: Literal["submitted", "running", "completed", "failed", "uncertain"]
    submitted_at: str | None = None
    remote_cancel_supported: bool = False
    reason: str | None = None


class Task(BaseModel):
    task_id: str
    session_id: str
    agent: str
    message: str
    cwd: str
    model: str | None = None
    effort: str | None = None
    request_id: str | None = None
    operation: Literal["image"] | None = None
    capability: NativeCapability | None = None
    capability_duration_sec: int | None = None
    previous_call_id: str | None = None
    input_images: list[str] = Field(default_factory=list)
    input_image_metadata: list[OutputFile] = Field(default_factory=list)
    native_calls: dict[str, str] = Field(default_factory=dict)
    observed_model: str | None = None
    observed_effort: str | None = None
    status: TaskStatus = TaskStatus.queued
    stop_reason: str | None = None
    result_text: str = ""
    result_chars: int = 0
    files_changed: list[str] = Field(default_factory=list)
    files_changed_total: int = 0
    files_changed_truncated: bool = False
    files_changed_state: FilesChangedState = FilesChangedState.pending
    # Paths the worker REPORTED that resolve outside task.cwd. Kept separate
    # so an escape is never silently folded into an in-workspace path (E3).
    files_outside_cwd: list[str] = Field(default_factory=list)
    files_outside_cwd_total: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)
    sources: list[SourceReference] = Field(default_factory=list)
    output_files: list[OutputFile] = Field(default_factory=list)
    job: ProviderJob | None = None
    checkpoint_error: bool = False
    error: str | None = None
    failure: TaskFailure | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=iso)
    started_at: str | None = None
    finished_at: str | None = None
    owner_pid: int | None = None
    owner_create_time: float | None = None


class TranscriptEvent(BaseModel):
    ts: str = Field(default_factory=iso)
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class TurnResult(BaseModel):
    text: str = ""
    files_changed: list[str] = Field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: dict[str, Any] = Field(default_factory=dict)
    native_session_id: str | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    output_files: list[OutputFile] = Field(default_factory=list)
    job: ProviderJob | None = None
    # Last model/effort the adapter successfully applied this turn. Grok and
    # Kimi observers in Registry overwrite these with on-disk sampler truth.
    observed_model: str | None = None
    observed_effort: str | None = None
