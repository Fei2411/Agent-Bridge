import { createInterface } from 'node:readline';
import { randomUUID } from 'node:crypto';
import { readFile, writeFile, appendFile, access, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const root = process.env.FIXTURE_STATE;
await mkdir(root, { recursive: true });
const sessions = new Map();
const active = new Map();
const send = value => process.stdout.write(JSON.stringify({ jsonrpc: '2.0', ...value }) + '\n');
const respond = (id, result) => send({ id, result });
function options(s) {
  return [
    { id: 'model', category: 'model', type: 'select', name: 'Model', currentValue: s.model,
      options: [{ group: 'models', name: 'Models', options: ['alpha', 'beta'].map(value => ({ value, name: value })) }] },
    { id: 'thinking', category: 'thought_level', type: 'select', name: 'Thinking', currentValue: s.effort,
      options: (s.model === 'alpha' ? ['low', 'high', 'max'] : ['low', 'high']).map(value => ({ value, name: value })) },
    { id: 'fast', type: 'select', name: 'Fast', currentValue: String(s.fast),
      options: ['true', 'false'].map(value => ({ value, name: value })) },
  ];
}
async function save(s) { await writeFile(join(root, `${s.id}.json`), JSON.stringify({ ...s, mcpServers: undefined })); }
async function tool(c, name, args = {}) {
  const result = await c.callTool({ name, arguments: args });
  if (result.isError) throw new Error(result.content[0].text);
  return JSON.parse(result.content[0].text);
}
async function handle(msg) {
  const p = msg.params ?? {};
  if (msg.method === 'initialize') {
    if (process.env.FIXTURE_BLOCK_INITIALIZE) {
      process.on('SIGTERM', () => { setTimeout(() => process.exit(0), 300); });
      const beat = async () => appendFile(join(root, 'starting-heartbeat'), 'x');
      await beat(); setInterval(() => { void beat(); }, 15);
      return;
    }
    return respond(msg.id, { protocolVersion: 1, agentCapabilities: { loadSession: true, sessionCapabilities: { resume: {} } } });
  }
  if (msg.method === 'session/new') {
    if (!p.mcpServers?.length) throw new Error('Missing worker communication tools');
    const s = { id: randomUUID(), cwd: p.cwd, mcpServers: p.mcpServers, model: 'alpha', effort: 'low', fast: true, turn: 0 };
    sessions.set(s.id, s); await save(s);
    return respond(msg.id, { sessionId: s.id, configOptions: options(s) });
  }
  if (['session/load', 'session/resume'].includes(msg.method)) {
    const s = JSON.parse(await readFile(join(root, `${p.sessionId}.json`), 'utf8'));
    s.mcpServers = p.mcpServers; sessions.set(s.id, s);
    send({ method: 'session/update', params: { sessionId: s.id, update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'REPLAY MUST NOT ENTER CURRENT RESULT' } } } });
    return respond(msg.id, { configOptions: options(s) });
  }
  const s = sessions.get(p.sessionId);
  if (!s) throw new Error('Unknown native session');
  if (msg.method === 'session/set_mode') return respond(msg.id, {});
  if (msg.method === 'session/set_config_option') {
    if (p.configId === 'model') {
      if (!['alpha', 'beta'].includes(p.value)) return send({ id: msg.id, error: { code: -32602, message: 'unknown model; offered alpha, beta' } });
      s.model = p.value; s.effort = 'low';
    } else if (p.configId === 'thinking') {
      if (!(s.model === 'alpha' ? ['low', 'high', 'max'] : ['low', 'high']).includes(p.value)) throw new Error('invalid effort');
      s.effort = p.value;
    } else if (p.configId === 'fast') s.fast = p.value === 'true';
    else return send({ id: msg.id, error: { code: -32602, message: 'unknown config option' } });
    await save(s); return respond(msg.id, { configOptions: options(s) });
  }
  if (msg.method === 'session/cancel') { active.get(s.id)?.abort(); return; }
  if (msg.method !== 'session/prompt') return send({ id: msg.id, error: { code: -32601, message: 'unsupported method' } });
  if (active.has(s.id)) throw new Error('Overlapping prompts in one native session');
  const controller = new AbortController(); active.set(s.id, controller);
  try {
    const prompt = p.prompt.map(x => x.text ?? '').join('');
    const gate = /WAIT_GATE=([a-z-]+)/.exec(prompt)?.[1];
    const cancelled = /WAIT_FOR_CANCEL/.test(prompt);
    const childWork = /SPAWN_CHILD/.test(prompt);
    if (childWork) {
      // Real CLI shell tools may create their own process group and ignore TERM.
      spawn(process.execPath, ['-e', `const fs=require('fs');process.on('SIGTERM',()=>{});setInterval(()=>fs.appendFileSync(process.env.FIXTURE_STATE+'/heartbeat','x'),25)`], { detached: process.platform !== 'win32', stdio: 'ignore', env: process.env });
    }
    await writeFile(join(root, `started-${s.id}`), JSON.stringify({ pid: process.pid, gate }));
    if (gate || cancelled || childWork) {
      send({ method: 'session/update', params: { sessionId: s.id, update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'Waiting at gate\n' } } } });
      while (!controller.signal.aborted) {
        if (gate && await access(join(root, gate)).then(() => true, () => false)) break;
        await delay(10);
      }
    }
    if (controller.signal.aborted) return respond(msg.id, { stopReason: 'cancelled' });
    let collaboration;
    if (prompt.includes('BRIDGE_ROUNDTRIP')) {
      const mcp = s.mcpServers[0];
      const transport = new StdioClientTransport({ command: mcp.command, args: mcp.args,
        env: { ...process.env, ...Object.fromEntries(mcp.env.map(x => [x.name, x.value])) }, stderr: 'pipe' });
      transport.stderr.on('data', () => {});
      const client = new Client({ name: 'wire-fixture-worker', version: '1' });
      try {
        await client.connect(transport);
        const discovery = await tool(client, 'list_agents');
        if (discovery.coordinator.runtime_context !== 'worker' || !discovery.coordinator.dispatch_enabled) throw new Error('Worker cannot cooperate');
        const peers = await tool(client, 'list_peers');
        const child = await tool(client, 'dispatch_task', { agent: 'helper', cwd: s.cwd, message: 'Write a helper result', request_id: randomUUID() });
        await tool(client, 'wait_task', { task_id: child.task_id, timeout_sec: 5 });
        const result = await tool(client, 'get_result', { task_id: child.task_id });
        if (result.status !== 'completed') throw new Error(JSON.stringify(result));
        const thread = /thread ([^ .]+)\./.exec(prompt)?.[1];
        await tool(client, 'send_message', { recipient_id: 'alice', request_id: randomUUID(), text: 'worker reply after child completion', thread_id: thread, wake: false });
        collaboration = { peers: peers.map(p => p.id), childStatus: result.status };
      } finally { await client.close(); }
    }
    s.turn++; await save(s);
    await appendFile(join(s.cwd, 'fixture-output.txt'), `${s.id}:${s.turn}:${s.model}:${s.effort}\n`);
    const text = JSON.stringify({ nativeSessionId: s.id, turn: s.turn, model: s.model, effort: s.effort, fast: s.fast, collaboration });
    send({ method: 'session/update', params: { sessionId: s.id, update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text } } } });
    respond(msg.id, { stopReason: 'end_turn' });
  } finally { active.delete(s.id); }
}
createInterface({ input: process.stdin }).on('line', line => {
  const msg = JSON.parse(line);
  void handle(msg).catch(error => send({ id: msg.id ?? null, error: { code: -32603, message: error.message } }));
});
