import { ArrowLeft, Pencil, Square } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import type { RunDetail } from '../api/types';
import { Link } from '../components/Link';
import { LogViewer } from '../components/LogViewer';
import { Empty, Spinner, StatusBadge } from '../components/ui';
import { formatDateTime, formatDuration, timelineBars } from '../lib/format';
import { errorMessage, useApp } from '../store/app';

export function RunDetailPage({ id }: { id: string }) {
  const { t } = useTranslation();
  const toast = useApp((s) => s.toast);
  const active = useApp((s) => s.active[id]);
  const [run, setRun] = useState<RunDetail | null>(null);
  const [missing, setMissing] = useState(false);
  const live = active !== undefined;

  // Reload when the run starts or ends, and while it runs when a step changes state.
  const stepSignature = active ? JSON.stringify(active.steps) : '';
  useEffect(() => {
    let cancelled = false;
    api
      .runDetail(id)
      .then((detail) => !cancelled && (setRun(detail), setMissing(false)))
      .catch(() => !cancelled && setMissing(true));
    return () => {
      cancelled = true;
    };
  }, [id, live, stepSignature]);

  if (missing && !run) {
    return (
      <div className="page">
        <Empty
          title={t('runs.not_found')}
          action={
            <Link to={{ name: 'runs' }} className="button">
              {t('runs.back')}
            </Link>
          }
        />
      </div>
    );
  }
  if (!run) return <Spinner label={t('common.loading')} />;

  const bars = timelineBars(run.steps);
  const status = live ? 'running' : run.status;

  return (
    <div className="page run-detail">
      <header className="page-head">
        <div>
          <Link to={{ name: 'runs' }} className="crumb">
            <ArrowLeft size={14} /> {t('runs.back')}
          </Link>
          <h1>
            {run.pipeline_name} <StatusBadge status={status} />
          </h1>
          <p className="muted">
            {formatDateTime(run.started_at)} · {formatDuration(run.duration_s)} ·{' '}
            {t(`runs.trigger_${run.trigger}`, { defaultValue: run.trigger })} ·{' '}
            <span className="mono">{run.run_id}</span>
          </p>
        </div>
        <div className="toolbar">
          {live && (
            <button
              className="danger"
              onClick={() => void api.cancel(id).catch((e: unknown) => toast('error', errorMessage(e)))}
            >
              <Square size={14} /> {t('editor.stop')}
            </button>
          )}
          {run.file && (
            <Link to={{ name: 'editor', file: run.file }} className="button">
              <Pencil size={14} /> {t('runs.open_pipeline')}
            </Link>
          )}
        </div>
      </header>

      <section className="timeline" aria-label={t('runs.timeline')}>
        {run.steps.map((step, index) => {
          const bar = bars[index];
          const live_status = active?.steps[step.step_id] ?? step.status;
          return (
            <div className="tl-row" key={step.step_id}>
              <div className="tl-name" title={step.step_id}>
                {step.step_id}
              </div>
              <div className="tl-track">
                {bar && (
                  <div
                    className={`tl-bar s-${live_status}`}
                    style={{ left: `${bar.left * 100}%`, width: `${bar.width * 100}%` }}
                    title={`${t(`status.${live_status}`)} · ${formatDuration(step.duration_s)}`}
                  />
                )}
              </div>
              <div className="tl-meta">
                <StatusBadge status={live_status} small />
                <span className="muted small">
                  {formatDuration(step.duration_s)}
                  {step.attempts > 1 && ` · ${t('runs.attempts', { count: step.attempts })}`}
                  {step.exit_code !== null && step.exit_code !== 0 && ` · exit ${step.exit_code}`}
                </span>
              </div>
            </div>
          );
        })}
      </section>

      <section className="run-logs">
        <h2>{t('logs.title')}</h2>
        <LogViewer runId={id} live={live} steps={run.steps.map((s) => s.step_id)} className="tall" />
      </section>
    </div>
  );
}
