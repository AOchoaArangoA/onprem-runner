import type { LogFilters, LogRow } from '../api/types';

export const LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL', 'NONE'] as const;
export type Level = (typeof LEVELS)[number];

export interface LogQuery {
  step: string;
  levels: Level[];
  stream: '' | 'stdout' | 'stderr';
  q: string;
}

export const emptyQuery: LogQuery = { step: '', levels: [], stream: '', q: '' };

/** The same filter the engine applies, for lines that arrive live and are never refetched. */
export function matches(row: LogRow, query: LogQuery): boolean {
  if (query.step && row.step_id !== query.step) return false;
  if (query.stream && row.stream !== query.stream) return false;
  if (query.levels.length) {
    const level = (row.level ?? 'NONE') as Level;
    if (!query.levels.includes(level)) return false;
  }
  if (query.q && !row.message.toLowerCase().includes(query.q.toLowerCase())) return false;
  return true;
}

export function toApiFilters(query: LogQuery): LogFilters {
  return {
    step: query.step || undefined,
    level: query.levels.length ? query.levels : undefined,
    stream: query.stream || undefined,
    q: query.q || undefined,
  };
}

/** Merge lines from the API and from the live feed: unique by seq, ordered by seq. */
export function mergeRows(base: LogRow[], extra: LogRow[]): LogRow[] {
  if (extra.length === 0) return base;
  const bySeq = new Map<number, LogRow>();
  for (const row of base) bySeq.set(row.seq, row);
  for (const row of extra) bySeq.set(row.seq, row);
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

export function levelClass(row: LogRow): string {
  switch (row.level) {
    case 'ERROR':
    case 'CRITICAL':
      return 'lv-error';
    case 'WARNING':
      return 'lv-warn';
    case 'DEBUG':
      return 'lv-debug';
    default:
      return row.stream === 'stderr' && !row.level ? 'lv-stderr' : '';
  }
}
