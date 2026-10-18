"""Upstream contract regressions for the native capability integration."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from agent_bridge import capabilities
from agent_bridge.acp_results import AcpNativeResults
from agent_bridge.adapters.acp import AcpAdapter, AcpError, _Live
from agent_bridge.adapters.codex import CodexAdapter
from agent_bridge.codex_image import CodexImageTurn
from agent_bridge.config import AgentConfig, EnvConfig
from agent_bridge.models import Session, Task, TaskStatus, TurnResult
from agent_bridge.persist import read_json_strict
from agent_bridge.registry import Registry, _request_fingerprint
from test_capabilities import finish, search_registry


@pytest.mark.parametrize("native_id", [None, "native-missing"])
async def test_strict_resume_does_not_create_new_session(tmp_path, monkeypatch, native_id):
    adapter = AcpAdapter(AgentConfig(name="grok", protocol="acp", command=["unused"]), tmp_path / "home")
    session = Session(session_id="sess_resume", agent="grok", cwd=str(tmp_path), native_session_id=native_id)
    spawned = False

    async def fake_spawn(session):
        nonlocal spawned
        spawned = True
        live = _Live()
        live.conn = object()
        adapter._live[session.session_id] = live
        return live

    async def fail_load(*args, **kwargs):
        raise RuntimeError("resume rejected")

    async def forbidden_new(*args, **kwargs):
        pytest.fail("native continuation must not create a replacement session")

    monkeypatch.setattr(adapter, "_spawn", fake_spawn)
    monkeypatch.setattr(adapter, "_call_load_session", fail_load)
    monkeypatch.setattr(adapter, "_call_new_session", forbidden_new)
    monkeypatch.setattr(adapter, "can_revive", lambda: True)
    task = Task(
        task_id="task_resume", session_id=session.session_id, agent="grok", cwd=str(tmp_path),
        message="continue", capability="search", previous_call_id="task_previous",
    )
    with pytest.raises(AcpError, match=r"resume rejected|cannot be resumed") as caught:
        await adapter.run_turn(session, task)
    assert caught.value.failure.operation == ("session/load" if native_id else "process_start")
    assert caught.value.failure.prompt_may_have_been_sent is False
    assert native_id is not None or not spawned
    assert session.native_session_id == native_id


async def test_native_finalization_failure_preserves_structured_transport_error(tmp_path, monkeypatch):
    adapter = AcpAdapter(AgentConfig(name="minimax", protocol="acp", command=["unused"]), tmp_path / "home")
    session = Session(session_id="sess_fault", agent="minimax", cwd=str(tmp_path), native_session_id="native_fault")
    live = _Live()

    class Connection:
        async def prompt(self, **kwargs):
            raise ConnectionError("transport disconnected")

    from agent_bridge.adapters.acp import _BridgeClient

    live.conn = Connection()
    live.client = _BridgeClient(session.session_id, adapter.home)
    adapter._live[session.session_id] = live

    async def ensure(*args, **kwargs):
        pass

    def fail_end(self):
        raise OSError("native checkpoint unavailable")

    monkeypatch.setattr(adapter, "ensure_session", ensure)
    monkeypatch.setattr(AcpNativeResults, "end", fail_end)
    task = Task(
        task_id="task_fault", session_id=session.session_id, agent="minimax", cwd=str(tmp_path),
        message="search", capability="search",
    )
    with pytest.raises(AcpError, match="transport disconnected") as caught:
        await adapter.run_turn(session, task)
    assert caught.value.failure.operation == "session/prompt"
    assert caught.value.failure.prompt_may_have_been_sent
    assert task.checkpoint_error
    assert live.client.native is None


async def test_image_spawn_applies_final_env_deny(tmp_path, monkeypatch):
    adapter = CodexAdapter(
        AgentConfig(name="codex", protocol="codex", command=["unused"]),
        tmp_path / "home", EnvConfig(deny=["NATIVE_PRIVATE_*"]),
    )
    monkeypatch.setattr(adapter, "_worker_env", lambda: {
        "NATIVE_PRIVATE_FIXTURE": "must-be-removed",
        "AGENT_BRIDGE_WORKER": "1",
    })
    monkeypatch.setattr(adapter, "_base_cmd", lambda env=None: ["unused"])
    inspected = False

    async def inspect_run(self, command, overrides):
        nonlocal inspected
        inspected = True
        assert "NATIVE_PRIVATE_FIXTURE" not in self.env
        assert self.env["AGENT_BRIDGE_WORKER"] == "1"
        return TurnResult(stop_reason="cancelled")

    monkeypatch.setattr(CodexImageTurn, "run", inspect_run)
    session = Session(session_id="sess_image", agent="codex", cwd=str(tmp_path))
    task = Task(
        task_id="task_image", session_id=session.session_id, agent="codex", cwd=str(tmp_path),
        message="image", operation="image",
    )
    await adapter.run_turn(session, task)
    assert inspected


async def test_native_failed_state_flush_submits_zero_calls(tmp_path, monkeypatch):
    registry, worker, args = search_registry(tmp_path)
    original = registry.flush_state

    async def unavailable():
        return False

    monkeypatch.setattr(registry, "flush_state", unavailable)
    try:
        call = await capabilities.call_capability(registry, **args)
        result = await finish(registry, call)
        assert call["persisted"] is False
        assert result["persisted"] is False
        assert result["status"] == "failed"
        assert worker.calls == 0
        assert "submission blocked" in result["error"]
    finally:
        monkeypatch.setattr(registry, "flush_state", original)
        await registry.stop()


async def test_ordinary_request_keeps_eight_field_binding_after_restart(tmp_path):
    registry, worker, _args = search_registry(tmp_path)
    request_id = str(uuid.uuid4())
    dispatch = dict(agent="grok", message="ordinary", cwd=str(tmp_path), request_id=request_id)
    first = await registry.dispatch_task(**dispatch)
    await registry.wait_task(first["task_id"], 5)
    expected = _request_fingerprint(("grok", "ordinary", str(tmp_path.resolve()), None, None, None, None, False))
    assert registry._requests[request_id] == (expected, first["task_id"])
    await registry.stop()
    payload = read_json_strict(registry.home / "state.json", {})
    assert next(row for row in payload["requests"] if row["request_id"] == request_id)["fingerprint"] == expected
    restored = Registry.create(registry.home, registry.config)
    await restored.start()
    try:
        again = await restored.dispatch_task(**dispatch)
        assert again["reused"] and again["task_id"] == first["task_id"]
        assert worker.calls == 1
    finally:
        await restored.stop()


async def test_forced_cancel_cannot_unlock_delayed_native_cleanup(tmp_path, monkeypatch):
    registry, worker, args = search_registry(tmp_path, blocked=True)
    entered, release = asyncio.Event(), asyncio.Event()
    real_wait = asyncio.wait

    async def brief_wait(aws, *, timeout=None, **kwargs):
        return await real_wait(aws, timeout=0.01 if timeout == 15 else timeout, **kwargs)

    async def shutdown(session):
        entered.set()
        await release.wait()

    monkeypatch.setattr(asyncio, "wait", brief_wait)
    monkeypatch.setattr(worker, "shutdown", shutdown)
    try:
        call = await capabilities.call_capability(registry, **args)
        await worker.ready.wait()
        task = registry.tasks[call["call_id"]]
        cancelled = await capabilities.cancel_capability(registry, call["call_id"])
        await entered.wait()
        assert cancelled["status"] == "cancelled"
        assert cancelled["pending"] is True
        assert not registry._done[task.task_id].is_set()
        assert task.task_id in registry._bg
        assert task.session_id in registry._adapters
        pending = await capabilities.get_capability_result(registry, task.task_id)
        assert pending["pending"] is True
        with pytest.raises(ValueError, match="completed"):
            await capabilities.call_capability(
                registry, **{**args, "request_id": str(uuid.uuid4()), "previous_call_id": task.task_id},
            )
        release.set()
        final = await finish(registry, call)
        assert final["pending"] is False
        assert final["status"] == "cancelled"
    finally:
        release.set()
        await registry.stop()


async def test_failed_native_cleanup_keeps_pending_and_blocks_continuation(tmp_path, monkeypatch):
    registry, worker, args = search_registry(tmp_path)
    original_shutdown = worker.shutdown
    entered = asyncio.Event()

    async def fail_shutdown(session):
        entered.set()
        raise OSError("worker was not reaped")

    monkeypatch.setattr(worker, "shutdown", fail_shutdown)
    try:
        call = await capabilities.call_capability(registry, **args)
        await asyncio.wait_for(entered.wait(), 5)
        await registry._bg[call["call_id"]]
        result = await capabilities.get_capability_result(registry, call["call_id"])
        task = registry.tasks[call["call_id"]]
        assert result["status"] == "failed"
        assert result["pending"] is True
        assert task.session_id in registry._adapters
        with pytest.raises(ValueError, match="completed"):
            await capabilities.call_capability(
                registry, **{**args, "request_id": str(uuid.uuid4()), "previous_call_id": task.task_id},
            )
        assert task.status == TaskStatus.failed
    finally:
        monkeypatch.setattr(worker, "shutdown", original_shutdown)
        await registry.stop()



async def test_cancelled_native_dispatch_flush_still_finishes_registered_call(tmp_path, monkeypatch):
    registry, worker, args = search_registry(tmp_path)
    original_flush = registry.flush_state
    entered = asyncio.Event()
    gate = asyncio.Event()
    first_flush = True

    async def block_first_flush():
        nonlocal first_flush
        if first_flush:
            first_flush = False
            entered.set()
            await gate.wait()
        return await original_flush()

    monkeypatch.setattr(registry, "flush_state", block_first_flush)
    dispatching = asyncio.create_task(capabilities.call_capability(registry, **args))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert worker.calls == 0
        dispatching.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dispatching
        task = next(iter(registry.tasks.values()))
        result = await capabilities.get_capability_result(registry, task.task_id, wait_sec=5)
        assert result["status"] == "completed" and result["persisted"] is True
        replay = await capabilities.call_capability(registry, **args)
        assert replay["reused"] and replay["call_id"] == task.task_id
        assert worker.calls == 1
    finally:
        gate.set()
        monkeypatch.setattr(registry, "flush_state", original_flush)
        await registry.stop()


async def test_codex_jsonl_search_sources_survive_registry_recovery_and_restart(tmp_path, monkeypatch):
    import json
    import sys

    from agent_bridge.config import AppConfig

    script = tmp_path / "offline_search.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "sys.stdin.read()\n"
        "with pathlib.Path('search-submissions.log').open('a') as handle:\n"
        "    handle.write('submit\\n')\n"
        "events = [\n"
        "    {'type': 'thread.started', 'thread_id': 'thread_search'},\n"
        "    {'type': 'item.completed', 'item': {\n"
        "        'id': 'search_native', 'type': 'web_search',\n"
        "        'action': {'type': 'search', 'query': 'offline fixture'},\n"
        "        'results': [{'type': 'text_result', 'url': 'https://example.com/native', 'title': 'Native source'}],\n"
        "    }},\n"
        "    {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Offline search findings'}},\n"
        "    {'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 2}},\n"
        "]\n"
        "for event in events:\n"
        "    print(json.dumps(event), flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(CodexAdapter, "_base_cmd", lambda self, env=None: [sys.executable, str(script)])
    config = AppConfig(agents={
        "codex": AgentConfig(name="codex", protocol="codex", command=["unused"], stall_timeout_sec=0),
    })
    registry = Registry.create(tmp_path / "bridge", config, runtime_context="coordinator")
    args = dict(
        agent="codex", capability="search", prompt="Find the offline source",
        cwd=str(tmp_path), request_id=str(uuid.uuid4()),
    )
    await registry.start()
    try:
        call = await capabilities.call_capability(registry, **args)
        result = await capabilities.get_capability_result(registry, call["call_id"], wait_sec=5)
        assert result["status"] == "completed"
        assert not result["pending"] and result["persisted"]
        assert result["sources"] == [{
            "title": "Native source", "url": "https://example.com/native", "tool_call_id": "search_native",
        }]
        checkpoint = read_json_strict(registry.home / "native-results" / f"{call['call_id']}.json", {})
        assert checkpoint["sources"][0]["url"] == result["sources"][0]["url"]
        assert checkpoint["native_calls"] == {"search_native": "web_search"}
        assert result["result_text"] == "Offline search findings"
        assert (tmp_path / "search-submissions.log").read_text().splitlines() == ["submit"]
    finally:
        await registry.stop()

    restored = Registry.create(registry.home, config, runtime_context="coordinator")
    await restored.start()
    try:
        replay = await capabilities.call_capability(restored, **args)
        result = await capabilities.get_capability_result(restored, replay["call_id"])
        assert replay["reused"] and replay["call_id"] == call["call_id"]
        assert result["status"] == "completed"
        assert result["sources"][0]["tool_call_id"] == "search_native"
        assert (tmp_path / "search-submissions.log").read_text().splitlines() == ["submit"]
        assert json.loads((registry.home / "native-results" / f"{call['call_id']}.json").read_text())["sources"]
    finally:
        await restored.stop()


class NonexitingImageProcess:
    """Speak the offline image protocol but time out on every reap wait."""

    def __init__(self, official_home):
        self.pid = 999999999
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.stdin = self
        self.official_home = official_home
        self.waits = 0
        self.submissions = 0

    def emit(self, value):
        import json
        self.stdout.feed_data((json.dumps(value) + "\n").encode())

    def write(self, raw):
        import json

        from fake_codex_image import png

        request = json.loads(raw)
        method = request.get("method")
        if method == "initialize":
            self.emit({"id": request["id"], "result": {"codexHome": str(self.official_home)}})
        elif method == "thread/start":
            self.emit({"id": request["id"], "result": {"thread": {"id": "thread_timeout"}}})
        elif method == "turn/start":
            self.submissions += 1
            path = self.official_home / "generated_images" / "thread_timeout" / "image_native.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png())
            self.emit({"id": request["id"], "result": {"turn": {"id": "turn_timeout"}}})
            self.emit({
                "method": "item/completed", "params": {
                    "threadId": "thread_timeout", "turnId": "turn_timeout",
                    "item": {
                        "type": "imageGeneration", "id": "image_native",
                        "status": "completed", "savedPath": str(path),
                    },
                },
            })
            self.emit({
                "method": "turn/completed", "params": {
                    "threadId": "thread_timeout", "turn": {"id": "turn_timeout", "status": "completed"},
                },
            })

    async def drain(self):
        pass

    async def wait(self):
        self.waits += 1
        if self.returncode is None:
            raise TimeoutError("simulated process remains live after kill")
        return self.returncode


async def test_image_cancel_retains_pid_when_real_reaper_exhausts_timeouts(tmp_path, monkeypatch):
    from agent_bridge.processes import record_pid

    process = NonexitingImageProcess(tmp_path / "official")
    kills = []
    monkeypatch.setattr(
        "agent_bridge.processes.kill_tree", lambda pid, handle=None, force=False: kills.append((pid, force)),
    )
    session = Session(session_id="sess_timeout", agent="codex", cwd=str(tmp_path))
    task = Task(
        task_id="task_timeout", session_id=session.session_id, agent="codex", cwd=str(tmp_path),
        message="offline image", operation="image",
    )
    home = tmp_path / "bridge"
    turn = CodexImageTurn(home, session, task, {})
    turn.proc = process
    record_pid(home, session.session_id, process.pid, None, None)
    with pytest.raises(RuntimeError, match=r"exit.*unconfirmed"):
        await turn.cancel()
    assert process.returncode is None
    assert process.waits == 2
    assert kills == [(process.pid, False), (process.pid, True)]
    assert read_json_strict(home / "pids.json", {})[session.session_id]["pid"] == process.pid
    process.returncode = 0
    await turn.cancel()
    assert session.session_id not in read_json_strict(home / "pids.json", {})


async def test_image_real_reaper_timeout_keeps_registry_pending_and_owned_references(tmp_path, monkeypatch):
    from agent_bridge.config import AppConfig
    from agent_bridge.output_files import verify_output

    official_home = tmp_path / "official"
    official_home.mkdir()
    process = NonexitingImageProcess(official_home)
    kills = []

    async def spawn(*args, **kwargs):
        assert args[:2] == ("offline-codex", "app-server")
        return process

    monkeypatch.setattr(CodexAdapter, "_base_cmd", lambda self, env=None: ["offline-codex"])
    monkeypatch.setattr("agent_bridge.codex_image.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr(
        "agent_bridge.processes.kill_tree", lambda pid, handle=None, force=False: kills.append((pid, force)),
    )
    registry = Registry.create(
        tmp_path / "bridge",
        AppConfig(agents={
            "codex": AgentConfig(
                name="codex", protocol="codex", command=["unused"], stall_timeout_sec=0,
                env={"CODEX_HOME": str(official_home)},
            ),
        }),
        runtime_context="coordinator",
    )
    args = dict(
        agent="codex", capability="image", prompt="Generate an offline image",
        cwd=str(tmp_path), request_id=str(uuid.uuid4()),
    )
    await registry.start()
    try:
        call = await capabilities.call_capability(registry, **args)
        background = registry._bg[call["call_id"]]
        await asyncio.wait_for(asyncio.shield(background), 5)
        task = registry.tasks[call["call_id"]]
        adapter = registry._adapters[task.session_id]
        image = adapter._image_turns[task.session_id]
        result = await capabilities.get_capability_result(registry, task.task_id)
        assert result["status"] == "failed" and result["pending"]
        assert not registry._done[task.task_id].is_set()
        assert registry._bg[task.task_id] is background
        assert image.proc is process and process.returncode is None
        assert process.submissions == 1 and process.waits == 4
        assert kills == [(process.pid, False), (process.pid, True)] * 2
        assert read_json_strict(registry.home / "pids.json", {})[task.session_id]["pid"] == process.pid
        assert result["job"]["status"] == "completed"
        verify_output(task.output_files[0], tmp_path, task.task_id)
        with pytest.raises(RuntimeError, match=r"exit.*unconfirmed"):
            await adapter.shutdown(registry.sessions[task.session_id])
        assert adapter._image_turns[task.session_id] is image
        assert registry._adapters[task.session_id] is adapter
        with pytest.raises(ValueError, match="completed"):
            await capabilities.call_capability(
                registry, **{**args, "request_id": str(uuid.uuid4()), "previous_call_id": task.task_id},
            )
        assert process.submissions == 1
    finally:
        process.returncode = 0
        await registry.stop()
    assert registry._adapters == {}
    assert task.session_id not in read_json_strict(registry.home / "pids.json", {})
