import { AlertTriangle, KeyRound, Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import type { SecretInfo } from '../api/types';
import { Link } from '../components/Link';
import { ConfirmDialog, Dialog, Empty, Field } from '../components/ui';
import { errorMessage, useApp } from '../store/app';

const NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;

export function SecretsPage() {
  const { t } = useTranslation();
  const secrets = useApp((s) => s.secrets);
  const toast = useApp((s) => s.toast);
  const [editing, setEditing] = useState<{ name: string; fixed: boolean } | null>(null);
  const [deleting, setDeleting] = useState<SecretInfo | null>(null);

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>{t('nav.secrets')}</h1>
          <p className="muted">{t('secrets.intro')}</p>
        </div>
        <button className="primary" onClick={() => setEditing({ name: '', fixed: false })}>
          <Plus size={16} /> {t('secrets.new')}
        </button>
      </header>

      <p className="notice">
        <KeyRound size={15} aria-hidden /> {t('secrets.how_to')} <code>{'${secret:NAME}'}</code>. {t('secrets.local_only')}
      </p>

      {secrets.length === 0 ? (
        <Empty title={t('secrets.empty_title')} hint={t('secrets.empty_hint')} />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{t('secrets.name')}</th>
                <th>{t('secrets.state')}</th>
                <th>{t('secrets.used_by')}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {secrets.map((secret) => (
                <tr key={secret.name}>
                  <td className="mono">{secret.name}</td>
                  <td>
                    {secret.exists ? (
                      <span className="badge s-success">{t('secrets.set')}</span>
                    ) : (
                      <span className="badge s-failed">
                        <AlertTriangle size={11} aria-hidden /> {t('secrets.missing')}
                      </span>
                    )}
                  </td>
                  <td>
                    {secret.used_by.length === 0 ? (
                      <span className="muted">{t('secrets.unused')}</span>
                    ) : (
                      secret.used_by.map((file) => (
                        <Link key={file} to={{ name: 'editor', file }} className="chip-link">
                          {file}
                        </Link>
                      ))
                    )}
                  </td>
                  <td className="actions">
                    <button onClick={() => setEditing({ name: secret.name, fixed: true })}>
                      {secret.exists ? t('secrets.replace') : t('secrets.set_value')}
                    </button>
                    {secret.exists && (
                      <button
                        className="icon-btn danger-hover"
                        aria-label={t('common.delete')}
                        onClick={() => setDeleting(secret)}
                      >
                        <Trash2 size={15} />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && (
        <SecretDialog
          initialName={editing.name}
          nameFixed={editing.fixed}
          onClose={() => setEditing(null)}
          onSave={async (name, value) => {
            try {
              await api.setSecret(name, value);
              toast('success', t('secrets.saved', { name }));
              await useApp.getState().refreshSecrets();
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
          title={t('secrets.delete_title')}
          message={t('secrets.delete_message', { name: deleting.name })}
          confirmLabel={t('common.delete')}
          onClose={() => setDeleting(null)}
          onConfirm={() =>
            void api.deleteSecret(deleting.name).catch((error: unknown) => toast('error', errorMessage(error)))
          }
        />
      )}
    </div>
  );
}

function SecretDialog({
  initialName,
  nameFixed,
  onSave,
  onClose,
}: {
  initialName: string;
  nameFixed: boolean;
  onSave: (name: string, value: string) => Promise<void>;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(initialName);
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const nameError = name && !NAME_PATTERN.test(name) ? t('secrets.invalid_name') : null;
  const ready = NAME_PATTERN.test(name) && value !== '';
  const submit = async () => {
    if (!ready || busy) return;
    setBusy(true);
    try {
      await onSave(name, value);
      onClose();
    } catch {
      /* the page already showed the error */
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={nameFixed ? t('secrets.set_title', { name }) : t('secrets.new')}
      onClose={onClose}
      actions={
        <>
          <button onClick={onClose}>{t('common.cancel')}</button>
          <button className="primary" disabled={!ready || busy} onClick={() => void submit()}>
            {t('common.save')}
          </button>
        </>
      }
    >
      <Field label={t('secrets.name')} error={nameError}>
        <input value={name} disabled={nameFixed} spellCheck={false} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label={t('secrets.value')} hint={t('secrets.value_hint')}>
        <input
          type="password"
          value={value}
          autoComplete="new-password"
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && void submit()}
        />
      </Field>
    </Dialog>
  );
}
