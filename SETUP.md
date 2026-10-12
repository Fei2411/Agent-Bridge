# Coordinator setup for Agent Bridge

Codex, Cursor, Kimi Code, ZCode, Grok Build, Claude Code, and Devin can all act as the coordinator. Register the same stdio server in whichever host you use. Devin Desktop needs no entry of its own: Devin imports `~/.cursor/mcp.json` and the Cursor / Claude skill directories (`read_config_from.cursor`, on by default; `devin mcp list` shows what it picked up), so a Cursor setup is enough. The same product can also be a worker (Grok Build, Claude Code, Codex CLI, and Devin CLI are); those are different processes and different session roles.

## Install

```powershell
uv tool install git+https://github.com/Fei2411/Agent-Bridge.git
```

That is the whole install. Then register the MCP server in the coordinator. The first time a top-level host starts Bridge, the coordinator skill is written automatically (skipped if Bridge was inherited inside a worker); later starts rewrite it only after the bundled skill changed, so local edits survive restarts but not upgrades. Need [uv](https://docs.astral.sh/uv/) first.

## Resolve the Agent Bridge executable

Hosts must launch a real executable. A PowerShell function named `agent-bridge` is not resolved by ZCode or Grok child processes.

```powershell
Get-Command agent-bridge | Select-Object -ExpandProperty Source
```

If that prints nothing, install first (see above). Paste the printed path into the host config. Do not invent a home-directory path.

A clone is only for people changing Bridge itself. Pointing MCP at `uv --directory … run --no-sync agent-bridge` still works; `--no-sync` is required on Windows because a live instance locks `.venv\Scripts\agent-bridge.exe`.

## Register the MCP server (Codex)

From a trusted project, or edit `%USERPROFILE%\.codex\config.toml`:

```powershell
codex mcp add agent_bridge -- %USERPROFILE%\.local\bin\agent-bridge.exe
```

Use the full path so Codex does not have to inherit `PATH`. Then add the tuning keys. Codex **clears** the MCP child environment, so list every variable workers need:

```toml
[mcp_servers.agent_bridge]
command = "C:\\Users\\YOU\\.local\\bin\\agent-bridge.exe"
startup_timeout_sec = 30
tool_timeout_sec = 600
supports_parallel_tool_calls = true
default_tools_approval_mode = "approve"
env_vars = [
  "HTTP_PROXY",
  "HTTPS_PROXY",
  "ALL_PROXY",
  "NO_PROXY",
  "AGENT_BRIDGE_HTTP_PROXY",
  "DSH_HOME",
  "SSL_CERT_FILE",
]
```

The `command` above is the `uv tool` shim. If `uv tool dir --bin` is not `%USERPROFILE%\.local\bin`, use that directory instead.

DSH does **not** require `DEEPSEEK_API_KEY`. It uses whatever provider the user already configured in DSH (`%USERPROFILE%\.dsh\settings.yaml` and `.credentials.yaml`). Add extra key names to `env_vars` / `[env.inherit]` only if that user's DSH `apiKeyEnv` points at a process environment variable instead of the credentials file.

Current `dsh` releases serve ACP through the built-in `dsh --profile acp` profile. Bridge prefers that profile when `@deepseek-ai/dsh-acp-app` is present in the installed dsh package. Older dsh releases can use the published `@deepseek-ai/dsh-acp-demo` package. Discovery order: `DSH_ACP_BIN`, native dsh ACP, PATH `dsh-acp-demo`, the user's npm global prefix, `$AGENT_BRIDGE_HOME/dsh-acp`, then a *built* `$DSH_HARNESS` checkout.

```powershell
npm install -g @deepseek-ai/dsh-acp-demo
# or, without writing the global prefix:
.\.venv\Scripts\python.exe scripts\install_dsh_acp.py
```

The helper writes `$AGENT_BRIDGE_HOME/dsh-acp` (default `~/.agent-bridge/dsh-acp`) and installs the ACP peers the cordis file imports. Bridge copies that cordis file next to the chosen `node_modules` so ESM can resolve plugins. An unbuilt checkout `src/bin.ts` is ignored unless `tsx` is installed. DSH persistence is `$AGENT_BRIDGE_HOME/dsh-sessions/<session_id>` so a user project does not get `./.sessions`. `get_result.files_changed` is a turn-scoped workspace diff, not only ACP tool_call events (DSH often sends none). It is capped at 200 paths; `files_changed_total` / `files_changed_truncated` tell you when to fall back to `git status`. `files_changed_state` is `pending` until that diff runs, `collected` when an empty list is a confirmed zero, and `unavailable` if collection could not run. `files_changed` lists only paths inside the task `cwd`; worker-reported paths outside it land in `files_outside_cwd` (absolute, capped the same way, `files_outside_cwd_total`). `.agent-bridge` and Windows junctions are not scanned.

Restart Codex after editing `config.toml`.

Inspect what Bridge reconstructed:

```powershell
agent-bridge --env
```

## Register the MCP server (Cursor)

`%USERPROFILE%\.cursor\mcp.json` applies to every project; `<repo>\.cursor\mcp.json` applies to one repo:

```json
{
  "mcpServers": {
    "agent-bridge": {
      "command": "C:/Users/YOU/.local/bin/agent-bridge.exe"
    }
  }
}
```

- Use the same full path `uv tool` installed. If you still launch from a checkout, keep `--no-sync` (see Install).
- Cursor usually picks up `mcp.json` edits without a restart, though the docs still recommend restarting after changes. If the server gets stuck in an error state after a failed spawn, rename the server key — a new identity forces a fresh connection; a full Cursor restart also works.
- Cursor forwards more of the desktop environment than Codex, but Bridge rebuilds proxy/env itself either way; `agents.toml [env]` still applies.
- Cursor's MCP tool timeout is around one minute (not configurable like Codex `tool_timeout_sec`). From Cursor, call `wait_task` with `timeout_sec` ≈ 45 and loop; the default 180 gets killed by the host first.

## Register the MCP server (Kimi Code)

`%USERPROFILE%\.kimi-code\mcp.json` applies to every project; `<repo>\.kimi-code\mcp.json` applies to one repo:

```json
{
  "mcpServers": {
    "agent-bridge": {
      "command": "C:/Users/YOU/.local/bin/agent-bridge.exe",
      "toolTimeoutMs": 600000,
      "startupTimeoutMs": 60000
    }
  }
}
```

- Kimi Code's default single-tool-call timeout is 60 s, so a default `wait_task(timeout_sec=180)` gets killed by the host. `toolTimeoutMs` is the per-server override; `[mcp] tool_timeout_ms` in `config.toml` or `KIMI_MCP_TOOL_TIMEOUT_MS` moves the global default. With the value above, `wait_task` behaves like it does under Codex; without it, pass ≈ 45 and loop.
- Kimi asks for approval per MCP tool call unless the run is in YOLO mode. To pre-approve Bridge only, add to `%USERPROFILE%\.kimi-code\config.toml`:

```toml
[[permission.rules]]
decision = "allow"
pattern = "mcp__agent-bridge__*"
```

- A project-level `.kimi-code/mcp.json` only activates after you trust the folder at the workspace trust prompt.
- Servers added mid-session do not join open sessions. Restart `kimi` after editing `mcp.json`.

## Register the MCP server (ZCode)

ZCode has **three** JSON shapes. Do not mix them.

1. Settings → MCP → 新建 → **完整配置** accepts a bare server map:

```json
{
  "agent_bridge": {
    "type": "stdio",
    "command": "C:\\ABSOLUTE\\PATH\\agent-bridge.exe",
    "args": [],
    "timeoutMs": 600000
  }
}
```

2. The same dialog also accepts an `mcpServers` wrapper:

```json
{
  "mcpServers": {
    "agent_bridge": {
      "type": "stdio",
      "command": "C:\\ABSOLUTE\\PATH\\agent-bridge.exe",
      "args": [],
      "timeoutMs": 600000
    }
  }
}
```

3. The on-disk native file is a different object. Do **not** paste the dialog JSON into this file unchanged:

```json
{
  "mcp": {
    "servers": {
      "agent_bridge": {
        "type": "stdio",
        "command": "C:\\ABSOLUTE\\PATH\\agent-bridge.exe",
        "args": [],
        "timeoutMs": 600000
      }
    }
  }
}
```

- User file: `%USERPROFILE%\.zcode\cli\config.json` (`mcp.servers`).
- Workspace file: `<project>\.zcode\config.json` (`mcp.servers`).
- `timeoutMs` is milliseconds. 600000 leaves room for `wait_task` default 180 s. Without it, poll about 15–20 s.
- If a `.zcode` file in the **same scope** already lists any MCP server, ZCode skips that scope's `.agents/mcp.json` entirely. The two sources are not merged.
- After saving, confirm the server is enabled under **Settings → MCP 服务器** (configured-MCP group). Agent Bridge is a hand-added stdio server, not a marketplace plugin — it will not appear under 设置 → 插件 / 插件市场. Official: [MCP](https://zcode.z.ai/cn/docs/mcp-services), [Plugin](https://zcode.z.ai/cn/docs/plugin), [Skill](https://zcode.z.ai/en/docs/skill).
- Native user skills live in `%USERPROFILE%\.zcode\skills`. Bridge writes `agent-bridge` there on first top-level start.

## Register the MCP server (Grok Build)

Project file `<project>\.grok\config.toml` or user file `%USERPROFILE%\.grok\config.toml`:

```toml
[mcp_servers.agent_bridge]
command = 'C:\ABSOLUTE\PATH\agent-bridge.exe'
args = []
enabled = true
startup_timeout_sec = 30
tool_timeout_sec = 600

[[permission.rules]]
action = "allow"
tool = "mcp"
pattern = "agent_bridge__*"
```

- `startup_timeout_sec` / `tool_timeout_sec` are seconds. Official Grok default for tools is 6000; 600 is the Agent Bridge recommendation so `wait_task` default 180 s has headroom. If the host still kills the call, poll about 30–45 s.
- Permission rules use the official verbose `[[permission.rules]]` form. MCP tools are named `{server}__{tool}` (for example `agent_bridge__dispatch_task`).
- Project files may contain MCP servers and permission rules. `[ui] permission_mode` is a user-level setting — do not put it in a project example.
- Grok also discovers the coordinator skill from `%USERPROFILE%\.agents\skills` (already installed). It reads `~/.grok/skills` too; Bridge does not need a second copy. Official: [MCP](https://docs.x.ai/build/features/mcp-servers), [permissions](https://docs.x.ai/build/features/permissions), [skills](https://docs.x.ai/build/features/skills-plugins-marketplaces).
- Grok also merges `~/.cursor/mcp.json` and project `.cursor/mcp.json` below `config.toml` ([compat](https://docs.x.ai/build/features/mcp-servers)). That second server is usually named `agent-bridge` (hyphen). To test the native `[mcp_servers.agent_bridge]` block only, set `[compat.cursor] mcps = false` (and `[compat.claude] mcps = false` if needed). `grok inspect` shows each server's origin.
- Grok as a **worker** is a different process. If that worker inherits this MCP config, the nested Bridge has dispatch, preference updates, cancel, and session shutdown disabled: `dispatch_enabled=false`, and nested `dispatch_task` / `set_preferences` / `cancel_task` / `end_session` are rejected. It also uses a `nested/` subdirectory of `AGENT_BRIDGE_HOME` so it cannot share the coordinator's `state.json`. A host that env-clears the MCP child drops both the worker-context mark and that home override; Grok's stdio MCP does not env-clear.

CLI checks:

```powershell
grok mcp list
grok mcp doctor agent_bridge
grok inspect
```

CLI add (still use the resolved absolute exe):

```powershell
grok mcp add agent_bridge -- "C:\ABSOLUTE\PATH\agent-bridge.exe"
```

## Register the MCP server (Claude Code)

Claude Code has **two** on-disk files. Do not put `mcpServers` in `settings.json`.

1. Project file `<project>\.mcp.json` (shared with the repo; this is what `scripts/setup_lab.py` writes):

```json
{
  "mcpServers": {
    "agent-bridge": {
      "type": "stdio",
      "command": "C:/ABSOLUTE/PATH/agent-bridge.exe",
      "args": [],
      "timeout": 600000
    }
  }
}
```

2. User file `%USERPROFILE%\.claude.json` top-level `mcpServers` (all projects). Local scope (the `claude mcp add` default) nests the same object under that project's path in `~/.claude.json` — it is not the project file above.

CLI add, still with the resolved absolute exe:

```powershell
claude mcp add --scope project --transport stdio agent-bridge -- "C:\ABSOLUTE\PATH\agent-bridge.exe"
```

Use `--scope user` for every project. After adding, put `"timeout": 600000` on the server entry (milliseconds). Official: [MCP](https://code.claude.com/docs/en/mcp), [MCP servers](https://code.claude.com/docs/en/mcp-servers).

- `timeout` is milliseconds. 600000 leaves room for `wait_task` default 180 s. The CLI default for an unset `MCP_TOOL_TIMEOUT` is about 28 hours, so 180 s usually survives without the field; set it anyway so the budget is explicit. If the host still kills the call (the desktop app has historically ignored this and died around 60 s), poll about 45 s.
- Startup wait is `MCP_TIMEOUT` (milliseconds, default 30 s). 30 s is enough for Bridge.
- To pre-approve Bridge tools only, add to `<project>\.claude\settings.json` or `%USERPROFILE%\.claude\settings.json`:

```json
{
  "permissions": {
    "allow": ["mcp__agent-bridge__*"]
  }
}
```

MCP tools are named `mcp__{server}__{tool}` (for example `mcp__agent-bridge__dispatch_task`). Do not put `permissions.defaultMode` in a project example — that is a user-scoped preference.
- Native user skills live in `%USERPROFILE%\.claude\skills`. Bridge writes `agent-bridge` there on first top-level start. Claude Code does **not** read `~/.agents/skills`.
- Rules file: copy [ORCHESTRATION.md](ORCHESTRATION.md) as `CLAUDE.md` at the project root (native). Claude Code may also read `AGENTS.md`; CLAUDE.md is the one to use when you have a choice.
- Claude Code stdio MCP inherits the process environment (no Codex-style env-clear). Still use the resolved absolute `command`.
- Claude Code as a **worker** is a different process (`claude-agent-acp`, not the product TUI). If that worker inherits this MCP config from the project `.mcp.json`, the nested Bridge has dispatch, preference updates, cancel, and session shutdown disabled.

CLI checks:

```powershell
claude mcp list
```

Live coordinator loop (product `claude` as host, OpenCode as worker): `uv run python scripts/smoke_claude_coordinator.py`. That script uses `lab/`, `--mcp-config`, and denies Write/Edit/Bash so a pass cannot be the coordinator writing the file.

## Environment and proxy

Worker CLIs (Grok, Kimi, DSH, agy, OpenCode, Claude Code, Codex, Devin, ZCode, MiniMax Code) read API keys — and, on machines that need one, `HTTPS_PROXY` — from **their** process environment. Two things strip that:

1. Codex env-clears the MCP server.
2. Bridge launches `grok.exe` / `kimi` / `agy` / `opencode` / `claude-agent-acp` directly, so PowerShell functions that wrap those CLIs never run.

Bridge rebuilds the environment **once at startup** (and again for each worker spawn) in this order, later wins:

| Layer | Source |
| --- | --- |
| Discovery | PowerShell `grok` / `opencode` wrappers, Windows system proxy (`discover_proxy`) |
| Inherit | Windows user then machine env, keys in `[env.inherit]` |
| Process | Whatever Codex still forwarded |
| `[env.proxy]` | `url` / `no_proxy` in `agents.toml` |
| `[env.set]` | Explicit key/value map |
| `[agents.<name>.env]` | Per-worker overlay |

`[env] deny = ["GLOB", ...]` removes worker env keys matching those globs. Keys set explicitly via `[env] set` or the agent's `env` always survive, and Bridge's own `AGENT_BRIDGE_HOME` and worker-context marker are never removed. There is no built-in denylist.

Do **not** put secrets in the repo `agents.toml`. Pin machine-local values in `%USERPROFILE%\.agent-bridge\agents.toml` (see [agents.toml.example](agents.toml.example)):

```toml
[env.proxy]
url = "http://127.0.0.1:7897"
no_proxy = "localhost,127.0.0.1,::1"
```

`list_agents` returns an `env` object (`proxy`, `proxy_source`, `present`, `missing`, `warnings`). A null `env.proxy` is normal on a direct network. Behind a firewall, configure `[env.proxy]`; Grok talking to `cli-chat-proxy.grok.com` is usually the first casualty.

## Update

One command, after you close every coordinator that is holding a Bridge instance (Windows will lock the tool exe while one is running):

```powershell
agent-bridge upgrade
```

- `uv tool` install → `uv tool upgrade agent-bridge`, then refresh skill copies that already exist.
- git checkout → `git pull` and `uv sync --extra dev` in that checkout, then the same skill refresh.
- Anything else → the command tells you to `uv tool install git+https://github.com/Fei2411/Agent-Bridge.git`.

Restart Codex / Cursor / Kimi Code / ZCode / Grok Build / Claude Code so they reconnect and pick up new tools and the MCP handshake instructions.

What you do **not** redo: `codex mcp add` / `mcp.json`, `%USERPROFILE%\.agent-bridge\agents.toml` (proxy, `[coordinator]`, `set_preferences`), or worker CLIs and their logins. If you copied `ORCHESTRATION.md` into another repo as `AGENTS.md`, that copy is still yours to refresh.

Already on a clone and want the tool install instead: `uv tool install git+https://github.com/Fei2411/Agent-Bridge.git`, point each host `command` at `agent-bridge`, then delete the clone. The overlay in `~/.agent-bridge` stays.

## Multiple coordinators on one machine

Running Codex, Cursor, Kimi Code, ZCode, Grok Build, and Claude Code coordinators at the same time is supported: each host spawns its own Bridge process, and `list_agents` merely counts independent siblings (not a nested Bridge inside a worker this instance started). A `uv tool` install is shared and does not sync on spawn.

What must not run twice is the **installer** of a git checkout. A plain `uv run` syncs the project before executing, and that sync rewrites `.venv\Scripts\agent-bridge.exe` — on Windows a file every running instance holds open. The second host's spawn then dies before the MCP handshake with:

```text
error: failed to remove file `...\.venv\Lib\site-packages\../../Scripts/agent-bridge.exe` (os error 32)
```

If you still launch from a checkout, use `uv --directory … run --no-sync agent-bridge` or `.venv/Scripts/python.exe -m agent_bridge`. POSIX hosts can replace a running binary, so this failure is Windows-only. `agent-bridge upgrade` / `uv tool upgrade` also need the coordinators closed first — same lock.

Instances share the `~/.agent-bridge` state directory but not sessions: every session and task record carries the identity of the Bridge instance that owns it. `list_sessions` shows only the calling instance's records, saves leave a live sibling's records untouched on disk, and records whose owning instance has exited are adopted at the next boot — their in-flight tasks surface as `failed` / `bridge_restarted`. A session started from one host is continued from that host; it does not appear in another host's `list_sessions` while its owner is alive. `check_task` / `get_result` on a task owned by another instance answer from `state.json` (`from_state: true`, `silent_for_sec: null`); `wait_task` / `cancel_task` still require the owning instance.

## Server lifecycle

Bridge stays available while its host keeps the stdio connection open. `server.idle_exit_sec` defaults to `0` (disabled); EOF ends the connection and runs the normal worker and state cleanup. Worker idle unloading remains controlled separately by each agent's `idle_unload_sec`.

A positive `idle_exit_sec` opts into server shutdown after that many seconds with no MCP tool calls and no queued or running tasks. Configure it in `[server]` (repo `agents.toml` or `%USERPROFILE%\.agent-bridge\agents.toml`) only when the host can reliably reconnect; explicit existing values remain effective. Idle time cannot distinguish an abandoned thread from one the user will continue later. `list_agents` reports other Bridge instances, but multiple coordinator threads may legitimately own separate instances.

## ACP failure diagnostics

`wait_task`, `check_task`, and `get_result` retain the string `error` and include an optional `failure` object for ACP failures:

- `operation`: the failed operation, such as `process_start`, `initialize`, `model_discovery`, `session/new`, `session/load`, `session/resume`, `session/configure`, `session/set_config_option model`, or `session/prompt`.
- `kind`: `auth_required`, `unsupported_model`, `timeout`, `connection_closed`, `worker_exit`, `protocol_error`, or `unknown`. Classification uses protocol codes and observed facts, not guesses from stderr.
- `exit_code`: the observed exit code of the failing process, or `null`. During model discovery this belongs to the discovery command. An exit caused by Bridge's timeout cleanup is not substituted for the original failure.
- `prompt_may_have_been_sent`: `false` only when this turn has not entered the prompt send operation; `true` means delivery or execution may already have occurred, not that delivery was acknowledged.
- `stderr_summary`: recognized generic diagnostic phrases only, at most 2048 characters, or `null`. Raw stderr, arbitrary lines, credentials, URLs and local paths are not included. The human-readable `error` is also bounded and common credential fields are redacted.

`failure = null` means no structured evidence is available (including old records, other adapters and interrupted tasks restored after a Bridge restart), not that retrying is safe. Bridge does not automatically replay failed prompts. Even `false` does not prove initialization was side-effect-free. An uncertain dispatch response can be retried with its original `request_id`; replaying that ID retrieves the same retained task, not a fresh execution of a failed task. `available` still describes command resolution, not authentication or session health.

## Remaining quota in `list_agents`

Every `list_agents` row carries a `quota` block so the coordinator can weigh how much plan a worker has left before dispatching — and so a worker that has run dry does not silently turn into "the coordinator does it itself" on a budget you never meant to spend:

```json
"quota": {
  "status": "ok",
  "windows": [
    {"name": "5h", "remaining_percent": 82.0, "resets_at": "2026-09-07T05:00:00+00:00", "resets_in_sec": 9120},
    {"name": "weekly", "remaining_percent": 61.5, "resets_at": "2026-09-11T00:00:00+00:00", "resets_in_sec": 331200}
  ],
  "balance": null,
  "plan": "plus",
  "source": "codex app-server account/rateLimits/read",
  "fetched_at": "2026-09-07T02:28:00+00:00",
  "cached": false,
  "stale": false,
  "detail": null
}
```

- `status` is `ok`, `exhausted` (an applicable window is fully used, or the account is blocked), or `unknown`. `unknown` means Bridge could not read the number — the reason is in `detail` — and never that the quota is empty. Quota does not change `available`, and Bridge does not route on it: the rulebook tells the coordinator to treat it as information next to your `[coordinator] instructions`.
- `windows[]` are the CLI's rolling limits; `balance` is optional provider-reported credit; `cached` means the reading was reused from the last lookup, `stale` that a fresh lookup failed and the last good reading is shown instead.
- Claude's `status` summarizes only its shared `5h` and `weekly` limits because `list_agents` has no target model. The `weekly:opus` and `weekly:sonnet` windows retain their values, including zero: the coordinator must check the requested model's window before dispatch. A depleted model alone does not exhaust the shared status; without a usable shared reading, status is `unknown`. An `ok` shared status does not guarantee that a particular model has quota.

Where each number comes from:

| Worker | Source | Needs |
| --- | --- | --- |
| Codex CLI | `codex app-server` → `account/rateLimits/read` (5h + weekly, plan, credits) | ChatGPT sign-in (`codex login`); API-key logins have no plan quota |
| Kimi Code | `GET https://api.kimi.com/coding/v1/usages` — the same call the interactive `/usage` makes | `kimi login` (OAuth); Moonshot API-key sessions answer `unknown` |
| DeepSeek Harness | `unknown` — balance lookup is not supported | DSH can use different providers and accounts; a DeepSeek key does not identify the active account |
| Grok Build | `GET cli-chat-proxy.grok.com/v1/billing` — what `/usage` calls; **experimental** | Default first-party xAI OAuth scope in `~/.grok/auth.json` (or `GROK_AUTH_PATH`); `[quota] experimental = true` |
| Claude Code | `GET api.anthropic.com/api/oauth/usage` — what `/usage` calls; **experimental** | `claude auth login` OAuth; `[quota] experimental = true` |
| Antigravity, Cursor, OpenCode, Devin, ZCode, MiniMax Code, others | — | always `unknown` with the reason in `detail` |

Custom API or OAuth endpoints are unsupported for quota lookup. Bridge checks each supported CLI's environment, selected provider/configuration and supported startup selectors before using either credentials or cached quota. Unreadable or unverified configuration returns `unknown`; ordinary HTTP(S)/ALL_PROXY transport proxies are supported. Kimi reads only the default `kimi-code.json` slot. Grok verifies the exact default OAuth scope, issuer, expiry and user ID, and sends the CLI's version and authentication headers. DSH balance remains unsupported.

Bridge reads credential files the CLIs already wrote; it never refreshes or rewrites them. Tune it in `[quota]` (repo `agents.toml` or `%USERPROFILE%\.agent-bridge\agents.toml`):

```toml
[quota]
enabled = true        # false: every row answers unknown, nothing is spawned
timeout_sec = 4       # one worker's lookup is cut off after this
cache_sec = 300       # maximum lifetime, capped at the earliest window reset
experimental = false  # also read Grok Build / Claude Code private endpoints
```

At a window reset, the next lookup re-reads the provider. If that refresh fails, the expired reading is not reused, even as stale data; the result is `unknown`.

A task that fails with a quota-looking error (`quota`, `rate limit`, `insufficient balance`, …) drops that worker's cached reading so the next `list_agents` re-reads it. `agent-bridge --quota` prints a fresh reading for every worker without the cache — use it when a row says `unknown` and you want to see why.

## Orchestration rules

The coordinator learns how to drive the Bridge through three channels; [ORCHESTRATION.md](ORCHESTRATION.md) is the source of truth for all of them ([ORCHESTRATION.zh-CN.md](ORCHESTRATION.zh-CN.md) is the human-readable translation):

1. **MCP instructions — automatic.** The server sends its hard rules (tools-only access, `cwd` semantics, timeout-is-not-failure, verify-yourself) in the MCP handshake. Nothing to install, but hosts vary in how prominently they surface it, so don't rely on it alone.
2. **Skill — automatic.** The first MCP start, any start after the bundled skill changed, and `agent-bridge upgrade` write [skills/agent-bridge/SKILL.md](skills/agent-bridge/SKILL.md) into the host skill directories. It loads on demand when the coordinator is about to dispatch.
3. **Rules file — fallback for hosts without skills.** Copy [ORCHESTRATION.md](ORCHESTRATION.md) to the target repository root as `AGENTS.md` (Codex, Cursor, Kimi Code, ZCode, and Grok Build all read it there) or as `CLAUDE.md` for Claude Code, or to `%USERPROFILE%\.codex\AGENTS.md` / `%USERPROFILE%\.kimi-code\AGENTS.md` / `%USERPROFILE%\.claude\CLAUDE.md` for a host-global default; the Cursor-global equivalent is User Rules in Cursor settings. Keep the English file under 9500 characters (Grok may copy it as `AGENTS.md`) and under 32 KiB — Codex concatenates the home file with per-directory `AGENTS.md` files from the git root down to cwd; Kimi Code does the same and warns past 32 KiB.

Your own project's `AGENTS.md` stays yours: if you use the skill or the MCP instructions, the rulebook takes no `AGENTS.md` slot at all.

## Coordinator mode and routing preferences

`[coordinator]` in `agents.toml` (repo or `%USERPROFILE%\.agent-bridge\agents.toml`) sets how eagerly the coordinator dispatches and carries your persistent routing preferences:

```toml
[coordinator]
mode = "auto"   # manual | auto | eager  (aliases: safe -> manual, yolo -> eager)
instructions = """Research goes to antigravity. Coding goes to grok.
Never dispatch test runs; run them yourself."""
```

- `manual` — the Bridge rejects `dispatch_task` unless the coordinator passes `user_requested=true`, which it may only do when you explicitly asked for a worker. Enforced server-side, not just prompted.
- `auto` (default) — the coordinator weighs dispatch overhead against doing the work itself.
- `eager` — the coordinator is told to prefer dispatching multi-step work; advisory, not enforced.

`instructions` is free text relayed verbatim through `list_agents`; the rulebook tells the coordinator these preferences override its default worker split. Per-host override: set `AGENT_BRIDGE_MODE=manual|auto|eager` in that host's MCP entry `env` block to give, say, Codex a different mode than Cursor.

You don't have to edit the file yourself: tell the coordinator in chat ("以后调研都给 antigravity" / "from now on, research goes to antigravity") and it calls the `set_preferences` tool. The Bridge rewrites only the `[coordinator]` section of `%USERPROFILE%\.agent-bridge\agents.toml` (the rest of the file, including comments, is preserved), applies the change to the running instance immediately, and other Bridge instances pick it up at their next start. A config file edited by hand is picked up at the next start too — `AGENT_BRIDGE_MODE` still outranks the file where it is set.

## End-to-end drill

The live workspace is a local `lab/` folder created by `scripts/setup_lab.py`. It is not in git, not `tests/` (pytest), and not a Temp folder.

```powershell
uv run --no-sync python scripts/setup_lab.py
```

1. Open the coordinator (Codex, Cursor, Kimi Code, ZCode, Grok Build, or Claude Code) on the `lab/` folder — not the Agent Bridge repo root.
2. Ask: `用 grok 在当前目录写一个 smoke.txt，内容 hello-bridge，做完后你自己 git diff 验收。` The coordinator must pass that folder as `dispatch_task.cwd` (not the Agent Bridge install path).
3. Confirm it calls `dispatch_task` → loops `wait_task` → `get_result` → inspects the diff. `wait_task` defaults to 180 seconds; a timeout is not failure.
4. Ask it to find a nit and send a follow-up on the same `session_id`.
5. Confirm the second turn does not start a new Grok session.

If a worker is missing, `list_agents` reports `available: false` and the coordinator should pick another worker or do the work itself.

The coordinator can pin a worker model and thinking intensity on `dispatch_task`:

```text
dispatch_task(agent="antigravity", model="gemini-3.7-flash", effort="low", ...)
```

`agy models` lists slugs such as `gemini-3.7-flash-low`. Either pass that full slug, or pass the family plus `effort=low|medium|high`. New agy sessions reuse the existing Antigravity project whose folder is the cwd via `--project <id>` (creating one with `--new-project` only if none exists), plus `--add-dir <cwd>`, so work stays in the requested repo, not `~\.gemini\antigravity-cli\scratch`. Grok accepts a `grok models` slug plus `effort=off|low|medium|high|max` (`off` maps to Grok `none`, `max` to Grok `xhigh`). Grok `/new` still starts on the campaign default (currently grok-4.6 xhigh); Bridge calls `session/setModel` after the session exists. Accept Grok model selection from `get_result.observed_model` (Grok `turn_started.model_id`), not from the worker quoting `You are Grok 4.6`. Kimi accepts one of the slugs its own session advertises (`kimi-code/k3`, `kimi-code/k3-256k`, `kimi-code/kimi-for-coding`, ...) plus `effort=off|low|medium|high|max`. OpenCode accepts a `provider/model` slug the session advertises plus the same five effort tokens mapped onto that model's variants. DSH accepts `model="deepseek-official/deepseek-v4-flash"` and `effort=low|high|max`. Changing DSH model/effort on an existing session respawns the process. Claude Code accepts a slug the session advertises (`sonnet`, `opus`, `haiku`, or a full id) plus `effort=off|low|medium|high|max` mapped onto that model's levels (`off` → `default`, `max` → `xhigh` unless the session lists `max`). Codex CLI accepts a Codex slug plus `effort=off|low|medium|high|max` (`off` → `none`). Bridge runs `codex exec --json` with the prompt on stdin and `--approve-for-me` by default. Devin CLI accepts a model id the session advertises (`swe-1-7-medium`, `claude-opus-5-high`, …); the level is part of the id and `effort` is ignored with a warning.

## Worker: Kimi Code

`kimi acp` is a first-class ACP server, so Bridge drives it exactly like Grok — no extra shim to install:

```powershell
npm install -g @moonshot-ai/kimi-code
kimi login
```

`kimi login` is not optional. Kimi's ACP host gates every `session/new` on auth and answers `auth_required` when no token is on disk; `list_agents` cannot see that, so it will still report `available: true`. On Windows Kimi's Bash tool needs Git Bash — set `KIMI_SHELL_PATH` if it is not in a standard location, and Bridge will pass it through.

Two behaviors worth knowing before you read a Kimi result:

- **Thinking levels are declared per model, not global.** Bridge maps `effort` onto whatever the selected model advertises. `kimi-code/k3-256k` only advertises `low` / `high` / `max`, so `medium` lands on `high` and `off` on `low`; a boolean model gets `off` / `on` instead. When nothing maps, `get_result.warnings` says so and the turn still runs on the model's own default.
- **A failed Kimi turn does not look failed.** Its ACP host maps a failed turn to `stopReason: end_turn` with empty text, so a quota or provider error is indistinguishable on the wire from a clean no-op. Bridge reads Kimi's `wire.jsonl` after every turn and puts the real reason in `get_result.warnings`, alongside `observed_model` / `observed_effort`. Never read an empty Kimi turn as success without checking there.

Bridge also forces the session into Kimi's `yolo` mode after creating it (Kimi starts in manual-approval `default` mode), and revives sessions with `session/resume` rather than `session/load`, because `load` replays the entire persisted history before it answers.

## Worker: OpenCode

`opencode acp` is a first-class ACP server:

```powershell
npm i -g opencode-ai@latest
opencode auth
```

OpenCode has **no product login**. You connect providers by storing API keys (or that provider's own oauth) with `opencode auth`. Official routes are OpenCode Zen and OpenCode Go; anything else is just another connected provider. `list_agents` reports the CLI as available when `opencode` is on PATH — a missing provider key only shows up when a turn actually talks to the model.

`dispatch_task.model` is a `provider/model` slug the live session advertises (`opencode/...`, `xai/...`, …). `effort` maps onto that model's variants (`default|low|medium|high` is common; Bridge `max` usually becomes `high`). An unknown slug fails the turn and lists the real options; a model with no variants, or an effort that will not map, comes back as a warning. `get_result.observed_model` / `observed_effort` are the last values Bridge successfully set on the session after that mapping, not a live sampler dump. Switching model on a live session re-applies effort: OpenCode resets the variant to the new model's default. Bridge revives with `session/resume` rather than `session/load`. Permissions are ACP `requestPermission`; Bridge answers per `[agents.<name>] permission_policy` — `allow_once` (default), `allow_always`, or `deny`. Under `allow_once` a prompt that offers only allow-always is denied.

If OpenCode's configured default model is disabled, the first prompt fails with `Model is disabled`. Pass a slug from `opencode models` for a provider that still has a working key.

## Worker: Codex CLI

Bridge drives Desktop-bundled or PATH `codex` through `codex exec --json`. It does not open ChatGPT.exe. Discovery order: `CODEX_CLI_PATH` (env or `$CODEX_HOME/config.toml` / `~/.codex/config.toml`), then `%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<hash>\\codex.exe` (newest mtime among binaries that pass `exec --help` capability checks), then PATH `codex`.

Login stays in `$CODEX_HOME` when set, otherwise `~/.codex`. `--ignore-user-config` skips Desktop `config.toml` (MCP / Computer Use), and Bridge adds `-c cli_auth_credentials_store="auto"` so ChatGPT login still resolves from keyring or `auth.json`. Project `.codex/config.toml` and project hooks/rules normally do not load, because Codex reads project trust from the user config this flag skips; `AGENTS.md` and rules under `$CODEX_HOME/rules` still apply. Bridge inherits `CODEX_HOME` so a custom home used by Desktop is visible to the worker.

`[agents.codex] config_overrides` adds raw `codex exec -c <item>` pairs — for settings `--ignore-user-config` would skip, e.g. a custom model provider. The block below merges field-wise into the bundled codex entry, so only `config_overrides` is needed:

```toml
[agents.codex]
config_overrides = [
  'model_provider="my-gateway"',
  'model_providers.my-gateway.name="My Gateway"',
  'model_providers.my-gateway.base_url="https://example.com/v1"',
  'model_providers.my-gateway.env_key="MY_GATEWAY_API_KEY"',
]
```

Keep secrets out of overrides: a key can only be referenced via `env_key` (command-line arguments are visible in the process list). Since Codex clears the MCP child environment, a variable named by `env_key` must also be listed in the host's `env_vars` / `[env.inherit]` like any other worker credential. Task-level `model`/`effort` are appended after overrides, so they keep priority.

Default approval is `--approve-for-me` (auto review + workspace-write). `--yolo` is only used when `[agents.codex] session_meta = { yolo = true }`. Prompt is UTF-8 stdin (`-`), never an argv string.

`dispatch_task.model` is a Codex slug (`gpt-5.6-sol`, …). `effort` maps `off` → `none`; `low|medium|high|max` pass through. Follow-ups reuse `exec resume <thread_id>`.

Config, login, or execpolicy failures that happen before JSONL starts return a bounded stderr tail in `get_result.error`. `get_transcript` keeps the upstream `turn.completed` payload as `raw` and records one normalized `turn_end` per completed turn.

## Worker: Claude Code

Product `claude` has **no** ACP profile. Bridge drives the published adapter:

```powershell
npm install -g @agentclientprotocol/claude-agent-acp @anthropic-ai/claude-code
claude auth login
```

`claude-agent-acp` is the worker binary (`@zed-industries/claude-agent-acp` / `claude-code-acp` is the older name; Bridge lists it as a fallback). `list_agents` reports available when that binary is on PATH — a missing login only fails on the first prompt. Auth is `claude auth login` on disk, `ANTHROPIC_API_KEY`, or a gateway:

```powershell
$env:ANTHROPIC_BASE_URL = "https://openrouter.ai/api"
$env:ANTHROPIC_AUTH_TOKEN = $env:OPENROUTER_API_KEY
$env:ANTHROPIC_API_KEY = ""
$env:CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY = "1"
```

If `OPENROUTER_API_KEY` is set and `ANTHROPIC_AUTH_TOKEN` is not, Bridge copies it and defaults the base URL to OpenRouter **only when** `ANTHROPIC_API_KEY` is also unset. A machine that uses OpenRouter for OpenCode and a direct Anthropic key for Claude keeps the Anthropic key. Set `ANTHROPIC_BASE_URL` yourself if you want that Anthropic key treated as a gateway conflict (Bridge then blanks it once a token and base URL are both present).

`dispatch_task.model` is a slug the live session advertises (`sonnet`, `opus`, `haiku`, or a full id). `effort` maps onto that model's levels (`default|low|medium|high|xhigh` is common; Bridge `off` → `default`, `max` → `xhigh` unless the session lists `max`). An unknown slug fails the turn and lists the real options; a model with no effort option, or an effort that will not map, comes back as a warning. `get_result.observed_model` / `observed_effort` are the last values Bridge successfully set after that mapping. Switching model on a live session re-applies effort. Bridge forces `bypassPermissions` after `session/new` (a fresh session starts in manual `default` mode); if that mode is not advertised — for example when the process is root — ACP `requestPermission` is still answered per `[agents.<name>] permission_policy` (`allow_once` default; a prompt offering only allow-always is denied). Revive uses `session/resume`: `session/load` replays the whole history.

## Worker: Devin CLI

`devin acp` is the Devin CLI's own ACP server — the same binary Devin Desktop, Zed and JetBrains launch. Install the standalone CLI (it puts `devin` on PATH; the copy bundled inside Devin Desktop is not) and log in once:

```powershell
irm https://static.devin.ai/cli/setup.ps1 | iex
devin auth login
```

Auth is `devin auth login` on disk or `WINDSURF_API_KEY`. `list_agents` runs `devin auth status` and reports its first line as `auth=`. Bridge drops `ACP_BACKEND` from the worker environment: Devin Desktop stamps that variable on every child process, and with it set the CLI trusts only credentials the ACP host passes in and refuses `session/new` — so a Devin Desktop coordinator can still dispatch to a `devin` worker.

`dispatch_task.model` is a model id the live session advertises (`devin models list`): the level is part of the id (`swe-1-7`, `swe-1-7-medium`, `claude-opus-5-high`), so there is no `effort` option — a Bridge `effort` is ignored with a warning. An unknown id fails the turn and lists the real options. Bridge forces `bypass` after `session/new` (a fresh session starts in `accept-edits`; `DEVIN_PERMISSION_MODE` is parsed but not applied to ACP sessions). Revive uses `session/load` — Devin has no `session/resume` — and Devin ignores `noReplay`, so the persisted history is replayed as `session/update` notifications before `load` answers. Bridge resets its turn state before prompting, so `get_result` and `files_changed` for the new turn are clean, but `get_transcript` will show the replayed history again after a revive; that is a side effect, not the way to fetch history. Mode and model survive the reload. A long session may take longer than the 60 s handshake timeout to replay; if that bites, lower `idle_unload_sec` for devin or open a new session.

## Worker: ZCode

Product `zcode` is not an ACP server. Bridge drives `zcode-acp-server` from npm package `zcode-acp-server` (the `zcode-acp server` subcommand is the fallback; bare `zcode-acp` is a terminal UI). The adapter launches `zcode app-server` itself. Install the adapter, install ZCode, and sign in once in the ZCode app:

```powershell
npm install -g zcode-acp-server
```

Login lives in `~/.zcode/v2` (`config.json` / `credentials.json`), or in `ZCODE_HOME`. If the ZCode CLI is not on `PATH`, set `ZCODE_BIN` to the bundled `zcode` or `zcode.cjs`. `list_agents` reports that login state in `auth=` and does not hide the worker when the login is missing.

`dispatch_task.model` is an advertised `provider\model` value, or a bare model id such as `GLM-5.3` when only one option has that id. A slash form `provider/model` is accepted too. An unknown or ambiguous id fails the turn and lists the real options. `effort` maps onto that model's thought levels (`low|high|max` is common; Bridge `off` → `low`, `medium` → `high` unless the session lists a closer value). A model with no thought option, or an effort that will not map, comes back as a warning. `get_result.observed_model` is the slug that matched; `observed_effort` is the thought level Bridge set. Switching model on a live session re-applies effort. Bridge forces mode `yolo` after `session/new` (the adapter default is already `yolo` unless `ZCODE_ACP_MODE` or the saved session mode says otherwise). Revive uses `session/resume`: `session/load` replays the whole history.

## Worker: MiniMax Code

`mcode acp` is MiniMax Code's own ACP server (npm `@minimax-ai/code`). Install it and log in once:

```powershell
npm install -g @minimax-ai/code
mcode login
```

Auth is `mcode login` under `~/.minimax` (`auth/<build>/<region>/auth.json`), or `MINIMAX_DATA_DIR` / `MAVIS_DATA_DIR`. `list_agents` reports `auth=` and still lists the worker when the login is missing — the probe only checks that `mcode` is on `PATH`.

`dispatch_task.model` is `provider/model` or `provider/model#variant`, the same shape as `mcode exec --model`. Bridge encodes that into the ACP value the session actually advertises. An unknown slug fails the turn and lists the real `provider/model` options. `effort` maps onto that model's `thinkingEffort` (`default|low|medium|high|xhigh|max` is common; Bridge `off` → `default`). A model with no effort option, or an effort that will not map, comes back as a warning. `get_result.observed_model` / `observed_effort` are the last values Bridge successfully set after that mapping. Switching model on a live session re-applies effort. Bridge forces `permissionMode=bypassPermissions` after `session/new` (a fresh process starts in `default`, which asks). That option is not the session mode — `session/set_mode` is only `default` / `plan`, and Bridge leaves it on `default`. Revive uses `session/resume`: `session/load` replays the whole history.

## Permissions

Most worker CLIs run in always-approve / skip-permissions mode. Codex CLI defaults to `--approve-for-me` (auto review + workspace-write); `--yolo` is opt-in via `[agents.codex] session_meta = { yolo = true }`. Review is the coordinator's job: `git diff`, build, tests. Cap follow-up turns at three, then the coordinator patches the rest.
