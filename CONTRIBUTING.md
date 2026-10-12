# Contributing

[中文](CONTRIBUTING.zh-CN.md)

## Setup

This is the source checkout. End users install with `uv tool install git+https://github.com/Fei2411/Agent-Bridge.git` and do not need this repo.

```powershell
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy
```

Live coordinator drills use a local `lab/` folder (not in git). Run `uv run --no-sync python scripts/setup_lab.py`, then open that folder in the host — not the repo root and not `tests/`.

## Adding a worker

Connecting a new agent CLI has its own process: [skills/add-worker/SKILL.md](skills/add-worker/SKILL.md). A plain ACP CLI usually needs no code at all — just a block in `~/.agent-bridge/agents.toml`. Read it before opening a PR that touches the adapters.

## Adding a coordinator

Proving a new host can drive existing workers over MCP is a different process: [skills/add-coordinator/SKILL.md](skills/add-coordinator/SKILL.md). That skill does not add adapters or worker entries. Read it before claiming Codex, Cursor, Kimi Code, ZCode, Grok Build, or Claude Code support is complete.

## Pull requests

- Keep changes focused.
- Add or update tests when behavior changes.
- Do not commit secrets, `.env`, machine-local `~/.agent-bridge/agents.toml`, or personal paths.
- Do not put API keys in issues or commit messages.

## License

By contributing you agree the work is released under the [MIT License](LICENSE).
