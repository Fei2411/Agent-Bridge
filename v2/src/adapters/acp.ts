import { Readable, Writable } from 'node:stream';
import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { basename, join } from 'node:path';
import { client, ndJsonStream, PROTOCOL_VERSION } from '@agentclientprotocol/sdk/unstable';
import type { ClientConnection } from '@agentclientprotocol/sdk/unstable';
import { parse as yaml } from 'yaml';
import { launch, capture, enforceEnvDeny } from '../process.js';
import type { WorkerProcess } from '../process.js';
import type { Session, Task, TurnResult } from '../contracts.js';
import { redact } from '../config.js';
import { array, object, emptyResult } from './types.js';
import type { Adapter, AdapterOptions, TurnContext } from './types.js';
import { option, effortFor, modelFor, modelLabel, cursorBase, cursorParameters } from './selection.js';

export class AcpAdapter implements Adapter {
  readonly resident = true;
  readonly #options: AdapterOptions;
  #process: WorkerProcess | undefined;
  #connection: ClientConnection | undefined;
  #closing: Promise<void> | undefined;
  #config: unknown[] = [];
  #context: TurnContext | undefined;
  #result = emptyResult();
  #stderr = '';
  #nativeDsh = false;
  #dshSelection = '';
  #prompting = false;
  #catalog = new Map<string, string>();

  constructor(options: AdapterOptions) { this.#options = options; }

  async #request(method: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    if (!this.#connection) throw new Error('ACP worker is not connected');
    const timeout = this.#options.config.startup_timeout_sec;
    const connection = this.#connection;
    const timer = timeout > 0 ? setTimeout(() => { connection.close(new Error(`Configured startup_timeout_sec elapsed during ${method}`)); void this.close().catch(() => {}); }, timeout * 1000) : undefined;
    try {
      const result = object(await connection.agent.request(method, params));
      if (Array.isArray(result.configOptions)) this.#config = result.configOptions;
      return result;
    } finally { if (timer) clearTimeout(timer); }
  }

  async #set(session: Session, key: string, value: string): Promise<void> {
    if (option(this.#config, key).current === value) return;
    await this.#request('session/set_config_option', { sessionId: session.nativeSessionId, configId: key, value });
  }

  async #configure(session: Session): Promise<void> {
    const { name, config } = this.#options;
    const warn = (message: string) => this.#result.warnings.push(message);
    const modes: Record<string, string> = { kimi: 'yolo', claude: 'bypassPermissions', devin: 'bypass' };
    if (config.permission_policy !== 'deny') {
      try {
        if (modes[name]) await this.#request('session/set_mode', { sessionId: session.nativeSessionId, modeId: modes[name] });
        if (name === 'zcode') await this.#set(session, 'mode', 'yolo');
        if (name === 'minimax') await this.#set(session, 'permissionMode', 'bypassPermissions');
      } catch (error) { warn(`Permission mode was not applied; using ACP permission callbacks: ${String(error)}`); }
    }
    if (name === 'dsh' && !this.#nativeDsh) {
      this.#result.observedModel = session.model;
      this.#result.observedEffort = session.effort === 'medium' ? 'high' : session.effort;
      return;
    }
    if (name === 'grok') {
      const effort = session.effort === 'max' ? 'xhigh' : session.effort === 'off' ? 'none' : session.effort;
      if (session.model) {
        const params = { sessionId: session.nativeSessionId, modelId: session.model, ...(effort ? { _meta: { reasoningEffort: effort } } : {}) };
        try { await this.#request('session/setModel', params); }
        catch (error) { if (object(error).code !== -32601) throw error; await this.#request('session/set_model', params); }
        this.#result.observedModel = session.model; this.#result.observedEffort = effort;
      } else if (effort) warn('Grok cannot change effort on an existing session without a model ID');
      return;
    }
    let selected = session.model;
    if (name === 'cursor') {
      const base = selected ? cursorBase(selected, this.#catalog.get(selected) ?? selected, this.#config) : undefined;
      if (selected && !base) throw new Error(`Cursor model ${selected} is not advertised by this session`);
      if (base) await this.#set(session, 'model', base.value);
      const targets = cursorParameters(selected ?? '', this.#catalog.get(selected ?? '') ?? '', base ?? { value: '', name: '' }, session.effort, this.#config);
      for (const [key, value] of Object.entries(targets)) await this.#set(session, key, value);
      this.#result.observedModel = selected ?? option(this.#config, 'model').current;
    } else if (selected) {
      const { choices } = option(this.#config, 'model');
      const target = modelFor(name, selected, choices) ?? (choices.length === 0 ? selected : undefined);
      if (!target) throw new Error(`${name} model ${selected} is unavailable; offered: ${choices.map(c => modelLabel(name, c.value)).join(', ')}`);
      try { await this.#set(session, 'model', target); }
      catch (error) { throw new Error(`${name} rejected model ${selected}; offered: ${choices.map(c => modelLabel(name, c.value)).join(', ')}; ${String(error)}`); }
      const current = option(this.#config, 'model').current;
      if (current && current !== target) throw new Error(`${name} confirmed model ${current} after selecting ${target}`);
      this.#result.observedModel = current ? modelLabel(name, current) : selected;
    } else {
      selected = option(this.#config, 'model').current;
      this.#result.observedModel = selected ? modelLabel(name, selected) : null;
    }
    const effortIds: Record<string, string> = { kimi: 'thinking', zcode: 'thought', minimax: 'thinkingEffort', dsh: 'reasoning_effort' };
    const effortOption = effortIds[name] ?? String(this.#config.map(object).find(o => o.category === 'thought_level')?.id ?? 'effort');
    const current = option(this.#config, effortOption);
    this.#result.observedEffort = current.current;
    if (session.effort) {
      const mapped = effortFor(name, session.effort, current.choices.map(c => c.value));
      if (!mapped || name === 'devin') warn(`${name} cannot map effort ${session.effort}; offered: ${current.choices.map(c => c.value).join(', ') || '(none)'}`);
      else {
        try {
          await this.#set(session, effortOption, mapped);
          this.#result.observedEffort = option(this.#config, effortOption).current ?? mapped;
        } catch (error) { warn(`${name} rejected effort ${mapped}: ${String(error)}`); }
      }
    }
  }

  async #ensure(session: Session, context: TurnContext): Promise<void> {
    const { name, config } = this.#options;
    await this.#closing;
    context.signal.throwIfAborted();
    if (this.#connection?.signal.aborted) await this.close();
    if (name === 'dsh' && !this.#nativeDsh && this.#process && this.#dshSelection !== JSON.stringify([session.model, session.effort])) {
      await this.close(); session.nativeSessionId = null;
      this.#result.warnings.push('Legacy DSH model/effort change starts a new native session');
    }
    if (!this.#connection) {
      let command = [...this.#options.command];
      const env = { ...this.#options.env };
      if (name === 'claude') {
        if (env.OPENROUTER_API_KEY && !env.ANTHROPIC_AUTH_TOKEN && (!env.ANTHROPIC_API_KEY || env.ANTHROPIC_BASE_URL)) {
          env.ANTHROPIC_AUTH_TOKEN = env.OPENROUTER_API_KEY; env.ANTHROPIC_BASE_URL ??= 'https://openrouter.ai/api';
        }
        if (env.ANTHROPIC_AUTH_TOKEN && env.ANTHROPIC_BASE_URL) env.ANTHROPIC_API_KEY = '';
      }
      if (name === 'dsh') {
        this.#nativeDsh = command.includes('--profile') && command.includes('acp');
        this.#dshSelection = JSON.stringify([session.model, session.effort]);
        try {
          const settings = object(yaml(await readFile(join(env.DSH_HOME ?? join(homedir(), '.dsh'), 'settings.yaml'), 'utf8')));
          const model = object(settings['agent-default-model']);
          if (typeof model.provider === 'string') env.DSH_ACP_PROVIDER ??= model.provider;
          if (typeof model.model === 'string') env.DSH_ACP_MODEL ??= model.model;
        } catch (error) { if (object(error).code !== 'ENOENT') this.#result.warnings.push(`DSH settings: ${String(error)}`); }
        if (session.model) {
          const slash = session.model.indexOf('/');
          if (slash >= 0) { env.DSH_ACP_PROVIDER = session.model.slice(0, slash); env.DSH_ACP_MODEL = session.model.slice(slash + 1); }
          else env.DSH_ACP_MODEL = session.model;
        }
        if (session.effort) env.DSH_ACP_REASONING_EFFORT = session.effort === 'medium' ? 'high' : session.effort;
      }
      if (name === 'grok') {
        const extras = [...(session.model ? ['--model', session.model] : []), ...(session.effort ? ['--effort', session.effort === 'max' ? 'xhigh' : session.effort === 'off' ? 'none' : session.effort] : [])];
        const at = command.findIndex(x => ['stdio', 'headless', 'serve'].includes(x));
        command.splice(at >= 0 ? at : command.length, 0, ...extras);
      }
      enforceEnvDeny(env, this.#options.environment, config);
      if (name === 'cursor' && session.model) {
        const at = command.indexOf('acp');
        if (at < 0) throw new Error('Cursor command needs the acp subcommand');
        const listing = await capture([...command.slice(0, at), /^agent(?:\.exe)?$/i.test(basename(command[0]!)) ? 'models' : '--list-models'], { env, cwd: session.cwd, signal: context.signal });
        for (const line of listing.stdout.split('\n')) {
          const match = /^\s*(\S+) - (.+)$/.exec(line); if (match) this.#catalog.set(match[1]!, match[2]!);
        }
        if (!this.#catalog.has(session.model)) throw new Error(`Cursor model ${session.model} is unavailable; offered: ${[...this.#catalog.keys()].join(', ')}`);
        command.splice(at, 0, '--model', session.model);
      }
      context.signal.throwIfAborted();
      this.#process = launch(command, config.cwd ?? session.cwd, env, config.shutdown_grace_sec);
      this.#process.child.stderr.setEncoding('utf8').on('data', chunk => { this.#stderr += redact(String(chunk), env); this.#context?.event('worker.stderr', { text: redact(String(chunk), env) }); });
      const app = client()
        .onRequest('session/request_permission', ({ params }) => {
          const preferred = config.permission_policy === 'allow_always' ? ['allow_always', 'allow_once'] : ['allow_once', 'allow_always'];
          const choice = config.permission_policy === 'deny' ? undefined : preferred.flatMap(kind => params.options.filter(x => x.kind === kind))[0];
          return { outcome: choice ? { outcome: 'selected' as const, optionId: choice.optionId } : { outcome: 'cancelled' as const } };
        })
        .onNotification('session/update', ({ params }) => {
          const update = object(params.update);
          if (update.sessionUpdate === 'config_option_update') this.#config = array(update.configOptions);
          if (!this.#context || !this.#prompting) return;
          if (update.sessionUpdate === 'agent_message_chunk') {
            const content = object(update.content);
            if (content.type === 'text' && typeof content.text === 'string') {
              this.#result.text += content.text; this.#context.output(content.text);
            }
          } else this.#context.event('worker.update', update);
          if (update.sessionUpdate === 'usage_update') this.#result.usage = update;
          for (const content of array(update.content).map(object)) if (content.type === 'diff' && typeof content.path === 'string') this.#result.filesChanged.push(content.path);
        });
      this.#connection = app.connect(ndJsonStream(Writable.toWeb(this.#process.child.stdin), Readable.toWeb(this.#process.child.stdout) as unknown as ReadableStream<Uint8Array>, { maxMessageBytes: Number.MAX_SAFE_INTEGER }));
      const connection = this.#connection;
      void this.#process.exited.then(() => connection.close(), error => connection.close(error));
      const initialized = await this.#request('initialize', {
        protocolVersion: PROTOCOL_VERSION, clientInfo: { name: 'agent-bridge', version: '2.0.0' },
        clientCapabilities: { ...(name === 'cursor' ? { _meta: { parameterizedModelPicker: true } } : {}) },
      });
      const capabilities = object(initialized.agentCapabilities);
      const nativeId = session.nativeSessionId;
      let loaded = false;
      if (nativeId && (config.revivable || (name === 'dsh' && this.#nativeDsh))) {
        const resume = object(capabilities.sessionCapabilities).resume || ['kimi', 'claude', 'opencode', 'zcode', 'minimax'].includes(name) || this.#nativeDsh;
        try {
          await this.#request(resume ? 'session/resume' : 'session/load', { sessionId: nativeId, cwd: session.cwd, mcpServers: this.#options.mcpServers });
          loaded = true;
        } catch (error) {
          if (!this.#connection || this.#connection.signal.aborted) throw error;
          this.#result.warnings.push(`Native session restore failed; starting a new conversation: ${String(error)}`);
        }
      }
      if (!loaded) {
        if (nativeId && !config.revivable) this.#result.warnings.push('This adapter cannot restore the previous native conversation');
        const meta = { ...config.session_meta, ...(name === 'grok' && session.effort ? { reasoningEffort: session.effort === 'max' ? 'xhigh' : session.effort === 'off' ? 'none' : session.effort } : {}) };
        const response = await this.#request('session/new', { cwd: session.cwd, mcpServers: this.#options.mcpServers, _meta: meta });
        if (typeof response.sessionId !== 'string') throw new Error('ACP did not return a sessionId');
        session.nativeSessionId = response.sessionId; context.saveSession();
      }
    }
    await this.#configure(session);
  }

  async run(session: Session, _task: Task, context: TurnContext): Promise<TurnResult> {
    context.signal.throwIfAborted();
    this.#result = emptyResult(); this.#stderr = '';
    // Ignore replayed history during load; only the active prompt owns output.
    this.#context = context;
    let timer: NodeJS.Timeout | undefined;
    const abort = () => {
      if (this.#connection && this.#prompting && session.nativeSessionId) {
        void this.#connection.agent.notify('session/cancel', { sessionId: session.nativeSessionId }).catch(() => {});
        timer = setTimeout(() => { void this.close().catch(() => {}); }, this.#options.config.shutdown_grace_sec * 1000);
      } else void this.close().catch(() => {});
    };
    context.signal.addEventListener('abort', abort, { once: true });
    try {
      await this.#ensure(session, context);
      context.signal.throwIfAborted();
      this.#context = context; this.#prompting = true;
      const response = await this.#connection!.agent.request('session/prompt', {
        sessionId: session.nativeSessionId!, prompt: [{ type: 'text', text: _task.message }],
      });
      this.#result.stopReason = response.stopReason;
      if (!Object.keys(this.#result.usage).length) this.#result.usage = object(object(response).usage);
      if (this.#options.name === 'kimi' && !this.#result.text && this.#stderr.trim()) {
        this.#result.warnings.push('Kimi returned no text; inspect worker stderr and events before treating this as success');
      }
      return this.#result;
    } catch (error) {
      if (context.signal.aborted) return { ...this.#result, stopReason: 'cancelled' };
      this.#result.error = redact(`${String(error)}${this.#stderr ? `\n${this.#stderr}` : ''}`, this.#options.env);
      this.#result.stopReason = 'error';
      await this.close();
      return this.#result;
    } finally {
      if (timer) clearTimeout(timer);
      this.#context = undefined; this.#prompting = false;
      context.signal.removeEventListener('abort', abort);
    }
  }

  async close(): Promise<void> {
    if (this.#closing) return this.#closing;
    const worker = this.#process, connection = this.#connection;
    this.#process = undefined; this.#connection = undefined; this.#config = [];
    connection?.close();
    this.#closing = (worker?.stop() ?? Promise.resolve()).finally(() => { this.#closing = undefined; });
    return this.#closing;
  }
}
