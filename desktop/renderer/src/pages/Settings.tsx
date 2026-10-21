import { CheckCircle2, FolderOpen, XCircle } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import type { Settings } from '../api/types';
import { Field, Toggle } from '../components/ui';
import { formatBytes } from '../lib/format';
import { errorMessage, useApp } from '../store/app';

export function SettingsPage() {
  const { t } = useTranslation();
  const settings = useApp((s) => s.settings);
  const engine = useApp((s) => s.engine);
  const interpreters = useApp((s) => s.interpreters);
  const loadInterpreters = useApp((s) => s.loadInterpreters);
  const toast = useApp((s) => s.toast);
  const bridge = window.opr;
  const [version, setVersion] = useState('');
  const [workspace, setWorkspace] = useState('');
  const [python, setPython] = useState('');
  const [check, setCheck] = useState<{ ok: boolean; version: string | null } | null>(null);

  useEffect(() => {
    void loadInterpreters().catch(() => undefined);
    void bridge?.appVersion().then(setVersion);
  }, [loadInterpreters, bridge]);
  useEffect(() => {
    setWorkspace(settings?.workspace ?? engine?.workspace ?? '');
    setPython(settings?.default_python ?? '');
  }, [settings?.workspace, settings?.default_python, engine?.workspace]);

  if (!settings) return null;

  const update = async (patch: Partial<Settings>) => {
    try {
      await api.putSettings(patch);
      await useApp.getState().refreshSettings();
    } catch (error) {
      toast('error', errorMessage(error));
    }
  };

  const applyWorkspace = async (path: string) => {
    if (!path.trim()) return;
    await update({ workspace: path.trim() });
  };

  return (
    <div className="page narrow">
      <header className="page-head">
        <h1>{t('nav.settings')}</h1>
      </header>

      <section className="panel">
        <h2>{t('settings.general')}</h2>
        <div className="grid-2">
          <Field label={t('settings.language')}>
            <select value={settings.language} onChange={(e) => void update({ language: e.target.value as Settings['language'] })}>
              <option value="auto">{t('settings.auto')}</option>
              <option value="es">Español</option>
              <option value="en">English</option>
            </select>
          </Field>
          <Field label={t('settings.theme')}>
            <select value={settings.theme} onChange={(e) => void update({ theme: e.target.value as Settings['theme'] })}>
              <option value="system">{t('settings.theme_system')}</option>
              <option value="light">{t('settings.theme_light')}</option>
              <option value="dark">{t('settings.theme_dark')}</option>
            </select>
          </Field>
        </div>
      </section>

      <section className="panel">
        <h2>{t('settings.workspace')}</h2>
        <p className="muted">{t('settings.workspace_hint')}</p>
        <div className="row">
          <input
            value={workspace}
            spellCheck={false}
            aria-label={t('settings.workspace')}
            onChange={(e) => setWorkspace(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && void applyWorkspace(workspace)}
          />
          {bridge && (
            <button
              onClick={async () => {
                const picked = await bridge.pickFolder();
                if (picked) {
                  setWorkspace(picked);
                  await applyWorkspace(picked);
                }
              }}
            >
              <FolderOpen size={15} /> {t('settings.browse')}
            </button>
          )}
          <button
            className="primary"
            disabled={workspace.trim() === (settings.workspace ?? engine?.workspace)}
            onClick={() => void applyWorkspace(workspace)}
          >
            {t('common.apply')}
          </button>
        </div>
      </section>

      <section className="panel">
        <h2>{t('settings.python')}</h2>
        <p className="muted">{t('settings.python_hint')}</p>
        <div className="row">
          <select
            value={interpreters.some((i) => i.path === python) ? python : python === '' ? '' : '__custom__'}
            aria-label={t('settings.python')}
            onChange={(e) => {
              const value = e.target.value;
              setCheck(null);
              if (value === '__custom__') return setPython(python || ' ');
              setPython(value);
              void update({ default_python: value || null });
            }}
          >
            <option value="">{t('settings.python_auto')}</option>
            <option value="embedded">{t('editor.python_embedded')}</option>
            {interpreters.map((i) => (
              <option key={i.path} value={i.path}>
                {i.version ? `Python ${i.version}` : 'Python'} — {i.path}
              </option>
            ))}
            <option value="__custom__">{t('editor.python_custom')}</option>
          </select>
        </div>
        {python !== '' && python !== 'embedded' && !interpreters.some((i) => i.path === python) && (
          <div className="row">
            <input
              value={python}
              spellCheck={false}
              placeholder="C:\Python311\python.exe"
              aria-label={t('editor.python_custom')}
              onChange={(e) => {
                setPython(e.target.value);
                setCheck(null);
              }}
            />
            <button
              onClick={async () => {
                const result = await api.checkInterpreter(python.trim());
                setCheck(result);
                if (result.ok) await update({ default_python: python.trim() });
              }}
            >
              {t('settings.check')}
            </button>
            {check &&
              (check.ok ? (
                <span className="ok-text">
                  <CheckCircle2 size={15} aria-hidden /> Python {check.version}
                </span>
              ) : (
                <span className="bad-text">
                  <XCircle size={15} aria-hidden /> {t('settings.not_python')}
                </span>
              ))}
          </div>
        )}
      </section>

      <section className="panel">
        <h2>{t('settings.data')}</h2>
        <div className="grid-2">
          <Field label={t('settings.retention')} hint={t('settings.retention_hint')}>
            <input
              type="number"
              min={1}
              max={3650}
              defaultValue={settings.retention_days}
              key={settings.retention_days}
              onBlur={(e) => {
                const value = Number(e.target.value);
                if (value >= 1 && value <= 3650 && value !== settings.retention_days) void update({ retention_days: value });
              }}
            />
          </Field>
          <Field label={t('settings.max_lines')} hint={t('settings.max_lines_hint')}>
            <input
              type="number"
              min={1000}
              step={1000}
              defaultValue={settings.max_lines_per_run}
              key={settings.max_lines_per_run}
              onBlur={(e) => {
                const value = Number(e.target.value);
                if (value >= 1000 && value !== settings.max_lines_per_run) void update({ max_lines_per_run: value });
              }}
            />
          </Field>
        </div>
        {engine && (
          <p className="muted">
            {t('settings.db_stats', {
              runs: engine.stats.runs_total,
              lines: engine.stats.log_lines.toLocaleString(),
              size: formatBytes(engine.stats.db_bytes),
            })}
          </p>
        )}
      </section>

      <section className="panel">
        <h2>{t('settings.desktop')}</h2>
        {bridge ? (
          <>
            <Toggle
              checked={settings.close_to_tray}
              label={t('settings.close_to_tray')}
              onChange={(value) => {
                void bridge.setCloseToTray(value);
                void update({ close_to_tray: value });
              }}
            />
            <Toggle
              checked={settings.start_with_windows}
              label={t('settings.start_with_windows')}
              onChange={(value) => {
                void bridge.setStartWithWindows(value);
                void update({ start_with_windows: value });
              }}
            />
          </>
        ) : (
          <p className="muted">{t('settings.desktop_only')}</p>
        )}
      </section>

      <p className="muted small about">
        OPR Studio {version && `v${version}`} · {t('settings.about')}
      </p>
    </div>
  );
}
