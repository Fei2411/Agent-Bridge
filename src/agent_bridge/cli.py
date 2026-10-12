from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from agent_bridge.paths import bridge_home, bundled_skill, ensure_home
from agent_bridge.persist import atomic_write_text
from agent_bridge.processes import count_sibling_servers
from agent_bridge.worker_env import is_worker_context

log = logging.getLogger(__name__)

GIT_SOURCE = "git+https://github.com/Fei2411/Agent-Bridge.git"
PACKAGE = "agent-bridge"

SKILL_RELATIVE = (
    Path(".agents") / "skills" / "agent-bridge" / "SKILL.md",
    Path(".cursor") / "skills" / "agent-bridge" / "SKILL.md",
    Path(".codex") / "skills" / "agent-bridge" / "SKILL.md",
    Path(".kimi-code") / "skills" / "agent-bridge" / "SKILL.md",
    Path(".zcode") / "skills" / "agent-bridge" / "SKILL.md",
    Path(".claude") / "skills" / "agent-bridge" / "SKILL.md",
)

HELP = f"""Agent Bridge — connect local coding agents.

Usage:
  agent-bridge                 start the MCP server on stdio
  agent-bridge --env           print reconstructed worker environment
  agent-bridge --quota         print each worker's remaining quota (fresh, no cache)
  agent-bridge --version       print the installed version
  agent-bridge upgrade         one-command update (close coordinators first)
  agent-bridge install-skill   copy the coordinator skill into host skill dirs
  agent-bridge help            this text

Install:
  uv tool install {GIT_SOURCE}

Then register the MCP server in Codex, Cursor, Kimi Code, ZCode, Grok
Build, or Claude Code (Devin reads the Cursor entry). Skill files are written when a top-level
coordinator first starts the server and refreshed after each upgrade;
ordinary restarts leave your local edits alone.

Update:
  agent-bridge upgrade
"""


def package_version() -> str:
    try:
        return version(PACKAGE)
    except PackageNotFoundError:
        return "0+unknown"


def skill_destinations(home: Path) -> list[Path]:
    return [home / rel for rel in SKILL_RELATIVE]


def detect_install(
    *,
    executable: Path | None = None,
    start: Path | None = None,
) -> str:
    """Return 'uv-tool', 'checkout', or 'unknown'."""
    exe = Path(executable or sys.executable).resolve()
    parts = [part.lower() for part in exe.parts]
    try:
        index = parts.index("tools")
    except ValueError:
        index = -1
    if index >= 0 and "uv" in parts[:index] and PACKAGE in parts[index:]:
        return "uv-tool"

    here = Path(start or __file__).resolve()
    for parent in here.parents:
        pyproject = parent / "pyproject.toml"
        if not pyproject.is_file() or not (parent / ".git").exists():
            continue
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            continue
        if f'name = "{PACKAGE}"' in text:
            return "checkout"
    return "unknown"


def checkout_root(start: Path | None = None) -> Path | None:
    here = Path(start or __file__).resolve()
    for parent in here.parents:
        pyproject = parent / "pyproject.toml"
        if pyproject.is_file() and (parent / ".git").exists():
            try:
                if f'name = "{PACKAGE}"' in pyproject.read_text(encoding="utf-8"):
                    return parent
            except OSError:
                continue
    return None


def skill_stamp_path(home: Path | None = None) -> Path:
    root = bridge_home() if home is None else home / ".agent-bridge"
    return root / "skill.sha256"


def ensure_coordinator_skill(*, home: Path | None = None) -> list[Path]:
    """Drop the skill into host skill dirs. Fail-open; skip under pytest.

    Nested Bridge processes inherited by a worker must not overwrite host
    skills. ``agent-bridge install-skill`` still writes them on purpose.
    """
    if is_worker_context():
        return []
    if home is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return []
    if os.environ.get("AGENT_BRIDGE_SKIP_SKILL") == "1":
        return []
    try:
        text = bundled_skill().read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        stamp = skill_stamp_path(home)
        try:
            current = stamp.read_text(encoding="utf-8").strip()
        except OSError:
            current = ""
        if current == digest:
            return []
        written = install_skill(home=home)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(stamp, digest + "\n")
        return written
    except OSError:
        log.warning("could not install coordinator skill", exc_info=True)
        return []


def install_skill(*, home: Path | None = None, only_existing: bool = False) -> list[Path]:
    source = bundled_skill()
    if not source.is_file():
        raise FileNotFoundError(f"bundled skill not found at {source}")
    content = source.read_bytes()
    written: list[Path] = []
    for dest in skill_destinations(home or Path.home()):
        if only_existing and not dest.is_file():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        written.append(dest)
    return written


def upgrade(
    *,
    kind: str | None = None,
    siblings: int | None = None,
    run: Callable[..., Any] | None = None,
) -> list[str]:
    live = count_sibling_servers() if siblings is None else siblings
    if live:
        raise RuntimeError(
            f"{live} Agent Bridge instance(s) are still running; close every "
            "coordinator that is holding a Bridge connection, then retry"
        )
    detected = kind or detect_install()
    runner = run or subprocess.run
    notes: list[str] = []
    if detected == "uv-tool":
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError("uv is not on PATH; install uv, then retry")
        completed = runner([uv, "tool", "upgrade", PACKAGE], check=False)
        if getattr(completed, "returncode", 0) not in (0, None):
            raise RuntimeError("uv tool upgrade agent-bridge failed")
        notes.append("uv tool upgrade agent-bridge")
    elif detected == "checkout":
        root = checkout_root()
        if root is None:
            raise RuntimeError("could not find the Agent Bridge git checkout")
        git = shutil.which("git")
        uv = shutil.which("uv")
        if not git or not uv:
            raise RuntimeError("git and uv must be on PATH to upgrade a checkout")
        for command in ([git, "pull"], [uv, "sync", "--extra", "dev"]):
            completed = runner(command, cwd=str(root), check=False)
            if getattr(completed, "returncode", 0) not in (0, None):
                raise RuntimeError(f"{' '.join(command)} failed in {root}")
        notes.append(f"git pull && uv sync --extra dev in {root}")
    else:
        raise RuntimeError(
            "this copy is neither a uv tool install nor a git checkout. "
            f"Install with: uv tool install {GIT_SOURCE}"
        )
    refreshed = install_skill(only_existing=True)
    if refreshed:
        notes.append("refreshed " + ", ".join(str(path) for path in refreshed))
    return notes


def main(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        from agent_bridge.logging_setup import setup_logging
        from agent_bridge.server import mcp

        setup_logging(ensure_home())
        ensure_coordinator_skill()
        mcp.run(transport="stdio")
        return

    command = args[0]
    if command in {"-h", "--help", "help"}:
        print(HELP, end="")
        return
    if command in {"--version", "-V", "version"}:
        print(package_version())
        return
    if command in {"--env", "env", "--print-env"}:
        from agent_bridge.config import load_config
        from agent_bridge.worker_env import describe_env

        print(json.dumps(describe_env(load_config().env), indent=2, ensure_ascii=False))
        return
    if command in {"--quota", "quota"}:
        import asyncio

        from agent_bridge.config import load_config
        from agent_bridge.quota import describe_quotas

        print(json.dumps(asyncio.run(describe_quotas(load_config())), indent=2, ensure_ascii=False))
        return
    if command in {"install-skill", "--install-skill"}:
        written = install_skill()
        for path in written:
            print(path)
        return
    if command in {"upgrade", "--upgrade"}:
        for note in upgrade():
            print(note)
        print("restart each coordinator so it reconnects")
        return
    raise SystemExit(f"unknown command: {command}\n{HELP}")
