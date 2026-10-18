"""Fault regressions using native-shaped events and offline workers only."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from acp.schema import (
    AgentMessageChunk,
    BlobResourceContents,
    EmbeddedResourceContentBlock,
    TextResourceContents,
)

from agent_bridge import native_results
from agent_bridge.acp_results import AcpNativeResults, grok_media_session
from agent_bridge.adapters.acp import _BridgeClient
from agent_bridge.adapters.codex import CodexAdapter
from agent_bridge.codex_image import CodexImageTurn
from agent_bridge.config import AgentConfig, AppConfig
from agent_bridge.models import Session, Task, TurnResult
from agent_bridge.native_results import MAX_RAW_CHARS, bounded_output, load_checkpoint, start_job
from agent_bridge.output_files import verify_output
from agent_bridge.registry import Registry
from agent_bridge.transcript import flush_session, read_events
from fake_codex_image import png

FAKE_IMAGE = Path(__file__).with_name("fake_codex_image.py")
PRIVATE_SENTINEL = "PRIVATE_NATIVE_FAULT_SENTINEL"


def image_config(tmp_path: Path, case: str = "no_image") -> AgentConfig:
    official_home = tmp_path / "official-home"
    official_home.mkdir(exist_ok=True)
    return AgentConfig(
        name="codex", protocol="codex", command=["unused"], stall_timeout_sec=0,
        env={"CODEX_HOME": str(official_home), "BRIDGE_IMAGE_FIXTURE": case},
    )


def use_offline_codex(monkeypatch) -> None:
    monkeypatch.setattr(CodexAdapter, "_base_cmd", lambda self, env=None: [sys.executable, str(FAKE_IMAGE)])


def image_setup(tmp_path: Path, monkeypatch, case: str = "no_image"):
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "bridge"
    adapter = CodexAdapter(image_config(tmp_path, case), home)
    use_offline_codex(monkeypatch)
    session = Session(session_id="session_fault", agent="codex", cwd=str(work))
    task = Task(
        task_id="task_fault", session_id=session.session_id, agent="codex",
        message="Generate an offline fixture", cwd=str(work), operation="image",
    )
    return adapter, session, task, home, work


def journal(home: Path, task: Task) -> dict:
    return json.loads((home / "native-results" / f"{task.task_id}.json").read_text(encoding="utf-8"))


def assert_one_generation(work: Path) -> None:
    assert (work / "submissions.log").read_text(encoding="utf-8").splitlines() == ["submit"]


@pytest.mark.parametrize("operation,with_request_id", [("image", True), ("image", False), (None, True)])
async def test_registry_persists_dispatch_before_provider_call(tmp_path, monkeypatch, operation, with_request_id):
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "bridge"
    registry = Registry.create(home, AppConfig(agents={"codex": image_config(tmp_path)}), runtime_context="coordinator")
    use_offline_codex(monkeypatch)
    await registry.start()
    original_write = registry._write_state
    original_run = CodexAdapter.run_turn
    entered, release = threading.Event(), threading.Event()
    provider_calls = 0
    request_id = str(uuid.uuid4()) if with_request_id else None

    writes = 0

    def gated_write(own):
        nonlocal writes
        writes += 1
        if own["tasks"] and writes == 1:
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test state gate was not released")
        original_write(own)

    async def observed_run(self, session, task):
        nonlocal provider_calls
        provider_calls += 1
        rows = json.loads((home / "state.json").read_text(encoding="utf-8"))["tasks"]
        persisted = next(row for row in rows if row["task_id"] == task.task_id)
        assert persisted["operation"] == operation
        if request_id:
            bindings = json.loads((home / "state.json").read_text(encoding="utf-8"))["requests"]
            binding = next(row for row in bindings if row["request_id"] == request_id)
            assert binding["task_id"] == task.task_id
            assert len(binding["fingerprint"]) == 64
        return await original_run(self, session, task) if operation else TurnResult(text="offline text")

    monkeypatch.setattr(registry, "_write_state", gated_write)
    monkeypatch.setattr(CodexAdapter, "run_turn", observed_run)
    dispatched = asyncio.create_task(registry.dispatch_task(
        "codex", "offline fixture", str(work), operation=operation, request_id=request_id,
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        # Let the scheduled task run while the state writer is deliberately held.
        await asyncio.sleep(0.05)
        assert provider_calls == 0
        assert not (work / "submissions.log").exists()
        assert json.loads((home / "state.json").read_text(encoding="utf-8"))["tasks"] == []
        release.set()
        response = await asyncio.wait_for(dispatched, 10)
        await registry.wait_task(response["task_id"], 10)
        assert provider_calls == 1
        assert (await registry.get_result(response["task_id"]))["status"] == ("failed" if operation else "completed")
        if operation:
            assert_one_generation(work)
    finally:
        release.set()
        await asyncio.gather(dispatched, return_exceptions=True)
        monkeypatch.setattr(registry, "_write_state", original_write)
        await registry.stop()


@pytest.mark.parametrize("operation,with_request_id", [("image", True), ("image", False), (None, True)])
async def test_registry_permanent_state_failure_blocks_only_native_provider(tmp_path, monkeypatch, operation, with_request_id):
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(
        tmp_path / "bridge", AppConfig(agents={"codex": image_config(tmp_path)}), runtime_context="coordinator",
    )
    await registry.start()
    original_write = registry._write_state
    provider_calls = 0

    def fail_write(own):
        raise PermissionError("offline state persistence failure")

    async def forbidden_run(self, session, task):
        nonlocal provider_calls
        provider_calls += 1
        return TurnResult(text="must never run")

    monkeypatch.setattr(registry, "_write_state", fail_write)
    monkeypatch.setattr(CodexAdapter, "run_turn", forbidden_run)
    try:
        response = await registry.dispatch_task(
            "codex", "offline fixture", str(work), operation=operation,
            request_id=str(uuid.uuid4()) if with_request_id else None,
        )
        assert response["persisted"] is False
        await registry.wait_task(response["task_id"], 10)
        assert (await registry.get_result(response["task_id"]))["status"] == ("failed" if operation else "completed")
        assert provider_calls == (0 if operation else 1)
        assert not (work / "submissions.log").exists()
    finally:
        monkeypatch.setattr(registry, "_write_state", original_write)
        await registry.stop()


async def test_terminal_checkpoint_permission_retry_only_retries_metadata(tmp_path, monkeypatch):
    adapter, session, task, home, work = image_setup(tmp_path, monkeypatch)
    original_write = native_results.atomic_write_json
    terminal_attempts = 0

    def flaky_write(path, row):
        nonlocal terminal_attempts
        if row.get("job", {}).get("status") == "failed":
            terminal_attempts += 1
            if terminal_attempts == 1:
                raise PermissionError(PRIVATE_SENTINEL)
        original_write(path, row)

    monkeypatch.setattr(native_results, "atomic_write_json", flaky_write)
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "error"
    assert result.job.status == "failed"
    assert terminal_attempts == 2
    assert task.checkpoint_error is False
    stored = journal(home, task)
    assert stored["job"] == result.job.model_dump(mode="json")
    assert stored["output_files"] == [output.model_dump(mode="json") for output in result.output_files]
    assert stored["sources"] == [source.model_dump(mode="json") for source in result.sources]
    assert_one_generation(work)
    assert PRIVATE_SENTINEL not in result.model_dump_json()


async def test_persistent_terminal_checkpoint_failure_is_safe_uncertain_and_not_replayed(tmp_path, monkeypatch):
    adapter, session, task, home, work = image_setup(tmp_path, monkeypatch)
    original_write = native_results.atomic_write_json
    failure_started = False
    failures = 0

    def failed_terminal_write(path, row):
        nonlocal failure_started, failures
        failure_started |= row.get("job", {}).get("status") == "failed"
        if failure_started:
            failures += 1
            raise PermissionError(f"{PRIVATE_SENTINEL}: {path}")
        original_write(path, row)

    monkeypatch.setattr(native_results, "atomic_write_json", failed_terminal_write)
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "error" and result.error
    assert failures == 3
    assert task.checkpoint_error is True
    assert task.job.status == result.job.status == "uncertain"
    assert result.job.remote_cancel_supported is False
    encoded = result.model_dump_json()
    assert PRIVATE_SENTINEL not in encoded
    assert str(home) not in encoded
    # A stale running journal must not clear the failed-write flag or outcome.
    assert journal(home, task)["job"]["status"] == "running"
    load_checkpoint(home, task, interrupted=True)
    assert task.checkpoint_error is True and task.job.status == "uncertain"
    assert_one_generation(work)


def grok_setup(tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "bridge"
    native_home = tmp_path / "grok"
    task = Task(task_id="task_fault", session_id="session_fault", agent="grok", message="fixture", cwd=str(work))
    folder = grok_media_session(work, "native_fault", native_home) / "images"
    folder.mkdir(parents=True)
    evidence = AcpNativeResults(task, home, "native_fault", {"GROK_HOME": str(native_home)})
    return evidence, task, home, folder


def grok_start(evidence: AcpNativeResults, call_id: str) -> None:
    evidence.consume({"toolCallId": call_id, "toolName": "image_gen", "status": "in_progress"})


def grok_complete(evidence: AcpNativeResults, folder: Path, call_id: str) -> None:
    path = folder / f"{call_id}.png"
    path.write_bytes(png())
    evidence.consume({
        "toolCallId": call_id, "status": "completed",
        "rawOutput": {"type": "ImageGen", "path": str(path), "filename": path.name, "session_folder": "images"},
    })


def test_grok_completed_file_is_durable_while_another_call_runs(tmp_path):
    evidence, task, home, folder = grok_setup(tmp_path)
    grok_start(evidence, "A")
    grok_start(evidence, "B")
    grok_complete(evidence, folder, "A")
    assert task.job.status == "running"
    stored = journal(home, task)
    assert stored["job"]["status"] == "running"
    assert stored["output_files"] == [output.model_dump(mode="json") for output in task.output_files]
    assert len(stored["output_files"]) == 1
    assert stored["output_files"][0]["tool_call_id"] == "A"
    recovered = task.model_copy(deep=True, update={"output_files": [], "job": None})
    load_checkpoint(home, recovered, interrupted=True)
    assert recovered.job.status == "uncertain"
    assert len(recovered.output_files) == 1
    verify_output(recovered.output_files[0], Path(task.cwd), task.task_id)


def test_grok_failure_is_not_overwritten_by_later_success(tmp_path):
    evidence, task, home, folder = grok_setup(tmp_path)
    grok_start(evidence, "A")
    evidence.consume({"toolCallId": "A", "status": "failed"})
    grok_start(evidence, "B")
    assert task.job.status == "failed"
    grok_complete(evidence, folder, "B")
    evidence.end()
    assert task.job.status == journal(home, task)["job"]["status"] == "failed"
    assert [output.tool_call_id for output in task.output_files] == ["B"]


def test_grok_unfinished_sibling_becomes_uncertain_at_end(tmp_path):
    evidence, task, home, folder = grok_setup(tmp_path)
    grok_start(evidence, "A")
    grok_start(evidence, "B")
    grok_complete(evidence, folder, "A")
    assert task.job.status == "running"
    evidence.end()
    assert task.job.status == journal(home, task)["job"]["status"] == "uncertain"
    assert [output.tool_call_id for output in task.output_files] == ["A"]


def test_grok_all_successful_calls_complete_aggregate(tmp_path):
    evidence, task, home, folder = grok_setup(tmp_path)
    grok_start(evidence, "A")
    grok_start(evidence, "B")
    grok_complete(evidence, folder, "A")
    grok_complete(evidence, folder, "B")
    evidence.end()
    assert task.job.status == journal(home, task)["job"]["status"] == "completed"
    assert [output.tool_call_id for output in task.output_files] == ["A", "B"]


@pytest.mark.parametrize("statuses", [("failed", "completed"), ("completed", "failed"), ("completed", "completed")])
def test_codex_image_job_aggregates_native_item_outcomes(tmp_path, monkeypatch, statuses):
    adapter, session, task, home, _work = image_setup(tmp_path, monkeypatch)
    turn = CodexImageTurn(home, session, task, adapter._worker_env())
    turn.thread_id, turn.turn_id = "thread_fault", "turn_fault"
    turn.image_root = tmp_path / "official-home" / "generated_images" / turn.thread_id
    turn.image_root.mkdir(parents=True)
    start_job(task, home, provider="codex", native_id=turn.thread_id)
    for call_id, status in zip(("A", "B"), statuses, strict=True):
        path = turn.image_root / f"{call_id}.png"
        if status == "completed":
            path.write_bytes(png())
        turn.consume({
            "method": "item/completed", "params": {
                "threadId": turn.thread_id, "turnId": turn.turn_id,
                "item": {"type": "imageGeneration", "id": call_id, "status": status, "savedPath": str(path)},
            },
        })
    expected = "failed" if "failed" in statuses else "completed"
    assert task.job.status == journal(home, task)["job"]["status"] == expected
    assert len(task.output_files) == statuses.count("completed")


async def test_registry_rejects_file_corrupted_after_adapter_delivery(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(
        tmp_path / "bridge", AppConfig(agents={"codex": image_config(tmp_path, "success")}),
        runtime_context="coordinator",
    )
    use_offline_codex(monkeypatch)
    original_run = CodexAdapter.run_turn

    async def corrupt_delivered_file(self, session, task):
        result = await original_run(self, session, task)
        assert result.job.status == "completed" and len(result.output_files) == 1
        (Path(task.cwd) / result.output_files[0].path).write_bytes(b"corrupted after native delivery")
        return result

    monkeypatch.setattr(CodexAdapter, "run_turn", corrupt_delivered_file)
    await registry.start()
    try:
        response = await registry.dispatch_task("codex", "offline image", str(work), operation="image")
        await registry.wait_task(response["task_id"], 10)
        result = (await registry.get_result(response["task_id"]))
        assert result["status"] == "failed"
        assert result["job"]["status"] == "failed"
        assert result.get("output_files", []) == []
        assert result["error"]
        assert_one_generation(work)
    finally:
        await registry.stop()


@pytest.mark.parametrize("binary", [True, False], ids=["blob-is-filtered", "text-is-preserved"])
async def test_acp_embedded_resource_is_safe_in_result_and_transcript(tmp_path, monkeypatch, binary):
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "bridge"
    registry = Registry.create(home, AppConfig(agents={"codex": image_config(tmp_path)}), runtime_context="coordinator")
    text = "Ordinary embedded resource text is retained."
    resource = (
        BlobResourceContents(blob=PRIVATE_SENTINEL, mimeType="image/png", uri="fixture://image")
        if binary else TextResourceContents(text=text, mimeType="text/plain", uri="fixture://text")
    )
    content = EmbeddedResourceContentBlock(type="resource", resource=resource)

    async def resource_turn(self, session, task):
        client = _BridgeClient(session.session_id, self.home)
        await client.session_update("native_fault", AgentMessageChunk(sessionUpdate="agent_message_chunk", content=content))
        assert PRIVATE_SENTINEL not in "".join(client.text_parts)
        return TurnResult(text="".join(client.text_parts))

    monkeypatch.setattr(CodexAdapter, "run_turn", resource_turn)
    await registry.start()
    try:
        response = await registry.dispatch_task("codex", "offline resource", str(work))
        await registry.wait_task(response["task_id"], 10)
        result = (await registry.get_result(response["task_id"]))
        assert result["status"] == "completed"
        await registry.flush_state()
        flush_session(response["session_id"], home)
        transcript = json.dumps(read_events(response["session_id"], home))
        assert PRIVATE_SENTINEL not in json.dumps(result)
        assert PRIVATE_SENTINEL not in transcript
        assert PRIVATE_SENTINEL not in "".join(
            path.read_text(encoding="utf-8") for path in home.rglob("*.json*") if path.suffix in {".json", ".jsonl"}
        )
        if not binary:
            assert text in json.dumps(result) and text in transcript
    finally:
        await registry.stop()


def oversized_output(case: str):
    if case == "large_value":
        return {"body": "x" * (MAX_RAW_CHARS + 1)}
    if case == "large_key":
        return {"x" * (MAX_RAW_CHARS + 1): "value"}
    if case == "nested":
        value = {"body": "small"}
        for _ in range(80):
            value = {"nested": value}
        return value
    if case == "array":
        return {"items": [0] * 10000}
    raise AssertionError(case)


@pytest.mark.parametrize("case", ["large_value", "large_key", "nested", "array"])
@pytest.mark.parametrize("encoded", [False, True], ids=["dict", "json-string"])
def test_bounded_output_rejects_oversized_parsed_and_string_shapes(case, encoded):
    value = oversized_output(case)
    assert bounded_output(json.dumps(value) if encoded else value) is None


def test_bounded_output_accepts_small_native_payload_and_rejects_top_level_array():
    value = {"code": 0, "results": [{"url": "https://example.com", "title": "fixture"}]}
    assert bounded_output(value) == bounded_output(json.dumps(value)) == value
    assert bounded_output([]) is None
    assert bounded_output("[]") is None


@pytest.mark.parametrize("envelope", ["params", "result"])
async def test_codex_read_strips_nested_image_bytes_before_pending(tmp_path, monkeypatch, envelope):
    adapter, session, task, home, _work = image_setup(tmp_path, monkeypatch)
    turn = CodexImageTurn(home, session, task, adapter._worker_env())
    image_item = {
        "type": "imageGeneration", "id": "A", "status": "completed", "savedPath": "fixture.png",
        "result": PRIVATE_SENTINEL, "revisedPrompt": PRIVATE_SENTINEL + "_PROMPT",
    }
    text_item = {"type": "agentMessage", "id": "text", "text": "ordinary text", "result": "non-media-result"}
    message = {envelope: {"turn": {"id": "turn_fault", "items": [image_item, text_item]}}}
    if envelope == "params":
        message["method"] = "turn/completed"
    else:
        message["id"] = 1
    stream = asyncio.StreamReader()
    stream.feed_data((json.dumps(message) + "\n").encode("utf-8"))
    stream.feed_eof()
    turn.proc = SimpleNamespace(stdout=stream)
    received = await turn._read()
    items = received[envelope]["turn"]["items"]
    assert "result" not in items[0] and "revisedPrompt" not in items[0]
    assert items[0]["savedPath"] == "fixture.png"
    assert items[1] == text_item
    assert PRIVATE_SENTINEL not in json.dumps(received)
    if envelope == "params":
        await turn._queue_or_reject(received)
        assert turn.pending == [received]
        assert PRIVATE_SENTINEL not in json.dumps(turn.pending)


@pytest.mark.parametrize("second_outcome", ["missing_file", "started_disconnect"])
async def test_codex_second_image_fault_retains_first_delivery(tmp_path, monkeypatch, second_outcome):
    adapter, session, task, home, work = image_setup(tmp_path, monkeypatch, "success")
    original_read = CodexImageTurn._read
    first_received = False
    injected = False
    disconnected = False
    first_output = None

    async def read_with_second_image(self):
        nonlocal first_received, injected, disconnected, first_output
        if first_received and not injected:
            assert len(task.output_files) == 1
            first_output = task.output_files[0].model_dump(mode="json")
            assert journal(home, task)["output_files"] == [first_output]
            assert self.image_root is not None
            missing_path = self.image_root / "call_second.png"
            assert not missing_path.exists()
            injected = True
            return {
                "method": "item/completed" if second_outcome == "missing_file" else "item/started",
                "params": {
                    "threadId": self.thread_id, "turnId": self.turn_id,
                    "item": {
                        "type": "imageGeneration", "id": "call_second",
                        "status": "completed" if second_outcome == "missing_file" else "in_progress",
                        "savedPath": str(missing_path),
                    },
                },
            }
        if injected and second_outcome == "started_disconnect":
            assert task.job.status == journal(home, task)["job"]["status"] == "running"
            disconnected = True
            raise ConnectionError("offline disconnect after second native image started")
        value = await original_read(self)
        if value.get("method") == "item/completed":
            first_received = True
        return value

    monkeypatch.setattr(CodexImageTurn, "_read", read_with_second_image)
    result = await adapter.run_turn(session, task)
    expected = "failed" if second_outcome == "missing_file" else "uncertain"
    assert first_received and injected
    assert disconnected is (second_outcome == "started_disconnect")
    assert result.stop_reason == "error" and result.error
    assert task.job.status == result.job.status == expected
    assert result.job.remote_cancel_supported is False
    assert task.checkpoint_error is False
    assert [output.model_dump(mode="json") for output in result.output_files] == [first_output]
    verify_output(result.output_files[0], work, task.task_id)
    stored = journal(home, task)
    assert stored["job"] == result.job.model_dump(mode="json")
    assert stored["output_files"] == [first_output]
    recovered = task.model_copy(deep=True, update={"output_files": [], "job": None})
    load_checkpoint(home, recovered, interrupted=True)
    assert recovered.job == result.job and recovered.output_files == result.output_files
    assert_one_generation(work)


async def test_codex_image_started_before_turn_response_is_queued_until_ids_bound(tmp_path, monkeypatch):
    adapter, session, task, home, work = image_setup(tmp_path, monkeypatch, "success")
    original_read = CodexImageTurn._read
    original_consume = CodexImageTurn.consume
    held_response = None
    injected = False
    started_consumed = False

    async def read_with_early_start(self):
        nonlocal held_response, injected
        if held_response is not None:
            assert self.turn_id is None
            assert len(self.pending) == 1 and self.pending[0]["method"] == "item/started"
            assert task.output_files == []
            assert task.job.native_turn_id is None
            value, held_response = held_response, None
            return value
        value = await original_read(self)
        result = value.get("result")
        turn = result.get("turn") if isinstance(result, dict) else None
        if isinstance(turn, dict) and not injected:
            assert self.thread_id == "thread_fixture" and self.turn_id is None
            held_response = value
            injected = True
            return {
                "method": "item/started", "params": {
                    "threadId": self.thread_id, "turnId": turn["id"],
                    "item": {"type": "imageGeneration", "id": "call_fixture", "status": "in_progress"},
                },
            }
        return value

    def consume_after_ids(self, value):
        nonlocal started_consumed
        if value.get("method") == "item/started":
            assert self.thread_id == value["params"]["threadId"]
            assert self.turn_id == value["params"]["turnId"] == "turn_fixture"
            assert task.job.native_turn_id == self.turn_id
            assert task.output_files == []
            started_consumed = True
        return original_consume(self, value)

    monkeypatch.setattr(CodexImageTurn, "_read", read_with_early_start)
    monkeypatch.setattr(CodexImageTurn, "consume", consume_after_ids)
    result = await adapter.run_turn(session, task)
    assert injected and started_consumed and held_response is None
    assert result.stop_reason == "end_turn" and result.error is None
    assert result.job.status == task.job.status == "completed"
    assert len(result.output_files) == 1
    verify_output(result.output_files[0], work, task.task_id)
    assert journal(home, task)["job"] == result.job.model_dump(mode="json")
    assert_one_generation(work)


def test_minimax_full_unicode_source_checkpoint_roundtrips(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "bridge"
    task = Task(
        task_id="task_unicode", session_id="session_unicode", agent="minimax",
        message="offline raw service search results", cwd=str(work),
    )
    evidence = AcpNativeResults(task, home, "native_unicode", {})
    title, snippet = "\U0001f642" * 500, "\U0001f642" * 2000
    assert len(title.encode("utf-8")) == 2000
    assert len(snippet.encode("utf-8")) == 8000
    for index in range(64):
        body = {
            "code": 0, "results": [{
                "url": f"https://example.com/search/{index}", "title": title, "snippet": snippet,
            }],
        }
        assert len(json.dumps(body).encode("utf-8")) < MAX_RAW_CHARS
        assert bounded_output(body) == body
        evidence.consume({
            "toolCallId": f"search_{index}", "name": "web_search", "status": "completed", "rawOutput": body,
        })
        assert len(task.sources) == index + 1
    expected = [source.model_dump(mode="json") for source in task.sources]
    checkpoint_file = home / "native-results" / f"{task.task_id}.json"
    assert 512 * 1024 < checkpoint_file.stat().st_size <= native_results.MAX_CHECKPOINT_BYTES
    assert task.checkpoint_error is False
    assert journal(home, task)["sources"] == expected
    task.sources.clear()
    load_checkpoint(home, task)
    assert len(task.sources) == 64
    assert [source.model_dump(mode="json") for source in task.sources] == expected
    assert [source.tool_call_id for source in task.sources] == [f"search_{index}" for index in range(64)]
    assert all(source.title == title and source.snippet == snippet for source in task.sources)


async def test_codex_started_image_without_output_is_uncertain_when_turn_completes(tmp_path, monkeypatch):
    adapter, session, task, home, work = image_setup(tmp_path, monkeypatch, "no_image")
    original_read = CodexImageTurn._read
    held_completion = None
    injected = False

    async def read_with_unfinished_image(self):
        nonlocal held_completion, injected
        if held_completion is not None:
            assert task.output_files == []
            assert task.job.status == journal(home, task)["job"]["status"] == "running"
            value, held_completion = held_completion, None
            return value
        value = await original_read(self)
        if value.get("method") == "turn/completed" and not injected:
            assert self.thread_id and self.turn_id
            assert task.output_files == []
            held_completion = value
            injected = True
            return {
                "method": "item/started", "params": {
                    "threadId": self.thread_id, "turnId": self.turn_id,
                    "item": {"type": "imageGeneration", "id": "call_unfinished", "status": "in_progress"},
                },
            }
        return value

    monkeypatch.setattr(CodexImageTurn, "_read", read_with_unfinished_image)
    result = await adapter.run_turn(session, task)
    assert injected and held_completion is None
    assert result.stop_reason == "error" and result.error
    assert task.job.status == result.job.status == "uncertain"
    assert result.job.remote_cancel_supported is False
    assert task.output_files == result.output_files == []
    assert task.checkpoint_error is False
    stored = journal(home, task)
    assert stored["job"] == result.job.model_dump(mode="json")
    assert stored["output_files"] == []
    assert_one_generation(work)
