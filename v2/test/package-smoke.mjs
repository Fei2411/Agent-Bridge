import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import spawn from 'cross-spawn';

const root = fileURLToPath(new URL('..', import.meta.url));
const scratch = await mkdtemp(join(tmpdir(), 'bridge package '));
const install = join(scratch, 'install'), home = join(scratch, 'profile');
const bin = join(install, 'node_modules', '.bin', 'agent-bridge-hub');
const run = (command, args, cwd = root) => new Promise((resolve, reject) => {
  const child = spawn(command, args, { cwd, stdio: ['ignore', 'pipe', 'pipe'] });
  let output = '', errors = '';
  child.stdout.on('data', x => { output += x; }); child.stderr.on('data', x => { errors += x; });
  child.once('error', reject);
  child.once('close', code => code === 0 ? resolve(output) : reject(new Error(`${command} exited ${code}: ${errors}`)));
});
try {
  await mkdir(install); await mkdir(home);
  await writeFile(join(home, 'agents.toml'), '');
  await run('npm', ['pack', '--pack-destination', scratch]);
  const tarball = (await readdir(scratch)).find(x => x.endsWith('.tgz'));
  assert.ok(tarball);
  await run('npm', ['install', '--prefix', install, '--ignore-scripts', '--no-audit', '--no-fund', join(scratch, tarball)]);
  assert.match(await run(bin, ['--help'], install), /centralized Agent runtime/);
  const args = ['--home', home, '--config', join(home, 'agents.toml')];
  const started = JSON.parse(await run(bin, ['start', ...args], install));
  assert.equal(started.status, 'ok');
  const peers = JSON.parse(await run(bin, ['call', 'list_peers', '--agent-id', 'package-smoke', '--project', 'smoke', ...args], install));
  assert.equal(peers[0].id, 'package-smoke');
  console.log('Installed tarball CLI, bundled config, daemon startup and tool call passed.');
} finally {
  await run(bin, ['stop', '--home', home], install).catch(() => {});
  let stopped = false;
  for (let i = 0; i < 500; i++) {
    if (!await readFile(join(home, 'hub.json')).then(() => true, () => false)) { stopped = true; break; }
    await delay(10);
  }
  assert.ok(stopped, 'Installed Hub did not stop after the smoke test');
  // Windows can briefly retain the daemon's cwd handle after discovery removal.
  await rm(scratch, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
}
