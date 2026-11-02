import { timingSafeEqual } from 'node:crypto';
import type { IncomingMessage, ServerResponse } from 'node:http';
import { z } from 'zod';
import { acknowledgeMessage, createSession, cursor, dispatchTask, HubError, id, registerAgent, sendMessage } from './contracts.js';
import type { Agent } from './contracts.js';
import { Store } from './store.js';
import { Runtime } from './runtime.js';

export function json(response: ServerResponse, status: number, value: unknown): void {
  response.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  response.end(JSON.stringify(value));
}

async function body(request: IncomingMessage): Promise<unknown> {
  if (request.headers['content-type']?.split(';')[0]?.trim() !== 'application/json') throw new HubError(415, 'Expected application/json');
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(chunk as Buffer);
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')) as unknown; }
  catch { throw new HubError(400, 'Invalid JSON'); }
}

export function createApi(store: Store, runtime: Runtime, token: string,
  notify: () => void,
  stream: (response: ServerResponse, projectId: string, after: number) => void,
  shutdown: () => Promise<void>) {
  const expected = Buffer.from(`Bearer ${token}`);
  return async (request: IncomingMessage, response: ServerResponse): Promise<void> => {
    try {
      const provided = Buffer.from(request.headers.authorization ?? '');
      const admin = provided.length === expected.length && timingSafeEqual(provided, expected);
      const peer = admin ? undefined : store.authenticate((request.headers.authorization ?? '').replace(/^Bearer /, ''));
      if (request.headers.origin || (!admin && !peer)) throw new HubError(401, 'Unauthorized');
      const requireAdmin = () => { if (!admin) throw new HubError(403, 'This operation requires the local Hub owner'); };
      const project = (projectId: string): string => {
        if (peer && peer.projectId !== projectId) throw new HubError(403, 'Project does not match this connection');
        return projectId;
      };
      const agent = (agentId: string): Agent => { const result = store.agent(agentId); project(result.projectId); return result; };
      const self = (agentId: string): string => {
        if (peer && peer.id !== agentId) throw new HubError(403, 'Agent identity does not match this connection');
        agent(agentId); return agentId;
      };
      const session = (sessionId: string) => { const result = store.session(sessionId); project(result.projectId); return result; };
      const task = (taskId: string) => { const result = store.task(taskId); project(result.projectId); return result; };
      const url = new URL(request.url ?? '/', 'http://127.0.0.1');
      const path = url.pathname;
      const limit = () => z.coerce.number().int().positive().safe().parse(url.searchParams.get('limit') ?? 100);
      const scopedProject = () => project(id.parse(url.searchParams.get('projectId') ?? peer?.projectId));
      if (request.method === 'GET' && path === '/health') return json(response, 200, { status: 'ok', protocol: 2 });
      if (request.method === 'GET' && path === '/v1/self') return json(response, 200, peer ?? { role: 'owner' });
      if (request.method === 'POST' && path === '/v1/shutdown') {
        requireAdmin();
        response.once('finish', () => { void shutdown().catch(error => console.error(error)); });
        return json(response, 200, { stopping: true });
      }
      if (request.method === 'POST' && path === '/v1/config/reload') {
        requireAdmin(); await runtime.reload(); return json(response, 200, { reloaded: true });
      }
      if (request.method === 'GET' && path === '/v1/preferences') return json(response, 200, runtime.preferences());
      if (request.method === 'POST' && path === '/v1/preferences') {
        // Managed peers can cooperate, but global operator preferences remain owner-controlled.
        requireAdmin();
        return json(response, 200, runtime.setPreferences(z.object({
          mode: z.enum(['manual', 'auto', 'eager']).optional(), instructions: z.string().optional(),
        }).strict().parse(await body(request))));
      }
      if (request.method === 'POST' && ['/v1/agents', '/v1/connect'].includes(path)) {
        requireAdmin(); const identity = store.register(registerAgent.parse(await body(request))); notify();
        return json(response, 200, path === '/v1/connect' ? { agent: identity, token: store.credential(identity.id) } : identity);
      }
      if (request.method === 'GET' && path === '/v1/agents') return json(response, 200, store.agents(scopedProject()));
      if (request.method === 'GET' && path === '/v1/workers') return json(response, 200, await runtime.workers());
      if (request.method === 'POST' && path === '/v1/messages') {
        const input = sendMessage.parse(await body(request)); self(input.senderId); agent(input.recipientId);
        return json(response, 200, runtime.send(input));
      }
      if (request.method === 'GET' && path === '/v1/inbox') {
        const recipient = self(id.parse(url.searchParams.get('recipientId') ?? peer?.id));
        return json(response, 200, store.inbox(recipient, limit()));
      }
      const messagePath = /^\/v1\/messages\/([^/]+)(\/ack)?$/.exec(path);
      if (messagePath) {
        const message = store.message(decodeURIComponent(messagePath[1]!)); project(message.projectId);
        if (request.method === 'POST' && messagePath[2]) {
          const input = acknowledgeMessage.parse(await body(request)); self(input.recipientId);
          const updated = store.acknowledge(message.id, input.recipientId); notify(); return json(response, 200, updated);
        }
        if (request.method === 'GET' && !messagePath[2]) {
          const offset = cursor.parse(url.searchParams.get('offset') ?? 0);
          const requested = url.searchParams.has('limit') ? limit() : undefined;
          const text = message.text.slice(offset, requested === undefined ? undefined : offset + requested);
          return json(response, 200, { ...message, text, offset, totalChars: message.text.length, nextOffset: offset + text.length < message.text.length ? offset + text.length : null });
        }
      }
      if (request.method === 'GET' && (path === '/v1/events' || path === '/v1/events/stream')) {
        const projectId = scopedProject();
        const after = cursor.parse(request.headers['last-event-id'] ?? url.searchParams.get('after') ?? 0);
        if (path.endsWith('/stream')) return stream(response, projectId, after);
        return json(response, 200, store.events(projectId, after, limit()));
      }
      if (request.method === 'POST' && path === '/v1/sessions') {
        const input = createSession.parse(await body(request)); project(input.projectId);
        return json(response, 200, await runtime.createSession(input));
      }
      if (request.method === 'GET' && path === '/v1/sessions') return json(response, 200, store.sessions(scopedProject()));
      const sessionPath = /^\/v1\/sessions\/([^/]+)(\/end)?$/.exec(path);
      if (sessionPath) {
        const current = session(decodeURIComponent(sessionPath[1]!));
        if (request.method === 'GET' && !sessionPath[2]) return json(response, 200, current);
        if (request.method === 'POST' && sessionPath[2]) return json(response, 200, await runtime.endSession(current.id));
      }
      if (request.method === 'POST' && path === '/v1/tasks') {
        const input = dispatchTask.parse(await body(request)); project(input.projectId); self(input.requesterId);
        if (input.sessionId) session(input.sessionId);
        return json(response, 200, await runtime.dispatch(input));
      }
      if (request.method === 'GET' && path === '/v1/tasks') {
        const projectId = scopedProject(), sessionId = url.searchParams.get('sessionId') ?? undefined;
        if (sessionId) session(sessionId);
        return json(response, 200, store.tasks(projectId, sessionId).map(({ text: _text, message: _message, ...metadata }) => metadata));
      }
      const taskPath = /^\/v1\/tasks\/([^/]+)(\/(?:cancel|wait))?$/.exec(path);
      if (taskPath) {
        const current = task(decodeURIComponent(taskPath[1]!));
        if (request.method === 'GET' && !taskPath[2]) {
          const offset = cursor.parse(url.searchParams.get('offset') ?? 0);
          const size = url.searchParams.has('limit') ? limit() : undefined;
          const text = current.text.slice(offset, size === undefined ? undefined : offset + size);
          return json(response, 200, { ...current, text, offset, totalChars: current.text.length, nextOffset: offset + text.length < current.text.length ? offset + text.length : null });
        }
        if (request.method === 'POST' && taskPath[2] === '/cancel') return json(response, 200, await runtime.cancel(current.id));
        if (request.method === 'GET' && taskPath[2] === '/wait') {
          const controller = new AbortController();
          const disconnect = () => controller.abort(); response.once('close', disconnect);
          try {
            const timeout = z.coerce.number().nonnegative().finite().parse(url.searchParams.get('timeout') ?? 180);
            const ready = await runtime.wait(current.id, timeout, controller.signal, peer?.id);
            const { text: _text, message: _message, ...metadata } = ready;
            return json(response, 200, metadata);
          } finally { response.off('close', disconnect); }
        }
      }
      throw new HubError(404, 'Route not found');
    } catch (error) {
      if (response.headersSent || response.destroyed) { response.destroy(); return; }
      const status = error instanceof HubError ? error.status : error instanceof z.ZodError ? 400 : 500;
      if (status === 500) console.error(error);
      json(response, status, { error: error instanceof HubError ? error.message : status === 400 ? 'Invalid request' : 'Internal Hub error' });
    }
  };
}
