---
name: agent-bridge
description: Coordinate local worker agents (Grok Build, Kimi Code, Antigravity, DeepSeek Harness, OpenCode, Claude Code, Codex CLI, Devin CLI, ZCode, MiniMax Code) through the Agent Bridge MCP tools. Use when dispatching implementation, research, or test work to a worker; when the user mentions Agent Bridge, dispatch_task, or a worker by name; or when deciding whether to delegate a coding task instead of doing it yourself.
---

# Dispatching workers through Agent Bridge

You are the coordinator: users talk only to you, and you call the workers. Workers are reached **only** through the Agent Bridge MCP tools (`list_agents`, `list_models`, `set_preferences`, `dispatch_task`, `wait_task`, `check_task`, `get_result`, `get_transcript`, `cancel_task`, `list_sessions`, `end_session`). If those tools are missing from this session, stop and say so — never run `grok`, `kimi`, `agy`, `dsh`, `opencode`, `claude`, `claude-agent-acp`, `codex`, `devin`, `zcode-acp-server`, or `mcode` CLIs directly, and never drive their GUIs. The same product can be a coordinator and a worker; those are different processes.

## Before anything: read the policy

Call `list_agents` first. Its result carries:

- `coordinator.mode` — `manual`: dispatch only what the user explicitly asked for (`dispatch_task` requires `user_requested=true`); `auto`: your judgment via Step 1; `eager`: prefer dispatching multi-step work.
- `coordinator.instructions` — the user's persistent routing preferences. They override Step 2 below. When the user states a lasting preference in chat, persist it with `set_preferences` (its `instructions` argument replaces the stored text — read the current value first and write the merged result; effective immediately in this instance, elsewhere at next start). One-off wishes are not preferences; follow them without persisting.
- `coordinator.runtime_context` / `coordinator.dispatch_enabled` — a top-level host is `coordinator` / `true`. If `dispatch_enabled` is false, this Bridge was inherited inside a worker: stop dispatching immediately. Do not call `dispatch_task`, `set_preferences`, `cancel_task`, or `end_session`. `user_requested=true` does not bypass that. Nested instances write under a `nested/` data directory so they cannot share the coordinator's `state.json`.
- `env.proxy` / `env.warnings` — a null proxy on a direct network is normal; on a machine that needs one, fix `[env.proxy]` in `agents.toml` instead of retrying a failed dispatch.
- `agents[].quota` — remaining plan per worker: `status` ok / exhausted / unknown, `windows[]` with `remaining_percent` and `resets_at` / `resets_in_sec` (Codex 5h + weekly, Kimi Code limits, Claude 5h + weekly, Grok weekly), optional `balance` when reported by the provider, `plan`, `cached` / `stale`, `detail`. Information, not a routing rule: `exhausted` means the turn will most likely fail — prefer another worker or tell the user when it resets; `unknown` means Bridge could not read it (unsupported CLI, API-key login, timeout — the reason is in `detail`), never that it is empty. Quota does not change `available`. Custom API/auth endpoints are unsupported and return `unknown` before credentials or cached quota are read. Cached readings expire at the earliest window reset. DSH balance lookup is unsupported.

Claude `status`: shared `5h`/`weekly` only. Before dispatch, even if `ok`, check the target model's `weekly:opus`/`weekly:sonnet`: 0% is exhausted; missing/null unknown. Report reset time or a user-permitted alternative. Model-only data leaves shared status unknown.

## Model discovery

When `coordinator.dispatch_enabled=true` and `list_agents.model_discovery.supported=true`, use `list_models(agent=..., cwd=...)` to obtain worker-advertised model IDs before explicitly selecting a model. Supply this conversation's absolute project directory; use `refresh=true` after changing accounts or worker configuration. `supported_efforts` reports worker-native levels; pass `off|low|medium|high|max` to `dispatch_task.effort` for adapter mapping. See SETUP.md for result fields, caching, and session behavior.

## Step 1 — dispatch, or do it yourself?

A cost question, not a category question; "it is implementation work" is never by itself a reason to dispatch.

Do it yourself when: after reading 1–2 files you already know the exact edit; the whole job is reading a little code and answering; or writing the dispatch message would cost more than making the change.

Dispatch when: the change spans several files or needs exploration you have not done; tests must be written or a build/test loop iterated; breadth research across many sources; or not dispatching would eat many turns of mechanical work.

Examples: "fix the README typo" → yourself. "Add a None check at line 120" → yourself, even though it is implementation. "Add retry logic to the ACP adapter with tests" → Grok. "Port this 4000-line module" → Kimi (`kimi-code/k3-256k`). "Survey how other CLIs handle session resume" → Antigravity.

## Step 2 — which worker (user instructions override this)

- **Antigravity (Gemini):** research, surveys, breadth-heavy or lightweight tasks.
- **Grok Build:** default implementer — features, refactors, tests, multi-file code.
- **Kimi Code:** second implementer — Grok busy or wrong, independent second take, or big single-context jobs.
- **OpenCode:** optional third implementer — user asked for it, wants a connected provider/model, or Grok and Kimi are busy.
- **Claude Code:** optional implementer — user asked, or Grok and Kimi are busy. Worker binary is `claude-agent-acp`.
- **Codex CLI:** optional implementer — user asked, or others are busy. Desktop-bundled `codex exec`, not the Desktop GUI; startup failures before JSONL are returned in `get_result.error`.
- **Devin CLI:** optional implementer — user asked, or others are busy. `devin acp`, not Devin Desktop; model ids carry the level (`swe-1-7-medium`), `effort` is ignored with a warning.
- **ZCode:** optional implementer — user asked, or others are busy. Worker is `zcode-acp-server`, not the ZCode app. Mode is forced to `yolo`.
- **MiniMax Code:** optional implementer — user asked, or others are busy. `mcode acp`. Permission is forced to `bypassPermissions`.
- **DeepSeek Harness:** only when others are unavailable or the user asks.

## The dispatch loop

1. `dispatch_task` with `cwd` = **this conversation's project folder** (absolute) — never the Agent Bridge install path, never a temp dir. The `message` must be self-contained: background, absolute paths, acceptance criteria, things not to do. Leave `model`/`effort` unset unless you have a reason; slugs and effort mappings per worker are documented in ORCHESTRATION.md in the Agent-Bridge repo.
2. Loop `wait_task` until terminal. A timeout is **not** failure — call it again. `silent_for_sec` in the payload is the time since the worker's last output; a turn silent past `stall_timeout_sec` (default 1800) ends `failed` / `stop_reason="stalled"` — raise that worker's limit in `agents.toml` if the step was legitimately long. Stall counts only worker output; `turn_timeout_sec` (default 0 = off) instead caps a turn's wall-clock and ends `failed` / `timed_out`. `dispatch_task` and every terminal task answer (`wait_task` / `check_task` / `get_result` / `cancel_task`) carry `persisted`; `false` means `state.json` could not be written — the answer is still valid but may not survive a restart. `files_changed` lists only paths inside the task `cwd`; outside-cwd paths land in `files_outside_cwd` (absolute, capped the same way, `files_outside_cwd_total`). A warning that `session/load` failed and prior context is lost means a fresh native session — re-supply context next message. Size `timeout_sec` under the host MCP tool timeout:
   - Codex: `tool_timeout_sec` 600; default 180 is fine.
   - Cursor: host ~45–60 s; pass ~30 and loop.
   - Kimi Code: configure `toolTimeoutMs` 600000; otherwise ~45 s polls.
   - ZCode: configure `timeoutMs` 600000; otherwise ~15–20 s polls.
   - Grok Build: official default `tool_timeout_sec` is 6000; set 600. If unsure or the host kills the call, ~30–45 s polls.
   - Claude Code: per-server `timeout` 600000 (ms) in `.mcp.json`. CLI default is long; if unsure, ~45 s polls.
3. `get_result`; while `has_more` is true, call it again with `cursor=next_cursor` and concatenate the pages. Then verify yourself: `git status` / `git diff`, run the relevant build and tests. Never trust the worker's self-report. Grok's real model is `observed_model` (its "I am Grok X" banner is baked at `/new` and does not track model switches). OpenCode, Claude Code, ZCode, and MiniMax Code `observed_model` / `observed_effort` are the last values Bridge set after mapping, not a live sampler. An empty Kimi result with non-empty `warnings` is a failed turn, not a no-op.
4. If review fails, `dispatch_task` again on the same `session_id` with a concrete problem list — at most three follow-ups, then fix it yourself and tell the user.
5. Summarize the diff, leftover risk, and worker usage. `end_session` when the worker is no longer needed.

ACP failures retain string `error` plus optional `failure`. If `failure.prompt_may_have_been_sent` is true or evidence is missing, do not blindly replay; false is not proof of safe retry. Bridge never auto-retries prompts. See SETUP.md for diagnostic fields.

For optional retry deduplication, send a UUID `request_id` on the first `dispatch_task` call, then retry with the same ID and original arguments; keep `session_id` omitted if it was originally omitted. Adding an ID only on retry cannot deduplicate the first call. Identical retries reuse the task (`reused=true`) only within the same Bridge instance while the task is retained. Different arguments are rejected, and normal dispatch validation still applies. Restart, another instance, or task pruning loses the binding; this does not guarantee exactly-once worker side effects.

`check_task` / `get_result` on a task owned by another Bridge instance answer from `state.json` (`from_state: true`, `silent_for_sec: null`); `wait_task` / `cancel_task` still require the owning instance.
