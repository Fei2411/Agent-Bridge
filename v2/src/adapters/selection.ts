import { array, object } from './types.js';

type Preferences = Record<string, string[]>;
const common: Preferences = {
  off: ['default', 'off', 'none', 'minimal', 'low'], low: ['low', 'default', 'minimal'],
  medium: ['medium', 'high', 'low', 'default'], high: ['high', 'xhigh', 'max', 'medium'],
  max: ['max', 'xhigh', 'high', 'medium'],
};
const preferences: Record<string, Preferences> = {
  kimi: { off: ['off', 'low', 'on'], low: ['low', 'on', 'medium', 'high'], medium: ['medium', 'high', 'on', 'low'],
    high: ['high', 'on', 'max', 'medium', 'low'], max: ['max', 'xhigh', 'high', 'on'] },
  claude: { ...common, max: ['max', 'ultracode', 'xhigh', 'high', 'medium'] },
  opencode: { ...common, high: ['high', 'medium', 'max', 'default'] },
  zcode: { ...common, off: ['off', 'nothink', 'none', 'disabled', 'minimal', 'low'], max: ['max', 'xhigh', 'high'] },
  minimax: { ...common, max: ['max', 'xhigh', 'high'] },
  dsh: { off: ['off'], low: ['low'], medium: ['high'], high: ['high'], max: ['max'] },
};
export function effortFor(kind: string, requested: string, offered: string[]): string | undefined {
  return (preferences[kind]?.[requested] ?? common[requested] ?? [requested]).find(x => offered.includes(x));
}
export interface Choice { value: string; name: string }
function flatten(value: unknown): Choice[] {
  return array(value).flatMap(item => {
    const entry = object(item);
    return typeof entry.value === 'string'
      ? [{ value: entry.value, name: typeof entry.name === 'string' ? entry.name : entry.value }]
      : flatten(entry.options);
  });
}
export function option(options: unknown[], id: string): { current: string | null; choices: Choice[] } {
  const item = options.map(object).find(item => item.id === id);
  return { current: typeof item?.currentValue === 'string' ? item.currentValue : null, choices: flatten(item?.options) };
}
export function modelLabel(kind: string, value: string): string {
  if (kind === 'minimax' && value.startsWith('m:')) {
    const parts = value.split(':');
    if (parts.length >= 4) return `${decodeURIComponent(parts[1]!)}/${decodeURIComponent(parts[2]!)}${parts[3] === 'v' ? `#${decodeURIComponent(parts[4] ?? '')}` : ''}`;
  }
  if (kind === 'dsh') {
    try { const parts: unknown = JSON.parse(value); if (Array.isArray(parts) && parts.length === 2) return parts.join('/'); } catch { /* Not an encoded model. */ }
  }
  return value;
}
export function modelFor(kind: string, requested: string, choices: Choice[]): string | undefined {
  if (choices.some(c => c.value === requested)) return requested;
  const matches = choices.filter(c => {
    if (kind === 'zcode') return c.value.split('\\').slice(1).join('\\') === requested || c.value.replace('\\', '/') === requested;
    const label = modelLabel(kind, c.value);
    return label === requested || (kind === 'dsh' && label.slice(label.indexOf('/') + 1) === requested);
  });
  return matches.length === 1 ? matches[0]!.value : undefined;
}
const words = (value: string): string[] => value.toLowerCase().match(/[a-z0-9]+/g) ?? [];
const phrase = (all: string[], part: string[]): boolean => part.length > 0 && all.some((_, i) => part.every((v, j) => all[i + j] === v));
export function cursorBase(requested: string, label: string, options: unknown[]): Choice | undefined {
  const choices = option(options, 'model').choices;
  return choices.find(c => c.value === requested) ?? choices.filter(c => {
    const base = words(c.name), full = words(label);
    return base.length > 0 && base.every((v, i) => full[i] === v);
  }).sort((a, b) => words(b.name).length - words(a.name).length)[0];
}
export function cursorParameters(requested: string, label: string, base: Choice, effort: string | null, options: unknown[]): Record<string, string> {
  const without = (all: string[], part: string[]): string[] => {
    const left = [...all]; for (const word of part) { const i = left.indexOf(word); if (i >= 0) left.splice(i, 1); } return left;
  };
  const modelWords = without(words(requested), words(base.value));
  const suffix = without(without(words(label), words(base.name)), words(base.value));
  const targets: Record<string, string> = {};
  for (const item of options.map(object)) {
    if (typeof item.id !== 'string' || ['model', 'mode'].includes(item.id)) continue;
    const choices = option(options, item.id).choices;
    const offered = choices.map(c => c.value);
    if (requested && offered.length === 2 && offered.includes('true') && offered.includes('false')) {
      targets[item.id] = phrase(modelWords, words(item.id)) || choices.some(c => c.value === 'true' && (phrase(modelWords, words(c.name)) || phrase(suffix, words(c.name)))) ? 'true' : 'false';
    } else if (effort && (item.category === 'thought_level' || ['effort', 'reasoning', 'reasoning_effort'].includes(item.id))) {
      const value = effortFor('cursor', effort, offered); if (value) targets[item.id] = value;
    } else {
      const match = choices.filter(c => phrase(modelWords, words(c.value)) || phrase(suffix, words(c.name)))
        .sort((a, b) => Math.max(words(b.value).length, words(b.name).length) - Math.max(words(a.value).length, words(a.name).length))[0];
      if (match) targets[item.id] = match.value;
    }
  }
  return targets;
}
