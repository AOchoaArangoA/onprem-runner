import type { Schedule, StepStatus } from '../api/types';

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '–';
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes} min ${rest} s`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

/** The engine sends naive local timestamps; `new Date()` reads those as local time. */
export function parseTime(iso: string): Date {
  return new Date(iso);
}

export function formatClock(iso: string, withMillis = false): string {
  const d = parseTime(iso);
  const base = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  return withMillis ? `${base}.${String(d.getMilliseconds()).padStart(3, '0')}` : base;
}

export function formatDateTime(iso: string): string {
  return parseTime(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'medium' });
}

export function relativeTime(iso: string, locale: string, now = Date.now()): string {
  const seconds = Math.round((parseTime(iso).getTime() - now) / 1000);
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' });
  const abs = Math.abs(seconds);
  if (abs < 45) return rtf.format(0, 'second');
  if (abs < 3600) return rtf.format(Math.round(seconds / 60), 'minute');
  if (abs < 86400) return rtf.format(Math.round(seconds / 3600), 'hour');
  if (abs < 86400 * 30) return rtf.format(Math.round(seconds / 86400), 'day');
  return parseTime(iso).toLocaleDateString(locale, { dateStyle: 'medium' });
}

/** "today 07:30", "tomorrow 07:30" or "Thu, 8 Oct 07:30": when something will happen next. */
export function formatNextRun(iso: string, locale: string, now = new Date()): string {
  const date = parseTime(iso);
  const time = date.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit', hour12: false });
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((startOfDay(date) - startOfDay(now)) / 86_400_000);
  if (days >= -1 && days <= 1) {
    const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' });
    return `${rtf.format(days, 'day')} ${time}`;
  }
  const day = date.toLocaleDateString(locale, { weekday: 'short', day: 'numeric', month: 'short' });
  return `${day} ${time}`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

type Translate = (key: string, options?: Record<string, unknown>) => string;

export function describeSchedule(schedule: Schedule, t: Translate): string {
  const every = schedule.every;
  switch (schedule.frequency) {
    case 'minutes':
      return every === 1 ? t('schedule.every_minute') : t('schedule.every_n_minutes', { n: every });
    case 'hourly':
      return every === 1 ? t('schedule.every_hour') : t('schedule.every_n_hours', { n: every });
    case 'daily':
      return every === 1
        ? t('schedule.daily_at', { at: schedule.at })
        : t('schedule.every_n_days_at', { n: every, at: schedule.at });
    case 'weekly': {
      const days = schedule.days.map((d) => t(`weekday.${d}`)).join(', ');
      return every === 1
        ? t('schedule.weekly_on', { days, at: schedule.at })
        : t('schedule.every_n_weeks_on', { n: every, days, at: schedule.at });
    }
    default:
      return t('schedule.at_startup');
  }
}

export const isFinalStatus = (status: StepStatus) =>
  status === 'success' || status === 'failed' || status === 'timeout' || status === 'cancelled';

/** Where each step's bar sits on a run timeline: offsets as fractions of the whole run. */
export function timelineBars(
  steps: { started_at: string | null; finished_at: string | null }[],
): ({ left: number; width: number } | null)[] {
  const times = steps
    .flatMap((s) => [s.started_at, s.finished_at])
    .filter((t): t is string => t !== null)
    .map((t) => parseTime(t).getTime());
  if (times.length === 0) return steps.map(() => null);
  const start = Math.min(...times);
  const span = Math.max(Math.max(...times) - start, 1);
  return steps.map((s) => {
    if (!s.started_at) return null;
    const from = parseTime(s.started_at).getTime();
    const to = s.finished_at ? parseTime(s.finished_at).getTime() : Math.max(...times);
    return { left: (from - start) / span, width: Math.max((to - from) / span, 0.006) };
  });
}
