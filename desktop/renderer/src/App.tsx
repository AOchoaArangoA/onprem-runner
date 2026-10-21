import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Sidebar } from './components/Sidebar';
import { Toasts } from './components/Toasts';
import { Dialog, Empty, Spinner } from './components/ui';
import { resolveLanguage } from './i18n';
import { go, useGuard } from './lib/guard';
import { navigate, useRoute } from './lib/router';
import { EditorPage } from './pages/Editor';
import { PipelinesPage } from './pages/Pipelines';
import { RunDetailPage } from './pages/RunDetail';
import { RunsPage } from './pages/Runs';
import { SecretsPage } from './pages/Secrets';
import { SettingsPage } from './pages/Settings';
import { useApp } from './store/app';
import { useEditor } from './store/editor';

export default function App() {
  const { t, i18n } = useTranslation();
  const route = useRoute();
  const { init, loaded, fatal, settings } = useApp();
  const pending = useGuard((s) => s.pending);
  const clearGuard = useGuard((s) => s.clear);

  useEffect(() => init(), [init]);

  useEffect(() => {
    if (!settings) return;
    const language = resolveLanguage(settings.language);
    if (i18n.language !== language) void i18n.changeLanguage(language);
    document.documentElement.lang = language;
    if (settings.theme === 'system') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = settings.theme;
  }, [settings, i18n]);

  if (fatal) {
    return (
      <div className="fatal">
        <Empty title={t(`fatal.${fatal}.title`)} hint={t(`fatal.${fatal}.hint`)} />
      </div>
    );
  }
  if (!loaded) {
    return (
      <div className="fatal">
        <Spinner label={t('common.loading')} />
      </div>
    );
  }

  const page = (() => {
    switch (route.name) {
      case 'editor':
        return <EditorPage key={route.file} file={route.file} />;
      case 'runs':
        return <RunsPage />;
      case 'run':
        return <RunDetailPage key={route.id} id={route.id} />;
      case 'secrets':
        return <SecretsPage />;
      case 'settings':
        return <SettingsPage />;
      default:
        return <PipelinesPage />;
    }
  })();

  return (
    <div className="app">
      <Sidebar current={route} />
      <main className={route.name === 'editor' ? 'fill' : ''}>{page}</main>
      <Toasts />
      {pending && (
        <Dialog
          title={t('guard.title')}
          onClose={clearGuard}
          actions={
            <>
              <button onClick={clearGuard}>{t('common.cancel')}</button>
              <button
                onClick={() => {
                  const target = pending;
                  clearGuard();
                  useEditor.getState().close();
                  navigate(target);
                }}
              >
                {t('guard.discard')}
              </button>
              <button
                className="primary"
                onClick={async () => {
                  const target = pending;
                  if (await useEditor.getState().save()) {
                    clearGuard();
                    go(target);
                  } else {
                    clearGuard();
                  }
                }}
              >
                {t('guard.save_and_leave')}
              </button>
            </>
          }
        >
          <p>{t('guard.message')}</p>
        </Dialog>
      )}
    </div>
  );
}
