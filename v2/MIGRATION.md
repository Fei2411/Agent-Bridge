# V2 migration and verification

## Implemented

The TypeScript runtime owns execution end to end: profile ownership and discovery,
automatic detached startup, shared SQLite state, per-session FIFO scheduling,
parallel sessions, cancellation, recovery and process-tree cleanup. Managed
Workers receive the same Hub through identity-bound MCP/CLI tools and can delegate,
read events and reply. Communication does not depend on the originating host.

ACP adapters preserve the existing products' selection conventions, model-specific
effort updates, native restore methods and permission callbacks. Codex exec and
Antigravity use their own JSON transports. Configuration merges `agents.toml`
overlays, preserves explicit argv, supports Windows command shims, and applies
environment rules at the launch boundary.

There are no Bridge-wide quotas or hard concurrency, task duration, message size,
or delegation-depth caps. Explicit operator settings still apply.

## Moving a host

1. Install Node 24, run `npm ci && npm test` inside `v2`, then use the compiled
   `dist/cli.js mcp` entry point or install an `npm pack` tarball.
2. Replace that host's Python MCP entry with the [V2 entry](README.md#connect-mcp-hosts).
   Use one profile and project key across participating hosts and a distinct peer
   ID for each native conversation. Avoid exposing both generations under the
   same MCP tool name in one host.
3. Existing `~/.agent-bridge/agents.toml` settings are read as an overlay. For an
   isolated trial, pass an explicit `--config` file and `--home` directory. Arrays
   replace defaults; an empty array deliberately clears them.
4. Run the product's own login/setup first. `list_agents.available` only establishes
   executable discovery. Start with a disposable project and a file-writing task,
   then confirm the result from a second MCP host.
5. To revert, stop the V2 Hub and restore the Python MCP entry. Python's original
   state and package entry point have not been rewritten.

## Compatibility differences

| Area | V2 behavior |
| --- | --- |
| Scope | Sessions, tasks and peers are shared by project in one Hub, rather than owned by a stdio server |
| Nested Workers | Bound to the same Hub and allowed to delegate; no nested registry or blanket refusal |
| Tool names | Existing dispatch/wait/result/session tools remain; peer/message/event tools are added |
| Tool shape | Task/session arguments and views use snake_case; message/event records and direct HTTP routes use camelCase |
| `list_agents` | Worker availability plus project peers and preferences; quota is explicitly `unknown` |
| Preferences | Hub-wide operator preferences; per-host `AGENT_BRIDGE_MODE` overrides are not used |
| Python state | `state.json`, transcript files and result files are not imported or shared; start new V2 sessions |
| Quota providers | Existing Python account-usage providers are not ported; V2 never invents remaining quota |
| Legacy DSH | Native `dsh --profile acp` is preferred. For old demos, provide a working command including `--config`; no legacy Cordis composition is silently rewritten |
| Environment | Hub process env, named Windows user/machine restoration and system proxy discovery; static PowerShell wrapper parsing is not ported |
| Startup | Explicit Node/bin path replaces Python/uv launch; no automatic login, CLI installation, OS service or update daemon |
| External hosts | Inbox reads are supported; waking an idle native desktop conversation requires host support |
| Crash recovery | All unfinished tasks become interrupted; no automatic replay of workspace mutations |
| Timeouts | Bridge task/startup/stall timeouts disabled by default; poll timeout does not stop work |
| UI | CLI and MCP only; no dashboard, tray or remote-node transport in this runtime change |

The legacy Python skills still describe Python-specific setup and tools. Their
product validation scenarios remain useful, but they are not installed as a V2
runtime instruction bundle. Use this directory's tool descriptions and docs for V2.

## Verification performed

The automated suite uses real MCP proxies, TCP HTTP/SSE, SQLite files and child
processes. Its independent protocol fixtures exercise ACP, Codex and Antigravity
wire events rather than calling adapter internals. It verifies:

- Two hosts share sessions/results after the submitting host disconnects.
- One session queues turns, restores its native ID, changes model and reapplies
  the effort accepted by that model; invalid model selection fails visibly.
- A managed message wakes a Worker, which connects back, delegates to a second
  Worker, waits for its result and replies in the original thread.
- Cancellation stops active work and removes queued work; later continuation can
  restore the native session without leaking replayed history into the result.
- Killing the Hub stops a Worker grandchild; restart reports unfinished work as
  interrupted instead of rerunning it.
- Message/task retries are deduplicated; inbox acknowledgement and SSE replay
  survive restart. Larger payloads and caller-selected page sizes are accepted.
- Scoped credentials reject impersonation, cross-project access and owner-only
  configuration. Manual routing also applies to message-triggered work.
- Simultaneous clients converge on one background Hub and survive their own exit.
- A packed tarball installs and runs its actual bin, discovers bundled config,
  starts an independent Hub, calls a tool and shuts down.

CI defines Node 24 jobs on Linux, Windows and macOS, alongside unchanged Python
jobs. Local Linux results are not evidence that the other operating systems or
live product versions have passed; inspect the branch's CI before merging.

### Real CLI with a mock model

The opt-in smoke runs **OpenCode 1.19.5** against a loopback OpenAI-compatible
streaming endpoint. Only model responses are mocked: OpenCode executes its real
shell tools and injected MCP tools, using separate Hub and MCP proxy processes,
native session storage, and disposable Git workspaces. No real API key is needed.
The Linux CI job installs the same pinned CLI and runs this smoke.

On Linux, from `v2`:

```sh
npm install --prefix /tmp/bridge-opencode opencode-ai@1.19.5 --no-audit --no-fund
OPENCODE_SMOKE_BIN=/tmp/bridge-opencode/node_modules/.bin/opencode npm run smoke:opencode
```

Verified locally with Node 24.22.1 on Linux:

- File edits are made by actual CLI tool calls and appear in Bridge's result.
- Two MCP clients share work; disconnecting the submitter does not stop it.
- Repeated request IDs deduplicate; queued turns execute in order and switch
  between mock model IDs. The model receives the earlier native history.
- Cancelling active and queued tasks aborts the model request; later turns retain
  the native session ID and history after the worker process restarts.
- A message wakes a real CLI, which uses injected Bridge MCP tools to discover,
  delegate to another real CLI, wait, read its result, and reply in the same thread.
- Abrupt Hub death stops an active shell tool, including a separate process group.
  Restart retains completed results, marks unfinished work interrupted, and resumes
  the original native conversation without repeating the file write.

This smoke found a process-cleanup defect: signalling only the CLI's process group
left OpenCode's shell process running. Cleanup now discovers descendants before
stopping the CLI and retains their IDs for forced cleanup. The regression also
uses a separate-group child that ignores SIGTERM. It fails before the fix and
passes after it. A background process already orphaned before shutdown cannot be
identified by its former ancestry and is not covered by this cleanup guarantee.

The mock scripts deterministic responses; it does not validate model reasoning,
provider authentication, billing, effort semantics, or other CLI versions/platforms.

## Live product release gate

Real OpenCode with a mock model is covered above. Authenticated providers and the
remaining product Agent CLIs have not yet been exercised in this environment.
Before promoting this alpha as the Python replacement, use each available real CLI:

1. Record CLI/version, selected model/effort and login route. Run a real file edit
   in a disposable Git checkout; inspect both the file and returned result.
2. Reuse the session, change model/effort, unload/revive it and confirm the native
   conversation continues. Inspect warnings rather than assuming restore worked.
3. Close the submitting MCP host during work; use another host to observe and
   continue it. Cancel another turn and verify its process tree exits.
4. Have a managed Worker call the injected Bridge, delegate and send a same-thread
   reply. For Antigravity exercise the documented CLI fallback as needed.
5. Check the installed tarball on Windows with npm `.cmd` commands and paths
   containing spaces, then run the same flows on macOS/Linux.

Quota parity and historical state import remain separate compatibility work; neither
is necessary for fresh V2 execution, but both must be explicitly resolved or accepted
before replacing an installation that relies on them.
