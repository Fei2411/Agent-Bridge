from __future__ import annotations

import asyncio
import types

import pytest

from agent_bridge.config import AgentConfig, AppConfig, EnvConfig, ModelsConfig, load_config
from agent_bridge.model_catalog import model_discovery_capability
from agent_bridge.registry import Registry
from agent_bridge.server import list_models


@pytest.fixture
def catalog_registry(tmp_path, monkeypatch):
    config = AppConfig(
        agents={"kimi": AgentConfig(name="kimi", protocol="acp", command=["not-launched"])},
        env=EnvConfig(discover_proxy=False),
    )
    registry = Registry(tmp_path / "bridge", config, runtime_context="coordinator")
    calls = []

    class Discovery:
        async def list_models(self, cwd):
            calls.append(cwd)
            await asyncio.sleep(0)
            return {
                "source": "fake catalog",
                "models": [{"id": "small", "is_default": True, "supported_efforts": ["low", "high"]}],
                "default_model": "small",
            }

    monkeypatch.setattr("agent_bridge.registry.build_adapter", lambda *args: Discovery())
    return registry, calls


async def test_cache_refresh_and_defensive_copy(catalog_registry, tmp_path):
    registry, calls = catalog_registry
    first = await registry.list_models("kimi", str(tmp_path))
    assert first["cached"] is False
    first["models"][0]["id"] = "changed by caller"
    cached = await registry.list_models("kimi", str(tmp_path))
    assert cached["cached"] is True
    assert cached["models"][0]["id"] == "small"
    assert cached["availability"] == "advertised"
    assert cached["invocation_verified"] is False
    assert cached["effort_vocabulary"] == "worker_native"
    assert len(calls) == 1
    assert not (await registry.list_models("kimi", str(tmp_path), refresh=True))["cached"]
    assert len(calls) == 2
    assert registry.sessions == registry.tasks == registry._adapters == {}


async def test_concurrent_queries_share_discovery(catalog_registry, tmp_path):
    registry, calls = catalog_registry
    results = await asyncio.gather(*(registry.list_models("kimi", str(tmp_path)) for _ in range(5)))
    assert len(calls) == 1
    assert sum(not r["cached"] for r in results) == 1


async def test_cache_scope_and_expiry(catalog_registry, tmp_path, monkeypatch):
    registry, calls = catalog_registry
    clock = [1000.0]
    monkeypatch.setattr("agent_bridge.registry.time.monotonic", lambda: clock[0])
    await registry.list_models("kimi", str(tmp_path))
    clock[0] += 3599
    assert (await registry.list_models("kimi", str(tmp_path)))["cached"]
    clock[0] += 2
    assert not (await registry.list_models("kimi", str(tmp_path)))["cached"]
    other = tmp_path / "other"
    other.mkdir()
    await registry.list_models("kimi", str(other))
    assert len(calls) == 3


async def test_failed_refresh_is_explicit_and_preserves_cache(catalog_registry, tmp_path, monkeypatch):
    registry, _ = catalog_registry
    await registry.list_models("kimi", str(tmp_path))

    class Broken:
        async def list_models(self, cwd):
            raise RuntimeError("Authorization: Bearer catalog-secret")

    monkeypatch.setattr("agent_bridge.registry.build_adapter", lambda *args: Broken())
    with pytest.raises(RuntimeError) as caught:
        await registry.list_models("kimi", str(tmp_path), refresh=True)
    assert "catalog-secret" not in str(caught.value)
    assert (await registry.list_models("kimi", str(tmp_path)))["cached"]


async def test_discovery_timeout_cancels_adapter(catalog_registry, tmp_path, monkeypatch):
    registry, _ = catalog_registry
    registry.config.models.timeout_sec = 0.01
    cleaned = asyncio.Event()

    class Slow:
        async def list_models(self, cwd):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    monkeypatch.setattr("agent_bridge.registry.build_adapter", lambda *args: Slow())
    with pytest.raises(RuntimeError, match="TimeoutError"):
        await registry.list_models("kimi", str(tmp_path))
    assert cleaned.is_set()
    assert not registry._model_cache


async def test_deadline_includes_waiting_for_worker_lock(catalog_registry, tmp_path):
    registry, calls = catalog_registry
    registry.config.models.timeout_sec = 0.01
    lock = registry._model_locks.setdefault("kimi", asyncio.Lock())
    async with lock:
        with pytest.raises(RuntimeError, match="TimeoutError"):
            await registry.list_models("kimi", str(tmp_path), refresh=True)
    assert calls == []


async def test_fresh_cache_does_not_wait_for_discovery_lock(catalog_registry, tmp_path):
    registry, calls = catalog_registry
    await registry.list_models("kimi", str(tmp_path))
    registry.config.models.timeout_sec = 0.01
    async with registry._model_locks["kimi"]:
        cached = await registry.list_models("kimi", str(tmp_path))
        assert cached["cached"]
        cached["models"][0]["id"] = "caller mutation"
        again = await registry.list_models("kimi", str(tmp_path))
        assert again["models"][0]["id"] == "small"
    assert len(calls) == 1


async def test_list_agents_reports_capability_without_discovery(catalog_registry, monkeypatch):
    registry, calls = catalog_registry
    registry.config.quota.enabled = False

    async def probe(cfg, env):
        return {"agent": cfg.name, "available": True}

    monkeypatch.setattr("agent_bridge.registry.probe_agent", probe)
    rows = await registry.list_agents()
    assert rows[0]["model_discovery"]["supported"]
    assert calls == []


async def test_rejects_bad_context_and_unsupported_worker(catalog_registry, tmp_path):
    registry, calls = catalog_registry
    with pytest.raises(ValueError, match="absolute"):
        await registry.list_models("kimi", "relative")
    registry.config.agents["grok"] = AgentConfig(name="grok", protocol="acp", command=["unused"])
    with pytest.raises(NotImplementedError):
        await registry.list_models("grok", str(tmp_path))
    registry.dispatch_enabled = False
    with pytest.raises(RuntimeError, match="worker-inherited"):
        await registry.list_models("kimi", str(tmp_path))
    assert calls == []


async def test_mcp_tool_wraps_success_and_error(catalog_registry, tmp_path):
    registry, _ = catalog_registry
    ctx = types.SimpleNamespace(request_context=types.SimpleNamespace(lifespan_context=registry))
    result = await list_models(ctx, "kimi", str(tmp_path))
    assert result["ok"] and result["default_model"] == "small"
    assert not (await list_models(ctx, "unknown", str(tmp_path)))["ok"]


def test_model_discovery_config_overlay(tmp_path):
    (tmp_path / "agents.toml").write_text("[models]\ncache_sec = 86400\ntimeout_sec = 12\n", encoding="utf-8")
    config = load_config(tmp_path)
    assert config.models.cache_sec == 86400
    assert config.models.timeout_sec == 12
    assert not config.warnings
    assert ModelsConfig().cache_sec == 3600
    with pytest.raises(ValueError):
        ModelsConfig(cache_sec=-1)
    with pytest.raises(ValueError):
        ModelsConfig(timeout_sec=0)


def test_capability_does_not_claim_unimplemented_agents():
    for name, protocol in [("kimi", "acp"), ("dsh", "acp"), ("claude", "acp"), ("codex", "codex"), ("antigravity", "agy")]:
        cfg = AgentConfig(name=name, protocol=protocol, command=["unused"])
        assert model_discovery_capability(cfg)["supported"]
    assert not model_discovery_capability(AgentConfig(name="grok", protocol="acp", command=["unused"]))["supported"]
