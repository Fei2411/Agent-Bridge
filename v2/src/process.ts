import { fork, spawn as nativeSpawn } from 'node:child_process';
import type { ChildProcessWithoutNullStreams } from 'node:child_process';
import { access, readdir, stat } from 'node:fs/promises';
import { constants } from 'node:fs';
import { homedir } from 'node:os';
import { basename, delimiter, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import spawn from 'cross-spawn';
import type { AppConfig, WorkerConfig } from './config.js';

export async function capture(command: string[], options: { env?: NodeJS.ProcessEnv; cwd?: string; signal?: AbortSignal } = {}): Promise<{ code: number | null; stdout: string; stderr: string }> {
  if (options.signal) {
    options.signal.throwIfAborted();
    const worker = launch(command, options.cwd ?? process.cwd(), options.env ?? process.env, 5);
    let stdout = '', stderr = '';
    worker.child.stdout.setEncoding('utf8').on('data', chunk => { stdout += chunk; });
    worker.child.stderr.setEncoding('utf8').on('data', chunk => { stderr += chunk; });
    worker.child.stdin.end();
    const cancel = () => { void worker.stop().catch(() => {}); };
    options.signal.addEventListener('abort', cancel, { once: true });
    try {
      const result = await worker.exited;
      options.signal.throwIfAborted();
      return { code: result.code, stdout, stderr };
    } finally { options.signal.removeEventListener('abort', cancel); }
  }
  const child = spawn(command[0]!, command.slice(1), { ...options, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
  let stdout = '', stderr = '';
  child.stdout!.setEncoding('utf8').on('data', chunk => { stdout += chunk; });
  child.stderr!.setEncoding('utf8').on('data', chunk => { stderr += chunk; });
  return new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('close', code => resolve({ code, stdout, stderr }));
  });
}

async function executable(command: string, env: NodeJS.ProcessEnv): Promise<string | undefined> {
  const dirs = isAbsolute(command) || /[/\\]/.test(command) ? [''] : (env.PATH ?? env.Path ?? '').split(delimiter);
  const extensions = process.platform === 'win32' ? ['', ...(env.PATHEXT ?? '.COM;.EXE;.BAT;.CMD').split(';')] : [''];
  for (const dir of dirs) for (const ext of extensions) {
    const path = resolve(dir, command + ext);
    try {
      await access(path, process.platform === 'win32' ? constants.F_OK : constants.X_OK);
      if ((await stat(path)).isFile()) return path;
    } catch { /* Search the next PATH entry. */ }
  }
  return undefined;
}

export async function resolveWorker(name: string, config: WorkerConfig, env: NodeJS.ProcessEnv, home: string): Promise<string[]> {
  const candidates: string[][] = [];
  if (isAbsolute(config.command[0]!) || /[/\\]/.test(config.command[0]!)) candidates.push(config.command);
  if (name === 'codex' && basename(config.command[0]!).replace(/\.exe$/i, '') === 'codex') {
    const args = config.command.slice(1);
    if (env.CODEX_CLI_PATH) candidates.push([env.CODEX_CLI_PATH, ...args]);
    if (process.platform === 'win32') {
      const root = join(env.LOCALAPPDATA ?? join(homedir(), 'AppData', 'Local'), 'OpenAI', 'Codex', 'bin');
      try {
        const bins = (await readdir(root)).map(dir => join(root, dir, 'codex.exe'));
        const dates = await Promise.all(bins.map(async path => ({ path, date: await stat(path).then(s => s.mtimeMs).catch(() => 0) })));
        candidates.push(...dates.sort((a, b) => b.date - a.date).filter(x => x.date).map(x => [x.path, ...args]), [join(root, 'codex.exe'), ...args]);
      } catch { /* Desktop installation is optional. */ }
    }
  }
  if (name === 'dsh' && config.command[0] === 'dsh-acp-demo' && config.command.length === 1) {
    if (env.DSH_ACP_BIN) candidates.push([env.DSH_ACP_BIN]);
    candidates.push(['dsh', '--profile', 'acp'], ['dsh-acp-demo']);
    candidates.push([process.execPath, join(home, 'dsh-acp', 'dist', 'index.js')]);
  }
  candidates.push(config.command, ...config.fallback_commands);
  for (const command of candidates) {
    const path = await executable(command[0]!, env);
    if (!path) continue;
    if (command[0] === process.execPath && command[1]?.endsWith('.js')) {
      try { await access(command[1]); } catch { continue; }
    }
    return [path, ...command.slice(1)];
  }
  throw new Error(`${name} executable not found; configure agents.${name}.command in agents.toml`);
}

export async function workerEnvironment(config: AppConfig, worker: WorkerConfig): Promise<NodeJS.ProcessEnv> {
  let env: NodeJS.ProcessEnv = { ...process.env };
  // A GUI MCP host may have stripped variables. The Hub restores only named
  // keys, without sourcing a shell profile or executing wrapper functions.
  if (process.platform === 'win32' && (config.env.discover_proxy || config.env.inherit.some(key => !env[key]))) {
    const result = await capture(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
      '[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); $e=@{}; foreach($s in @("Machine","User")){[Environment]::GetEnvironmentVariables($s).GetEnumerator() | ForEach-Object {$e[$_.Key]=$_.Value}}; $p=Get-ItemProperty "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings" -ErrorAction SilentlyContinue; @{env=$e; proxy=$(if($p.ProxyEnable){$p.ProxyServer}else{""})} | ConvertTo-Json -Compress']);
    if (result.code === 0) {
      const data = JSON.parse(result.stdout) as { env: Record<string, string>; proxy: string };
      const restored = data.env;
      for (const key of config.env.inherit) if (!env[key] && restored[key]) env[key] = restored[key];
      if (config.env.discover_proxy && data.proxy) {
        const entries = data.proxy.includes('=') ? data.proxy.split(';').map(x => x.split('=')) : [['http', data.proxy], ['https', data.proxy]];
        for (const [scheme, host] of entries) if (host && ['http', 'https'].includes(scheme!)) {
          const key = `${scheme!.toUpperCase()}_PROXY`;
          env[key] ??= env[key.toLowerCase()] ?? (/^[\w+.-]+:\/\//.test(host) ? host : `http://${host}`);
        }
      }
    }
  }
  const bridgeProxy = env.AGENT_BRIDGE_HTTPS_PROXY ?? env.AGENT_BRIDGE_HTTP_PROXY;
  if (bridgeProxy) for (const key of ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'GROK_WEB_FETCH_PROXY']) env[key] ??= bridgeProxy;
  if (env.AGENT_BRIDGE_NO_PROXY) env.NO_PROXY ??= env.AGENT_BRIDGE_NO_PROXY;
  const proxy = config.env.proxy.url ?? config.env.proxy_url;
  if (proxy) for (const key of ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'GROK_WEB_FETCH_PROXY']) env[key] = proxy;
  const noProxy = config.env.proxy.no_proxy ?? config.env.no_proxy;
  if (noProxy) env.NO_PROXY = noProxy;
  env = { ...env, ...config.env.set, ...worker.env };
  for (const key of ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY']) {
    env[key] ??= env[key.toLowerCase()];
    if (env[key]) env[key.toLowerCase()] = env[key];
  }
  return enforceEnvDeny(env, config.env, worker);
}

export function enforceEnvDeny(env: NodeJS.ProcessEnv, config: AppConfig['env'], worker: WorkerConfig): NodeJS.ProcessEnv {
  const explicit = new Set([...Object.keys(config.set), ...Object.keys(worker.env),
    'AGENT_BRIDGE_HOME', 'AGENT_BRIDGE_AGENT_ID', 'AGENT_BRIDGE_PROJECT', 'AGENT_BRIDGE_TOKEN', 'AGENT_BRIDGE_WORKER']);
  for (const pattern of config.deny) {
    const regex = new RegExp(`^${pattern.split('*').map(x => x.split('?').map(v => v.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('.')).join('.*')}$`, 'i');
    for (const key of Object.keys(env)) if (regex.test(key) && !explicit.has(key)) delete env[key];
  }
  return env;
}

export interface WorkerProcess {
  child: ChildProcessWithoutNullStreams;
  exited: Promise<{ code: number | null; signal: NodeJS.Signals | null }>;
  stop(): Promise<void>;
}

export function launch(command: string[], cwd: string, env: NodeJS.ProcessEnv, graceSeconds: number): WorkerProcess {
  const child = fork(fileURLToPath(new URL('./worker-host.js', import.meta.url)), [], {
    env, cwd, execArgv: [], stdio: ['pipe', 'pipe', 'pipe', 'ipc'], windowsHide: true,
  }) as ChildProcessWithoutNullStreams;
  const exited = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolve, reject) => {
    child.once('error', reject);
    child.once('close', (code, signal) => resolve({ code, signal }));
  });
  child.send!({ command, graceSeconds });
  return {
    child, exited,
    async stop() {
      if (child.connected) child.send!({ stop: true }, () => {});
      await exited;
    },
  };
}

// Native spawn is used only for OS process-tree cleanup; application commands
// go through cross-spawn so npm .cmd shims work on Windows without shell strings.
export async function killTree(pid: number, signal: NodeJS.Signals): Promise<void> {
  if (process.platform === 'win32') {
    await new Promise<void>(resolve => {
      const child = nativeSpawn('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
      child.once('error', () => resolve()); child.once('close', () => resolve());
    });
  } else {
    try { process.kill(-pid, signal); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error; }
  }
}
