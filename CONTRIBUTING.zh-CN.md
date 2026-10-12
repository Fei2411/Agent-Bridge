# 参与贡献

[English](CONTRIBUTING.md)

## 环境

这是源码检出。最终用户用 `uv tool install git+https://github.com/Fei2411/Agent-Bridge.git` 安装，不需要这份仓库。

```powershell
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy
```

真实协调者联调只用本机 `lab/`（不进 git）。先跑 `uv run --no-sync python scripts/setup_lab.py`，再用宿主打开那个文件夹——不要开仓库根，也不要开 `tests/`。

## 接入新 Worker

接一个新 Agent CLI 有独立流程：[skills/add-worker/SKILL.md](skills/add-worker/SKILL.md)。普通 ACP CLI 通常一行代码都不用改，在 `~/.agent-bridge/agents.toml` 加一个块就行。要动 adapter 的 PR 请先读它。

## 接入新 Coordinator

证明一个新宿主能通过 MCP 调度现有 Worker 是另一套流程：[skills/add-coordinator/SKILL.md](skills/add-coordinator/SKILL.md)。那份 skill 不加 adapter，也不加 worker 配置项。声称 Codex、Cursor、Kimi Code、ZCode、Grok Build 或 Claude Code 已支持之前，先读它。

## Pull request

- 改动保持小而明确。
- 行为有变化时补测试。
- 不要提交密钥、`.env`、本机的 `~/.agent-bridge/agents.toml`，或带用户名的本机路径。
- 不要把 API key 写进 issue 或提交说明。

## 许可证

提交即表示同意以 [MIT License](LICENSE) 发布。
