import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { HubError } from './contracts.js';

export class HubClient {
  readonly #home: string;
  readonly #token: string | undefined;
  constructor(home: string, token?: string) { this.#home = home; this.#token = token; }

  async request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    // Rediscover per request so an existing proxy can reconnect after Hub restart.
    const [metadata, token] = await Promise.all([
      readFile(join(this.#home, 'hub.json'), 'utf8'),
      this.#token === undefined ? readFile(join(this.#home, 'token'), 'utf8') : Promise.resolve(this.#token),
    ]);
    const { url } = JSON.parse(metadata) as { url: string };
    const endpoint = new URL(url);
    if (endpoint.protocol !== 'http:' || endpoint.hostname !== '127.0.0.1' || endpoint.username || endpoint.password) {
      throw new Error('Hub endpoint must be on loopback');
    }
    const response = await fetch(new URL(path, endpoint), {
      method: body === undefined ? 'GET' : 'POST',
      headers: { Authorization: `Bearer ${token.trim()}`, 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      ...(signal ? { signal } : {}),
      redirect: 'error',
    });
    const result = await response.json() as T & { error?: string };
    if (!response.ok) throw new HubError(response.status, result.error ?? `Hub returned ${response.status}`);
    return result;
  }
}
