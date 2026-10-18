"""Anonymous reconstructed protocol fixtures, not account acceptance evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from acp.connection import StreamDirection, StreamEvent
from acp.schema import ToolCallProgress, ToolCallStart

from agent_bridge.acp_results import AcpNativeResults, grok_media_session
from agent_bridge.adapters.acp import _BridgeClient
from agent_bridge.adapters.codex import CodexAdapter
from agent_bridge.codex_exec import CodexTurnState, apply_codex_event, build_codex_exec_argv
from agent_bridge.models import SourceReference, Task, TaskStatus, iso
from agent_bridge.native_results import MAX_RAW_CHARS, checkpoint, load_checkpoint, safe_url, start_job
from agent_bridge.persist import atomic_write_json
from agent_bridge.registry import Registry
from agent_bridge.transcript import read_events


def make_task(tmp_path: Path, provider: str = "grok") -> Task:
    work = tmp_path / "workspace"
    work.mkdir(exist_ok=True)
    return Task(
        task_id="task_native", session_id="session_native", agent=provider,
        message="fixture", cwd=str(work),
    )


def test_codex_native_results_and_query_only_compatibility():
    state = CodexTurnState()
    apply_codex_event(state, {
        "type": "item.completed",
        "item": {"id": "call_search", "type": "web_search", "query": "fixture", "results": [
            {"type": "text_result", "url": "https://example.com/doc", "title": "Official result"},
            {"type": "unknown_result", "url": "https://example.com/unknown"},
        ]},
    })
    apply_codex_event(state, {"type": "item.completed", "item": {"id": "call_query", "type": "web_search", "query": "https://example.com/guess"}})
    apply_codex_event(state, {"type": "item.completed", "item": {"type": "agent_message", "text": "https://example.com/prose"}})
    assert [s.url for s in state.sources] == ["https://example.com/doc"]
    assert state.sources[0].tool_call_id == "call_search"
    assert state.text == "https://example.com/prose"


def test_codex_live_search_override_is_explicit_and_task_effort_wins():
    normal = build_codex_exec_argv(["codex"], cwd="/fixture")
    assert 'web_search="live"' not in normal
    explicit = build_codex_exec_argv(
        ["codex"], cwd="/fixture", effort="high",
        config_overrides=['web_search="live"', 'model_reasoning_effort="low"'],
    )
    assert explicit.index('model_reasoning_effort="low"') < explicit.index('model_reasoning_effort="high"')
    assert 'web_search="live"' in explicit


@pytest.mark.parametrize("url", [
    "file:///credential", "https://user:secret@example.com/a", "https://example.com/?token=x",
    "https://example.com/?X-Amz-Signature=x", "https://127.0.0.1/a", "https://localhost/a",
    "data:image/png;base64,fixture", "https://example.com/\nsecret",
])
def test_source_urls_do_not_export_credentials_or_signed_links(url):
    assert safe_url(url) is None


@pytest.mark.parametrize("provider,identity,output", [
    ("grok", "toolName", {"type": "WebSearch", "citations": [{"url": "https://example.com/g", "title": "Grok"}]}),
    ("minimax", "name", {"code": 0, "results": [{"link": "https://example.com/m", "title": "MiniMax", "snippet": "Fixture"}]}),
])
def test_acp_search_requires_tool_identity_and_success(tmp_path, provider, identity, output):
    task = make_task(tmp_path, provider)
    evidence = AcpNativeResults(task, tmp_path / "bridge", "native_fixture", {"GROK_HOME": str(tmp_path / "grok")})
    # Free titles do not identify native tools.
    evidence.consume({"toolCallId": "title_only", "title": "web_search", "status": "completed", "rawOutput": output})
    assert task.sources == []
    evidence.consume({"toolCallId": "failed", identity: "web_search", "status": "failed", "rawOutput": output})
    assert task.sources == []
    evidence.consume({"toolCallId": "ok", identity: "web_search", "status": "in_progress"})
    evidence.consume({"toolCallId": "ok", "status": "completed", "rawOutput": output})
    evidence.consume({"toolCallId": "ok", "status": "completed", "rawOutput": output})
    assert len(task.sources) == 1
    assert task.sources[0].tool_call_id == "ok"


def test_minimax_content_body_is_bounded_and_native_identity_is_required(tmp_path):
    task = make_task(tmp_path, "minimax")
    evidence = AcpNativeResults(task, tmp_path / "bridge", "native_fixture", {})
    body = {"code": 0, "results": [{"link": "https://example.com/doc", "title": "Fixture"}]}
    evidence.consume({
        "toolCallId": "large", "name": "web_search", "status": "completed",
        "rawOutput": "x" * (MAX_RAW_CHARS + 1),
    })
    assert task.sources == []
    evidence.consume({
        "toolCallId": "body", "name": "web_search", "status": "completed",
        "rawOutput": {"content": [{"type": "text", "text": json.dumps(body)}]},
    })
    assert task.sources[0].url == "https://example.com/doc"


async def test_python_acp_preserves_vendor_identity_and_logs_bounded_evidence(tmp_path):
    task = make_task(tmp_path)
    home = tmp_path / "bridge"
    client = _BridgeClient(task.session_id, home)
    client.native = AcpNativeResults(task, home, "native_fixture", {"GROK_HOME": str(tmp_path / "grok")})
    client.observe_native_message(StreamEvent(
        StreamDirection.INCOMING,
        {"method": "session/update", "params": {"sessionId": "native_fixture", "update": {
            "sessionUpdate": "tool_call", "toolCallId": "call_search", "toolName": "web_search",
        }}},
    ))
    await client.session_update("native_fixture", ToolCallStart(
        sessionUpdate="tool_call",
        toolCallId="call_search", title="Search free title", kind="search",
        toolName="web_search", status="in_progress", rawInput={"query": "fixture"},
    ))
    await client.session_update("native_fixture", ToolCallProgress(
        sessionUpdate="tool_call_update",
        toolCallId="call_search", status="completed",
        rawOutput={"type": "WebSearch", "citations": [{"title": "Fixture", "url": "https://example.com"}], "ignored": "PRIVATE_SENTINEL" * 100},
    ))
    assert len(task.sources) == 1
    encoded = json.dumps(read_events(task.session_id, home))
    assert "PRIVATE_SENTINEL" not in encoded
    assert "source_count" in encoded


@pytest.mark.parametrize("identity", ["meta", "variant"])
def test_grok_live_protocol_identity_and_completion(tmp_path, identity):
    task = make_task(tmp_path)
    task.capability = "search"
    evidence = AcpNativeResults(task, tmp_path / "bridge", None, {})
    update = {"toolCallId": "native_search", "title": "not a tool identity"}
    if identity == "meta":
        update["_meta"] = {"x.ai/tool": {"version": 1, "name": "web_search", "namespace": "grok_build"}}
    else:
        update["rawInput"] = {"variant": "WebSearch", "query": "anonymous fixture"}
    evidence.consume(update)
    evidence.consume({
        "toolCallId": "native_search", "status": "completed",
        "rawOutput": {"type": "WebSearch", "citations": ["https://example.com/native"]},
    })
    assert task.native_calls == {"native_search": "web_search"}
    assert task.sources[0].tool_call_id == "native_search"
    assert task.sources[0].url == "https://example.com/native"


def test_grok_title_and_malformed_variant_are_not_identity(tmp_path):
    evidence = AcpNativeResults(make_task(tmp_path), tmp_path / "bridge", None, {})
    assert evidence.tool_name({"toolCallId": "call", "title": "web_search"}) is None
    assert evidence.tool_name({"toolCallId": "call", "rawInput": {"variant": []}}) is None


def test_grok_variant_fallback_does_not_identify_minimax_tools(tmp_path):
    task = make_task(tmp_path, "minimax")
    task.capability = "search"
    evidence = AcpNativeResults(task, tmp_path / "bridge", None, {})
    evidence.consume({
        "toolCallId": "unidentified", "rawInput": {"variant": "WebSearch"},
        "status": "completed", "rawOutput": {"results": [{"url": "https://example.com"}]},
    })
    assert task.native_calls == {}
    assert task.sources == []


def test_grok_only_delivers_named_native_output_not_scanned_files(tmp_path):
    task = make_task(tmp_path)
    native_home = tmp_path / "grok"
    folder = grok_media_session(Path(task.cwd), "native_fixture", native_home)
    images = folder / "images"
    images.mkdir(parents=True)
    old = images / "old.jpg"
    # Only file header fixtures are used; no account media is represented.
    old.write_bytes(b"\xff\xd8\xfffixture-jpeg\xff\xd9")
    evidence = AcpNativeResults(task, tmp_path / "bridge", "native_fixture", {"GROK_HOME": str(native_home)})
    new = images / "new.jpg"
    new.write_bytes(old.read_bytes())
    update = {
        "toolCallId": "image_call", "toolName": "image_gen", "status": "completed",
        "rawOutput": {"type": "ImageGen", "path": str(new), "filename": new.name, "session_folder": "images"},
    }
    assert task.output_files == []
    evidence.consume(update)
    evidence.consume(update)
    assert len(task.output_files) == 1
    assert task.job.status == "completed"
    assert task.job.task_id is None
    assert task.job.native_session_id == "native_fixture"


@pytest.mark.parametrize("case", ["old", "other_session", "filename", "folder", "fake", "copy_failed"])
def test_grok_rejects_unsafe_or_unverified_output(tmp_path, monkeypatch, case):
    task = make_task(tmp_path)
    native_home = tmp_path / "grok"
    folder = grok_media_session(Path(task.cwd), "native_fixture", native_home)
    images = folder / "images"
    images.mkdir(parents=True)
    path = images / "output.jpg"
    if case == "old":
        path.write_bytes(b"\xff\xd8\xfffixture-jpeg\xff\xd9")
    evidence = AcpNativeResults(task, tmp_path / "bridge", "native_fixture", {"GROK_HOME": str(native_home)})
    path.write_bytes(b"not-jpeg" if case == "fake" else b"\xff\xd8\xfffixture-jpeg\xff\xd9")
    if case == "copy_failed":
        def fail_copy(*args, **kwargs):
            raise OSError("fixture copy failure")
        monkeypatch.setattr("agent_bridge.acp_results.deliver_file", fail_copy)
    evidence.consume({
        "toolCallId": "image_call", "toolName": "image_gen", "status": "completed",
        "rawOutput": {
            "type": "ImageGen",
            "path": str(images.parent.parent / "other" / "images" / path.name) if case == "other_session" else str(path),
            "filename": "mismatch.jpg" if case == "filename" else path.name,
            "session_folder": "downloads" if case == "folder" else "images",
        },
    })
    assert task.output_files == []
    assert task.job.status == "failed"
    evidence.end()
    assert task.job.status == "failed"


def test_unfinished_acp_media_and_restart_never_replay_generation(tmp_path):
    task = make_task(tmp_path)
    home = tmp_path / "bridge"
    evidence = AcpNativeResults(task, home, "native_fixture", {"GROK_HOME": str(tmp_path / "grok")})
    evidence.consume({"toolCallId": "image_call", "toolName": "image_gen", "status": "in_progress"})
    assert task.job.status == "running"
    evidence.end()
    load_checkpoint(home, task, interrupted=True)
    assert task.job.status == "uncertain"
    assert task.output_files == []


async def test_registry_restores_job_and_sources_without_dispatch(bridge_home, tmp_path, monkeypatch):
    task = make_task(tmp_path, "codex")
    task.status = TaskStatus.running
    task.operation = "image"
    task.started_at = iso()
    task.sources = [SourceReference(title="Fixture", url="https://example.com", tool_call_id="call_search")]
    start_job(task, bridge_home, provider="codex", native_id="thread_fixture")
    atomic_write_json(bridge_home / "state.json", {"tasks": [task.model_dump(mode="json")], "sessions": []})
    registry = Registry.create(bridge_home)
    async def forbidden(*args, **kwargs):
        raise AssertionError("restart must not submit to a provider")
    monkeypatch.setattr(registry, "dispatch_task", forbidden)
    await registry.start()
    try:
        result = (await registry.get_result(task.task_id))
        assert result["status"] == "failed"
        assert result["job"]["status"] == "uncertain"
        assert result["sources"][0]["tool_call_id"] == "call_search"
    finally:
        await registry.stop()


async def test_plain_results_omit_optional_native_fields_and_image_scope_is_validated(bridge_home, tmp_path):
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        plain = await registry.dispatch_task("fake", "fixture text", str(tmp_path))
        await registry.wait_task(plain["task_id"], 5)
        result = (await registry.get_result(plain["task_id"]))
        assert "sources" not in result and "output_files" not in result and "job" not in result
        with pytest.raises(ValueError, match="only by Codex"):
            await registry.dispatch_task("fake", "fixture", str(tmp_path), operation="image")
        with pytest.raises(ValueError, match="fresh session"):
            await registry.dispatch_task("codex", "fixture", str(tmp_path), session_id=plain["session_id"], operation="image")
    finally:
        await registry.stop()


def test_checkpoints_exclude_raw_media(tmp_path):
    task = make_task(tmp_path)
    home = tmp_path / "bridge"
    start_job(task, home, provider="grok", native_id="fixture")
    checkpoint(home, task)
    stored = (home / "native-results" / "task_native.json").read_text()
    assert set(json.loads(stored)) == {"task_id", "sources", "output_files", "job", "native_calls"}
    assert "rawOutput" not in stored


async def test_image_request_retry_after_restart_reuses_uncertain_task(bridge_home, tmp_path, monkeypatch):
    submissions = 0
    async def disconnected_image(self, session, task):
        nonlocal submissions
        submissions += 1
        start_job(task, self.home, provider="codex", native_id="thread_fixture")
        raise ConnectionError("fixture disconnect")
    monkeypatch.setattr(CodexAdapter, "run_turn", disconnected_image)
    request_id = "b2d10cda-c104-45f7-a239-e88c581ac464"
    registry = Registry.create(bridge_home)
    await registry.start()
    first = await registry.dispatch_task(
        "codex", "fixture image", str(tmp_path), operation="image", request_id=request_id,
    )
    await registry.wait_task(first["task_id"], 5)
    assert (await registry.get_result(first["task_id"]))["job"]["status"] == "uncertain"
    await registry.stop()
    restored = Registry.create(bridge_home)
    await restored.start()
    try:
        retry = await restored.dispatch_task(
            "codex", "fixture image", str(tmp_path), operation="image", request_id=request_id,
        )
        assert retry["reused"] is True and retry["task_id"] == first["task_id"]
        assert submissions == 1
        with pytest.raises(ValueError, match="different dispatch"):
            await restored.dispatch_task(
                "codex", "different image", str(tmp_path), operation="image", request_id=request_id,
            )
    finally:
        await restored.stop()
