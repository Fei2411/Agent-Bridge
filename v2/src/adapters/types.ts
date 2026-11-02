import type { McpServer } from '@agentclientprotocol/sdk/unstable';
import type { Session, Task, TurnResult } from '../contracts.js';
import type { AppConfig, WorkerConfig } from '../config.js';

export interface TurnContext {
  signal: AbortSignal;
  output(text: string): void;
  event(type: string, data: unknown): void;
  saveSession(): void;
}
export interface AdapterOptions {
  name: string; config: WorkerConfig; command: string[]; env: NodeJS.ProcessEnv;
  environment: AppConfig['env'];
  mcpServers: McpServer[];
}
export interface Adapter {
  readonly resident: boolean;
  run(session: Session, task: Task, context: TurnContext): Promise<TurnResult>;
  close(): Promise<void>;
}
export const emptyResult = (): TurnResult => ({
  text: '', stopReason: 'end_turn', error: null, warnings: [], observedModel: null,
  observedEffort: null, usage: {}, filesChanged: [],
});
export function object(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
export function array(value: unknown): unknown[] { return Array.isArray(value) ? value : []; }
