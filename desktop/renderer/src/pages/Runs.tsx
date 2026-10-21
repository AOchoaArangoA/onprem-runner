import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import type { RunSummary } from '../api/types';
import { Link } from '../components/Link';
import { Empty, Spinner, StatusBadge } from '../components/ui';
import { formatDateTime, formatDuration, relativeTime } from '../lib/format';
import { useApp } from '../store/app';

export function RunsPage() {
  const { t, i18n } = useTranslation();
  const pipelines = useApp((s) => s.pipelines);
  const recent = useApp((s) => s.runs);
  const [file, setFile] = useState('');
  const [status, setStatus] = useState('');
  const [runs, setRuns] = useState<RunSummary[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .runs({ file: file || undefined, status: status || undefined, limit: 200 })
      .then((rows) => !cancelled && setRuns(rows))
      .catch(() => !cancelled && setRuns([]));
    return () => {
      cancelled = true;
    };
    // `recent` changes whenever a run finishes, which is when this list must refresh.
  }, [file, status, recent]);

  return (
    <div className="page">
      <header className="page-head">
        <h1>{t('nav.runs')}</h1>
        <div className="filters">
          <select aria-label={t('runs.filter_pipeline')} value={file} onChange={(e) => setFile(e.target.value)}>
            <option value="">{t('runs.all_pipelines')}</option>
            {pipelines.map((p) => (
              <option key={p.file} value={p.file}>
                {p.name}
              </option>
            ))}
          </select>
          <select aria-label={t('runs.filter_status')} value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">{t('runs.all_statuses')}</option>
            {['success', 'failed', 'cancelled', 'running'].map((s) => (
              <option key={s} value={s}>
                {t(`status.${s}`)}
              </option>
            ))}
          </select>
        </div>
      </header>

      {runs === null ? (
        <Spinner label={t('common.loading')} />
      ) : runs.length === 0 ? (
        <Empty title={t('runs.empty_title')} hint={t('runs.empty_hint')} />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{t('runs.status')}</th>
                <th>{t('runs.pipeline')}</th>
                <th>{t('runs.started')}</th>
                <th>{t('runs.duration')}</th>
                <th>{t('runs.trigger')}</th>
                <th>{t('runs.id')}</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.run_id}>
                  <td>
                    <StatusBadge status={run.status} />
                  </td>
                  <td>
                    <Link to={{ name: 'run', id: run.run_id }} className="name">
                      {run.pipeline_name}
                    </Link>
                  </td>
                  <td title={formatDateTime(run.started_at)}>{relativeTime(run.started_at, i18n.language)}</td>
                  <td>{formatDuration(run.duration_s)}</td>
                  <td>{t(`runs.trigger_${run.trigger}`, { defaultValue: run.trigger })}</td>
                  <td className="mono muted small">{run.run_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
