import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { parse } from 'smol-toml';
import { z } from 'zod';

const strings = z.record(z.string(), z.string());
const seconds = z.number().nonnegative();
export const workerSchema = z.object({
  protocol: z.enum(['acp', 'codex', 'agy']),
  command: z.array(z.string()).min(1),
  fallback_commands: z.array(z.array(z.string()).min(1)).default([]),
  cwd: z.string().optional(), env: strings.default({}),
  session_meta: z.record(z.string(), z.unknown()).default({}),
  config_overrides: z.array(z.string()).default([]),
  revivable: z.boolean().default(false), idle_unload_sec: seconds.default(0),
  stall_timeout_sec: seconds.default(0), turn_timeout_sec: seconds.default(0),
  startup_timeout_sec: seconds.default(0), shutdown_grace_sec: seconds.default(5),
  print_timeout: z.string().optional(),
  permission_policy: z.enum(['allow_once', 'allow_always', 'deny']).default('allow_once'),
});
const environmentSchema = z.object({
  inherit: z.union([z.string(), z.array(z.string())]).transform(x => typeof x === 'string' ? [x] : x).default([]),
  set: strings.default({}),
  deny: z.union([z.string(), z.array(z.string())]).transform(x => typeof x === 'string' ? [x] : x).default([]),
  proxy: z.object({ url: z.string().optional(), no_proxy: z.string().optional() }).default({}),
  proxy_url: z.string().optional(), no_proxy: z.string().optional(),
  discover_proxy: z.boolean().default(true),
});
const appSchema = z.object({
  agents: z.record(z.string(), workerSchema),
  env: environmentSchema.prefault({}),
  coordinator: z.object({
    mode: z.enum(['manual', 'auto', 'eager', 'safe', 'yolo']).default('auto'),
    instructions: z.string().default(''),
  }).default({ mode: 'auto', instructions: '' }),
});
export type WorkerConfig = z.infer<typeof workerSchema>;
export type AppConfig = z.infer<typeof appSchema>;

function merge(base: Record<string, unknown>, overlay: Record<string, unknown>): Record<string, unknown> {
  const out = { ...base };
  for (const [key, value] of Object.entries(overlay)) {
    if (value && typeof value === 'object' && !Array.isArray(value) && out[key] && typeof out[key] === 'object' && !Array.isArray(out[key])) {
      out[key] = merge(out[key] as Record<string, unknown>, value as Record<string, unknown>);
    } else out[key] = value;
  }
  return out;
}

export async function loadConfig(home: string, configPath?: string): Promise<AppConfig> {
  let raw = parse(await readFile(new URL('../agents.toml', import.meta.url), 'utf8')) as Record<string, unknown>;
  const paths = configPath ? [resolve(configPath)] : [...new Set([
    join(homedir(), '.agent-bridge', 'agents.toml'), join(home, 'agents.toml'),
  ])];
  for (const path of paths) {
    try { raw = merge(raw, parse(await readFile(path, 'utf8')) as Record<string, unknown>); }
    catch (error) {
      if (!configPath && (error as NodeJS.ErrnoException).code === 'ENOENT') continue;
      throw new Error(`Cannot load ${path}: ${String(error)}`);
    }
  }
  return appSchema.parse(raw);
}

export function redact(text: string, env: NodeJS.ProcessEnv = process.env): string {
  let output = text;
  for (const [key, value] of Object.entries(env)) {
    if (value && /TOKEN|API_KEY|SECRET|PASSWORD|AUTHORIZATION/i.test(key)) output = output.split(value).join('[redacted]');
  }
  return output.replace(/(Bearer\s+)\S+/gi, '$1[redacted]');
}
