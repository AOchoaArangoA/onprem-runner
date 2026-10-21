import { History, KeyRound, Settings, Workflow } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useApp } from '../store/app';
import type { Route } from '../lib/router';
import { Link } from './Link';

const ITEMS: { route: Route; match: Route['name'][]; icon: typeof Workflow; label: string }[] = [
  { route: { name: 'pipelines' }, match: ['pipelines', 'editor'], icon: Workflow, label: 'nav.pipelines' },
  { route: { name: 'runs' }, match: ['runs', 'run'], icon: History, label: 'nav.runs' },
  { route: { name: 'secrets' }, match: ['secrets'], icon: KeyRound, label: 'nav.secrets' },
  { route: { name: 'settings' }, match: ['settings'], icon: Settings, label: 'nav.settings' },
];

export function Sidebar({ current }: { current: Route }) {
  const { t } = useTranslation();
  const connection = useApp((s) => s.connection);
  const running = useApp((s) => Object.keys(s.active).length);
  const missing = useApp((s) => s.secrets.filter((x) => !x.exists).length);

  return (
    <nav className="sidebar" aria-label={t('nav.main')}>
      <div className="brand">
        <svg width="26" height="26" viewBox="0 0 32 32" aria-hidden>
          <rect width="32" height="32" rx="8" fill="var(--accent)" />
          <circle cx="9" cy="16" r="3" fill="#fff" />
          <circle cx="23" cy="9" r="3" fill="#fff" opacity=".85" />
          <circle cx="23" cy="23" r="3" fill="#fff" opacity=".85" />
          <path d="M12 16h4c2 0 3-1 4-5M12 16h4c2 0 3 1 4 5" stroke="#fff" strokeWidth="1.8" fill="none" strokeLinecap="round" />
        </svg>
        <span>OPR Studio</span>
      </div>
      <ul>
        {ITEMS.map(({ route, match, icon: Icon, label }) => {
          const active = match.includes(current.name);
          const badge =
            route.name === 'pipelines' ? running : route.name === 'secrets' ? missing : 0;
          return (
            <li key={route.name}>
              <Link to={route} className={`nav-item${active ? ' active' : ''}`}>
                <Icon size={18} aria-hidden />
                <span>{t(label)}</span>
                {badge > 0 && (
                  <span className={`nav-badge${route.name === 'secrets' ? ' warn' : ''}`} aria-label={String(badge)}>
                    {badge}
                  </span>
                )}
              </Link>
            </li>
          );
        })}
      </ul>
      <div className="side-foot">
        <span className={`conn conn-${connection}`} aria-hidden />
        <span className="muted small">{t(`connection.${connection}`)}</span>
      </div>
    </nav>
  );
}
