#!/usr/bin/env node
import { randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { z } from 'zod';
import { registerAgent } from './contracts.js';
import { startHub } from './hub.js';
import { connectMcp } from './mcp.js';
import { HubClient } from './client.js';
import { ensureHub } from './daemon.js';
import { BridgeTools } from './tools.js';

async function main(): Promise<void> {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      home: { type: 'string' }, port: { type: 'string' }, config: { type: 'string' },
      'agent-id': { type: 'string' }, kind: { type: 'string' }, project: { type: 'string' },
      name: { type: 'string' }, json: { type: 'string' }, help: { type: 'boolean' },
    },
  });
  if (values.help) {
    console.log('Agent Bridge — centralized Agent runtime\n\n' +
      'agent-bridge-hub [mcp] [--agent-id ID] [--kind KIND] [--project PROJECT] [--home DIR]\n' +
      'agent-bridge-hub start [--home DIR] [--config FILE]\n' +
      'agent-bridge-hub hub [--home DIR] [--config FILE] [--port PORT]\n' +
      'agent-bridge-hub status | stop [--home DIR]\n' +
      'agent-bridge-hub call TOOL --json JSON_ARGUMENTS [identity flags]\n\n' +
      'MCP automatically connects to or starts the independent Hub. start runs it in the background; hub runs in the foreground.');
    return;
  }
  const home = resolve(values.home ?? process.env.AGENT_BRIDGE_HOME ?? join(homedir(), '.agent-bridge', 'hub-v2'));
  const command = positionals[0] ?? 'mcp';
  const configPath = values.config ? resolve(values.config) : undefined;
  if (command === 'hub') {
    const port = z.coerce.number().int().min(0).max(65535).parse(values.port ?? 0);
    const hub = await startHub(home, port, configPath);
    console.error(JSON.stringify({ event: 'ready', url: hub.url }));
    const stop = () => { void hub.close().catch(error => { console.error(error); process.exitCode = 1; }); };
    process.once('SIGINT', stop); process.once('SIGTERM', stop);
  } else if (command === 'start') {
    if (process.env.AGENT_BRIDGE_TOKEN) throw new Error('A Worker cannot start another Hub');
    await ensureHub(home, configPath); console.log(JSON.stringify(await new HubClient(home).request('/health')));
  } else if (command === 'status' || command === 'stop') {
    const client = new HubClient(home, process.env.AGENT_BRIDGE_TOKEN);
    console.log(JSON.stringify(await client.request(command === 'status' ? '/health' : '/v1/shutdown', command === 'stop' ? {} : undefined)));
  } else if (command === 'mcp' || command === 'call') {
    const token = process.env.AGENT_BRIDGE_TOKEN;
    if (!token) await ensureHub(home, configPath);
    const agentId = values['agent-id'] ?? process.env.AGENT_BRIDGE_AGENT_ID ?? `host-${randomUUID()}`;
    const identity = registerAgent.parse({
      id: agentId, kind: values.kind ?? 'external', name: values.name ?? agentId,
      projectId: values.project ?? process.env.AGENT_BRIDGE_PROJECT ?? process.cwd(),
    });
    if (command === 'call') {
      if (!positionals[1]) throw new Error('call requires a tool name');
      const bridge = await BridgeTools.connect(home, identity, token);
      console.log(JSON.stringify(await bridge.call(positionals[1], JSON.parse(values.json ?? '{}'))));
    } else {
      const server = await connectMcp(home, identity, token);
      const stop = () => { void server.close().catch(error => { console.error(error); process.exitCode = 1; }); };
      process.once('SIGINT', stop); process.once('SIGTERM', stop); process.stdin.once('end', stop);
    }
  } else throw new Error('Unknown command; use --help for usage');
}
main().catch(error => { console.error(error instanceof Error ? error.message : String(error)); process.exitCode = 1; });
