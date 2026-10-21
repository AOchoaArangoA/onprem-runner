import { AlertTriangle, Clock, KeyRound, Pause, Pencil, Play, Plus, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import type { PipelineSummary } from '../api/types';
import { Link } from '../components/Link';
import { ConfirmDialog, Empty, PromptDialog, StatusBadge } from '../components/ui';
import { describeSchedule, formatDuration, formatNextRun, relativeTime } from '../lib/format';
import { navigate } from '../lib/router';
import { errorMessage, useApp } from '../store/app';

export function PipelinesPage() {
  const { t } = useTranslation();
  const pipelines = useApp((s) => s.pipelines);
  const engine = useApp((s) => s.engine);
  const toast = useApp((s) => s.toast);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<PipelineSummary | null>(null);

  // YAML files copied into the folder by hand raise no event, so look for them periodically.
  useEffect(() => {
    const id = setInterval(() => {
      if (!document.hidden) void useApp.getState().refreshPipelines();
    }, 4000);
    return () => clearInterval(id);
  }, []);

  const stats = engine?.stats;
  const rate = stats?.success_rate;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>{t('nav.pipelines')}</h1>
          <p className="muted">{engine?.workspace}</p>
        </div>
        <button className="primary" onClick={() => setCreating(true)}>
          <Plus size={16} /> {t('pipelines.new')}
        </button>
      </header>

      <section className="stats" aria-label={t('pipelines.summary')}>
        <Stat label={t('pipelines.count')} value={pipelines.length} />
        <Stat label={t('pipelines.runs_24h')} value={stats?.runs_24h ?? 0} />
        <Stat label={t('pipelines.success_rate')} value={rate === null || rate === undefined ? '–' : `${Math.round(rate * 100)}%`} />
        <Stat label={t('pipelines.running')} value={stats?.running ?? 0} />
        <Stat
          label={t('pipelines.scheduled')}
          value={pipelines.filter((p) => p.next_run && !p.paused).length}
        />
      </section>

      {pipelines.length === 0 ? (
        <Empty
          title={t('pipelines.empty_title')}
          hint={t('pipelines.empty_hint')}
          action={
            <button className="primary" onClick={() => setCreating(true)}>
              <Plus size={16} /> {t('pipelines.new')}
            </button>
          }
        />
      ) : (
        <div className="cards">
          {pipelines.map((pipeline) => (
            <PipelineCard
              key={pipeline.file}
              pipeline={pipeline}
              onDelete={() => setDeleting(pipeline)}
              onError={(error) => toast('error', errorMessage(error))}
            />
          ))}
        </div>
      )}

      {creating && (
        <PromptDialog
          title={t('pipelines.new')}
          label={t('pipelines.name')}
          confirmLabel={t('common.create')}
          onClose={() => setCreating(false)}
          onSubmit={async (name) => {
            try {
              const { file } = await api.createPipeline(name);
              navigate({ name: 'editor', file });
            } catch (error) {
              toast('error', errorMessage(error));
              throw error;
            }
          }}
        />
      )}
      {deleting && (
        <ConfirmDialog
          danger
          title={t('pipelines.delete_title')}
          message={t('pipelines.delete_message', { name: deleting.name })}
          confirmLabel={t('common.delete')}
          onClose={() => setDeleting(null)}
          onConfirm={() =>
            void api.deletePipeline(deleting.file).catch((error: unknown) => toast('error', errorMessage(error)))
          }
        />
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="stat">
      <div className="muted">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  );
}

function PipelineCard({
  pipeline,
  onDelete,
  onError,
}: {
  pipeline: PipelineSummary;
  onDelete: () => void;
  onError: (error: unknown) => void;
}) {
  const { t, i18n } = useTranslation();
  const active = useApp((s) => Object.values(s.active).find((run) => run.file === pipeline.file));
  const last = pipeline.last_run;
  const running = active !== undefined;
  const steps = active ? Object.entries(active.steps) : [];
  const finished = steps.filter(([, status]) => !['pending', 'running'].includes(status)).length;

  return (
    <article className={`card${pipeline.error ? ' has-error' : ''}`}>
      <div className="card-head">
        <div className="card-title">
          <Link to={{ name: 'editor', file: pipeline.file }} className="name">
            {pipeline.name}
          </Link>
          <div className="muted mono small">{pipeline.file}</div>
        </div>
        {running ? <StatusBadge status="running" /> : last ? <StatusBadge status={last.status} /> : <span className="badge s-none">{t('status.never')}</span>}
      </div>

      {pipeline.error && (
        <div className="card-alert error" title={pipeline.error}>
          <AlertTriangle size={14} aria-hidden /> {t('pipelines.invalid')}
        </div>
      )}
      {pipeline.secrets_missing.length > 0 && (
        <div className="card-alert warn" title={pipeline.secrets_missing.join(', ')}>
          <KeyRound size={14} aria-hidden /> {t('pipelines.secrets_missing', { names: pipeline.secrets_missing.join(', ') })}
        </div>
      )}

      <div className="card-meta">
        <span>{t('pipelines.steps', { count: pipeline.steps })}</span>
        {pipeline.schedule && (
          <span title={t('schedule.title')}>
            <Clock size={13} aria-hidden /> {describeSchedule(pipeline.schedule, t)}
          </span>
        )}
      </div>
      {pipeline.schedule && (
        <div className="next-run">
          {pipeline.paused ? (
            <span className="badge s-timeout small">{t('schedule.paused')}</span>
          ) : pipeline.next_run ? (
            <span>
              {t('schedule.next')}: <strong>{formatNextRun(pipeline.next_run, i18n.language)}</strong>
            </span>
          ) : (
            <span className="muted">{t('schedule.on_open')}</span>
          )}
          <button
            className="link"
            onClick={() => void api.pauseSchedule(pipeline.file, !pipeline.paused).catch(onError)}
          >
            {pipeline.paused ? <Play size={12} /> : <Pause size={12} />}{' '}
            {pipeline.paused ? t('schedule.resume') : t('schedule.pause')}
          </button>
        </div>
      )}

      {running ? (
        <div className="progress" aria-label={t('pipelines.progress')}>
          <div className="progress-bar" style={{ width: `${steps.length ? (finished / steps.length) * 100 : 0}%` }} />
        </div>
      ) : (
        <div className="muted small">
          {last
            ? `${relativeTime(last.started_at, i18n.language)} · ${formatDuration(last.duration_s)}`
            : t('pipelines.never_run')}
        </div>
      )}

      <div className="card-actions">
        <button
          className="run"
          disabled={running || !!pipeline.error}
          onClick={() => void api.run(pipeline.file).catch(onError)}
        >
          <Play size={14} /> {t('editor.run')}
        </button>
        <Link to={{ name: 'editor', file: pipeline.file }} className="button">
          <Pencil size={14} /> {t('pipelines.open')}
        </Link>
        <button className="icon-btn danger-hover" onClick={onDelete} aria-label={t('common.delete')} title={t('common.delete')}>
          <Trash2 size={15} />
        </button>
      </div>
    </article>
  );
}
