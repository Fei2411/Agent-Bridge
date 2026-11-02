import { randomBytes } from 'node:crypto';
import { EventEmitter } from 'node:events';
import { mkdir, readFile, writeFile, rename, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import type { ServerResponse } from 'node:http';
import type { Socket } from 'node:net';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { createApi } from './api.js';
import { loadConfig } from './config.js';
import { Runtime } from './runtime.js';
import { Store } from './store.js';

function streamEvents(response: ServerResponse, store: Store, wake: EventEmitter, projectId: string, after: number): void {
  response.writeHead(200, {
    'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache',
    'Connection': 'keep-alive', 'X-Accel-Buffering': 'no',
  });
  response.flushHeaders();
  let blocked = false;
  const pump = () => {
    if (blocked || response.destroyed) return;
    try {
      for (;;) {
        const events = store.events(projectId, after);
        if (events.length === 0) return;
        for (const event of events) {
          const writable = response.write(`id: ${event.sequence}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
          after = event.sequence;
          if (!writable) { blocked = true; return; }
        }
      }
    } catch { response.destroy(); }
  };
  const drain = () => { blocked = false; pump(); };
  wake.on('change', pump);
  response.on('drain', drain);
  const heartbeat = setInterval(() => {
    if (!blocked && !response.write(': heartbeat\n\n')) blocked = true;
  }, 15_000);
  heartbeat.unref();
  response.on('close', () => {
    clearInterval(heartbeat);
    wake.off('change', pump);
    response.off('drain', drain);
  });
  pump();
}

export async function startHub(home: string, port = 0, configPath?: string): Promise<{ url: string; close(): Promise<void> }> {
  await mkdir(home, { recursive: true, mode: 0o700 });
  // A separate SQLite exclusive transaction is an OS-released process lock.
  // It does not hold a write transaction on the application database.
  const lock = new DatabaseSync(join(home, 'owner.sqlite'));
  try { lock.exec('PRAGMA busy_timeout=0; BEGIN EXCLUSIVE;'); }
  catch { lock.close(); throw new Error('A Hub already owns this data directory'); }
  let store: Store | undefined;
  let runtime: Runtime | undefined;
  let closeHub: () => Promise<void>;
  const pending = new Set<Promise<void>>();
  const sockets = new Set<Socket>();
  const wake = new EventEmitter();
  wake.setMaxListeners(0);
  const server = createServer();
  try {
    const tokenPath = join(home, 'token');
    let token: string;
    try { token = (await readFile(tokenPath, 'utf8')).trim(); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
      token = randomBytes(32).toString('hex');
      await writeFile(tokenPath, token, { flag: 'wx', mode: 0o600 });
    }
    if (!/^[a-f0-9]{64}$/.test(token)) throw new Error('Invalid Hub token file');
    const database = new Store(join(home, 'hub.sqlite'));
    store = database;
    runtime = new Runtime(home, database, await loadConfig(home, configPath), wake, configPath);
    server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
    const handle = createApi(database, runtime, token, () => wake.emit('change'),
      (response, projectId, after) => streamEvents(response, database, wake, projectId, after), () => closeHub());
    server.on('request', (request, response) => {
      const work = handle(request, response).finally(() => pending.delete(work)); pending.add(work);
    });
    await new Promise<void>((resolve, reject) => {
      server.once('error', reject);
      server.listen(port, '127.0.0.1', () => { server.off('error', reject); resolve(); });
    });
    const address = server.address();
    if (!address || typeof address === 'string') throw new Error('Hub has no TCP address');
    const url = `http://127.0.0.1:${address.port}`;
    const discovery = join(home, 'hub.json.tmp');
    await writeFile(discovery, JSON.stringify({ url, pid: process.pid, protocol: 2 }), { mode: 0o600 });
    await rename(discovery, join(home, 'hub.json'));
    let closing: Promise<void> | undefined;
    closeHub = () => {
      closing ??= (async () => {
        try {
          await runtime!.close();
          for (const socket of sockets) socket.destroy();
          await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
          await Promise.all(pending);
          await rm(join(home, 'hub.json'), { force: true });
        } finally { database.close(); lock.close(); }
      })();
      return closing;
    };
    return { url, close: closeHub };
  } catch (error) {
    for (const socket of sockets) socket.destroy();
    if (server.listening) await new Promise<void>(resolve => server.close(() => resolve()));
    await runtime?.close();
    store?.close();
    lock.close();
    throw error;
  }
}
