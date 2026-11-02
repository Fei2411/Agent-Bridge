import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { execFileSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { fixture, call, stop } from './helpers.mjs';
import { mockModel, prompt } from './mock-model.mjs';

// Explicit opt-in: this exercises a real installed OpenCode, not an ACP fixture.
const executable = process.env.OPENCODE_SMOKE_BIN;
assert.ok(executable, 'Set OPENCODE_SMOKE_BIN to the installed OpenCode executable');
async function until(work) {
  const deadline = Date.now() + 60000;
  do { const value = await work(); if (value) return value; await delay(25); } while (Date.now() < deadline);
  throw new Error('Observable smoke-test condition did not occur within 60 seconds');
}

test('real OpenCode with a local mock model', { timeout: 180000 }, async t => {
  console.log('OpenCode', execFileSync(executable, ['--version'], { encoding: 'utf8' }).trim(), 'Node', process.version, process.platform);
  const model = await mockModel();
  t.after(() => model.close());
  const f = await fixture(t);
  const cwd = join(f.home, 'workspace');
  await mkdir(cwd);
  execFileSync('git', ['init', '-q'], { cwd });
  const config = {
    model: 'mock/alpha', small_model: 'mock/alpha', enabled_providers: ['mock'],
    autoupdate: false, share: 'disabled', plugin: [], lsp: false,
    provider: { mock: { npm: '@ai-sdk/openai-compatible', name: 'Local smoke mock',
      options: { baseURL: model.url, apiKey: 'local-mock-only' },
      models: Object.fromEntries(['alpha', 'beta'].map(id => [id, { name: id, tool_call: true, limit: { context: 128000, output: 4096 } }])) } },
  };
  const env = {
    OPENCODE_CONFIG_CONTENT: JSON.stringify(config), OPENCODE_TEST_HOME: join(f.home, 'opencode'),
    XDG_DATA_HOME: join(f.home, 'data'), XDG_CONFIG_HOME: join(f.home, 'config'),
    XDG_CACHE_HOME: join(f.home, 'cache'), XDG_STATE_HOME: join(f.home, 'state'),
    OPENCODE_DISABLE_MODELS_FETCH: '1', OPENCODE_DISABLE_AUTOUPDATE: '1',
    OPENCODE_DISABLE_DEFAULT_PLUGINS: '1', OPENCODE_DISABLE_EXTERNAL_SKILLS: '1',
    OPENCODE_DISABLE_CLAUDE_CODE: '1', OPENCODE_DISABLE_PROJECT_CONFIG: '1',
    OPENCODE_AUTH_CONTENT: '{}', NO_PROXY: '127.0.0.1,localhost',
  };
  await writeFile(join(f.home, 'agents.toml'), `[agents.opencode]\ncommand=${JSON.stringify([resolve(executable), 'acp'])}\nidle_unload_sec=0\n[agents.opencode.env]\n` + Object.entries(env).map(([key, value]) => `${key}=${JSON.stringify(value)}`).join('\n'));
  let hub = await f.start();
  const alice = await f.connect('alice'), bob = await f.connect('bob');
  async function finished(client, task) {
    await call(client, 'wait_task', { task_id: task.task_id, timeout_sec: 60 });
    const result = await call(client, 'get_result', { task_id: task.task_id });
    assert.equal(result.status, 'completed', JSON.stringify(result));
    return result;
  }
  async function session(id) {
    return (await call(bob, 'list_sessions')).sessions.find(s => s.session_id === id);
  }
  async function entered(id, step = 0) {
    return until(() => model.requests.find(r => r.id === id && r.tools.length && r.step === step));
  }
  const file = join(cwd, 'output.txt');
  const dispatch = (id, { spec, ...options } = {}) => call(bob, 'dispatch_task', { agent: 'opencode', cwd,
    ...options, message: prompt({ id, file, line: id, ...spec }) });
  try {
    const input = { agent: 'opencode', cwd, model: 'mock/alpha', request_id: 'first-request',
      message: prompt({ id: 'first', file, line: 'first', gate: 'release-first' }) };
    const first = await call(alice, 'dispatch_task', input);
    assert.equal((await call(alice, 'dispatch_task', input)).task_id, first.task_id);
    const initialRequest = await entered('first');
    const nativeId = (await session(first.session_id)).native_session_id;
    assert.ok(nativeId);
    const second = await dispatch('second', { session_id: first.session_id, model: 'mock/beta' });
    assert.equal(second.status, 'queued');
    assert.equal(initialRequest.closed, false);
    await alice.close();
    model.release('release-first');
    const one = await finished(bob, first), two = await finished(bob, second);
    assert.equal(await readFile(file, 'utf8'), 'first\nsecond\n');
    assert.equal(one.result_text, 'MOCK_DONE:first');
    assert.equal(two.result_text, 'MOCK_DONE:second');
    assert.ok(one.files_changed.includes('output.txt'));
    assert.equal(two.observed_model, 'mock/beta');
    assert.equal((await entered('second')).model, 'beta');
    assert.deepEqual((await entered('second')).history, ['first', 'second']);
    assert.equal((await session(first.session_id)).native_session_id, nativeId);
    console.log('PASS host disconnect, request deduplication, FIFO, real file writes and model switching');

    const waiting = await dispatch('cancel-running', { session_id: first.session_id, spec: { gate: 'cancel' } });
    const blocked = await entered('cancel-running');
    const queued = await dispatch('cancel-queued', { session_id: first.session_id });
    assert.equal((await call(bob, 'cancel_task', { task_id: queued.task_id })).status, 'cancelled');
    assert.equal((await call(bob, 'cancel_task', { task_id: waiting.task_id })).status, 'cancelled');
    await until(() => blocked.closed);
    const continued = await dispatch('after-cancel', { session_id: first.session_id });
    const resumed = await finished(bob, continued);
    assert.equal(resumed.result_text, 'MOCK_DONE:after-cancel');
    assert.deepEqual(resumed.warnings, []);
    assert.equal((await session(first.session_id)).native_session_id, nativeId);
    assert.equal(model.requests.some(r => r.id === 'cancel-queued'), false);
    assert.equal(await readFile(file, 'utf8'), 'first\nsecond\nafter-cancel\n');
    assert.ok((await entered('after-cancel')).history.includes('first'));
    console.log('PASS cancellation aborts the model connection; native history survives worker restart');

    const childCwd = join(f.home, 'child-workspace');
    await mkdir(childCwd);
    execFileSync('git', ['init', '-q'], { cwd: childCwd });
    const peer = await call(bob, 'create_session', { agent: 'opencode', cwd });
    const message = await call(bob, 'send_message', { recipient_id: peer.agent_id, request_id: 'delegate-message',
      text: prompt({ id: 'delegate', delegate: true, cwd: childCwd, childFile: join(childCwd, 'child.txt') }) });
    const reply = await until(async () => (await call(bob, 'read_inbox')).find(m => m.text === 'real CLI child completed'));
    assert.equal(reply.senderId, peer.agent_id);
    assert.equal(reply.threadId, message.threadId);
    const tasks = (await call(bob, 'list_tasks')).tasks;
    const outer = tasks.find(task => task.session_id === peer.session_id);
    assert.equal((await finished(bob, outer)).result_text, 'MOCK_DONE:delegate');
    assert.equal(await readFile(join(childCwd, 'child.txt'), 'utf8'), 'child wrote this\n');
    const injected = model.requests.filter(r => r.id === 'delegate' && r.call).map(r => r.call);
    for (const name of ['list_agents', 'dispatch_task', 'wait_task', 'get_result', 'send_message'])
      assert.ok(injected.some(actual => actual.endsWith(`_${name}`)), `Injected MCP ${name} was not used`);
    assert.equal((await f.request(`/v1/inbox?recipientId=${peer.agent_id}`)).length, 0);
    console.log('PASS real CLI uses injected MCP to delegate, wait, retrieve a result and reply in the original thread');

    const heartbeat = join(cwd, 'heartbeat');
    const crashing = await dispatch('crash', { session_id: first.session_id, spec: { heartbeat } });
    await until(() => readFile(heartbeat, 'utf8').then(x => x.length > 1, () => false));
    const pending = await dispatch('crash-queued', { session_id: first.session_id });
    await stop(hub, 'SIGKILL');
    await delay(250);
    const stopped = await readFile(heartbeat, 'utf8');
    await delay(250);
    assert.equal(await readFile(heartbeat, 'utf8'), stopped, 'Worker grandchild survived Hub loss');
    hub = await f.start();
    for (const task of [crashing, pending])
      assert.equal((await call(bob, 'get_result', { task_id: task.task_id })).status, 'interrupted');
    assert.equal((await call(bob, 'get_result', { task_id: first.task_id })).result_text, 'MOCK_DONE:first');
    const recovered = await dispatch('after-crash', { session_id: first.session_id, model: 'mock/alpha' });
    assert.deepEqual((await finished(bob, recovered)).warnings, []);
    assert.equal((await session(first.session_id)).native_session_id, nativeId);
    assert.equal(await readFile(file, 'utf8'), 'first\nsecond\nafter-cancel\ncrash\nafter-crash\n');
    assert.equal(model.requests.some(r => r.id === 'crash-queued'), false);
    assert.ok((await entered('after-crash')).history.includes('first'));
    console.log('PASS abrupt Hub loss kills the process tree; restart preserves results and resumes native history without replaying work');
    assert.deepEqual(model.errors, []);
    console.log('Model HTTP requests:', model.requests.length, 'Native session:', nativeId);
  } catch (error) {
    console.error('Smoke failure', error);
    console.error('Mock requests', JSON.stringify(model.requests.map(({ tools, ...r }) => r)), 'Mock errors', model.errors);
    console.error('Hub tasks', JSON.stringify(await call(bob, 'list_tasks').catch(error => String(error))));
    throw error;
  }
});
