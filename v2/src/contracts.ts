import { z } from 'zod';

export const id = z.string().min(1);
export const registerAgent = z.object({
  id,
  kind: id,
  projectId: id,
  name: z.string().trim().min(1),
}).strict();
export const sendMessage = z.object({
  senderId: id,
  recipientId: id,
  requestId: z.string().min(1),
  threadId: z.string().min(1).optional(),
  text: z.string().min(1),
  wake: z.boolean().default(true),
  userRequested: z.boolean().default(false),
}).strict();
export const acknowledgeMessage = z.object({ recipientId: id }).strict();
export const cursor = z.coerce.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER);

export type RegisterAgent = z.infer<typeof registerAgent>;
export type SendMessage = z.infer<typeof sendMessage>;
export interface Agent extends RegisterAgent { createdAt: string }
export const createSession = z.object({
  agent: id, projectId: id, cwd: z.string().min(1),
  model: z.string().optional(), effort: z.string().optional(), title: z.string().optional(),
}).strict();
export const dispatchTask = createSession.extend({
  requesterId: id, message: z.string().min(1), sessionId: id.optional(),
  requestId: z.string().min(1).optional(), userRequested: z.boolean().default(false),
}).strict();
export type CreateSession = z.infer<typeof createSession>;
export type DispatchTask = z.infer<typeof dispatchTask>;
export interface Session {
  id: string; agentId: string; agent: string; projectId: string; cwd: string;
  nativeSessionId: string | null; model: string | null; effort: string | null;
  title: string; state: 'idle' | 'running' | 'unloaded' | 'closed';
  createdAt: string; updatedAt: string;
}
export type TaskStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted';
export const terminal = (status: TaskStatus): boolean => !['queued', 'running'].includes(status);
export interface TurnResult {
  text: string; stopReason: string; error: string | null; warnings: string[];
  observedModel: string | null; observedEffort: string | null;
  usage: Record<string, unknown>; filesChanged: string[];
}
export interface Task extends TurnResult {
  id: string; sessionId: string; agent: string; projectId: string; requesterId: string;
  requestId: string | null; sourceMessageId: string | null;
  message: string; model: string | null; effort: string | null; status: TaskStatus;
  createdAt: string; startedAt: string | null; finishedAt: string | null;
  filesOutsideCwd: string[]; filesChangedState: 'pending' | 'collected' | 'unavailable';
}
export interface Message {
  id: string;
  projectId: string;
  senderId: string;
  recipientId: string;
  requestId: string;
  threadId: string;
  text: string;
  createdAt: string;
  acknowledgedAt: string | null;
  wake: boolean;
}
export interface HubEvent {
  sequence: number;
  projectId: string;
  type: string;
  data: unknown;
  createdAt: string;
}

export class HubError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}
