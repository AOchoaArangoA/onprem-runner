import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import en from './en.json';
import es from './es.json';
import { resolveLanguage } from './index';

type Tree = { [key: string]: string | Tree };

function flatten(tree: Tree, prefix = ''): string[] {
  return Object.entries(tree).flatMap(([key, value]) =>
    typeof value === 'string' ? [prefix + key] : flatten(value, `${prefix}${key}.`),
  );
}

const PLURAL = /_(zero|one|two|few|many|other)$/;
const base = (key: string) => key.replace(PLURAL, '');

const enKeys = flatten(en as Tree);
const esKeys = flatten(es as Tree);
const enBases = new Set(enKeys.map(base));
const esBases = new Set(esKeys.map(base));

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === 'i18n' ? [] : sourceFiles(path);
    return /\.(ts|tsx)$/.test(name) && !name.endsWith('.test.ts') ? [path] : [];
  });
}

const sourceRoot = join(import.meta.dirname, '..');
const used = new Set<string>();
for (const file of sourceFiles(sourceRoot)) {
  const text = readFileSync(file, 'utf8');
  for (const match of text.matchAll(/\bt\(\s*'([a-zA-Z0-9_.]+)'/g)) used.add(match[1] as string);
}

describe('translations', () => {
  it('has the same keys in Spanish and English', () => {
    expect([...esBases].filter((k) => !enBases.has(k))).toEqual([]);
    expect([...enBases].filter((k) => !esBases.has(k))).toEqual([]);
  });

  it('defines every key that the code asks for', () => {
    expect(used.size).toBeGreaterThan(100); // the scan itself works
    const missing = [...used].filter((key) => !enBases.has(key) && !enKeys.includes(key));
    expect(missing).toEqual([]);
  });

  it('covers the keys built dynamically', () => {
    const wanted = [
      ...['pending', 'running', 'success', 'failed', 'timeout', 'skipped', 'cancelled', 'never'].map((s) => `status.${s}`),
      ...['connecting', 'online', 'offline'].map((s) => `connection.${s}`),
      ...['missing_token', 'unauthorized', 'unreachable'].flatMap((s) => [`fatal.${s}.title`, `fatal.${s}.hint`]),
      ...['python', 'shell'].map((s) => `editor.types.${s}`),
      ...['success', 'failure', 'always'].flatMap((s) => [`editor.when_${s}`, `editor.when_${s}_hint`]),
      'editor.when_failure_short',
      'editor.when_always_short',
      ...['empty', 'invalid', 'duplicate'].map((s) => `editor.id_error.${s}`),
      ...['self', 'duplicate', 'cycle', 'unknown'].map((s) => `editor.connect_error.${s}`),
      ...['no_steps', 'empty_id', 'bad_id', 'duplicate_id', 'missing_script', 'missing_command', 'empty_name'].map(
        (s) => `editor.issue.${s}`,
      ),
      ...['minutes', 'hourly', 'daily', 'weekly', 'startup'].map((s) => `schedule.freq.${s}`),
      ...['minutes', 'hourly', 'daily', 'weekly'].map((s) => `schedule.every_${s}`),
      ...['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'].map((s) => `weekday.${s}`),
      ...['manual', 'schedule', 'cli', 'mcp'].map((s) => `runs.trigger_${s}`),
      ...['pending', 'running', 'success', 'failed', 'cancelled'].map((s) => `status.${s}`),
    ];
    for (const key of wanted) {
      expect(enBases.has(key) || enKeys.includes(key), `en: ${key}`).toBe(true);
      expect(esBases.has(key) || esKeys.includes(key), `es: ${key}`).toBe(true);
    }
  });

  it('keeps interpolation placeholders identical in both languages', () => {
    const placeholders = (text: string) => [...text.matchAll(/{{\s*(\w+)\s*}}/g)].map((m) => m[1]).sort();
    const get = (tree: Tree, key: string): string => {
      let node: string | Tree = tree;
      for (const part of key.split('.')) node = (node as Tree)[part] as string | Tree;
      return node as string;
    };
    for (const key of enKeys.filter((k) => !PLURAL.test(k))) {
      expect(placeholders(get(es as Tree, key)), key).toEqual(placeholders(get(en as Tree, key)));
    }
  });

  it('has no empty strings', () => {
    for (const key of [...enKeys]) {
      const value = key.split('.').reduce<unknown>((node, part) => (node as Tree)[part], en);
      expect((value as string).trim(), key).not.toBe('');
    }
  });
});

describe('resolveLanguage', () => {
  it('honours an explicit choice and detects Spanish otherwise', () => {
    expect(resolveLanguage('en', 'es-ES')).toBe('en');
    expect(resolveLanguage('es', 'en-US')).toBe('es');
    expect(resolveLanguage('auto', 'es-CO')).toBe('es');
    expect(resolveLanguage('auto', 'fr-FR')).toBe('en');
  });
});
