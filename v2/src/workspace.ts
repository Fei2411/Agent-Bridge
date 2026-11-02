import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { lstat, readlink } from 'node:fs/promises';
import { isAbsolute, relative, resolve, sep } from 'node:path';
import { capture } from './process.js';

export async function snapshot(cwd: string): Promise<Map<string, string> | undefined> {
  const files = await capture(['git', 'ls-files', '-c', '-o', '--exclude-standard', '-z'], { cwd }).catch(() => undefined);
  if (!files || files.code !== 0) return undefined;
  const result = new Map<string, string>();
  for (const path of new Set(files.stdout.split('\0').filter(Boolean))) {
    const absolute = resolve(cwd, path);
    try {
      const info = await lstat(absolute);
      if (info.isSymbolicLink()) result.set(path, `link:${await readlink(absolute)}`);
      else if (info.isFile()) {
        const hash = createHash('sha256');
        for await (const chunk of createReadStream(absolute)) hash.update(chunk);
        result.set(path, hash.digest('hex'));
      }
    } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  }
  return result;
}

export function changed(before: Map<string, string>, after: Map<string, string>): string[] {
  return [...new Set([...before.keys(), ...after.keys()])].filter(path => before.get(path) !== after.get(path)).sort();
}

export function classifyPaths(cwd: string, paths: string[]): { inside: string[]; outside: string[] } {
  const inside = new Set<string>(), outside = new Set<string>();
  for (const path of paths) {
    const absolute = resolve(cwd, path), local = relative(cwd, absolute);
    if (local === '..' || local.startsWith(`..${sep}`) || isAbsolute(local)) outside.add(absolute);
    else inside.add(local);
  }
  return { inside: [...inside].sort(), outside: [...outside].sort() };
}
