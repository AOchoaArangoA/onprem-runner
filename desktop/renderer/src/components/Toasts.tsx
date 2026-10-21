import { CheckCircle2, Info, X, XCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useApp } from '../store/app';

const ICONS = { info: Info, success: CheckCircle2, error: XCircle } as const;

export function Toasts() {
  const { t } = useTranslation();
  const toasts = useApp((s) => s.toasts);
  const dismiss = useApp((s) => s.dismissToast);
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((toast) => {
        const Icon = ICONS[toast.kind];
        return (
          <div key={toast.id} className={`toast ${toast.kind}`}>
            <Icon size={16} aria-hidden />
            <span>{toast.message}</span>
            <button className="icon-btn" onClick={() => dismiss(toast.id)} aria-label={t('common.close')}>
              <X size={14} />
            </button>
          </div>
        );
      })}
    </div>
  );
}
