from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent_bridge.adapters.codex import CodexAdapter
from agent_bridge.codex_image import CodexImageTurn
from agent_bridge.config import AgentConfig
from agent_bridge.models import Session, Task
from agent_bridge.native_results import load_checkpoint
from agent_bridge.output_files import verify_output
from agent_bridge.transcript import flush_session

FAKE = Path(__file__).with_name("fake_codex_image.py")


def setup_image(tmp_path, case, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    official_home = tmp_path / "official-home"
    official_home.mkdir()
    home = tmp_path / "bridge"
    cfg = AgentConfig(
        name="codex", protocol="codex_exec", command=["unused"],
        env={"CODEX_HOME": str(official_home), "BRIDGE_IMAGE_FIXTURE": case},
    )
    adapter = CodexAdapter(cfg, home)
    monkeypatch.setattr(adapter, "_base_cmd", lambda env=None: [sys.executable, str(FAKE)])
    session = Session(session_id="session_fixture", agent="codex", cwd=str(work))
    task = Task(
        task_id="task_fixture", session_id=session.session_id, agent="codex",
        message="Generate a fixture image", cwd=str(work), operation="image",
    )
    return adapter, session, task, home, work


@pytest.mark.parametrize("case", ["success", "duplicate", "before_response"])
async def test_image_delivers_native_file_with_matching_ids(tmp_path, monkeypatch, case):
    adapter, session, task, home, work = setup_image(tmp_path, case, monkeypatch)
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "end_turn"
    assert result.job.status == "completed"
    assert len(result.output_files) == 1
    assert result.usage == {"input_tokens": 3, "output_tokens": 2}
    verify_output(result.output_files[0], work, task.task_id)
    assert (work / "submissions.log").read_text().splitlines() == ["submit"]
    flush_session(session.session_id, home)
    persisted = "".join(p.read_text() for p in home.rglob("*.json*"))
    assert "fixture-base64-must-not-persist" not in persisted
    assert str(tmp_path / "official-home") not in persisted
    assert adapter._image_turns == {}


@pytest.mark.parametrize("case", ["wrong_thread", "wrong_turn", "old", "missing", "escape", "native_failed", "no_image", "turn_rejected"])
async def test_no_delivery_without_native_success_and_safe_file(tmp_path, monkeypatch, case):
    adapter, session, task, home, work = setup_image(tmp_path, case, monkeypatch)
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "error"
    assert result.output_files == []
    assert result.job.status == "failed"
    assert (work / "submissions.log").read_text().splitlines() == ["submit"]
    recovered = task.model_copy(update={"output_files": [], "job": None})
    assert json.loads((home / "native-results" / "task_fixture.json").read_text())["job"]["status"] == result.job.status, result.error
    load_checkpoint(home, recovered, interrupted=True)
    assert recovered.job.status == "failed"
    assert not recovered.job.remote_cancel_supported


async def test_disconnect_is_uncertain_and_recovery_does_not_resubmit(tmp_path, monkeypatch):
    adapter, session, task, home, work = setup_image(tmp_path, "disconnect", monkeypatch)
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "error"
    assert result.job.status == "uncertain"
    for _ in range(2):
        load_checkpoint(home, task, interrupted=True)
    assert task.job.status == "uncertain"
    assert (work / "submissions.log").read_text().splitlines() == ["submit"]


async def test_native_completion_survives_lost_turn_response(tmp_path, monkeypatch):
    adapter, session, task, _home, work = setup_image(tmp_path, "completed_disconnect", monkeypatch)
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "error"
    assert result.job.status == "completed"
    assert len(result.output_files) == 1
    verify_output(result.output_files[0], work, task.task_id)


async def test_rejection_before_submit_has_no_media_job(tmp_path, monkeypatch):
    adapter, session, task, _home, work = setup_image(tmp_path, "init_rejected", monkeypatch)
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "error"
    assert result.job is None
    assert not (work / "submissions.log").exists()


async def test_local_cancel_does_not_claim_remote_cancel(tmp_path, monkeypatch):
    adapter, session, task, home, work = setup_image(tmp_path, "hang", monkeypatch)
    started = asyncio.Event()
    original_rpc = CodexImageTurn._rpc
    async def observed_rpc(self, method, params):
        result = await original_rpc(self, method, params)
        if method == "turn/start":
            started.set()
        return result
    monkeypatch.setattr(CodexImageTurn, "_rpc", observed_rpc)
    running = asyncio.create_task(adapter.run_turn(session, task))
    await asyncio.wait_for(started.wait(), 10)
    await adapter.cancel(session)
    result = await asyncio.wait_for(running, 10)
    assert result.stop_reason == "cancelled"
    assert result.job.status == "uncertain"
    assert result.job.remote_cancel_supported is False
    assert (work / "submissions.log").read_text().splitlines() == ["submit"]
    assert json.loads((home / "native-results" / "task_fixture.json").read_text())["job"]["status"] == "uncertain"
