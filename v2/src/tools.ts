import { z } from 'zod';
import { HubClient } from './client.js';
import { cursor, id } from './contracts.js';
import type { Agent, RegisterAgent, Session, Task } from './contracts.js';

const page = { limit: z.number().int().positive().safe().optional() };
const selection = { model: z.string().optional(), effort: z.string().optional(), title: z.string().optional() };
export const toolDefinitions = {
  list_agents: { description: 'List configured Worker types, connected peer identities, and operator preferences. Availability means an executable is present, not that login is valid.', schema: z.object({}), readOnly: true },
  list_peers: { description: 'List registered peer identities in this project. Registration is not proof of an active model turn.', schema: z.object({}), readOnly: true },
  send_message: { description: 'Send to a peer. Managed sessions queue a new turn by default, including when busy; external hosts read their inbox. Retry with identical request_id and arguments. Set wake=false for information that should only enter the inbox.',
    schema: z.object({ recipient_id: id, request_id: id, text: z.string().min(1), thread_id: id.optional(), wake: z.boolean().default(true), user_requested: z.boolean().default(false) }), readOnly: false },
  read_inbox: { description: 'Read unacknowledged messages in arrival order. Reading does not acknowledge; limit is a page size, not a storage cap.', schema: z.object(page), readOnly: true },
  read_message: { description: 'Read a message, optionally paging its text by offset and limit.', schema: z.object({ message_id: id, offset: cursor.default(0), ...page }), readOnly: true },
  acknowledge_message: { description: 'Mark a message addressed to this identity as consumed. This does not mean its requested work is complete.', schema: z.object({ message_id: id }), readOnly: false },
  read_events: { description: 'Read durable project events after a saved sequence. Use the last sequence to continue; events include task progress and completion.', schema: z.object({ after: cursor.default(0), ...page }), readOnly: true },
  create_session: { description: 'Create a managed Agent session and return its addressable agent_id. The Worker starts when work arrives.', schema: z.object({ agent: id, cwd: z.string(), ...selection }), readOnly: false },
  dispatch_task: { description: 'Queue work on a new or existing managed session. Returns immediately. Same-session work runs in order; separate sessions run concurrently. Use request_id for durable retry deduplication.',
    schema: z.object({ agent: id, cwd: z.string(), message: z.string().min(1), session_id: id.optional(), request_id: id.optional(), user_requested: z.boolean().default(false), ...selection }), readOnly: false },
  wait_task: { description: 'Wait for completion or a polling timeout. A timeout does not cancel the task. Never wait on your own session or a cyclic task dependency.', schema: z.object({ task_id: id, timeout_sec: z.number().nonnegative().finite().default(180) }), readOnly: true },
  get_result: { description: 'Read task status, result, errors, observed model/effort and file changes. Optional offset/limit page the full persisted result without truncating storage.', schema: z.object({ task_id: id, offset: cursor.default(0), ...page }), readOnly: true },
  list_tasks: { description: 'List task metadata for this project or one session.', schema: z.object({ session_id: id.optional() }), readOnly: true },
  list_sessions: { description: 'List all managed sessions in this project across Hub clients.', schema: z.object({}), readOnly: true },
  cancel_task: { description: 'Cancel queued or running work and wait for its Worker to stop. Already-applied file edits are not undone.', schema: z.object({ task_id: id }), readOnly: false },
  end_session: { description: 'Close a session, cancel pending work and stop its Worker process tree.', schema: z.object({ session_id: id }), readOnly: false },
  set_preferences: { description: 'Update global operator routing preferences. Available to owner-host proxies, not injected Worker proxies.', schema: z.object({ mode: z.enum(['manual', 'auto', 'eager']).optional(), instructions: z.string().optional() }), readOnly: false },
  reload_config: { description: 'Reload agents.toml. Existing work continues; subsequent turns use the new configuration.', schema: z.object({}), readOnly: false },
};
export type ToolName = keyof typeof toolDefinitions;
function query(path: string, values: Record<string, unknown>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== undefined) params.set(key, String(value));
  return `${path}?${params}`;
}
function sessionView(session: Session) {
  return { session_id: session.id, agent_id: session.agentId, agent: session.agent, project_id: session.projectId,
    cwd: session.cwd, native_session_id: session.nativeSessionId, model: session.model, effort: session.effort,
    title: session.title, proc_state: session.state, created_at: session.createdAt, last_active_at: session.updatedAt };
}
function taskView(task: Task & { nextOffset?: number | null; totalChars?: number; offset?: number }) {
  return { task_id: task.id, session_id: task.sessionId, agent: task.agent, status: task.status,
    result_text: task.text, stop_reason: task.stopReason, error: task.error, warnings: task.warnings,
    observed_model: task.observedModel, observed_effort: task.observedEffort, usage: task.usage,
    files_changed: task.filesChanged, files_outside_cwd: task.filesOutsideCwd, files_changed_state: task.filesChangedState,
    created_at: task.createdAt, started_at: task.startedAt, finished_at: task.finishedAt,
    next_offset: task.nextOffset, result_chars: task.totalChars, offset: task.offset };
}

export class BridgeTools {
  readonly #client: HubClient;
  readonly #owner: HubClient | undefined;
  readonly identity: Agent;
  private constructor(client: HubClient, identity: Agent, owner?: HubClient) { this.#client = client; this.identity = identity; this.#owner = owner; }

  static async connect(home: string, identity: RegisterAgent, token?: string): Promise<BridgeTools> {
    if (token) {
      const client = new HubClient(home, token), actual = await client.request<Agent>('/v1/self');
      if (actual.id !== identity.id || actual.projectId !== identity.projectId) throw new Error('Worker credential does not match its assigned identity');
      return new BridgeTools(client, actual);
    }
    const owner = new HubClient(home);
    const result = await owner.request<{ agent: Agent; token: string }>('/v1/connect', identity);
    return new BridgeTools(new HubClient(home, result.token), result.agent, owner);
  }

  async call(name: string, raw: unknown): Promise<unknown> {
    if (!Object.hasOwn(toolDefinitions, name)) throw new Error(`Unknown Bridge tool: ${name}`);
    const args = toolDefinitions[name as ToolName].schema.parse(raw) as Record<string, unknown>;
    const c = this.#client, identity = this.identity;
    const scoped = (path: string, extra: Record<string, unknown> = {}) => query(path, { projectId: identity.projectId, ...extra });
    switch (name as ToolName) {
      case 'list_agents': return { ok: true, scope: 'hub', agents: await c.request('/v1/workers'), peers: await c.request(scoped('/v1/agents')),
        coordinator: { ...await c.request<object>('/v1/preferences'), dispatch_enabled: true, runtime_context: this.#owner ? 'host' : 'worker' } };
      case 'list_peers': return c.request(scoped('/v1/agents'));
      case 'send_message': return c.request('/v1/messages', { senderId: identity.id, recipientId: args.recipient_id, requestId: args.request_id, text: args.text, threadId: args.thread_id, wake: args.wake, userRequested: args.user_requested });
      case 'read_inbox': return c.request(query('/v1/inbox', { recipientId: identity.id, limit: args.limit }));
      case 'read_message': return c.request(query(`/v1/messages/${encodeURIComponent(String(args.message_id))}`, { offset: args.offset, limit: args.limit }));
      case 'acknowledge_message': return c.request(`/v1/messages/${encodeURIComponent(String(args.message_id))}/ack`, { recipientId: identity.id });
      case 'read_events': return c.request(scoped('/v1/events', { after: args.after, limit: args.limit }));
      case 'create_session': return sessionView(await c.request('/v1/sessions', { ...args, projectId: identity.projectId }));
      case 'dispatch_task': return taskView(await c.request('/v1/tasks', {
        agent: args.agent, cwd: args.cwd, message: args.message, model: args.model, effort: args.effort, title: args.title,
        projectId: identity.projectId, requesterId: identity.id, sessionId: args.session_id, requestId: args.request_id, userRequested: args.user_requested,
      }));
      case 'get_result': return taskView(await c.request(query(`/v1/tasks/${encodeURIComponent(String(args.task_id))}`, { offset: args.offset, limit: args.limit })));
      case 'wait_task': return taskView(await c.request(query(`/v1/tasks/${encodeURIComponent(String(args.task_id))}/wait`, { timeout: args.timeout_sec })));
      case 'cancel_task': return taskView(await c.request(`/v1/tasks/${encodeURIComponent(String(args.task_id))}/cancel`, {}));
      case 'list_tasks': return { scope: 'hub', tasks: (await c.request<Task[]>(scoped('/v1/tasks', { sessionId: args.session_id }))).map(taskView) };
      case 'list_sessions': return { scope: 'hub', sessions: (await c.request<Session[]>(scoped('/v1/sessions'))).map(sessionView) };
      case 'end_session': return sessionView(await c.request(`/v1/sessions/${encodeURIComponent(String(args.session_id))}/end`, {}));
      case 'set_preferences': case 'reload_config': {
        if (!this.#owner) throw new Error('Only an owner-host connection can change Hub configuration');
        return this.#owner.request(name === 'set_preferences' ? '/v1/preferences' : '/v1/config/reload', args);
      }
    }
  }
}
