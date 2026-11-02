import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
export const cli = fileURLToPath(new URL('../dist/cli.js', import.meta.url));
export const agent = (id, projectId = 'bridge') => ({ id, kind: 'external', name: id, projectId });
export async function stop(child, signal = 'SIGTERM') {
  if (child.exitCode !== null || child.signalCode !== null) return;
  const ended = once(child, 'exit');
  child.kill(signal);
  await ended;
}

export async function fixture(t) {
  const home = await mkdtemp(join(tmpdir(), 'agent-bridge-hub-'));
  await writeFile(join(home, 'agents.toml'), '');
  const children = [];
  const clients = [];
  t.after(async () => {
    await Promise.all(clients.map(client => client.close()));
    await Promise.all(children.map(child => stop(child)));
    await rm(home, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  });
  async function start() {
    const child = spawn(process.execPath, [cli, 'hub', '--home', home, '--config', join(home, 'agents.toml')], { stdio: ['ignore', 'ignore', 'pipe'] });
    children.push(child);
    await new Promise((resolve, reject) => {
      let output = '';
      const fail = code => reject(new Error(`Hub exited (${code}): ${output}`));
      child.once('exit', fail);
      child.once('error', reject);
      child.stderr.on('data', chunk => {
        output += chunk;
        if (output.includes('"event":"ready"')) { child.off('exit', fail); resolve(); }
      });
    });
    return child;
  }
  async function endpoint() {
    const { url } = JSON.parse(await readFile(join(home, 'hub.json'), 'utf8'));
    const token = (await readFile(join(home, 'token'), 'utf8')).trim();
    return { url, headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' } };
  }
  async function request(path, body, expected = 200, headers = {}) {
    const connection = await endpoint();
    const response = await fetch(connection.url + path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { ...connection.headers, ...headers },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: AbortSignal.timeout(5000),
    });
    const value = await response.json();
    assert.equal(response.status, expected, JSON.stringify(value));
    return value;
  }
  async function connect(id) {
    const transport = new StdioClientTransport({
      command: process.execPath,
      args: [cli, 'mcp', '--home', home, '--config', join(home, 'agents.toml'), '--agent-id', id, '--kind', 'external', '--project', 'bridge'],
      stderr: 'pipe',
    });
    transport.stderr?.on('data', () => {});
    const client = new Client({ name: 'integration-test', version: '1.0' });
    clients.push(client);
    await client.connect(transport);
    return client;
  }
  return { home, start, endpoint, request, connect };
}

export async function call(client, name, args = {}) {
  const result = await client.callTool({ name, arguments: args });
  assert.ok(!result.isError, JSON.stringify(result));
  return JSON.parse(result.content[0].text);
}

export async function stream(f, after) {
  const { url, headers } = await f.endpoint();
  const controller = new AbortController();
  const response = await fetch(`${url}/v1/events/stream?projectId=bridge`, {
    headers: { ...headers, 'Last-Event-ID': String(after) }, signal: controller.signal,
  });
  assert.equal(response.status, 200);
  const reader = response.body.getReader();
  let buffer = '';
  const decoder = new TextDecoder();
  return {
    async next() {
      for (;;) {
        const split = buffer.indexOf('\n\n');
        if (split !== -1) {
          const frame = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          const data = frame.split('\n').find(line => line.startsWith('data: '));
          if (data) return JSON.parse(data.slice(6));
        } else {
          const { done, value } = await reader.read();
          assert.equal(done, false, 'event stream ended prematurely');
          buffer += decoder.decode(value, { stream: true });
        }
      }
    },
    async close() { controller.abort(); await reader.cancel().catch(() => {}); },
  };
}
