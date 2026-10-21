import { useTranslation } from 'react-i18next';
import type { Frequency, Schedule, Weekday } from '../../api/types';
import { Field, Toggle } from '../../components/ui';

const FREQUENCIES: Frequency[] = ['minutes', 'hourly', 'daily', 'weekly', 'startup'];
const WEEKDAYS: Weekday[] = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];
const MAX_EVERY: Record<string, number> = { minutes: 1439, hourly: 23, daily: 365, weekly: 52 };

export function normalizeSchedule(schedule: Schedule): Schedule {
  const { frequency } = schedule;
  return {
    frequency,
    at: frequency === 'daily' || frequency === 'weekly' ? (schedule.at ?? '08:00') : null,
    every: frequency === 'startup' ? 1 : Math.min(Math.max(1, schedule.every || 1), MAX_EVERY[frequency] ?? 1),
    days: frequency === 'weekly' ? (schedule.days.length ? schedule.days : ['MON']) : [],
  };
}

export function ScheduleEditor({
  schedule,
  onChange,
}: {
  schedule: Schedule | null;
  onChange: (value: Schedule | null) => void;
}) {
  const { t } = useTranslation();
  const set = (patch: Partial<Schedule>) => schedule && onChange(normalizeSchedule({ ...schedule, ...patch }));
  return (
    <div className="stack">
      <Toggle
        checked={schedule !== null}
        label={t('schedule.enabled')}
        onChange={(on) =>
          onChange(on ? normalizeSchedule({ frequency: 'daily', at: '08:00', every: 1, days: [] }) : null)
        }
      />
      {schedule && (
        <>
          <Field label={t('schedule.frequency')}>
            <select value={schedule.frequency} onChange={(e) => set({ frequency: e.target.value as Frequency })}>
              {FREQUENCIES.map((f) => (
                <option key={f} value={f}>
                  {t(`schedule.freq.${f}`)}
                </option>
              ))}
            </select>
          </Field>
          {(schedule.frequency === 'daily' || schedule.frequency === 'weekly') && (
            <Field label={t('schedule.at')}>
              <input type="time" value={schedule.at ?? ''} onChange={(e) => set({ at: e.target.value })} />
            </Field>
          )}
          {schedule.frequency !== 'startup' && (
            <Field label={t(`schedule.every_${schedule.frequency}`)}>
              <input
                type="number"
                min={1}
                max={MAX_EVERY[schedule.frequency]}
                value={schedule.every}
                onChange={(e) => set({ every: Number(e.target.value) })}
              />
            </Field>
          )}
          {schedule.frequency === 'weekly' && (
            <Field label={t('schedule.days')} group>
              <div className="weekdays">
                {WEEKDAYS.map((day) => {
                  const on = schedule.days.includes(day);
                  return (
                    <button
                      key={day}
                      className={`chip${on ? ' on' : ''}`}
                      aria-pressed={on}
                      onClick={() =>
                        set({ days: on ? schedule.days.filter((d) => d !== day) : [...schedule.days, day] })
                      }
                    >
                      {t(`weekday.${day}`)}
                    </button>
                  );
                })}
              </div>
            </Field>
          )}
          <p className="hint">{t('schedule.note')}</p>
        </>
      )}
    </div>
  );
}
