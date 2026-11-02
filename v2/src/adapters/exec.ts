import { createInterface } from 'node:readline';
import { readdir, readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, normalize, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launch } from '../process.js';
import type { WorkerProcess } from '../process.js';
import { redact } from '../config.js';
import type { Session, Task, TurnResult } from '../contracts.js';
import { array, emptyResult, object } from './types.js';
import type { Adapter, AdapterOptions, TurnContext } from './types.js';

function conversationId(value: unknown): string | undefined {
  const obj = object(value);
  for (const key of ['conversation_id', 'conversationId', 'cascade_id', 'cascadeId', 'session_id', 'sessionId']) {
    if (typeof obj[key] === 'string' && obj[key]) return obj[key];
  }
  for (const key of ['result', 'data', 'conversation', 'session']) {
    const nested = obj[key]; if (nested && typeof nested === 'object') { const id = conversationId(nested); if (id) return id; }
  }
  return undefined;
}
function resultText(value: unknown): string {
  const obj = object(value);
  for (const key of ['response', 'result', 'text', 'message']) {
    if (typeof obj[key] === 'string') return obj[key];
    if (obj[key] && typeof obj[key] === 'object') { const nested = resultText(obj[key]); if (nested) return nested; }
  }
  return array(obj.content).map(object).map(x => typeof x.text === 'string' ? x.text : '').join('');
}

async function agyProject(cwd: string): Promise<string | undefined> {
  const root = join(homedir(), '.gemini', 'config', 'projects');
  const key = (path: string): string => {
    let resolved = path;
    if (path.startsWith('file:')) { try { resolved = fileURLToPath(path); } catch { return path; } }
    resolved = normalize(resolve(resolved));
    return process.platform === 'win32' ? resolved.toLowerCase() : resolved;
  };
  const files = await readdir(root).catch(() => []);
  for (const file of files.filter(x => x.endsWith('.json'))) {
    try {
      const data = object(JSON.parse(await readFile(join(root, file), 'utf8')));
      if (typeof data.id !== 'string') continue;
      for (const entry of array(object(data.projectResources).resources).map(object)) {
        if ([entry.folderUri, object(entry.gitFolder).folderUri].some(uri => typeof uri === 'string' && key(uri) === key(cwd))) return data.id;
      }
    } catch { /* A stale unrelated project entry should not prevent launching. */ }
  }
  return undefined;
}

export class ExecAdapter implements Adapter {
  readonly resident = false;
  readonly #options: AdapterOptions;
  #process: WorkerProcess | undefined;
  constructor(options: AdapterOptions) { this.#options = options; }

  async #command(session: Session): Promise<string[]> {
    const { config, command } = this.#options;
    if (config.protocol === 'codex') {
      const permission = config.permission_policy === 'deny' ? ['--sandbox', 'read-only', '-c', 'approval_policy="never"'] :
        [config.session_meta.yolo === true || config.session_meta.yolo === 'true' || config.session_meta.yolo === 1 ? '--yolo' : '--approve-for-me'];
      const args = [...command, 'exec', '--json', '--ignore-user-config', '-c', 'cli_auth_credentials_store="auto"',
        ...permission, '--skip-git-repo-check', '--thread-source', 'subagent', '-C', session.cwd];
      for (const value of config.config_overrides) args.push('-c', value);
      for (const server of this.#options.mcpServers) if ('command' in server) {
        args.push('-c', `mcp_servers.agent_bridge.command=${JSON.stringify(server.command)}`,
          '-c', `mcp_servers.agent_bridge.args=${JSON.stringify(server.args)}`);
        args.push('-c', `mcp_servers.agent_bridge.env_vars=${JSON.stringify(server.env.map(item => item.name))}`);
      }
      if (session.model) args.push('-m', session.model);
      if (session.effort) args.push('-c', `model_reasoning_effort=${JSON.stringify(session.effort === 'off' ? 'none' : session.effort)}`);
      if (session.nativeSessionId) args.push('resume', session.nativeSessionId);
      return [...args, '-'];
    }
    const args = [...command];
    if (session.model) args.push('--model', session.model);
    if (session.effort) args.push('--effort', session.effort === 'off' ? 'low' : session.effort === 'max' ? 'high' : session.effort);
    args.push('--add-dir', session.cwd);
    if (session.nativeSessionId) args.push('--conversation', session.nativeSessionId);
    else {
      const project = await agyProject(session.cwd);
      args.push(...(project ? ['--project', project] : ['--new-project']));
    }
    args.push('-p', '', '--input-format', 'stream-json', '--output-format', 'stream-json');
    if (config.permission_policy !== 'deny') args.push('--dangerously-skip-permissions');
    if (config.print_timeout) args.push('--print-timeout', config.print_timeout);
    return args;
  }

  async run(session: Session, task: Task, context: TurnContext): Promise<TurnResult> {
    context.signal.throwIfAborted();
    const result = emptyResult();
    const resumed = session.nativeSessionId !== null;
    const codex = this.#options.config.protocol === 'codex';
    result.observedModel = session.model;
    result.observedEffort = session.effort;
    if (codex && session.effort === 'off') result.observedEffort = 'none';
    if (!codex && session.effort === 'off') result.observedEffort = 'low';
    if (!codex && session.effort === 'max') result.observedEffort = 'high';
    const command = await this.#command(session);
    context.signal.throwIfAborted();
    const worker = launch(command, session.cwd, this.#options.env, this.#options.config.shutdown_grace_sec);
    this.#process = worker;
    const abort = () => { void worker.stop().catch(() => {}); };
    context.signal.addEventListener('abort', abort, { once: true });
    let stderr = '', completed = false, failed = false, final: Record<string, unknown> | undefined;
    const errors: string[] = [];
    worker.child.stderr.setEncoding('utf8').on('data', chunk => {
      const text = redact(String(chunk), this.#options.env); stderr += text;
      context.event('worker.stderr', { text });
    });
    worker.child.stdin.on('error', () => {}); // Early process exit is reported below with stderr.
    worker.child.stdin.end(codex ? task.message : JSON.stringify({ event: 'user', message: { role: 'user', content: task.message } }) + '\n');
    try {
      for await (const line of createInterface({ input: worker.child.stdout, crlfDelay: Infinity })) {
        if (!line.trim()) continue;
        let event: Record<string, unknown>;
        try { event = object(JSON.parse(line)); }
        catch { context.event('worker.raw', { text: redact(line, this.#options.env) }); continue; }
        const oldId = session.nativeSessionId;
        if (codex) {
          if (event.type === 'thread.started' && typeof event.thread_id === 'string') session.nativeSessionId = event.thread_id;
          const item = object(event.item);
          if (event.type === 'item.completed' && item.type === 'agent_message' && typeof item.text === 'string') {
            result.text = item.text; context.output(item.text);
          }
          if (event.type === 'item.completed' && item.type === 'file_change') {
            for (const change of array(item.changes).map(object)) if (typeof change.path === 'string') result.filesChanged.push(change.path);
          }
          if (event.type === 'turn.completed') { completed = true; result.usage = object(event.usage); }
          if (event.type === 'turn.failed') failed = true;
          if (event.type === 'error' || event.type === 'turn.failed') {
            const message = event.message ?? object(event.error).message ?? event.error ?? 'Worker failed';
            errors.push(redact(String(message), this.#options.env));
          }
        } else {
          session.nativeSessionId = conversationId(event) ?? session.nativeSessionId;
          const step = object(event.step_update);
          const delta = step.text_delta ?? event.text_delta ?? event.delta;
          if (typeof delta === 'string') { result.text += delta; context.output(delta); }
          if (event.event === 'result' || ['result', 'final', 'turn_complete', 'completed'].includes(String(event.type)) || (typeof event.status === 'string' && ('response' in event || 'error' in event))) {
            final = { ...event, ...object(event.result) };
          }
          const usage = object(event.usage ?? step.usage); if (Object.keys(usage).length) result.usage = usage;
          const info = object(step.tool_info), parameters = object(info.parameters);
          if (/write|edit|replace|delete|remove|move|rename|patch|create/i.test(String(info.name ?? step.tool_name))) {
            for (const key of ['Path', 'path', 'file', 'filePath', 'TargetFile', 'AbsolutePath']) if (typeof parameters[key] === 'string') result.filesChanged.push(parameters[key]);
          }
        }
        if (session.nativeSessionId !== oldId) context.saveSession();
        context.event('worker.event', event);
      }
      const exit = await worker.exited;
      if (context.signal.aborted) return { ...result, stopReason: 'cancelled' };
      if (codex) {
        if (failed || !completed) { result.stopReason = 'error'; result.error = errors.at(-1) ?? (stderr.trim() || `Codex exited (${exit.code}) without turn.completed`); }
        else result.warnings.push(...errors);
        if (exit.code !== 0) result.warnings.push(`Codex exited with code ${exit.code}`);
      } else if (final) {
        result.text = resultText(final) || result.text;
        session.nativeSessionId = conversationId(final) ?? session.nativeSessionId; context.saveSession();
        if (Object.keys(object(final.usage)).length) result.usage = object(final.usage);
        result.usage.scope = resumed ? 'conversation' : 'turn';
        const detail = typeof final.error === 'string' ? final.error : typeof object(final.error).message === 'string' ? String(object(final.error).message) : final.error ? JSON.stringify(final.error) : null;
        const error = detail || (final.status && final.status !== 'SUCCESS' ? String(final.status) : null);
        if (error) {
          if (/CodeContent|tool.*schema|convert.*tool/i.test(error) && result.text.trim() && exit.code === 0) result.warnings.push(error);
          else { result.stopReason = 'error'; result.error = redact(error, this.#options.env); }
        }
        if (exit.code !== 0) result.warnings.push(`Antigravity exited with code ${exit.code}`);
      } else if (exit.code !== 0 || !result.text) {
        result.stopReason = 'error'; result.error = stderr.trim() || `Antigravity exited (${exit.code}) without a result`;
      } else result.warnings.push('Antigravity returned text without a final result event');
      return result;
    } finally {
      context.signal.removeEventListener('abort', abort);
      await worker.stop();
      if (this.#process === worker) this.#process = undefined;
    }
  }

  async close(): Promise<void> { await this.#process?.stop(); }
}
