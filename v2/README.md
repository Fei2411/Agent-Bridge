# Agent Bridge Hub — TypeScript V2

One independent Node.js process owns the Workers, sessions, task queues and
messages for all connected MCP hosts. Closing the host that submitted work does
not stop it. Another host can inspect the same task, continue its native session
or send it a message. Managed Workers can delegate and reply through the same
Hub, with their own bound identity.

This is **2.0.0-alpha.1**. ACP, Codex exec and Antigravity stream transports are
implemented and exercised with real subprocess protocol fixtures. OpenCode 1.19.5
has also been exercised on Linux with a local mock model and real CLI tools;
authenticated providers and the other product CLIs still need validation. See [MIGRATION.md](MIGRATION.md)
for compatibility gaps and the product verification procedure. The Python release
remains separately usable; V2 does not modify its state files.

## Install and start

Use **Node.js 24.x** and npm. Node may print the `node:sqlite` experimental warning
to stderr. From a checkout:

```sh
cd v2
npm ci
npm test
npm run smoke:package
node dist/cli.js start
node dist/cli.js status
```

`start` runs the Hub in the background. `hub` runs it in the foreground, optionally
with `--port PORT`. `mcp` (the default command) and `call` automatically connect to
or start the same Hub. `stop` explicitly stops the Hub and its Workers. Closing a
stdio proxy only closes that client.

The default profile is `~/.agent-bridge/hub-v2`. Select a different one with
`--home /absolute/path` or `AGENT_BRIDGE_HOME`. The profile contains the database,
endpoint discovery, credentials and background log. The server binds only to
`127.0.0.1`; a separate SQLite lock prevents two owners of the same profile.

To install a distributable tarball without publishing to npm:

```sh
npm pack
npm install -g ./agent-bridge-hub-2.0.0-alpha.1.tgz
agent-bridge-hub start
```

The executable is named `agent-bridge-hub`, so it can coexist with Python's
`agent-bridge`. The package is private until a release is explicitly prepared.

## Connect MCP hosts

Use an absolute Node executable path if a GUI host does not have Node on PATH:

```json
{
  "mcpServers": {
    "agent-bridge-v2": {
      "command": "node",
      "args": [
        "/absolute/path/to/Agent-Bridge/v2/dist/cli.js",
        "mcp", "--agent-id", "codex-main", "--kind", "codex",
        "--project", "my-project"
      ]
    }
  }
}
```

For another host use a different `--agent-id`, such as `claude-review`, with the
same project key and profile. On Windows, JSON paths need escaped backslashes.
The `kind` field labels the external host; it does not launch a Worker. A stable
identity can reconnect only with the same kind, project and name. Omit the ID
for a new identity each launch; use distinct IDs for distinct conversations.

## Tools and execution

| Tools | Behavior |
| --- | --- |
| `list_agents`, `list_peers` | Configured Worker executables and registered project peers; registration is not an online-presence guarantee |
| `create_session` | Allocate a managed session and its addressable `agent_id`; start its Worker when work arrives |
| `dispatch_task` | Queue work on a new or existing session; returns immediately |
| `wait_task`, `get_result` | Poll completion and retrieve persisted results, errors, observed model/effort and file changes |
| `list_tasks`, `list_sessions` | Shared project state across every host |
| `cancel_task`, `end_session` | Stop work or close a session; already-applied edits are not undone |
| `send_message`, `read_inbox`, `read_message`, `acknowledge_message` | Durable messages and explicit consumption |
| `read_events` | Durable progress/history with a sequence cursor |
| `set_preferences`, `reload_config` | Owner-host configuration; unavailable to injected Worker credentials |

Task and session tool arguments use snake_case, for example:

```json
{
  "agent": "kimi",
  "cwd": "/absolute/path/to/project",
  "message": "Inspect the failing build and fix its cause.",
  "model": "your-model-id",
  "effort": "high",
  "request_id": "build-fix-1"
}
```

For continuation, supply the returned `session_id` along with the same Worker
and working directory. One session executes turns in order; different sessions
can execute concurrently. Model changes reapply the effort supported by that
model. Results expose `observed_model`, `observed_effort`, errors and warnings;
unsupported selections are not silently reported as successful.

`send_message` takes `recipient_id`, `request_id`, `text`, optional `thread_id`
and `wake` (default true). For managed sessions it atomically creates a queued
turn, even while the current turn is running. It does not interrupt the current
prompt. `wake=false` stores information without scheduling work. External hosts
must read their inbox; this does not wake an idle desktop model. Workers receive
an injected MCP connection, with a CLI fallback for transports lacking MCP
injection. Replies use the received thread ID and a fresh request ID.

Retry a message or task with its original request ID and arguments. A conflicting
retry fails. Reading an inbox does not acknowledge; successful message-triggered
work acknowledges its source message. Acknowledgement and task completion remain
distinct. The Hub rejects waits on one's own session or a known cyclic wait;
asynchronous messages remain available.

`wait_task.timeout_sec` defaults to 180 and only ends that polling call. It does
not cancel the task. Inbox and event calls default to 100 entries per page; callers
can request another page size. Result/message `offset` and `limit` page text in
UTF-16 code units. Storage is not truncated to those sizes. There are no Bridge
message-size, concurrent-session, task-duration or delegation-depth caps by default.
Native CLI/provider limits still apply.

## Configuration

Defaults come from bundled `agents.toml`, then `~/.agent-bridge/agents.toml`, then
`<profile>/agents.toml`. `--config FILE` instead selects one explicit overlay on
the bundled defaults. Tables merge; arrays replace, including empty arrays.
`reload_config` applies changes to subsequent turns without interrupting current
work. Existing Workers are recreated when their launch configuration changes.

Supported transports are `acp`, `codex` and `agy`; the bundled configuration names
Grok, Kimi, Cursor, DSH, Claude, OpenCode, Devin, ZCode, MiniMax, Antigravity and Codex.
An `available` executable has been found; this does not establish login or live
protocol compatibility. Configure command arguments as arrays, never shell strings.

Environment variables inherit from the Hub. `[env.set]` and per-Worker `env`
override inherited values. Explicit `env.deny` globs remove inherited variables,
including after gateway rewriting; explicitly configured values and Hub identity
wiring survive. There is no built-in credential denylist. On Windows, missing
named `env.inherit` variables are restored from user/machine settings. Proxy
configuration supports `[env.proxy]`, `AGENT_BRIDGE_*_PROXY` and Windows Internet
Settings discovery. Interactive shell wrappers are not executed or imported.

`stall_timeout_sec`, `turn_timeout_sec` and `startup_timeout_sec` default to zero
(disabled). `shutdown_grace_sec` is a process-cleanup grace period, not a job timeout.
Configured idle unloading applies only to revivable sessions and never to an active
turn. Resume uses the product's native session ID. A failed native restore starts a
new conversation with a warning; some products do not support restore.

Global routing modes are `manual`, `auto` and `eager`; legacy `safe`/`yolo` config
values map to manual/eager. Manual mode requires `user_requested=true` for dispatch
or a managed message wakeup. This is an operator routing preference, not an OS
sandbox. Permission handling follows each adapter and `permission_policy`; the
bundled product commands preserve their existing permission flags. Login remains
the responsibility of each product CLI.

## Persistence and transport

SQLite WAL stores identities, scoped credentials, sessions, tasks, messages and
an ordered event log. Message delivery and any induced task commit together.
Completed results survive restart. Unfinished queued/running tasks become
`interrupted` on recovery; inspect their workspace before explicitly retrying,
since arbitrary code execution cannot be replayed exactly once.

Each Worker has a guardian process. Loss of the Hub IPC connection triggers cleanup
of the owned process group/tree; cancelling a turn also stops descendants. POSIX
cleanup captures descendants before signalling the CLI, including shells that
start separate process groups. Already-orphaned background services are outside
this ownership boundary; Bridge does not act as an OS service supervisor. Output
and diagnostics are persisted incrementally. Git workspaces are compared before
and after turns, supplemented with adapter-reported changes; a nongit workspace
reports file collection as unavailable instead of pretending it is complete.
Concurrent writers to one directory can affect this comparison, so file lists do
not establish exclusive attribution to one task.

HTTP routes use camelCase. Owner connections register via `POST /v1/connect`;
subsequent proxy calls use the returned identity-bound token. Workers may read and
cooperate within their project, but cannot impersonate another sender or change
Hub-wide preferences. `/v1/sessions`, `/v1/tasks`, `/v1/messages`, `/v1/inbox`,
`/v1/events` and `/v1/events/stream` provide the corresponding API. SSE honors
backpressure and supports `Last-Event-ID`; consume by durable sequence, allowing
replayed events after reconnect. The endpoint is discovered on each request.

The trust boundary is one trusted OS user on one machine. Scoped tokens enforce
API identities; a process with unrestricted filesystem access as that user can
still read that user's profile. This is not a hostile-code sandbox. Browser-origin
requests are rejected. Keep the profile in a private local directory, protected
by user ACLs on Windows. History is retained until the profile is deliberately
removed; no automatic pruning policy is imposed.

## Validation

`npm test` compiles strict TypeScript and runs transport integration tests using
independent official MCP clients, HTTP/SSE, SQLite restart, actual child processes
and protocol fixtures. Covered flows include disconnect-and-continue, FIFO turns,
resume, model/effort changes, nested delegation and replies, cancellation, Hub crash
cleanup including a grandchild, idempotency, scoped identity and manual routing.
`npm run smoke:package` packs, installs, starts and stops the installed CLI in an
isolated directory. CI runs both on Linux, Windows and macOS with Node 24.
