import spawn from 'cross-spawn';
import type { ChildProcess } from 'node:child_process';
import { descendants, killTree } from './process.js';

// This small guardian owns the worker's process tree. IPC disconnect fires when
// the Hub is killed, so workers do not keep modifying files after losing owner.
let worker: ChildProcess | undefined;
let graceSeconds = 5;
let stopping = false;
let escalation: NodeJS.Timeout | undefined;
let children: number[] = [];
async function stop(): Promise<void> {
  if (stopping) return;
  stopping = true;
  if (!worker?.pid) { process.exitCode = 1; process.disconnect?.(); return; }
  try { children = await descendants(worker.pid); }
  catch (error) { console.error(`Worker descendant discovery: ${String(error)}`); }
  await killTree(worker.pid, 'SIGTERM', children);
  if (worker.exitCode === null && worker.signalCode === null) {
    escalation = setTimeout(() => { if (worker?.pid) void killTree(worker.pid, 'SIGKILL', children); }, graceSeconds * 1000);
  }
}
process.on('message', (input: { command?: string[]; graceSeconds?: number; stop?: boolean }) => {
  if (input.stop) { void stop(); return; }
  if (worker || !input.command) return;
  graceSeconds = input.graceSeconds ?? 5;
  worker = spawn(input.command[0]!, input.command.slice(1), {
    env: process.env, cwd: process.cwd(), detached: process.platform !== 'win32',
    stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
  });
  process.stdin.pipe(worker.stdin!);
  worker.stdin!.on('error', () => { process.stdin.unpipe(worker!.stdin!); process.stdin.resume(); });
  worker.stdout!.pipe(process.stdout);
  worker.stderr!.pipe(process.stderr);
  worker.once('error', error => { console.error(error.message); process.exitCode = 1; });
  worker.once('exit', () => { if (worker?.pid) void killTree(worker.pid, 'SIGKILL', children); });
  worker.once('close', code => {
    if (escalation) clearTimeout(escalation);
    process.stdin.unpipe(); process.stdin.destroy();
    process.exitCode = code ?? (stopping ? 0 : 1);
    if (process.connected) process.disconnect();
  });
  if (stopping) { stopping = false; void stop(); }
});
process.once('disconnect', () => { void stop(); });
process.once('SIGTERM', () => { void stop(); });
process.once('SIGINT', () => { void stop(); });
