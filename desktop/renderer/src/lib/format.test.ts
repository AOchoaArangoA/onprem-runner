import { describe, expect, it } from 'vitest';
import {
  describeSchedule,
  formatBytes,
  formatClock,
  formatDuration,
  formatNextRun,
  relativeTime,
  timelineBars,
} from './format';

describe('formatDuration', () => {
  it.each([
    [null, '–'],
    [undefined, '–'],
    [0.25, '250 ms'],
    [1.234, '1.2 s'],
    [42, '42 s'],
    [75, '1 min 15 s'],
    [3660, '1 h 1 min'],
  ])('%s -> %s', (input, expected) => {
    expect(formatDuration(input)).toBe(expected);
  });
});

describe('formatBytes', () => {
  it.each([
    [512, '512 B'],
    [2048, '2.0 KB'],
    [5 * 1024 * 1024, '5.0 MB'],
    [300 * 1024 * 1024, '300 MB'],
  ])('%d -> %s', (input, expected) => {
    expect(formatBytes(input)).toBe(expected);
  });
});

describe('relativeTime', () => {
  const now = new Date('2026-10-05T12:00:00').getTime();
  it('uses natural language in the requested locale', () => {
    expect(relativeTime('2026-10-05T11:55:00', 'en', now)).toBe('5 minutes ago');
    expect(relativeTime('2026-10-05T11:55:00', 'es', now)).toBe('hace 5 minutos');
    expect(relativeTime('2026-10-05T12:00:10', 'en', now)).toBe('now');
    expect(relativeTime('2026-10-05T09:00:00', 'en', now)).toBe('3 hours ago');
  });
});

describe('describeSchedule', () => {
  const t = (key: string, o?: Record<string, unknown>) => `${key}${o ? JSON.stringify(o) : ''}`;
  it('picks the right wording', () => {
    expect(describeSchedule({ frequency: 'daily', at: '08:30', every: 1, days: [], catch_up: true }, t)).toBe(
      'schedule.daily_at{"at":"08:30"}',
    );
    expect(describeSchedule({ frequency: 'minutes', at: null, every: 15, days: [], catch_up: true }, t)).toBe(
      'schedule.every_n_minutes{"n":15}',
    );
    expect(describeSchedule({ frequency: 'startup', at: null, every: 1, days: [], catch_up: true }, t)).toBe(
      'schedule.at_startup',
    );
    expect(
      describeSchedule({ frequency: 'weekly', at: '02:00', every: 1, days: ['MON', 'FRI'], catch_up: true }, t),
    ).toBe('schedule.weekly_on{"days":"weekday.MON, weekday.FRI","at":"02:00"}');
  });
});

describe('timelineBars', () => {
  it('places bars proportionally and gives instant steps a visible width', () => {
    const bars = timelineBars([
      { started_at: '2026-10-05T12:00:00', finished_at: '2026-10-05T12:00:02' },
      { started_at: '2026-10-05T12:00:02', finished_at: '2026-10-05T12:00:10' },
      { started_at: '2026-10-05T12:00:10', finished_at: '2026-10-05T12:00:10' },
      { started_at: null, finished_at: null },
    ]);
    expect(bars[0]).toEqual({ left: 0, width: 0.2 });
    expect(bars[1]?.left).toBeCloseTo(0.2);
    expect(bars[1]?.width).toBeCloseTo(0.8);
    expect(bars[2]?.width).toBeGreaterThan(0);
    expect(bars[3]).toBeNull();
  });

  it('handles runs with no timestamps', () => {
    expect(timelineBars([{ started_at: null, finished_at: null }])).toEqual([null]);
    expect(timelineBars([])).toEqual([]);
  });
});

describe('formatClock', () => {
  it('uses 24 hours and puts the milliseconds last', () => {
    expect(formatClock('2026-10-05T22:21:38.252', true)).toBe('22:21:38.252');
    expect(formatClock('2026-10-05T07:05:09.004', true)).toBe('07:05:09.004');
    expect(formatClock('2026-10-05T22:21:38.252')).toBe('22:21:38');
  });
});

describe('formatNextRun', () => {
  const now = new Date('2026-10-05T12:00:00');
  it('says today and tomorrow in the language of the user', () => {
    expect(formatNextRun('2026-10-05T18:30:00', 'en', now)).toBe('today 18:30');
    expect(formatNextRun('2026-10-06T07:30:00', 'en', now)).toBe('tomorrow 07:30');
    expect(formatNextRun('2026-10-05T18:30:00', 'es', now)).toBe('hoy 18:30');
    expect(formatNextRun('2026-10-06T07:30:00', 'es', now)).toBe('mañana 07:30');
  });

  it('shows weekday and date further ahead', () => {
    expect(formatNextRun('2026-10-09T02:00:00', 'en', now)).toBe("Fri, Oct 9 02:00");
  });
});
