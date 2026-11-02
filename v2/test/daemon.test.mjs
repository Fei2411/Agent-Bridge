import assert from 'node:assert/strict';
import { test } from 'node:test';
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { cli } from './helpers.mjs';

function command(home, ...args) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [cli, ...args, '--home', home], { stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    child.stdout.on('data', x => { stdout += x; }); child.stderr.on('data', x => { stderr += x; });
    child.once('error', reject);
    child.once('close', code => code === 0 ? resolve(JSON.parse(stdout)) : reject(new Error(stderr)));
  });
}

test('concurrent CLI clients auto-start one independent Hub and can stop it explicitly', { timeout: 30000 }, async t => {
  const home = await mkdtemp(join(tmpdir(), 'agent bridge daemon '));
  await writeFile(join(home, 'agents.toml'), '');
  t.after(async () => {
    await command(home, 'stop').catch(() => {});
    for (let i = 0; i < 500; i++) {
      const exists = await readFile(join(home, 'hub.json')).then(() => true, () => false);
      if (!exists) break;
      await delay(10);
    }
    await rm(home, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  });
  await Promise.all(['alice', 'bob'].map(id => command(home, 'call', 'list_peers', '--agent-id', id, '--project', 'daemon', '--config', join(home, 'agents.toml'))));
  const endpoint = JSON.parse(await readFile(join(home, 'hub.json'), 'utf8'));
  assert.equal((await command(home, 'status')).status, 'ok');
  const peers = await command(home, 'call', 'list_peers', '--agent-id', 'alice', '--project', 'daemon');
  assert.deepEqual(peers.map(x => x.id), ['alice', 'bob']);
  await command(home, 'start');
  assert.equal(JSON.parse(await readFile(join(home, 'hub.json'), 'utf8')).pid, endpoint.pid);
});
