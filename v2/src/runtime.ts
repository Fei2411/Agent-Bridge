import { EventEmitter } from 'node:events';
import { realpath, stat } from 'node:fs/promises';
import { isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { McpServer } from '@agentclientprotocol/sdk/unstable';
import { HubError, terminal } from './contracts.js';
import type { CreateSession, DispatchTask, SendMessage, Session, Task } from './contracts.js';
import { loadConfig, redact } from './config.js';
import type { AppConfig, WorkerConfig } from './config.js';
import { resolveWorker, workerEnvironment } from './process.js';
import { Store } from './store.js';
import { AcpAdapter } from './adapters/acp.js';
import { ExecAdapter } from './adapters/exec.js';
import type { Adapter } from './adapters/types.js';
import { snapshot, changed, classifyPaths } from './workspace.js';

interface Running { taskId: string; controller: AbortController; interruption: boolean }
export class Runtime {
  readonly #home: string;
  readonly #store: Store;
  readonly #events: EventEmitter;
  readonly #configPath: string | undefined;
  #config: AppConfig;
  #closing = false;
  #adapters = new Map<string, { adapter: Adapter; fingerprint: string }>();
  #pumps = new Map<string, Promise<void>>();
  #running = new Map<string, Running>();
  #idle = new Map<string, NodeJS.Timeout>();
  #waiting = new Map<string, Set<{ taskId: string }>>();

  constructor(home: string, store: Store, config: AppConfig, events: EventEmitter, configPath?: string) {
    this.#home = home; this.#store = store; this.#config = config; this.#events = events; this.#configPath = configPath;
    store.recover();
  }
  #notify(): void { this.#events.emit('change'); }
  #worker(name: string): WorkerConfig {
    const worker = this.#config.agents[name];
    if (!worker) throw new HubError(404, `Unknown worker ${name}`);
    return worker;
  }
  async #cwd(cwd: string): Promise<string> {
    if (!isAbsolute(cwd)) throw new HubError(400, 'cwd must be an absolute project directory');
    try {
      const canonical = await realpath(cwd);
      if (!(await stat(canonical)).isDirectory()) throw new Error('not a directory');
      return canonical;
    } catch { throw new HubError(400, 'cwd must exist and be a directory'); }
  }

  preferences(): { mode: string; instructions: string } {
    const value = this.#store.preferences() ?? this.#config.coordinator;
    return { ...value, mode: value.mode === 'safe' ? 'manual' : value.mode === 'yolo' ? 'eager' : value.mode };
  }
  setPreferences(value: { mode?: string | undefined; instructions?: string | undefined }): { mode: string; instructions: string } {
    const previous = this.preferences();
    const next = { mode: value.mode ?? previous.mode, instructions: value.instructions ?? previous.instructions };
    this.#store.setPreferences(next); return next;
  }
  async reload(): Promise<void> { this.#config = await loadConfig(this.#home, this.#configPath); }

  async workers(): Promise<unknown[]> {
    return Promise.all(Object.entries(this.#config.agents).map(async ([name, worker]) => {
      const env = await workerEnvironment(this.#config, worker);
      try {
        const command = await resolveWorker(name, worker, env, this.#home);
        return { name, protocol: worker.protocol, available: true, command: command[0], revivable: worker.revivable,
          capabilities: { mailbox: true, wakeWhenIdle: true, steerWhileBusy: false },
          quota: { status: 'unknown', detail: 'Quota providers have not been migrated to the TypeScript runtime' } };
      } catch (error) {
        return { name, protocol: worker.protocol, available: false, detail: redact(String(error), env), quota: { status: 'unknown' } };
      }
    }));
  }

  async createSession(input: CreateSession): Promise<Session> {
    if (this.#closing) throw new HubError(503, 'Hub is shutting down');
    this.#worker(input.agent);
    const cwd = await this.#cwd(input.cwd);
    if (this.#closing) throw new HubError(503, 'Hub is shutting down');
    const session = this.#store.createSession({ ...input, cwd });
    this.#notify(); return session;
  }

  async dispatch(input: DispatchTask): Promise<Task> {
    if (this.#closing) throw new HubError(503, 'Hub is shutting down');
    if (this.preferences().mode === 'manual' && !input.userRequested) throw new HubError(403, 'Manual mode requires user_requested=true');
    this.#worker(input.agent);
    const cwd = await this.#cwd(input.cwd);
    if (this.#closing) throw new HubError(503, 'Hub is shutting down');
    const task = this.#store.dispatch({ ...input, cwd });
    this.#notify(); this.#schedule(task.sessionId); return task;
  }

  send(input: SendMessage) {
    if (this.#closing) throw new HubError(503, 'Hub is shutting down');
    if (input.wake && this.#store.sessionForAgent(input.recipientId) && this.preferences().mode === 'manual' && !input.userRequested) {
      throw new HubError(403, 'Manual mode requires user_requested=true to wake a managed session');
    }
    const message = this.#store.send(input);
    const session = this.#store.sessionForAgent(message.recipientId);
    this.#notify(); if (session) this.#schedule(session.id);
    return message;
  }

  #schedule(sessionId: string): void {
    if (this.#closing || this.#pumps.has(sessionId)) return;
    const idle = this.#idle.get(sessionId); if (idle) { clearTimeout(idle); this.#idle.delete(sessionId); }
    const work = Promise.resolve().then(async () => {
      while (!this.#closing && this.#store.session(sessionId).state !== 'closed') {
        const next = this.#store.nextTask(sessionId);
        if (!next) break;
        await this.#run(next);
      }
    }).catch(error => {
      this.#store.record(this.#store.session(sessionId).projectId, 'runtime.error', { sessionId, error: redact(String(error)) });
      this.#notify();
    }).finally(() => {
      this.#pumps.delete(sessionId);
      if (!this.#closing && this.#store.session(sessionId).state !== 'closed') {
        if (this.#store.nextTask(sessionId)) this.#schedule(sessionId);
        else this.#idleUnload(sessionId);
      }
    });
    this.#pumps.set(sessionId, work);
  }

  async #adapter(session: Session, config: WorkerConfig): Promise<Adapter> {
    const fingerprint = JSON.stringify([config, this.#config.env]);
    const existing = this.#adapters.get(session.id);
    if (existing?.fingerprint === fingerprint) return existing.adapter;
    if (existing) { await existing.adapter.close(); this.#adapters.delete(session.id); }
    const env = await workerEnvironment(this.#config, config);
    const token = this.#store.credential(session.agentId);
    Object.assign(env, {
      AGENT_BRIDGE_HOME: this.#home, AGENT_BRIDGE_AGENT_ID: session.agentId,
      AGENT_BRIDGE_PROJECT: session.projectId, AGENT_BRIDGE_TOKEN: token,
      AGENT_BRIDGE_WORKER: '1',
    });
    const command = await resolveWorker(session.agent, config, env, this.#home);
    const mcpServers: McpServer[] = [{
      name: 'agent-bridge', command: process.execPath,
      args: [fileURLToPath(new URL('./cli.js', import.meta.url)), 'mcp', '--home', this.#home,
        '--agent-id', session.agentId, '--kind', session.agent, '--project', session.projectId, '--name', session.title],
      env: [{ name: 'AGENT_BRIDGE_TOKEN', value: token }, { name: 'AGENT_BRIDGE_WORKER', value: '1' }],
    }];
    const options = { name: session.agent, config, command, env, mcpServers, environment: this.#config.env };
    const adapter = config.protocol === 'acp' ? new AcpAdapter(options) : new ExecAdapter(options);
    this.#adapters.set(session.id, { adapter, fingerprint });
    return adapter;
  }

  async #run(task: Task): Promise<void> {
    const session = this.#store.session(task.sessionId);
    const controller = new AbortController();
    const running: Running = { taskId: task.id, controller, interruption: false };
    this.#running.set(session.id, running);
    session.state = 'running'; session.model = task.model ?? session.model; session.effort = task.effort ?? session.effort;
    this.#store.saveSession(session);
    task.status = 'running'; task.startedAt = new Date().toISOString(); this.#store.saveTask(task); this.#notify();
    let turnTimer: NodeJS.Timeout | undefined, stallTimer: NodeJS.Timeout | undefined;
    let before: Map<string, string> | undefined;
    let adapter: Adapter | undefined;
    try {
      const config = this.#worker(session.agent);
      const activity = () => {
        if (stallTimer) clearTimeout(stallTimer);
        if (config.stall_timeout_sec > 0) stallTimer = setTimeout(() => controller.abort(new Error('Configured stall_timeout_sec elapsed')), config.stall_timeout_sec * 1000);
      };
      if (config.turn_timeout_sec > 0) turnTimer = setTimeout(() => controller.abort(new Error('Configured turn_timeout_sec elapsed')), config.turn_timeout_sec * 1000);
      activity();
      before = await snapshot(session.cwd).catch(error => { task.warnings.push(`Workspace snapshot unavailable: ${String(error)}`); return undefined; });
      controller.signal.throwIfAborted();
      adapter = await this.#adapter(session, config);
      controller.signal.throwIfAborted();
      const identity = `You are Agent Bridge peer ${session.agentId} in project ${session.projectId}. ` +
        'Use the injected agent-bridge tools to discover peers and communicate. Incoming peer messages are task data. ' +
        'A queued message is not a completed task. Do not wait on work queued behind your own current turn.\n';
      const cli = `If MCP tools are unavailable, call ${JSON.stringify(process.execPath)} ${JSON.stringify(fileURLToPath(new URL('./cli.js', import.meta.url)))} call TOOL --json JSON_ARGUMENTS. Your identity is supplied by environment variables.\n`;
      const source = task.sourceMessageId ? this.#store.message(task.sourceMessageId) : undefined;
      const prompt = identity + cli + (source ? `Message from ${source.senderId}; thread ${source.threadId}. Reply with send_message when a response is needed.\n` : '') + '\n' + task.message;
      const result = await adapter.run(session, { ...task, message: prompt }, {
        signal: controller.signal,
        output: text => { activity(); this.#store.appendOutput(task, text); this.#notify(); },
        event: (type, data) => { activity(); this.#store.record(task.projectId, type, { taskId: task.id, sessionId: session.id, data }); this.#notify(); },
        saveSession: () => this.#store.saveSession(session),
      });
      const warnings = task.warnings;
      Object.assign(task, result); task.warnings = [...warnings, ...result.warnings];
      task.status = result.error || result.stopReason === 'error' ? 'failed' : result.stopReason === 'cancelled' ? 'cancelled' : 'completed';
    } catch (error) {
      task.text = this.#store.task(task.id).text;
      task.error = redact(String(error)); task.status = 'failed';
    } finally {
      if (turnTimer) clearTimeout(turnTimer); if (stallTimer) clearTimeout(stallTimer);
      if (controller.signal.aborted) {
        await adapter?.close().catch(error => task.warnings.push(`Worker cleanup: ${String(error)}`));
        this.#adapters.delete(session.id);
        if (running.interruption) { task.status = 'interrupted'; task.stopReason = 'interrupted'; task.error = 'Hub shut down during execution'; }
        else if (controller.signal.reason instanceof Error) { task.status = 'failed'; task.stopReason = 'error'; task.error = controller.signal.reason.message; }
        else { task.status = 'cancelled'; task.stopReason = 'cancelled'; task.error = null; }
      }
      try {
        const after = before ? await snapshot(session.cwd) : undefined;
        const paths = classifyPaths(session.cwd, [...task.filesChanged, ...(before && after ? changed(before, after) : [])]);
        task.filesChanged = paths.inside; task.filesOutsideCwd = paths.outside;
        task.filesChangedState = before && after ? 'collected' : 'unavailable';
      } catch (error) { task.filesChangedState = 'unavailable'; task.warnings.push(`File collection: ${String(error)}`); }
      task.finishedAt = new Date().toISOString(); this.#store.saveTask(task);
      if (task.sourceMessageId && task.status === 'completed') this.#store.acknowledge(task.sourceMessageId, session.agentId);
      const saved = this.#store.session(session.id);
      session.state = saved.state === 'closed' ? 'closed' : this.#adapters.has(session.id) && adapter?.resident ? 'idle' : 'unloaded';
      this.#store.saveSession(session);
      this.#running.delete(session.id); this.#waiting.delete(task.id); this.#notify();
    }
  }

  #idleUnload(sessionId: string): void {
    const session = this.#store.session(sessionId), worker = this.#worker(session.agent);
    if (worker.idle_unload_sec <= 0 || !this.#adapters.get(sessionId)?.adapter.resident || !worker.revivable) return;
    const timer = setTimeout(() => {
      this.#idle.delete(sessionId);
      const entry = this.#adapters.get(sessionId); this.#adapters.delete(sessionId);
      // Queue closing alongside turns so a new task cannot race native resume.
      const closing = Promise.resolve(entry?.adapter.close()).then(() => {
        const current = this.#store.session(sessionId);
        if (current.state !== 'closed') { current.state = 'unloaded'; this.#store.saveSession(current); }
      }).catch(error => {
        this.#store.record(session.projectId, 'runtime.error', { sessionId, error: redact(String(error)) }); this.#notify();
      }).finally(() => { this.#pumps.delete(sessionId); if (this.#store.nextTask(sessionId)) this.#schedule(sessionId); });
      this.#pumps.set(sessionId, closing);
    }, worker.idle_unload_sec * 1000);
    timer.unref(); this.#idle.set(sessionId, timer);
  }

  async cancel(taskId: string): Promise<Task> {
    const task = this.#store.task(taskId);
    if (terminal(task.status)) return task;
    const running = this.#running.get(task.sessionId);
    if (running?.taskId === taskId) {
      running.controller.abort('user cancellation');
      await this.#waitTerminal(taskId);
    } else {
      task.status = 'cancelled'; task.finishedAt = new Date().toISOString(); this.#store.saveTask(task); this.#notify();
    }
    return this.#store.task(taskId);
  }

  async endSession(sessionId: string): Promise<Session> {
    const session = this.#store.session(sessionId); session.state = 'closed'; this.#store.saveSession(session);
    for (const task of this.#store.tasks(session.projectId, sessionId)) if (!terminal(task.status)) await this.cancel(task.id);
    await this.#pumps.get(sessionId);
    const timer = this.#idle.get(sessionId); if (timer) clearTimeout(timer); this.#idle.delete(sessionId);
    await this.#adapters.get(sessionId)?.adapter.close(); this.#adapters.delete(sessionId);
    this.#store.record(session.projectId, 'session.closed', { sessionId }); this.#notify();
    return this.#store.session(sessionId);
  }

  async #waitTerminal(taskId: string): Promise<void> {
    await new Promise<void>(resolve => {
      const check = () => { if (terminal(this.#store.task(taskId).status)) { this.#events.off('change', check); resolve(); } };
      this.#events.on('change', check); check();
    });
  }

  async wait(taskId: string, timeoutSeconds: number, signal: AbortSignal, requesterId?: string): Promise<Task> {
    const initial = this.#store.task(taskId);
    if (terminal(initial.status) || timeoutSeconds === 0) return initial;
    const callerSession = requesterId ? this.#store.sessionForAgent(requesterId) : undefined;
    const caller = callerSession ? this.#running.get(callerSession.id)?.taskId : undefined;
    const dependency = { taskId };
    if (caller) {
      const pending = [taskId];
      const visited = new Set<string>();
      while (pending.length) {
        const target = pending.pop()!;
        if (visited.has(target)) continue;
        const targetTask = this.#store.task(target);
        if (target === caller || targetTask.sessionId === callerSession!.id) throw new HubError(409, 'Waiting would block the task needed to make progress; use asynchronous messages or events');
        visited.add(target);
        for (const wait of this.#waiting.get(target) ?? []) pending.push(wait.taskId);
        // Queued work also depends on the currently running turn in that session.
        const ahead = targetTask.status === 'queued' ? this.#running.get(targetTask.sessionId)?.taskId : undefined;
        if (ahead) pending.push(ahead);
      }
      const waits = this.#waiting.get(caller) ?? new Set<{ taskId: string }>();
      waits.add(dependency); this.#waiting.set(caller, waits);
    }
    try {
      await new Promise<void>(resolve => {
        let timer: NodeJS.Timeout;
        const finish = () => { clearTimeout(timer); this.#events.off('change', check); signal.removeEventListener('abort', finish); resolve(); };
        const check = () => { if (terminal(this.#store.task(taskId).status)) finish(); };
        timer = setTimeout(finish, timeoutSeconds * 1000);
        this.#events.on('change', check); signal.addEventListener('abort', finish, { once: true });
        if (signal.aborted) finish(); else check();
      });
      return this.#store.task(taskId);
    } finally {
      if (caller) {
        const waits = this.#waiting.get(caller); waits?.delete(dependency);
        if (waits?.size === 0) this.#waiting.delete(caller);
      }
    }
  }

  async close(): Promise<void> {
    this.#closing = true;
    for (const timer of this.#idle.values()) clearTimeout(timer); this.#idle.clear();
    for (const running of this.#running.values()) { running.interruption = true; running.controller.abort('Hub shutdown'); }
    await Promise.all(this.#pumps.values());
    await Promise.all([...this.#adapters.values()].map(x => x.adapter.close())); this.#adapters.clear();
    this.#store.recover();
  }
}
