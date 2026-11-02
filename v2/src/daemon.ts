import { spawn } from 'node:child_process';
import { mkdir, open } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DatabaseSync } from 'node:sqlite';
import { setTimeout as delay } from 'node:timers/promises';
import { HubClient } from './client.js';
import { HubError } from './contracts.js';

function owned(home: string): boolean {
  const lock = new DatabaseSync(join(home, 'owner.sqlite'));
  try { lock.exec('PRAGMA busy_timeout=0; BEGIN EXCLUSIVE; ROLLBACK'); return false; }
  catch { return true; }
  finally { lock.close(); }
}

export async function ensureHub(home: string, configPath?: string): Promise<void> {
  const client = new HubClient(home);
  const healthy = async () => {
    try { await client.request('/health', undefined, AbortSignal.timeout(1000)); return true; }
    catch (error) { if (error instanceof HubError) throw error; return false; }
  };
  if (await healthy()) return;
  await mkdir(home, { recursive: true, mode: 0o700 });
  const log = await open(join(home, 'hub.log'), 'a', 0o600);
  let child;
  try {
    child = spawn(process.execPath, [fileURLToPath(new URL('./cli.js', import.meta.url)), 'hub', '--home', home,
      ...(configPath ? ['--config', configPath] : [])], {
      detached: true, stdio: ['ignore', log.fd, log.fd], windowsHide: true,
    });
  } finally { await log.close(); }
  let spawnError: Error | undefined;
  child.once('error', error => { spawnError = error; });
  // Wait while an owner is starting; concurrent launches converge on the lock.
  while (!await healthy()) {
    if (spawnError) throw spawnError;
    if ((child.exitCode !== null || child.signalCode !== null) && !owned(home)) {
      throw new Error(`Hub failed to start; see ${join(home, 'hub.log')}`);
    }
    await delay(50);
  }
  child.unref();
}
