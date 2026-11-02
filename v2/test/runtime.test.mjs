import assert from 'node:assert/strict';
import { test } from 'node:test';
import { randomUUID } from 'node:crypto';
import { mkdir, writeFile, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fixture, call, agent } from './helpers.mjs';

const acp = fileURLToPath(new URL('./fixtures/acp-worker.mjs', import.meta.url));
const exec = fileURLToPath(new URL('./fixtures/exec-worker.mjs', import.meta.url));
async function until(work) {
  for (let i = 0; i < 500; i++) { const value = await work(); if (value) return value; await delay(10); }
  throw new Error('Expected observable condition did not occur');
}
async function setup(t, extra = '') {
  const f = await fixture(t);
  const cwd = join(f.home, 'workspace'), state = join(f.home, 'fixture-state');
  await mkdir(cwd); await mkdir(state);
  const git = spawn('git', ['init', '-q'], { cwd }); assert.equal((await once(git, 'exit'))[0], 0);
  const config = ['kimi', 'helper'].map(name => `[agents.${name}]\nprotocol="acp"\ncommand=${JSON.stringify([process.execPath, acp])}\nrevivable=true\nidle_unload_sec=0\n[agents.${name}.env]\nFIXTURE_STATE=${JSON.stringify(state)}\n`).join('\n');
  await writeFile(join(f.home, 'agents.toml'), config + (typeof extra === 'function' ? extra({ cwd, state }) : extra));
  await f.start();
  return { ...f, cwd, state };
}
async function finished(client, taskId) {
  await call(client, 'wait_task', { task_id: taskId, timeout_sec: 10 });
  return call(client, 'get_result', { task_id: taskId });
}

// Strong boundary: actual stdio MCP clients, TCP Hub, guardian and ACP worker.
// File writes and native-session identities are independent of the runtime state.
test('shared execution survives requester exit, serializes one session and reapplies per-model effort', { timeout: 40000 }, async t => {
  const f = await setup(t);
  const alice = await f.connect('alice'), bob = await f.connect('bob');
  const input = { agent: 'kimi', cwd: f.cwd, message: 'WAIT_GATE=release-first', model: 'alpha', effort: 'high', request_id: randomUUID() };
  const first = await call(alice, 'dispatch_task', input);
  const duplicate = await call(alice, 'dispatch_task', input);
  assert.equal(first.task_id, duplicate.task_id);
  const session = await until(async () => {
    const rows = await call(bob, 'list_sessions');
    return rows.sessions.find(s => s.session_id === first.session_id && s.native_session_id);
  });
  await until(() => readFile(join(f.state, `started-${session.native_session_id}`)).then(() => true, () => false));
  const second = await call(bob, 'dispatch_task', { agent: 'kimi', cwd: f.cwd, session_id: first.session_id, message: 'Continue', model: 'beta', effort: 'max' });
  assert.equal(second.status, 'queued');
  await alice.close();
  await writeFile(join(f.state, 'release-first'), 'go');
  const one = await finished(bob, first.task_id), two = await finished(bob, second.task_id);
  assert.equal(one.status, 'completed', JSON.stringify(one));
  assert.equal(two.status, 'completed', JSON.stringify(two));
  assert.equal(one.observed_model, 'alpha'); assert.equal(one.observed_effort, 'high');
  assert.equal(two.observed_model, 'beta'); assert.equal(two.observed_effort, 'high');
  const output = JSON.parse(two.result_text);
  assert.equal(output.turn, 2); assert.equal(output.nativeSessionId, session.native_session_id);
  assert.equal((await readFile(join(f.cwd, 'fixture-output.txt'), 'utf8')).trim().split('\n').length, 2);
  assert.ok(two.files_changed.includes('fixture-output.txt'));
  const invalid = await call(bob, 'dispatch_task', { agent: 'kimi', cwd: f.cwd, session_id: first.session_id, message: 'Never execute', model: 'missing' });
  const rejected = await finished(bob, invalid.task_id);
  assert.equal(rejected.status, 'failed'); assert.match(rejected.error, /alpha.*beta/);
});

test('managed inbox wakes a Worker that can delegate and reply through the same Hub', { timeout: 40000 }, async t => {
  const f = await setup(t);
  const alice = await f.connect('alice');
  const peer = await call(alice, 'create_session', { agent: 'kimi', cwd: f.cwd });
  const message = await call(alice, 'send_message', { recipient_id: peer.agent_id, request_id: randomUUID(), text: 'BRIDGE_ROUNDTRIP' });
  const reply = await until(async () => (await call(alice, 'read_inbox')).find(m => m.text === 'worker reply after child completion'));
  assert.equal(reply.senderId, peer.agent_id); assert.equal(reply.threadId, message.threadId);
  const tasks = await call(alice, 'list_tasks');
  const outer = tasks.tasks.find(task => task.session_id === peer.session_id);
  const result = await finished(alice, outer.task_id);
  assert.equal(result.status, 'completed', JSON.stringify(result));
  assert.equal(JSON.parse(result.result_text).collaboration.childStatus, 'completed');
  assert.equal((await call(alice, 'list_sessions')).sessions.length, 2);
  assert.equal((await f.request(`/v1/inbox?recipientId=${peer.agent_id}`)).length, 0);
});

test('cancelling a running prompt and queued task preserves a resumable session', { timeout: 30000 }, async t => {
  const f = await setup(t);
  const alice = await f.connect('alice');
  const waiting = await call(alice, 'dispatch_task', { agent: 'kimi', cwd: f.cwd, message: 'WAIT_FOR_CANCEL' });
  const session = await until(async () => (await call(alice, 'list_sessions')).sessions.find(s => s.native_session_id));
  await until(() => readFile(join(f.state, `started-${session.native_session_id}`)).then(() => true, () => false));
  const queued = await call(alice, 'dispatch_task', { agent: 'kimi', cwd: f.cwd, session_id: waiting.session_id, message: 'Must not execute' });
  assert.equal((await call(alice, 'cancel_task', { task_id: queued.task_id })).status, 'cancelled');
  assert.equal((await call(alice, 'cancel_task', { task_id: waiting.task_id })).status, 'cancelled');
  const next = await call(alice, 'dispatch_task', { agent: 'kimi', cwd: f.cwd, session_id: waiting.session_id, message: 'Resume after cancel' });
  const result = await finished(alice, next.task_id);
  assert.equal(result.status, 'completed', JSON.stringify(result));
  assert.equal(JSON.parse(result.result_text).nativeSessionId, session.native_session_id);
  assert.equal((await readFile(join(f.cwd, 'fixture-output.txt'), 'utf8')).trim().split('\n').length, 1);
  assert.doesNotMatch(result.result_text, /REPLAY/);
});

test('Hub crash stops the owned process tree and marks unfinished work interrupted', { timeout: 30000 }, async t => {
  const f = await setup(t);
  const alice = await f.connect('alice');
  const task = await call(alice, 'dispatch_task', { agent: 'kimi', cwd: f.cwd, message: 'SPAWN_CHILD' });
  await until(() => readFile(join(f.state, 'heartbeat'), 'utf8').then(x => x.length > 1, () => false));
  const meta = JSON.parse(await readFile(join(f.home, 'hub.json'), 'utf8'));
  process.kill(meta.pid, 'SIGKILL');
  await delay(250);
  const stopped = await readFile(join(f.state, 'heartbeat'), 'utf8');
  await delay(150);
  assert.equal(await readFile(join(f.state, 'heartbeat'), 'utf8'), stopped);
  await f.start();
  const result = await call(alice, 'get_result', { task_id: task.task_id });
  assert.equal(result.status, 'interrupted');
  assert.match(result.error, /inspect the workspace/);
  assert.equal((await call(alice, 'list_sessions')).sessions[0].proc_state, 'unloaded');
});

test('cancelling during ACP initialization waits for the worker to exit', { timeout: 30000 }, async t => {
  const f = await setup(t, ({ state }) => `\n[agents.starting]\nprotocol="acp"\ncommand=${JSON.stringify([process.execPath, acp])}\nshutdown_grace_sec=1\n[agents.starting.env]\nFIXTURE_BLOCK_INITIALIZE="1"\nFIXTURE_STATE=${JSON.stringify(state)}\n`);
  const alice = await f.connect('alice');
  const task = await call(alice, 'dispatch_task', { agent: 'starting', cwd: f.cwd, message: 'Never reaches prompt' });
  await until(() => readFile(join(f.state, 'starting-heartbeat'), 'utf8').then(x => x.length > 1, () => false));
  assert.equal((await call(alice, 'cancel_task', { task_id: task.task_id })).status, 'cancelled');
  const stopped = await readFile(join(f.state, 'starting-heartbeat'), 'utf8');
  await delay(100);
  assert.equal(await readFile(join(f.state, 'starting-heartbeat'), 'utf8'), stopped, 'cancel returned before the worker stopped');
});

test('Codex executable override preserves configured arguments when launching', { timeout: 30000 }, async t => {
  const f = await setup(t, `\n[agents.codex]\ncommand=${JSON.stringify(['codex', exec])}\n[agents.codex.env]\nCODEX_CLI_PATH=${JSON.stringify(process.execPath)}\n`);
  const alice = await f.connect('alice');
  const task = await call(alice, 'dispatch_task', { agent: 'codex', cwd: f.cwd, message: 'Launch configured entry point' });
  const result = await finished(alice, task.task_id);
  assert.equal(result.status, 'completed', JSON.stringify(result));
  assert.equal(JSON.parse(result.result_text).id, 'thread-fixture');
});

test('Cursor effort-only selection preserves native model flags', { timeout: 30000 }, async t => {
  const f = await setup(t, ({ state }) => `\n[agents.cursor]\ncommand=${JSON.stringify([process.execPath, acp, 'acp'])}\n[agents.cursor.env]\nFIXTURE_STATE=${JSON.stringify(state)}\n`);
  const alice = await f.connect('alice');
  const task = await call(alice, 'dispatch_task', { agent: 'cursor', cwd: f.cwd, message: 'Keep native model defaults', effort: 'high' });
  const result = await finished(alice, task.task_id);
  assert.equal(result.status, 'completed', JSON.stringify(result));
  assert.equal(JSON.parse(result.result_text).fast, true);
});

for (const protocol of ['codex', 'agy']) test(`${protocol} executable transport resumes, reports terminal failures and preserves argv`, { timeout: 30000 }, async t => {
  const extra = `\n[agents.execfixture]\nprotocol=${JSON.stringify(protocol)}\ncommand=${JSON.stringify([process.execPath, exec])}\nrevivable=true\nconfig_overrides=['custom_option="with spaces"']\n`;
  const f = await setup(t, extra), alice = await f.connect('alice');
  const first = await call(alice, 'dispatch_task', { agent: 'execfixture', cwd: f.cwd, message: 'First', model: 'chosen-model', effort: 'off' });
  const result = await finished(alice, first.task_id);
  assert.equal(result.status, 'completed', JSON.stringify(result));
  const second = await call(alice, 'dispatch_task', { agent: 'execfixture', cwd: f.cwd, session_id: first.session_id, message: 'Continue' });
  const resumed = await finished(alice, second.task_id);
  assert.equal(resumed.status, 'completed', JSON.stringify(resumed));
  assert.equal(JSON.parse(resumed.result_text).resumed, true);
  const args = JSON.parse(await readFile(join(f.cwd, 'exec-argv.json'), 'utf8'));
  if (protocol === 'codex') {
    assert.ok(args.includes('custom_option="with spaces"'));
    assert.ok(args.includes('model_reasoning_effort="none"'));
    assert.ok(args.includes('mcp_servers.agent_bridge.env_vars=["AGENT_BRIDGE_TOKEN","AGENT_BRIDGE_WORKER"]'));
    assert.deepEqual(resumed.warnings, ['transient retry']);
  } else { assert.equal(args[args.indexOf('-p') + 1], ''); assert.equal(resumed.usage.scope, 'conversation'); }
  const fail = await call(alice, 'dispatch_task', { agent: 'execfixture', cwd: f.cwd, session_id: first.session_id, message: 'FAIL_TURN' });
  const failure = await finished(alice, fail.task_id);
  assert.equal(failure.status, 'failed'); assert.match(failure.error, /deliberate worker failure/);
  if (protocol === 'agy') for (const [message, error] of [['NESTED_ERROR', /nested failure/], ['OBJECT_ERROR', /structured failure/]]) {
    await t.test(message, async () => {
      const task = await call(alice, 'dispatch_task', { agent: 'execfixture', cwd: f.cwd, message });
      const result = await finished(alice, task.task_id);
      assert.equal(result.status, 'failed', JSON.stringify(result));
      assert.match(result.error, error); assert.equal(result.result_text, 'Partial result');
    });
  }
});

test('scoped credentials bind sender and project while allowing worker delegation', { timeout: 30000 }, async t => {
  const f = await setup(t);
  const { token } = await f.request('/v1/connect', agent('alice'));
  await f.request('/v1/agents', agent('bob'));
  await f.request('/v1/agents', agent('outsider', 'other'));
  const headers = { Authorization: `Bearer ${token}` };
  await f.request('/v1/messages', { senderId: 'bob', recipientId: 'alice', requestId: 'impersonate', text: 'wrong sender' }, 403, headers);
  await f.request('/v1/messages', { senderId: 'alice', recipientId: 'outsider', requestId: 'cross-project', text: 'wrong project' }, 403, headers);
  await f.request('/v1/inbox?recipientId=bob', undefined, 403, headers);
  await f.request('/v1/agents?projectId=other', undefined, 403, headers);
  await f.request('/v1/preferences', { mode: 'manual' }, 403, headers);
  await f.request('/v1/shutdown', {}, 403, headers);
  const task = await f.request('/v1/tasks', { agent: 'kimi', cwd: f.cwd, projectId: 'bridge', requesterId: 'alice', message: 'Legitimate work' }, 200, headers);
  const alice = await f.connect('alice');
  assert.equal((await finished(alice, task.id)).status, 'completed');
});

test('manual routing applies to managed message wakeups without blocking passive mail', { timeout: 30000 }, async t => {
  const f = await setup(t), alice = await f.connect('alice');
  const peer = await call(alice, 'create_session', { agent: 'kimi', cwd: f.cwd });
  await call(alice, 'set_preferences', { mode: 'manual' });
  const denied = await alice.callTool({ name: 'send_message', arguments: { recipient_id: peer.agent_id, request_id: 'wake', text: 'Run this' } });
  assert.equal(denied.isError, true);
  assert.match(denied.content[0].text, /user_requested/);
  await call(alice, 'send_message', { recipient_id: peer.agent_id, request_id: 'passive', text: 'For reference', wake: false });
  assert.equal((await call(alice, 'list_tasks')).tasks.length, 0);
  await call(alice, 'send_message', { recipient_id: peer.agent_id, request_id: 'wake', text: 'Run this', user_requested: true });
  const tasks = await call(alice, 'list_tasks');
  assert.equal(tasks.tasks.length, 1);
  assert.equal((await finished(alice, tasks.tasks[0].task_id)).status, 'completed');
});
