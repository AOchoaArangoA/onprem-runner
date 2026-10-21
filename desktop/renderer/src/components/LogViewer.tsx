import { ArrowDownToLine, Download, Search } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import type { LogRow } from '../api/types';
import { formatClock } from '../lib/format';
import {
  emptyQuery,
  LEVELS,
  levelClass,
  matches,
  mergeRows,
  toApiFilters,
  type Level,
  type LogQuery,
} from '../lib/logFilter';
import { useApp } from '../store/app';

const ROW_HEIGHT = 22;
const PAGE = 1000;
const OVERSCAN = 12;

interface Props {
  runId: string;
  /** The run is still going: lines keep arriving through the event stream. */
  live: boolean;
  steps: string[];
  className?: string;
}

export function LogViewer({ runId, live, steps, className }: Props) {
  const { t } = useTranslation();
  const [query, setQuery] = useState<LogQuery>(emptyQuery);
  const [debouncedText, setDebouncedText] = useState('');
  const [fetched, setFetched] = useState<LogRow[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [follow, setFollow] = useState(true);
  const [open, setOpen] = useState<LogRow | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewport, setViewport] = useState(300);
  const scroller = useRef<HTMLDivElement>(null);
  const liveRows = useApp((s) => s.liveLogs[runId]);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedText(query.q), 300);
    return () => clearTimeout(timer);
  }, [query.q]);

  const effective = useMemo<LogQuery>(() => ({ ...query, q: debouncedText }), [query, debouncedText]);

  // First page (or a new filter): ask the engine.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setFetched([]);
    setOpen(null);
    api
      .logs(runId, toApiFilters(effective), 0, PAGE)
      .then((rows) => {
        if (cancelled) return;
        setFetched(rows);
        setHasMore(rows.length === PAGE);
      })
      .catch(() => !cancelled && setFetched([]))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [runId, effective, live]); // `live` flipping to false means: reread what was stored

  const loadMore = useCallback(async () => {
    const last = fetched.at(-1);
    if (!last || loading) return;
    setLoading(true);
    try {
      const rows = await api.logs(runId, toApiFilters(effective), last.seq, PAGE);
      setFetched((prev) => [...prev, ...rows]);
      setHasMore(rows.length === PAGE);
    } finally {
      setLoading(false);
    }
  }, [fetched, loading, runId, effective]);

  // While live, lines newer than what was fetched come from the event stream.
  const rows = useMemo(() => {
    if (!live || !liveRows) return fetched;
    const lastSeq = fetched.at(-1)?.seq ?? 0;
    const fresh = liveRows.filter((r) => r.seq > lastSeq && matches(r, effective));
    return mergeRows(fetched, fresh);
  }, [live, liveRows, fetched, effective]);

  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    setViewport(el.clientHeight);
    const observer = new ResizeObserver(() => setViewport(el.clientHeight));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const el = scroller.current;
    if (follow && el) el.scrollTop = el.scrollHeight;
  }, [rows.length, follow]);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    setScrollTop(el.scrollTop);
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
    if (atBottom !== follow && live) setFollow(atBottom);
  };

  const start = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const end = Math.min(rows.length, Math.ceil((scrollTop + viewport) / ROW_HEIGHT) + OVERSCAN);
  const visible = rows.slice(start, end);

  const toggleLevel = (level: Level) =>
    setQuery((q) => ({
      ...q,
      levels: q.levels.includes(level) ? q.levels.filter((l) => l !== level) : [...q.levels, level],
    }));

  const exportLinks = (['txt', 'csv', 'jsonl'] as const).map((format) => (
    <a key={format} href={api.exportUrl(runId, format, toApiFilters(effective))} download>
      {format.toUpperCase()}
    </a>
  ));

  return (
    <div className={`logviewer ${className ?? ''}`}>
      <div className="log-toolbar">
        <div className="search">
          <Search size={14} aria-hidden />
          <input
            type="search"
            placeholder={t('logs.search')}
            aria-label={t('logs.search')}
            value={query.q}
            onChange={(e) => setQuery({ ...query, q: e.target.value })}
          />
        </div>
        <select
          aria-label={t('logs.step')}
          value={query.step}
          onChange={(e) => setQuery({ ...query, step: e.target.value })}
        >
          <option value="">{t('logs.all_steps')}</option>
          {steps.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select
          aria-label={t('logs.stream')}
          value={query.stream}
          onChange={(e) => setQuery({ ...query, stream: e.target.value as LogQuery['stream'] })}
        >
          <option value="">{t('logs.all_streams')}</option>
          <option value="stdout">stdout</option>
          <option value="stderr">stderr</option>
        </select>
        <div className="level-chips" role="group" aria-label={t('logs.levels')}>
          {LEVELS.map((level) => (
            <button
              key={level}
              className={`chip lv-${level.toLowerCase()}${query.levels.includes(level) ? ' on' : ''}`}
              aria-pressed={query.levels.includes(level)}
              onClick={() => toggleLevel(level)}
            >
              {level === 'NONE' ? t('logs.no_level') : level}
            </button>
          ))}
        </div>
        <span className="spacer" />
        <span className="muted count">{t('logs.lines', { count: rows.length })}</span>
        {live && (
          <button
            className={`icon-btn${follow ? ' on' : ''}`}
            title={t('logs.follow')}
            aria-label={t('logs.follow')}
            aria-pressed={follow}
            onClick={() => setFollow(!follow)}
          >
            <ArrowDownToLine size={16} />
          </button>
        )}
        <span className="export">
          <Download size={14} aria-hidden /> {exportLinks}
        </span>
      </div>

      <div className="log-scroll" ref={scroller} onScroll={onScroll} tabIndex={0} aria-label={t('logs.title')}>
        {rows.length === 0 ? (
          <div className="log-empty muted">{loading ? t('common.loading') : t('logs.empty')}</div>
        ) : (
          <div style={{ height: rows.length * ROW_HEIGHT, position: 'relative' }}>
            {visible.map((row, i) => (
              <div
                key={row.seq}
                className={`log-row ${levelClass(row)}${open?.seq === row.seq ? ' open' : ''}`}
                style={{ top: (start + i) * ROW_HEIGHT, height: ROW_HEIGHT }}
                onClick={() => setOpen(open?.seq === row.seq ? null : row)}
              >
                <span className="log-time">{formatClock(row.ts, true)}</span>
                <span className="log-step">{row.step_id}</span>
                <span className="log-level">{row.level ?? (row.stream === 'stderr' ? 'ERR' : '')}</span>
                <span className="log-msg">{row.message}</span>
                {row.fields && <span className="log-fields">{JSON.stringify(row.fields)}</span>}
              </div>
            ))}
          </div>
        )}
        {hasMore && !live && (
          <button className="load-more" onClick={() => void loadMore()} disabled={loading}>
            {t('logs.load_more')}
          </button>
        )}
      </div>

      {open && (
        <div className="log-detail">
          <div className="muted">
            #{open.seq} · {open.step_id} · {open.stream} · {open.level ?? '–'}
          </div>
          <pre>{open.message}</pre>
          {open.fields && <pre className="fields">{JSON.stringify(open.fields, null, 2)}</pre>}
        </div>
      )}
    </div>
  );
}
