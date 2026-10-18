from __future__ import annotations

import asyncio
import json
import struct
import sys
import types
import uuid
import zlib
from pathlib import Path

import pytest

from agent_bridge import capabilities, server
from agent_bridge.adapters.base import Adapter
from agent_bridge.adapters.codex import CodexAdapter
from agent_bridge.config import AgentConfig, AppConfig, CoordinatorConfig
from agent_bridge.models import (
    ProcState,
    ProviderJob,
    Session,
    SourceReference,
    Task,
    TaskStatus,
    TurnResult,
)
from agent_bridge.native_results import observe_call
from agent_bridge.registry import Registry


def png_bytes(pixel=b"\x00\x00\x00\xff"):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00" + pixel))
        + chunk(b"IEND", b"")
    )


class SearchWorker(Adapter):
    resident = True

    def __init__(self, config, home, *, sources=True, blocked=False, calls_made=None, media=None):
        super().__init__(config, home)
        self.sources = sources
        self.calls = 0
        self.closed = 0
        self.sessions = []
        self.prompts = []
        self.native_ids = []
        self.ready = asyncio.Event()
        self.release = asyncio.Event()
        self.calls_made = calls_made
        self.media = media
        self.fail_resume = False
        if not blocked:
            self.release.set()

    async def ensure_session(self, session, *, strict_resume=False):
        self.sessions.append(session.session_id)
        if strict_resume and self.fail_resume:
            raise RuntimeError("native session could not be resumed")
        if session.native_session_id is None:
            session.native_session_id = f"native-{len(self.native_ids) + 1}"
        self.native_ids.append(session.native_session_id)

    async def run_turn(self, session, task):
        await self.ensure_session(
            session, strict_resume=bool(task.previous_call_id) and task.operation != "image",
        )
        self.calls += 1
        self.prompts.append(task.message)
        self.ready.set()
        await self.release.wait()
        if task.capability == "image":
            planned = [("image_1", "image_gen")]
            kinds = ["png"]
        elif task.capability == "video":
            planned = [("video_1", "image_to_video")]
            kinds = ["mp4"]
        else:
            planned = self.calls_made or [("search_1", "web_search")]
            kinds = []
        for call_id, name in planned:
            observe_call(task, self.home, call_id, name)
        outputs = []
        if kinds:
            from agent_bridge.output_files import deliver_file
            for index, kind in enumerate(kinds):
                source = Path(task.cwd) / f"native-{task.task_id}-{index}.{kind}"
                source.write_bytes(png_bytes() if kind == "png" else box(b"ftyp", b"isom") + box(b"moov", box(b"mvhd", b"\x00" * 12 + struct.pack(">II", 1000, 5000))) + box(b"mdat", b"fixture"))
                outputs.append(deliver_file(
                    source, allowed_root=Path(task.cwd), cwd=Path(task.cwd), task_id=task.task_id,
                    tool_call_id=planned[0][0], previous=set(), expected="image" if kind == "png" else "video",
                ))
            task.job = ProviderJob(provider=task.agent, status="completed")
        result = TurnResult(
            text="Native search fixture",
            sources=[SourceReference(
                title="Official", url="https://example.com/native", tool_call_id=planned[0][0],
            )] if self.sources and any(name == "web_search" for _call, name in planned) else [],
            output_files=outputs,
        )
        task.sources = result.sources
        task.output_files = outputs
        from agent_bridge.native_results import checkpoint
        checkpoint(self.home, task)
        return result

    async def cancel(self, session):
        self.release.set()

    async def shutdown(self, session):
        self.closed += 1


def search_registry(tmp_path, *, sources=True, blocked=False, mode="auto", context="coordinator", agent="grok", calls_made=None, media=None):
    config = AgentConfig(name=agent, protocol="acp", command=["unused"], stall_timeout_sec=0)
    home = tmp_path / "bridge"
    registry = Registry.create(
        home, AppConfig(
            agents={agent: config}, coordinator=CoordinatorConfig(mode=mode),
        ), runtime_context=context,
    )
    worker = SearchWorker(config, home, sources=sources, blocked=blocked, calls_made=calls_made, media=media)

    def adapter_for(session):
        registry._adapters[session.session_id] = worker
        return worker

    registry._adapter_for = adapter_for
    args = dict(
        agent=agent, capability="search", prompt="Find the official documentation",
        cwd=str(tmp_path), request_id=str(uuid.uuid4()),
    )
    return registry, worker, args


async def finish(registry, call):
    return await capabilities.get_capability_result(registry, call["call_id"], wait_sec=5)


@pytest.mark.asyncio
async def test_direct_call_result_replay_and_shutdown(tmp_path):
    registry, worker, args = search_registry(tmp_path)
    try:
        first = await capabilities.call_capability(registry, **args)
        again = await capabilities.call_capability(registry, **args)
        assert first["call_id"] == again["call_id"]
        assert again["reused"]
        result = await finish(registry, first)
        assert result["status"] == "completed"
        assert result["sources"][0]["tool_call_id"] == "search_1"
        assert not result["pending"]
        assert not {"task_id", "session_id", "task_mode", "write_paths"} & result.keys()
        assert "session_id" not in first
        assert worker.calls == worker.closed == 1
        assert next(iter(registry.sessions.values())).proc_state == ProcState.dead
        await registry.flush_state()
        fresh = Registry.create(registry.home, registry.config)
        await fresh.start()
        retained = await capabilities.call_capability(fresh, **args)
        assert retained["reused"]
        assert retained["call_id"] == first["call_id"]
        assert len(fresh.tasks) == 1
        assert (await finish(fresh, retained))["status"] == "completed"
        await fresh.stop()
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_missing_native_evidence_fails_without_reexecution(tmp_path):
    registry, worker, args = search_registry(tmp_path, sources=False)
    try:
        call = await capabilities.call_capability(registry, **args)
        result = await finish(registry, call)
        assert result["status"] == "failed"
        assert "native sources" in result["error"]
        replay = await capabilities.call_capability(registry, **args)
        assert (await finish(registry, replay))["status"] == "failed"
        assert worker.calls == 1
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_pending_and_local_cancel_close_internal_worker(tmp_path):
    registry, worker, args = search_registry(tmp_path, blocked=True)
    try:
        call = await capabilities.call_capability(registry, **args)
        await asyncio.wait_for(worker.ready.wait(), 5)
        pending = await capabilities.get_capability_result(registry, call["call_id"])
        assert pending["pending"]
        assert pending["status"] == "running"
        result = await capabilities.cancel_capability(registry, call["call_id"])
        assert result["status"] == "cancelled"
        assert worker.closed == 1
        assert worker.calls == 1
    finally:
        await registry.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,context,error", [
    ("manual", "coordinator", "manual"),
    ("auto", "worker", "nested dispatch"),
])
async def test_capabilities_keep_dispatch_authority(tmp_path, mode, context, error):
    registry, worker, args = search_registry(tmp_path, mode=mode, context=context)
    try:
        with pytest.raises(RuntimeError, match=error):
            await capabilities.call_capability(registry, **args)
        assert worker.calls == 0
        assert not registry.tasks
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_changed_request_cannot_reuse_identity(tmp_path):
    registry, worker, args = search_registry(tmp_path)
    try:
        call = await capabilities.call_capability(registry, **args)
        await finish(registry, call)
        with pytest.raises(ValueError, match="different dispatch"):
            await capabilities.call_capability(registry, **{**args, "prompt": "Different"})
        assert worker.calls == 1
    finally:
        await registry.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"capability": "video", "duration_sec": None},
    {"capability": "video", "duration_sec": True},
    {"capability": "video", "duration_sec": 0},
    {"capability": "video", "duration_sec": 61},
    {"agent": "minimax", "capability": "image"},
    {"agent": "codex", "capability": "video", "duration_sec": 5},
    {"duration_sec": 5}, {"prompt": ""}, {"request_id": ""}, {"request_id": None},
])
async def test_invalid_requests_never_reach_worker(tmp_path, change):
    registry, worker, args = search_registry(tmp_path)
    try:
        with pytest.raises(ValueError):
            await capabilities.call_capability(registry, **{**args, **change})
        assert not registry.tasks
        assert worker.calls == 0
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_codex_image_capability_uses_official_protocol_fixture(tmp_path, monkeypatch):
    config = AgentConfig(
        name="codex", protocol="codex", command=["unused"], stall_timeout_sec=0,
        env={"CODEX_HOME": str(tmp_path / "official"), "BRIDGE_IMAGE_FIXTURE": "success"},
    )
    monkeypatch.setattr(CodexAdapter, "_base_cmd", lambda self, env=None: [
        sys.executable, str(Path(__file__).with_name("fake_codex_image.py")),
    ])
    registry = Registry.create(tmp_path / "bridge", AppConfig(agents={"codex": config}))
    try:
        call = await capabilities.call_capability(
            registry, agent="codex", capability="image", prompt="An offline fixture",
            cwd=str(tmp_path), request_id=str(uuid.uuid4()),
        )
        result = await finish(registry, call)
        assert result["status"] == "completed"
        assert len(result["output_files"]) == 1
        assert result["job"]["status"] == "completed"
        assert (tmp_path / "submissions.log").read_text().splitlines() == ["submit"]
        file = tmp_path / result["output_files"][0]["path"]
        file.write_bytes(b"corrupt")
        reread = await finish(registry, call)
        assert reread["status"] == "failed"
        assert "output_files" not in reread
        assert (tmp_path / "submissions.log").read_text().splitlines() == ["submit"]
    finally:
        await registry.stop()


def box(kind, data):
    return struct.pack(">I4s", len(data) + 8, kind) + data


@pytest.mark.parametrize("version,extended", [(0, False), (1, False), (1, True)])
def test_mp4_duration_uses_movie_timeline(tmp_path, version, extended):
    header = (b"\x00" * 12 + struct.pack(">II", 1000, 5000)) if version == 0 else (
        b"\x01" + b"\x00" * 19 + struct.pack(">IQ", 1000, 5000)
    )
    movie = box(b"mvhd", header)
    moov = box(b"moov", movie) if not extended else (
        struct.pack(">I4sQ", 1, b"moov", len(movie) + 16) + movie
    )
    path = tmp_path / "test.mp4"
    path.write_bytes(box(b"ftyp", b"isom") + moov + box(b"mdat", b"fixture"))
    assert capabilities.mp4_duration(path) == 5


@pytest.mark.parametrize("data", [
    box(b"moov", box(b"mvhd", b"")),
    box(b"moov", box(b"mvhd", b"\x00" * 20)),
    box(b"moov", box(b"mvhd", b"\x02" + b"\x00" * 31)),
    box(b"moov", box(b"mvhd", b"\x00" * 12 + struct.pack(">II", 1000, 0xFFFFFFFF))),
    b"\x00\x00", struct.pack(">I4sQ", 1, b"moov", 100),
])
def test_invalid_movie_duration_is_not_claimed(tmp_path, data):
    path = tmp_path / "test.mp4"
    path.write_bytes(data)
    with pytest.raises(ValueError):
        capabilities.mp4_duration(path)


def test_media_count_and_video_duration_are_acceptance_requirements(tmp_path):
    from agent_bridge.models import ProviderJob
    from agent_bridge.output_files import deliver_file

    source = tmp_path / "native.mp4"
    movie = box(b"mvhd", b"\x00" * 12 + struct.pack(">II", 1000, 5000))
    source.write_bytes(box(b"ftyp", b"isom") + box(b"moov", movie) + box(b"mdat", b"fixture"))
    output = deliver_file(
        source, allowed_root=tmp_path, cwd=tmp_path, task_id="task_video",
        tool_call_id="video_1", previous=set(), expected="video",
    )
    task = Task(
        task_id="task_video", session_id="session", agent="grok", cwd=str(tmp_path),
        message="fixture", capability="video", capability_duration_sec=5,
        native_calls={"video_1": "image_to_video"},
        output_files=[output], job=ProviderJob(provider="grok", status="completed"),
    )
    capabilities.validate_capability_output(task)
    task.capability_duration_sec = 6
    with pytest.raises(ValueError, match="duration differs"):
        capabilities.validate_capability_output(task)
    task.capability_duration_sec = 5
    task.output_files.append(output)
    with pytest.raises(ValueError, match="exactly one"):
        capabilities.validate_capability_output(task)


def test_codex_search_override_is_per_capability(tmp_path, monkeypatch):
    config = AgentConfig(name="codex", protocol="codex", command=["unused"])
    adapter = CodexAdapter(config, tmp_path)
    monkeypatch.setattr(adapter, "_base_cmd", lambda env=None: ["codex"])
    session = Session(session_id="sess", agent="codex", cwd=str(tmp_path))
    task = Task(task_id="task", session_id="sess", agent="codex", cwd=str(tmp_path), message="hi")
    assert 'web_search="live"' not in adapter._build_cmd(session, task)
    task.capability = "search"
    assert 'web_search="live"' in adapter._build_cmd(session, task)


@pytest.mark.asyncio
async def test_mcp_capability_tools_expose_one_call_without_session(tmp_path):
    registry, worker, args = search_registry(tmp_path)
    ctx = types.SimpleNamespace(request_context=types.SimpleNamespace(lifespan_context=registry))
    try:
        call = await server.call_capability(ctx, **args)
        assert call["ok"]
        result = await server.get_capability_result(ctx, call["call_id"], wait_sec=5)
        assert result["ok"]
        assert result["status"] == "completed"
        assert "session_id" not in result
        for wait in [-1, 61, float("nan")]:
            invalid = await server.get_capability_result(ctx, call["call_id"], wait_sec=wait)
            assert not invalid["ok"]
        invalid = await server.cancel_capability(ctx, "missing")
        assert not invalid["ok"]
        assert worker.calls == 1
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_host_stop_during_capability_shutdown_finishes_cleanup(tmp_path):
    registry, worker, args = search_registry(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def shutdown(session):
        entered.set()
        await release.wait()
        worker.closed += 1

    worker.shutdown = shutdown
    call = await capabilities.call_capability(registry, **args)
    await asyncio.wait_for(entered.wait(), 5)
    stopping = asyncio.create_task(registry.stop())
    try:
        await asyncio.sleep(0.02)
        session = next(iter(registry.sessions.values()))
        assert session.session_id in registry._adapters
        release.set()
        await asyncio.wait_for(stopping, 5)
        assert worker.closed == 1
        assert registry._done[call["call_id"]].is_set()
        assert call["call_id"] not in registry._bg
        assert session.proc_state == ProcState.dead
        assert session.session_id not in registry._adapters
    finally:
        release.set()
        await stopping


@pytest.mark.parametrize("calls,success", [
    ({"first": "web_fetch"}, False),
    ({"first": "web_search", "second": "web_search"}, True),
    ({"first": "web_search", "second": "web_fetch"}, True),
    ({"first": "web_search", "second": "image_gen"}, False),
    ({"first": "web_search"}, True),
    ({}, False),
])
def test_search_requires_one_search_not_just_sources(tmp_path, calls, success):
    task = Task(
        task_id="task_search", session_id="session", agent="grok", cwd=str(tmp_path),
        message="fixture", capability="search", native_calls=calls,
        sources=[SourceReference(title=str(i), url=f"https://example.com/{i}", tool_call_id="first") for i in range(3)],
    )
    if success:
        capabilities.validate_capability_output(task)
    else:
        with pytest.raises(ValueError, match=r"native web_search evidence|native sources"):
            capabilities.validate_capability_output(task)


def test_acp_counts_second_media_attempt_before_output_validation(tmp_path):
    from agent_bridge.acp_results import AcpNativeResults

    task = Task(
        task_id="task_image", session_id="session", agent="grok", cwd=str(tmp_path),
        message="fixture", capability="image",
    )
    native = AcpNativeResults(task, tmp_path / "bridge", None, {})
    for call_id in ("first", "second"):
        native.consume({"toolCallId": call_id, "toolName": "image_gen", "status": "in_progress"})
    assert task.native_calls == {"first": "image_gen", "second": "image_gen"}
    with pytest.raises(ValueError, match="one matching native"):
        capabilities.validate_capability_output(task)


def test_metadata_defaults_remain_compatible():
    session = Session.model_validate({"session_id": "sess", "agent": "grok", "cwd": "C:/work"})
    task = Task.model_validate({
        "task_id": "task", "session_id": "sess", "agent": "grok", "message": "old", "cwd": "C:/work",
    })
    assert session.internal is False
    assert task.previous_call_id is None
    assert task.input_images == []


@pytest.mark.asyncio
async def test_capability_sessions_are_hidden_and_not_dispatchable(tmp_path):
    registry, worker, args = search_registry(tmp_path)
    try:
        normal = await registry.dispatch_task("grok", "ordinary work", cwd=str(tmp_path))
        first = await capabilities.call_capability(registry, **args)
        await finish(registry, first)
        second = await capabilities.call_capability(registry, **{**args, "request_id": str(uuid.uuid4())})
        await finish(registry, second)
        visible = {row["session_id"] for row in registry.list_sessions()}
        diagnostic = registry.list_sessions(include_internal=True)
        internal_ids = {row["session_id"] for row in diagnostic if row["internal"]}
        assert visible == {normal["session_id"]}
        assert "internal" not in registry.list_sessions()[0]
        assert internal_ids.isdisjoint(visible)
        assert len(internal_ids) == 2
        assert set(worker.sessions) >= visible | internal_ids
        hidden = next(row["session_id"] for row in diagnostic if row["internal"])
        with pytest.raises(ValueError, match="internal capability"):
            await registry.dispatch_task("grok", "do not reuse", cwd=str(tmp_path), session_id=hidden)
        assert worker.calls == 3
    finally:
        await registry.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["image", "video"])
async def test_grok_media_continuation_reuses_session_and_output(tmp_path, second):
    registry, worker, args = search_registry(tmp_path)
    try:
        image = await capabilities.call_capability(registry, **{**args, "capability": "image"})
        first = await finish(registry, image)
        assert len(first["output_files"]) == 1
        follow = await capabilities.call_capability(
            registry, **{
                **args, "capability": second, "request_id": str(uuid.uuid4()),
                "previous_call_id": image["call_id"], "duration_sec": 5 if second == "video" else None,
            },
        )
        result = await finish(registry, follow)
        assert result["status"] == "completed"
        assert len(set(worker.sessions)) == 1
        assert registry.tasks[follow["call_id"]].input_images == [first["output_files"][0]["path"]]
        assert f"previous_call_id: {image['call_id']}" in worker.prompts[-1]
        assert "Authorized reference images" in worker.prompts[-1]
        assert image["call_id"] not in worker.prompts[0]
        assert registry.tasks[image["call_id"]].output_files
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_search_continuation_reuses_session_without_files(tmp_path):
    registry, worker, args = search_registry(tmp_path, agent="minimax")
    try:
        first = await capabilities.call_capability(registry, **args)
        await finish(registry, first)
        second = await capabilities.call_capability(
            registry, **{**args, "request_id": str(uuid.uuid4()), "previous_call_id": first["call_id"]},
        )
        assert (await finish(registry, second))["status"] == "completed"
        assert worker.sessions == [worker.sessions[0], worker.sessions[0]]
        assert "Authorized reference images" not in worker.prompts[-1]
        assert registry.tasks[second["call_id"]].input_images == []
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_codex_image_continuation_uses_new_local_image_session(tmp_path, monkeypatch):
    config = AgentConfig(
        name="codex", protocol="codex", command=["unused"], stall_timeout_sec=0,
        env={"CODEX_HOME": str(tmp_path / "official"), "BRIDGE_IMAGE_FIXTURE": "success", "BRIDGE_IMAGE_UNIQUE": "1"},
    )
    monkeypatch.setattr(CodexAdapter, "_base_cmd", lambda self, env=None: [
        sys.executable, str(Path(__file__).with_name("fake_codex_image.py")),
    ])
    registry = Registry.create(tmp_path / "bridge", AppConfig(agents={"codex": config}))
    reference = tmp_path / "source.png"
    reference.write_bytes(png_bytes())
    try:
        first = await capabilities.call_capability(
            registry, agent="codex", capability="image", prompt="An offline fixture",
            cwd=str(tmp_path), request_id=str(uuid.uuid4()),
        )
        await finish(registry, first)
        second = await capabilities.call_capability(
            registry, agent="codex", capability="image", prompt="Continue offline",
            cwd=str(tmp_path), request_id=str(uuid.uuid4()), previous_call_id=first["call_id"],
            input_images=["source.png"],
        )
        result = await finish(registry, second)
        assert result["status"] == "completed"
        assert len(registry.sessions) == 2
        submitted = [json.loads(line) for line in (tmp_path / "inputs.log").read_text(encoding="utf-8").splitlines()]
        assert submitted[0] == [{"type": "text", "text": registry.tasks[first["call_id"]].message, "text_elements": []}]
        assert {"type": "localImage", "path": str(reference.resolve())} in submitted[1]
    finally:
        await registry.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("change,error", [
    ({"agent": "codex"}, "same agent"),
    ({"cwd_suffix": "elsewhere"}, "same agent"),
    ({"capability": "video", "source": "video"}, "unsupported"),
    ({"running": True}, "completed"),
    ({"stale": True}, "latest"),
])
async def test_invalid_continuation_does_not_execute(tmp_path, change, error):
    registry, worker, args = search_registry(tmp_path, blocked=change.get("running", False))
    registry.config.agents["codex"] = AgentConfig(
        name="codex", protocol="codex", command=["unused"], stall_timeout_sec=0,
    )
    try:
        source = change.get("source", "image")
        first = await capabilities.call_capability(registry, **{
            **args, "capability": source, "duration_sec": 5 if source == "video" else None,
        })
        if change.get("running"):
            await asyncio.wait_for(worker.ready.wait(), 5)
        else:
            await finish(registry, first)
        if change.get("stale"):
            latest = await capabilities.call_capability(
                registry, **{**args, "capability": "image", "request_id": str(uuid.uuid4()), "previous_call_id": first["call_id"]},
            )
            await finish(registry, latest)
            target = first["call_id"]
        else:
            target = first["call_id"]
        cwd = tmp_path / change["cwd_suffix"] if "cwd_suffix" in change else tmp_path
        cwd.mkdir(exist_ok=True)
        before = worker.calls
        with pytest.raises((ValueError, KeyError), match=error):
            await capabilities.call_capability(registry, **{
                **args, "agent": change.get("agent", "grok"), "capability": change.get("capability", "image"),
                "cwd": str(cwd), "request_id": str(uuid.uuid4()), "previous_call_id": target,
                "duration_sec": 5 if change.get("capability") == "video" else None,
            })
        assert worker.calls == before
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_failed_native_resume_does_not_create_replacement(tmp_path):
    registry, worker, args = search_registry(tmp_path)
    try:
        first = await capabilities.call_capability(registry, **args)
        await finish(registry, first)
        worker.fail_resume = True
        continued = await capabilities.call_capability(
            registry, **{**args, "request_id": str(uuid.uuid4()), "previous_call_id": first["call_id"]},
        )
        result = await finish(registry, continued)
        assert result["status"] == "failed"
        assert "could not be resumed" in result["error"]
        assert len(registry.sessions) == 1
        failed = registry.tasks[continued["call_id"]]
        assert failed.session_id == registry.tasks[first["call_id"]].session_id
        assert failed.status == TaskStatus.failed
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_result_stays_pending_until_worker_shutdown_finishes(tmp_path):
    registry, worker, args = search_registry(tmp_path)
    shutdown_entered = asyncio.Event()
    release_shutdown = asyncio.Event()
    original_shutdown = worker.shutdown

    async def slow_shutdown(session):
        shutdown_entered.set()
        await release_shutdown.wait()
        await original_shutdown(session)

    worker.shutdown = slow_shutdown
    try:
        first = await capabilities.call_capability(registry, **args)
        await asyncio.wait_for(shutdown_entered.wait(), 5)
        task = registry.tasks[first["call_id"]]
        assert task.status == TaskStatus.completed
        assert not registry._done[first["call_id"]].is_set()
        waited = await registry.wait_task(first["call_id"], timeout_sec=0.2)
        assert waited["timed_out"] is True
        assert waited["status"] == "completed"
        pending = await capabilities.get_capability_result(registry, first["call_id"], wait_sec=0.2)
        assert pending["pending"] is True
        assert pending["status"] == "completed"
        release_shutdown.set()
        finished = await capabilities.get_capability_result(registry, first["call_id"], wait_sec=5)
        assert finished["pending"] is False
        assert finished["status"] == "completed"
        continued = await capabilities.call_capability(
            registry, **{**args, "request_id": str(uuid.uuid4()), "previous_call_id": first["call_id"]},
        )
        result = await finish(registry, continued)
        assert result["status"] == "completed"
        assert worker.sessions == [worker.sessions[0], worker.sessions[0]]
    finally:
        release_shutdown.set()
        await registry.stop()


@pytest.mark.asyncio
async def test_original_and_continuation_replay_survives_loss_and_restart(tmp_path):
    registry, worker, args = search_registry(tmp_path)
    try:
        plain = await capabilities.call_capability(registry, **args)
        assert registry._requests[args["request_id"]][1] == plain["call_id"]
        assert registry.tasks[plain["call_id"]].message != (
            "Perform exactly one native web search. Return its findings and native sources.\n"
            "Do not retry, start another generation/search, delegate, edit project source, "
            "or use shell/network scripts as a substitute for the native capability. "
            "If the capability is unavailable or its result is uncertain, report the condition "
            "and stop. This request authorizes only this one operation.\nRequest:\n"
        ) + args["prompt"]
        await finish(registry, plain)
        reference = tmp_path / "source.png"
        reference.write_bytes(png_bytes())
        first = await capabilities.call_capability(registry, **{
            **args, "capability": "image", "request_id": str(uuid.uuid4()), "input_images": ["source.png"],
        })
        assert registry._requests[first["request_id"]][0] != registry._requests[plain["request_id"]][0]
        await finish(registry, first)
        follow_args = {
            **args, "capability": "image", "request_id": str(uuid.uuid4()),
            "previous_call_id": first["call_id"],
        }
        second = await capabilities.call_capability(registry, **follow_args)
        await finish(registry, second)
        reference.unlink()
        registry.tasks.pop(first["call_id"])
        registry._requests.pop(first["request_id"])
        registry.save()
        await registry.flush_state()
        restored = Registry.create(registry.home, registry.config)
        restored._adapter_for = registry._adapter_for
        await restored.start()
        try:
            replayed = await capabilities.call_capability(restored, **args)
            continued = await capabilities.call_capability(restored, **follow_args)
            assert replayed["reused"] and replayed["call_id"] == plain["call_id"]
            assert continued["reused"] and continued["call_id"] == second["call_id"]
            assert worker.calls == 3
        finally:
            await restored.stop()
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_changed_continuation_arguments_conflict(tmp_path):
    registry, _worker, args = search_registry(tmp_path)
    try:
        first = await capabilities.call_capability(registry, **{**args, "capability": "image"})
        await finish(registry, first)
        follow = {
            **args, "capability": "image", "request_id": str(uuid.uuid4()),
            "previous_call_id": first["call_id"],
        }
        await finish(registry, await capabilities.call_capability(registry, **follow))
        with pytest.raises(ValueError, match="different dispatch"):
            await capabilities.call_capability(registry, **{**follow, "prompt": "Different"})
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_reference_paths_are_checked_again_before_execution(tmp_path):
    registry, worker, args = search_registry(tmp_path)
    valid = tmp_path / "valid.png"
    valid.write_bytes(png_bytes())
    outside = tmp_path.parent / f"{tmp_path.name}-outside.png"
    outside.write_bytes(png_bytes())
    linked = tmp_path / "linked.png"
    try:
        with pytest.raises(ValueError):
            await capabilities.call_capability(registry, **{**args, "capability": "image", "input_images": ["https://example.test/a.png"]})
        with pytest.raises(ValueError):
            await capabilities.call_capability(registry, **{**args, "capability": "image", "input_images": [str(outside)]})
        with pytest.raises(ValueError):
            await capabilities.call_capability(registry, **{**args, "capability": "image", "input_images": ["../secret.png"]})
        with pytest.raises(ValueError):
            await capabilities.call_capability(registry, **{**args, "capability": "search", "input_images": ["valid.png"]})
        try:
            linked.symlink_to(valid)
        except OSError:
            linked = None
        if linked is not None:
            with pytest.raises(ValueError):
                await capabilities.call_capability(registry, **{**args, "capability": "image", "input_images": ["linked.png"]})
        original = registry._validate_reference_images

        def corrupt(task):
            if task.status == TaskStatus.running:
                valid.write_bytes(b"not-an-image")
            return original(task)

        registry._validate_reference_images = corrupt
        accepted = await capabilities.call_capability(
            registry, **{**args, "capability": "image", "input_images": [str(valid)]},
        )
        failed = await finish(registry, accepted)
        assert failed["status"] == "failed"
        assert "unsupported or forged media type" in failed["error"]
        assert registry.tasks[accepted["call_id"]].input_images == ["valid.png"]
        assert worker.calls == 0
        original_bytes = png_bytes()
        valid.write_bytes(original_bytes)
        image = await capabilities.call_capability(registry, **{
            **args, "capability": "image", "request_id": str(uuid.uuid4()),
        })
        completed = await finish(registry, image)
        saved = tmp_path / completed["output_files"][0]["path"]
        saved.write_bytes(png_bytes(b"\xff\x00\x00\xff"))
        before = worker.calls
        with pytest.raises(ValueError, match="recorded metadata"):
            await capabilities.call_capability(registry, **{
                **args, "capability": "image", "request_id": str(uuid.uuid4()),
                "previous_call_id": image["call_id"],
            })
        assert worker.calls == before
        saved.write_bytes(original_bytes)
        queued = tmp_path / "queued.png"
        queued.write_bytes(original_bytes)
        worker.release.clear()
        worker.ready.clear()

        def swap(task):
            if task.status == TaskStatus.running:
                queued.write_bytes(png_bytes(b"\xff\x00\x00\xff"))
            return original(task)

        registry._validate_reference_images = swap
        changed = await capabilities.call_capability(registry, **{
            **args, "capability": "image", "request_id": str(uuid.uuid4()),
            "input_images": ["queued.png"],
        })
        rejected = await finish(registry, changed)
        assert rejected["status"] == "failed"
        assert "recorded metadata" in rejected["error"]
        linked_replay = tmp_path / "replay.png"
        linked_replay.write_bytes(original_bytes)
        retained = await capabilities.call_capability(registry, **{
            **args, "capability": "image", "request_id": str(uuid.uuid4()),
            "input_images": ["replay.png"],
        })
        await finish(registry, retained)
        calls_after_retention = worker.calls
        linked_replay.unlink()
        replacement = tmp_path / "replacement.png"
        replacement.write_bytes(original_bytes)
        try:
            linked_replay.symlink_to(replacement)
        except OSError:
            linked_replay.write_bytes(b"changed")
        replayed = await capabilities.call_capability(registry, **{
            **args, "capability": "image", "request_id": retained["request_id"],
            "input_images": ["replay.png"],
        })
        assert replayed["reused"]
        assert replayed["call_id"] == retained["call_id"]
        assert worker.calls == calls_after_retention
    finally:
        outside.unlink(missing_ok=True)
        await registry.stop()


@pytest.mark.asyncio
async def test_cancelling_current_continuation_keeps_completed_previous(tmp_path):
    registry, worker, args = search_registry(tmp_path)
    try:
        first = await capabilities.call_capability(registry, **args)
        await finish(registry, first)
        worker.release.clear()
        worker.ready.clear()
        second = await capabilities.call_capability(
            registry, **{**args, "request_id": str(uuid.uuid4()), "previous_call_id": first["call_id"]},
        )
        await asyncio.wait_for(worker.ready.wait(), 5)
        cancelled = await capabilities.cancel_capability(registry, second["call_id"])
        assert cancelled["status"] == "cancelled"
        assert registry.tasks[first["call_id"]].status == TaskStatus.completed
    finally:
        await registry.stop()
