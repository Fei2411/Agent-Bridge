from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from acp.schema import (
    NewSessionResponse,
    PermissionOption,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
)

from agent_bridge.adapters import acp
from agent_bridge.adapters.acp import AcpAdapter, AcpError, _acp_model_catalog, _Live
from agent_bridge.config import AgentConfig


class FakeConn:
    def __init__(self, response=None, fail_at=None, block_at=None):
        self.response = response or NewSessionResponse(session_id="discovery")
        self.fail_at = fail_at
        self.block_at = block_at
        self.calls = []
        self.started = asyncio.Event()
        self.client = None

    async def _call(self, name, kwargs):
        self.calls.append((name, kwargs))
        if name == self.fail_at:
            raise RuntimeError(f"{name} failed")
        if name == self.block_at:
            self.started.set()
            await asyncio.Event().wait()

    async def initialize(self, **kwargs):
        await self._call("initialize", kwargs)

    async def new_session(self, **kwargs):
        await self._call("new_session", kwargs)
        return self.response

    async def close(self):
        await self._call("close", {})


@pytest.fixture
def discovery(monkeypatch, tmp_path):
    """Fail on registry, persistence, or normal-session paths, even on errors."""
    def forbidden(*args, **kwargs):
        pytest.fail("Discovery must not persist or configure a Bridge session")

    for name in ("append_event", "record_pid", "drop_pid"):
        monkeypatch.setattr(acp, name, forbidden)
    monkeypatch.setattr(acp, "resolve_command", lambda command, fallback: list(command))
    monkeypatch.setattr(acp, "resolve_dsh_command", lambda command, fallback: list(command))
    conn = FakeConn()
    proc = SimpleNamespace(stdin=object(), stdout=object(), stderr=None, returncode=None)
    spawns = []
    reaped = []

    async def spawn(*args, **kwargs):
        spawns.append((args, kwargs))
        return proc

    async def reap(value):
        reaped.append(value)

    def connect(client, stdin, stdout):
        assert (stdin, stdout) == (proc.stdin, proc.stdout)
        conn.client = client
        return conn

    monkeypatch.setattr(acp.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(acp, "connect_to_agent", connect)
    monkeypatch.setattr(acp, "reap_subprocess", reap)

    def build(name="kimi", command=None, **kwargs):
        adapter = AcpAdapter(
            AgentConfig(
                name=name, protocol="acp", command=command or [name, "acp"],
                session_meta={"yoloMode": True, "modelId": "must-not-select"},
                permission_policy="allow_always", **kwargs,
            ),
            tmp_path / "home",
        )
        monkeypatch.setattr(adapter, "_env", lambda: {"SAFE": "yes"})
        for method in ("_spawn", "ensure_session", "_sync_selection", "shutdown"):
            monkeypatch.setattr(adapter, method, forbidden)
        active = _Live()
        adapter._live["existing-session"] = active
        return adapter, active

    return SimpleNamespace(
        conn=conn, proc=proc, spawns=spawns, reaped=reaped, build=build, home=tmp_path / "home",
    )


def select_option(option_id, current, choices, **kwargs):
    return SessionConfigOptionSelect(
        id=option_id, name=option_id, type="select",
        current_value=current, options=choices, **kwargs,
    )


def snapshot(*options):
    return NewSessionResponse(session_id="discovery", config_options=list(options))


def test_grouped_model_and_effort_options():
    result = _acp_model_catalog(snapshot(
        select_option("model", "model-b", [{
            "group": "provider", "name": "Provider", "options": [
                {"value": "model-a", "name": "Model A"},
                {"value": "model-b", "name": "Model B"},
            ],
        }]),
        select_option("thinking", "high", [{
            "group": "levels", "name": "Levels", "options": [
                {"value": "low", "name": "Low"}, {"value": "high", "name": "High"},
            ],
        }]),
    ), "kimi")
    assert result == {
        "source": "acp",
        "models": [
            {"id": "model-a", "name": "Model A", "is_default": False, "supported_efforts": None},
            {"id": "model-b", "name": "Model B", "is_default": True, "supported_efforts": ["low", "high"]},
        ],
        "default_model": "model-b", "warnings": [],
    }


def test_model_and_effort_categories():
    result = _acp_model_catalog(snapshot(
        select_option("custom-picker", "a", [{"value": "a", "name": "A"}], category="model"),
        select_option("custom-depth", "high", [{"value": "high", "name": "High"}], category="thought_level"),
    ), "kimi")
    assert result["default_model"] == "a"
    assert result["models"] == [
        {"id": "a", "name": "A", "is_default": True, "supported_efforts": ["high"]},
    ]


def test_missing_catalog_warns():
    result = _acp_model_catalog(NewSessionResponse(session_id="discovery"), "kimi")
    assert result["models"] == []
    assert result["default_model"] is None
    assert result["warnings"]


def test_current_model_is_not_added_to_advertised_choices():
    result = _acp_model_catalog(snapshot(
        select_option("model", "current", [{"value": "offered", "name": "Offered"}]),
    ), "kimi")
    assert result["default_model"] == "current"
    assert [model["id"] for model in result["models"]] == ["offered"]


@pytest.mark.parametrize("thinking", [
    SessionConfigOptionBoolean(id="thinking", name="Thinking", type="boolean", current_value=True),
    select_option("thinking", "true", [
        {"value": "true", "name": "On"}, {"value": "false", "name": "Off"},
    ]),
])
def test_boolean_thinking_is_not_reported_as_effort(thinking):
    result = _acp_model_catalog(snapshot(
        select_option("model", "a", [{"value": "a", "name": "A"}]), thinking,
    ), "kimi")
    assert result["models"][0]["supported_efforts"] is None


async def test_private_handshake_never_prompts_configures_or_persists(discovery):
    adapter, active = discovery.build(cwd="process-directory")
    discovery.conn.response = snapshot(select_option("model", "a", [{"value": "a", "name": "A"}]))
    result = await adapter.list_models("workspace-directory")
    assert result["models"][0]["id"] == "a"
    assert [name for name, _ in discovery.conn.calls] == ["initialize", "new_session", "close"]
    assert discovery.conn.calls[1][1] == {"cwd": "workspace-directory", "mcp_servers": []}
    assert discovery.spawns[0][1]["cwd"] == "process-directory"
    assert discovery.spawns[0][1]["env"] == {"SAFE": "yes"}
    assert discovery.reaped == [discovery.proc]
    assert adapter._live == {"existing-session": active}
    assert not discovery.home.exists()


async def test_discovery_denies_permission_even_with_allow_always_policy(discovery):
    adapter, _active = discovery.build()
    await adapter.list_models(".")
    response = await discovery.conn.client.request_permission(
        "private", {"kind": "edit"},
        [PermissionOption(option_id="allow", name="Allow", kind="allow_always")],
    )
    assert response.outcome.outcome == "cancelled"
    await discovery.conn.client.session_update("private", {"toolCallId": "ignored"})
    with pytest.raises(RuntimeError, match="disabled"):
        await discovery.conn.client.write_text_file("private", "file", "content")
    with pytest.raises(RuntimeError, match="disabled"):
        await discovery.conn.client.create_terminal("private", "command")
    assert not discovery.home.exists()


@pytest.mark.parametrize("fail_at", ["initialize", "new_session"])
async def test_handshake_failure_reaps_private_process(discovery, fail_at):
    adapter, active = discovery.build()
    discovery.conn.fail_at = fail_at
    with pytest.raises(AcpError, match=f"{fail_at} failed") as caught:
        await adapter.list_models(".")
    assert caught.value.failure.operation == "model_discovery"
    assert caught.value.failure.prompt_may_have_been_sent is False
    assert discovery.conn.calls[-1][0] == "close"
    assert discovery.reaped == [discovery.proc]
    assert adapter._live == {"existing-session": active}


@pytest.mark.parametrize("block_at", ["initialize", "new_session"])
@pytest.mark.parametrize("cancel_method", ["cancel", "timeout"])
async def test_cancel_or_outer_timeout_reaps_private_process(discovery, block_at, cancel_method):
    adapter, active = discovery.build()
    discovery.conn.block_at = block_at
    task = asyncio.create_task(adapter.list_models("."))
    await discovery.conn.started.wait()
    if cancel_method == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, timeout=0.01)
    assert discovery.conn.calls[-1][0] == "close"
    assert discovery.reaped == [discovery.proc]
    assert adapter._live == {"existing-session": active}


async def test_connect_failure_still_reaps_process(discovery, monkeypatch):
    adapter, _active = discovery.build()

    def fail(*args):
        raise RuntimeError("connect failed")

    monkeypatch.setattr(acp, "connect_to_agent", fail)
    with pytest.raises(AcpError, match="connect failed"):
        await adapter.list_models(".")
    assert discovery.reaped == [discovery.proc]


async def test_close_failure_still_reaps_process(discovery):
    adapter, _active = discovery.build()
    discovery.conn.fail_at = "close"
    await adapter.list_models(".")
    assert discovery.reaped == [discovery.proc]


async def test_cancellation_during_cleanup_still_waits_for_reaping(discovery, monkeypatch):
    adapter, active = discovery.build()
    closing = asyncio.Event()
    finish_close = asyncio.Event()

    async def close():
        closing.set()
        await finish_close.wait()

    monkeypatch.setattr(discovery.conn, "close", close)
    task = asyncio.create_task(adapter.list_models("."))
    await closing.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert discovery.reaped == [discovery.proc]
    assert adapter._live == {"existing-session": active}


async def test_native_dsh_uses_advertised_provider_model_ids(discovery, monkeypatch):
    prepared = []

    def prepare(command, env, **kwargs):
        prepared.append(kwargs)
        return command, env

    monkeypatch.setattr(acp, "prepare_dsh_launch", prepare)
    adapter, _active = discovery.build("dsh", ["dsh", "--profile", "acp"])
    discovery.conn.response = snapshot(
        select_option("model", '["openai","reasoner"]', [{
            "group": "openai", "name": "OpenAI", "options": [
                {"value": '["openai","reasoner"]', "name": "Reasoner"},
                {"value": '["openai","fast"]', "name": "Fast"},
            ],
        }]),
        select_option("reasoning_effort", "high", [
            {"value": "medium", "name": "Medium"}, {"value": "high", "name": "High"},
        ]),
    )
    result = await adapter.list_models(".")
    assert prepared == [{}]
    assert result["default_model"] == "openai/reasoner"
    assert [(m["id"], m["supported_efforts"]) for m in result["models"]] == [
        ("openai/reasoner", ["medium", "high"]), ("openai/fast", None),
    ]


async def test_legacy_dsh_discovers_actual_advertised_catalog(discovery, monkeypatch):
    prepared = []

    def prepare(command, env, **kwargs):
        prepared.append(kwargs)
        return command, env

    monkeypatch.setattr(acp, "prepare_dsh_launch", prepare)
    adapter, active = discovery.build("dsh", ["dsh-acp-demo"])
    discovery.conn.response = snapshot(select_option(
        "model", "legacy-model", [{"value": "legacy-model", "name": "Legacy Model"}],
    ))
    result = await adapter.list_models(".")
    assert prepared == [{}]
    assert result["default_model"] == "legacy-model"
    assert result["models"] == [{
        "id": "legacy-model", "name": "Legacy Model",
        "is_default": True, "supported_efforts": None,
    }]
    assert result["warnings"] == []
    assert [name for name, _ in discovery.conn.calls] == ["initialize", "new_session", "close"]
    assert len(discovery.spawns) == 1
    assert discovery.reaped == [discovery.proc]
    assert adapter._live == {"existing-session": active}


async def test_legacy_dsh_warns_only_after_handshake_without_catalog(discovery, monkeypatch):
    monkeypatch.setattr(acp, "prepare_dsh_launch", lambda command, env: (command, env))
    adapter, active = discovery.build("dsh", ["dsh-acp-demo"])
    result = await adapter.list_models(".")
    assert result["models"] == []
    assert result["default_model"] is None
    assert "did not advertise a model catalog" in result["warnings"][0]
    assert [name for name, _ in discovery.conn.calls] == ["initialize", "new_session", "close"]
    assert len(discovery.spawns) == 1
    assert discovery.reaped == [discovery.proc]
    assert adapter._live == {"existing-session": active}


async def test_legacy_dsh_handshake_error_is_failure_not_unsupported(discovery, monkeypatch):
    monkeypatch.setattr(acp, "prepare_dsh_launch", lambda command, env: (command, env))
    adapter, _active = discovery.build("dsh", ["dsh-acp-demo"])
    discovery.conn.fail_at = "new_session"
    with pytest.raises(AcpError, match="new_session failed"):
        await adapter.list_models(".")
    assert discovery.reaped == [discovery.proc]


async def test_claude_catalog_uses_current_model_effort(discovery):
    adapter, active = discovery.build("claude")
    discovery.conn.response = snapshot(
        select_option("model", "opus", [
            {"value": "opus", "name": "Opus"},
            {"value": "sonnet", "name": "Sonnet"},
        ], category="model"),
        select_option("effort", "default", [
            {"value": "default", "name": "Default"},
            {"value": "low", "name": "Low"},
            {"value": "high", "name": "High"},
        ], category="thought_level"),
    )
    result = await adapter.list_models(".")
    assert result["default_model"] == "opus"
    assert result["models"] == [
        {"id": "opus", "name": "Opus", "is_default": True,
         "supported_efforts": ["default", "low", "high"]},
        {"id": "sonnet", "name": "Sonnet", "is_default": False, "supported_efforts": None},
    ]
    assert [name for name, _ in discovery.conn.calls] == ["initialize", "new_session", "close"]
    assert adapter._live == {"existing-session": active}
    assert discovery.reaped == [discovery.proc]
