# Native search and media

Agent Bridge exposes three MCP tools for direct native operations through the
existing local worker CLIs:

| Agent | Search | Image | Video |
| --- | --- | --- | --- |
| Codex CLI | Yes | Yes | No |
| Grok Build | Yes | Yes | Yes |
| MiniMax Code | Yes | No | No |

The integration uses the worker's existing account and configuration. Availability
depends on the installed CLI, account, model, and enabled native features.
Generation costs and reference-image uploads require explicit user authorization.
`user_requested=true` satisfies Bridge's manual dispatch policy; it does not itself
establish permission to incur costs or upload files.

## Calling and reading

Pass the following arguments to `call_capability`:

```json
{
  "agent": "grok",
  "capability": "video",
  "prompt": "Animate the authorized reference image with a slow camera orbit.",
  "cwd": "<absolute-authorized-workspace>",
  "input_images": ["reference.png"],
  "duration_sec": 5,
  "request_id": "<fresh-UUID-retained-for-replays>",
  "user_requested": true
}
```

`agent`, `capability`, `prompt`, `cwd`, and `request_id` are required. Prompts must
contain 1 to 16000 characters. Optional `model` and `effort` select the worker
settings for this call. Video requires an integer `duration_sec` from 1 to 60;
other capabilities reject that argument.

The call returns `call_id`, agent, capability, request ID, status, `reused`, and
`persisted`. Read with `get_capability_result(call_id=..., wait_sec=...)`.
`wait_sec` ranges from 0 to 60. `pending=true` means execution or worker shutdown
has not finished; keep reading the same call. A terminal status can be visible
while cleanup remains pending. Failed or interrupted cleanup blocks continuation.

Results include paginated text, status, warnings, observed model/effort, durability,
and any available native evidence:

- `sources`: native title, URL, optional snippet, and native tool-call ID.
- `output_files`: workspace-relative path, MIME type, byte count, SHA-256, and
  native tool-call ID.
- `job`: provider job/session/turn evidence, status, and remote cancellation support.
- `native_calls`: observed native tool-call identities and operation names.
- `failure`: upstream structured ACP failure information when available.

Absent native fields are omitted. `persisted=false` reports degraded state
durability. Native submission requires a positive state flush; an unpersisted
native request fails before adapter execution. Ordinary dispatch keeps its
existing degraded-persistence behavior.

Text pages use `cursor` and `max_chars` (1 to 60000); follow `next_cursor` while
`has_more` is true. Reading a result rechecks delivered files. Modified, missing,
or unsafe output files fail validation; a result read never regenerates them.

Use `cancel_capability(call_id=...)` to request local cancellation. Native remote
cancellation may be unavailable. A stopped local worker does not establish that
remote generation stopped or that no charge occurred.

## Replay and continuation

Create the UUID on the first call and replay the same ID with identical supplied
arguments after a timeout or uncertain response. Retained request bindings survive
Bridge restarts. Replay is checked before inspecting reference files or resolving
the previous call, so an existing result remains retrievable after those inputs
vanish. Bindings are limited to retained tasks in the same Bridge home; they are
not cross-instance provider idempotency keys.

A new UUID starts a new operation. Never use one merely to recover a failed,
cancelled, or uncertain generation. Bridge does not automatically retry provider
submission or recover by starting a replacement operation.

`previous_call_id` explicitly continues a successful, fully finalized latest call
from the same agent and workspace:

- Search can continue search.
- Image can continue image or become video.
- Search-to-image and video-to-video continuation are rejected.

Continuation requires the prior internal session and, except for Codex image
operations, a resumable native session. Native resume failure fails the call
without creating a replacement session. Codex image continuation starts a fresh
thread using verified local reference images.

Optional `input_images` accepts at most four PNG, JPEG, or WebP files inside
`cwd`. Paths are normalized; URLs, traversal, symbolic links, and junctions are
rejected. File content is inspected and hashed again before execution. Search
rejects reference images. If media continuation omits `input_images`, it uses the
previous verified image output; an explicitly supplied empty list means no images.
Ensure the user authorized each file upload.

Capability sessions are internal, hidden from ordinary `list_sessions`, and
unavailable to ordinary `dispatch_task`. Workers close when each call finishes;
native IDs remain available for authorized continuation.

## Evidence and delivery limits

Successful search requires observed native `web_search` activity and attributable
native sources. Native page retrieval may accompany search. URLs mentioned only
in model prose do not count as sources, and page retrieval alone is not search.
Source collection excludes credential-bearing and nonpublic URLs.

Successful media calls require exactly one matching native operation and one
verified local file. Video duration is read from the MP4 movie timeline and must
match the requested duration within 0.05 seconds. Output files are copied into
`.agent-bridge/outputs/<call-id>/` and checked for type, integrity, and safe path
binding. Existing files, unsafe paths, partial copies, and unsupported formats
cannot become successful deliveries. Successfully delivered files and uncertain
job evidence remain available after transport failure.

Native evidence is bounded. Raw image bytes, signed download URLs, and arbitrary
tool output do not become public delivery metadata. This integration adds no
provider-specific MCP service or general URL downloader.

Operation-count instructions and result validation are not a provider-side hard
execution budget. A worker can attempt extra operations before Bridge observes
them; those calls fail result validation rather than guaranteeing only one billed
operation. There is no permanent exactly-once execution guarantee.

Codex search adds `web_search="live"` only for that call, preserving worker
configuration and task model/effort precedence. Codex image uses a fresh official
app-server thread. Existing `dispatch_task(operation="image", agent="codex", ...)`
also supports that narrow fresh-session operation; it rejects `session_id`.
Ordinary Codex exec/resume remains on its existing path.

The port is checked using offline protocol fixtures and subprocess workers.
Earlier local Grok search/image/video runs provide limited integration evidence
for those protocol shapes. They do not verify this upstream candidate, another
installation, or current account availability. Codex image and MiniMax search
still require environment-specific account validation before claiming live
provider acceptance.
