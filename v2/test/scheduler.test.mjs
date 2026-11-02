import assert from 'node:assert/strict';
import { test } from 'node:test';
import { EventEmitter } from 'node:events';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { loadConfig } from '../dist/config.js';
import { Store } from '../dist/store.js';
import { Runtime } from '../dist/runtime.js';

// Runtime owns dependency waits; direct public calls make overlapping requests
// deterministic while the tasks still run through actual Worker processes.
test('finishing one parallel wait does not erase another dependency', { timeout: 30000 }, async t => {
  const home = await mkdtemp(join(tmpdir(), 'bridge-scheduler-'));
  const configPath = join(home, 'agents.toml');
  const worker = fileURLToPath(new URL('./fixtures/acp-worker.mjs', import.meta.url));
  await writeFile(configPath, `[agents.kimi]\ncommand=${JSON.stringify([process.execPath, worker])}\nidle_unload_sec=0\n[agents.kimi.env]\nFIXTURE_STATE=${JSON.stringify(home)}\n`);
  const store = new Store(':memory:');
  const runtime = new Runtime(home, store, await loadConfig(home, configPath), new EventEmitter(), configPath);
  const controller = new AbortController();
  t.after(async () => { controller.abort(); await runtime.close(); store.close(); await rm(home, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }); });
  store.register({ id: 'owner', kind: 'external', projectId: 'p', name: 'Owner' });
  const tasks = await Promise.all([1, 2, 3].map(() => runtime.dispatch({ agent: 'kimi', projectId: 'p', cwd: home, requesterId: 'owner', message: 'WAIT_FOR_CANCEL', userRequested: false })));
  for (const task of tasks) while (!store.session(task.sessionId).nativeSessionId) await delay(10);
  const [a, b, c] = tasks;
  const aId = store.session(a.sessionId).agentId, bId = store.session(b.sessionId).agentId;
  const long = runtime.wait(b.id, 10, controller.signal, aId);
  try {
    await runtime.wait(c.id, 0.01, controller.signal, aId);
    await assert.rejects(runtime.wait(a.id, 0.01, controller.signal, bId), error => error.status === 409);
  } finally { controller.abort(); await long; }
});
