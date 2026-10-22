from __future__ import annotations

import asyncio
import json
import os
import sys

import psutil
import pytest

from agent_bridge import model_discovery_exec
from agent_bridge.adapters.antigravity import AgyAdapter
from agent_bridge.adapters.codex import CodexAdapter
from agent_bridge.config import AgentConfig, EnvConfig
from agent_bridge.model_discovery_exec import (
    codex_app_server_list_models,
    parse_agy_models_output,
    parse_codex_models,
)

FAKE_AGY = r"""
import json
import os
import sys
import time

pid_file = os.environ.get("FAKE_AGY_PID")
if pid_file:
    with open(pid_file,"w") as f:f.write(str(os.getpid()))
mode = os.environ.get("FAKE_AGY_MODE", "text")

if mode == "exit3":
    print("Authorization: Bearer sk-testsecret123", file=sys.stderr)
    print("boom: token=apikeyhunter2", file=sys.stderr)
    sys.exit(3)

if mode == "hang":
    time.sleep(30)
    sys.exit(0)

print("gemini-4-argon-low\tGemini 4 Argon (Low)")
print("claude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)")
report = os.environ.get("FAKE_AGY_REPORT")
if report:
    with open(report, "w", encoding="utf-8") as fh:
        json.dump({
            "cwd": os.getcwd(),
            "denied_present": "DISCOVERY_SECRET" in os.environ,
            "agent_env": os.environ.get("AGY_MARKER"),
            "worker_context": os.environ.get("AGENT_BRIDGE_PARENT_CONTEXT"),
        }, fh)
sys.exit(0)
"""

FAKE_CODEX = r"""
import json
import os
import sys
import time

argv = sys.argv[1:]

if argv and argv[-1] != "app-server":
    print("unknown command", file=sys.stderr)
    sys.exit(2)

report = os.environ.get("FAKE_CODEX_REPORT")
requests = []

def emit(payload):
    text = json.dumps(payload)
    print(text, flush=True)

mode = os.environ.get("FAKE_CODEX_MODELS_MODE", "ok")

if mode == "hang":
    time.sleep(30)
    sys.exit(0)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    requests.append(msg)
    if report:
        with open(report, "w", encoding="utf-8") as fh:
            json.dump({"argv": argv, "cwd": os.getcwd(), "requests": requests}, fh)
    method = msg.get("method")
    if method == "initialize":
        emit({"id": msg["id"], "result": {"userAgent": "fake-codex"}})
        continue
    if method == "initialized":
        continue
    if method == "model/list":
        params = msg.get("params") or {}
        if mode == "rpc_error":
            emit({"id": msg["id"], "error": {"code": -32000,
                  "message": "Authorization: Bearer sk-leakedtok987"}})
            continue
        if mode == "bad_page":
            result = {"data": "not-a-list"}
        elif mode == "bad_cursor":
            result = {"data": [], "nextCursor": 17}
        elif mode == "malformed":
            result = {"data": [
                {"id": "gpt-good", "displayName": "Good", "isDefault": True},
                "not-a-mapping",
                {"displayName": "missing id"},
            ], "nextCursor": None}
        elif mode == "cursor_loop":
            result = {"data": [{"id": "gpt-a"}], "nextCursor": "again"}
        elif params.get("cursor") == "page2":
            result = {"data": [{"id": "gpt-b", "displayName": "B", "isDefault": False,
                                "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}],
                      "nextCursor": None}
        else:
            result = {"data": [{"id": "gpt-a", "model": "gpt-a", "displayName": "A",
                                "isDefault": True, "hidden": False,
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "medium"},
                                    {"reasoningEffort": "xhigh"},
                                ]}],
                      "nextCursor": "page2"}
        emit({"id": msg["id"], "result": result})
        continue
    emit({"id": msg["id"], "error": {"code": -32601, "message": "unknown method"}})

if report:
    with open(report, "w", encoding="utf-8") as fh:
        json.dump({"argv": argv, "cwd": os.getcwd(), "requests": requests}, fh)
sys.exit(0)
"""


def script_file(tmp_path, name, source):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def agy_adapter(tmp_path, script, env=None, env_config=None):
    return AgyAdapter(
        AgentConfig(name="antigravity", protocol="agy", command=[sys.executable, str(script)], env=env or {}),
        tmp_path / "home",
        env_config,
    )


def codex_adapter(tmp_path, monkeypatch, script, overrides=None):
    monkeypatch.setattr(
        "agent_bridge.adapters.codex.resolve_codex_command", lambda *args, **kwargs: [sys.executable, str(script)]
    )
    return CodexAdapter(
        AgentConfig(name="codex", protocol="codex", command=["unused"], config_overrides=overrides or []),
        tmp_path / "home",
    )


def test_agy_verified_tsv_format():
    result = parse_agy_models_output("\x1b[32mmodel-a\tModel A\x1b[0m\nmodel-b\tModel B\n")
    assert result == [
        {"id": "model-a", "name": "Model A", "is_default": False, "supported_efforts": None},
        {"id": "model-b", "name": "Model B", "is_default": False, "supported_efforts": None},
    ]


@pytest.mark.parametrize(
    "text",
    [
        "ERROR",
        "Unauthorized",
        "Models:\nmodel-a\tModel A",
        '{"models":[{"id":"a"}]}',
        "- model-a",
        "model-a (default)",
        "",
        "model-a\t",
        "bad id\tName",
    ],
)
def test_agy_unverified_formats_and_diagnostics_are_not_catalogs(text):
    with pytest.raises(RuntimeError):
        parse_agy_models_output(text)


def test_codex_advertised_metadata_preserves_levels_and_unknowns():
    catalog = parse_codex_models(
        [
            {
                "id": "a",
                "displayName": "A",
                "isDefault": True,
                "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "xhigh"}],
            },
            {"id": "b", "supportedReasoningEfforts": []},
            {"id": "d"},
        ]
    )
    assert catalog["default_model"] == "a"
    assert [m["supported_efforts"] for m in catalog["models"]] == [["medium", "xhigh"], [], None]
    assert catalog["models"][0]["name"] == "A"
    assert catalog["warnings"] == []


def test_codex_malformed_effort_metadata_fails():
    with pytest.raises(KeyError, match="reasoningEffort"):
        parse_codex_models([{"id": "a", "supportedReasoningEfforts": [{"unexpected": "high"}]}])


def test_codex_preserves_advertised_rows():
    catalog = parse_codex_models([
        {"id": "a", "displayName": "First", "isDefault": True},
        {"id": "a", "displayName": "Second", "isDefault": True},
    ])
    assert [model["name"] for model in catalog["models"]] == ["First", "Second"]
    assert all(model["is_default"] for model in catalog["models"])


async def test_agy_adapter_env_cwd_and_catalog(tmp_path):
    script = script_file(tmp_path, "agy.py", FAKE_AGY)
    report = tmp_path / "report.json"
    adapter = agy_adapter(tmp_path, script, {"FAKE_AGY_REPORT": str(report), "AGY_MARKER": "yes"})
    result = await adapter.list_models(str(tmp_path))
    assert [m["id"] for m in result["models"]] == ["gemini-4-argon-low", "claude-sonnet-5-5-high"]
    assert result["default_model"] is None and result["warnings"] == []
    data = json.loads(report.read_text())
    assert data["cwd"] == str(tmp_path) and data["agent_env"] == "yes"
    assert data["worker_context"] == "worker"


async def test_agy_respects_env_deny(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCOVERY_SECRET", "secret")
    script = script_file(tmp_path, "agy.py", FAKE_AGY)
    report = tmp_path / "report.json"
    adapter = agy_adapter(tmp_path, script, {"FAKE_AGY_REPORT": str(report)}, EnvConfig(deny=["DISCOVERY_*"]))
    await adapter.list_models(str(tmp_path))
    assert not json.loads(report.read_text())["denied_present"]


async def test_agy_nonzero_exit_is_redacted_failure(tmp_path):
    script = script_file(tmp_path, "agy.py", FAKE_AGY)
    adapter = agy_adapter(tmp_path, script, {"FAKE_AGY_MODE": "exit3"})
    with pytest.raises(RuntimeError, match="exited with code 3") as exc:
        await adapter.list_models(str(tmp_path))
    assert "sk-testsecret123" not in str(exc.value)
    assert "apikeyhunter2" not in str(exc.value)


@pytest.mark.parametrize("timeout", [True, False])
async def test_agy_timeout_or_cancel_reaps_child(tmp_path, monkeypatch, timeout):
    script = script_file(tmp_path, "agy.py", FAKE_AGY)
    adapter = agy_adapter(tmp_path, script, {"FAKE_AGY_MODE": "hang"})
    started = asyncio.Event()
    children = []
    original = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        proc = await original(*args, **kwargs)
        children.append(proc)
        started.set()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(adapter.list_models(str(tmp_path)))
    await asyncio.wait_for(started.wait(), timeout=5)
    if timeout:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, 0.01)
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not psutil.pid_exists(children[0].pid)


async def test_agy_output_limit_fails_and_reaps(tmp_path, monkeypatch):
    script = script_file(tmp_path, "agy.py", 'print("x"*10000)')
    monkeypatch.setattr(model_discovery_exec, "AGY_OUTPUT_LIMIT", 100)
    with pytest.raises(RuntimeError, match="output limit"):
        await agy_adapter(tmp_path, script).list_models(str(tmp_path))


async def test_codex_pages_overrides_and_observed_defaults(tmp_path, monkeypatch):
    script = script_file(tmp_path, "codex.py", FAKE_CODEX)
    report = tmp_path / "report.json"
    monkeypatch.setenv("FAKE_CODEX_REPORT", str(report))
    override = 'model_provider="custom"'
    result = await codex_adapter(tmp_path, monkeypatch, script, [override]).list_models(str(tmp_path))
    assert [m["id"] for m in result["models"]] == ["gpt-a", "gpt-b"]
    assert result["default_model"] == "gpt-a"
    assert result["models"][0]["supported_efforts"] == ["medium", "xhigh"]
    data = json.loads(report.read_text())
    assert data["argv"] == ["-c", override, "app-server"]
    assert data["cwd"] == str(tmp_path)
    requests = [r for r in data["requests"] if r.get("method") == "model/list"]
    assert len(requests) == 2 and requests[1]["params"]["cursor"] == "page2"
    assert requests[0]["params"]["includeHidden"] is False


@pytest.mark.parametrize(
    "mode,match",
    [
        ("rpc_error", "discovery failed"),
        ("bad_page", "malformed"),
        ("bad_cursor", "malformed"),
        ("cursor_loop", "repeated"),
    ],
)
async def test_codex_invalid_responses_fail(tmp_path, mode, match):
    script = script_file(tmp_path, "codex.py", FAKE_CODEX)
    with pytest.raises(RuntimeError, match=match) as exc:
        await codex_app_server_list_models(
            [sys.executable, str(script), "app-server"],
            env={**os.environ, "FAKE_CODEX_MODELS_MODE": mode},
            cwd=str(tmp_path),
        )
    assert "sk-leakedtok987" not in str(exc.value)
