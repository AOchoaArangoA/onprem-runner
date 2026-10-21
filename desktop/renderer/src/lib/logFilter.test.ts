import { describe, expect, it } from 'vitest';
import type { LogRow } from '../api/types';
import { emptyQuery, levelClass, matches, mergeRows, toApiFilters } from './logFilter';

const row = (seq: number, over: Partial<LogRow> = {}): LogRow => ({
  run_id: 'r',
  step_id: 'a',
  seq,
  ts: '2026-10-05T12:00:00',
  stream: 'stdout',
  level: null,
  message: `line ${seq}`,
  fields: null,
  ...over,
});

describe('matches', () => {
  it('lets everything through with an empty query', () => {
    expect(matches(row(1), emptyQuery)).toBe(true);
  });

  it('filters by step, stream and text (case-insensitive)', () => {
    expect(matches(row(1), { ...emptyQuery, step: 'b' })).toBe(false);
    expect(matches(row(1, { stream: 'stderr' }), { ...emptyQuery, stream: 'stdout' })).toBe(false);
    expect(matches(row(1, { message: 'Disk FULL' }), { ...emptyQuery, q: 'full' })).toBe(true);
    expect(matches(row(1, { message: 'ok' }), { ...emptyQuery, q: 'full' })).toBe(false);
  });

  it('treats NONE as lines without a level', () => {
    const plain = row(1);
    const warn = row(2, { level: 'WARNING' });
    expect(matches(plain, { ...emptyQuery, levels: ['NONE'] })).toBe(true);
    expect(matches(warn, { ...emptyQuery, levels: ['NONE'] })).toBe(false);
    expect(matches(warn, { ...emptyQuery, levels: ['WARNING', 'ERROR'] })).toBe(true);
    expect(matches(plain, { ...emptyQuery, levels: ['WARNING'] })).toBe(false);
  });
});

describe('toApiFilters', () => {
  it('omits empty values so the URL stays clean', () => {
    expect(toApiFilters(emptyQuery)).toEqual({
      step: undefined,
      level: undefined,
      stream: undefined,
      q: undefined,
    });
    expect(toApiFilters({ step: 'a', levels: ['ERROR'], stream: 'stderr', q: 'x' })).toEqual({
      step: 'a',
      level: ['ERROR'],
      stream: 'stderr',
      q: 'x',
    });
  });
});

describe('mergeRows', () => {
  it('deduplicates by seq, prefers newer data and keeps order', () => {
    const merged = mergeRows([row(1), row(2), row(4)], [row(3), row(4, { message: 'newer' })]);
    expect(merged.map((r) => r.seq)).toEqual([1, 2, 3, 4]);
    expect(merged.at(-1)?.message).toBe('newer');
  });

  it('returns the base array itself when there is nothing to merge', () => {
    const base = [row(1)];
    expect(mergeRows(base, [])).toBe(base);
  });
});

describe('levelClass', () => {
  it('colours errors, warnings and bare stderr', () => {
    expect(levelClass(row(1, { level: 'ERROR' }))).toBe('lv-error');
    expect(levelClass(row(1, { level: 'CRITICAL' }))).toBe('lv-error');
    expect(levelClass(row(1, { level: 'WARNING' }))).toBe('lv-warn');
    expect(levelClass(row(1, { stream: 'stderr' }))).toBe('lv-stderr');
    expect(levelClass(row(1, { stream: 'stderr', level: 'INFO' }))).toBe('');
    expect(levelClass(row(1))).toBe('');
  });
});
