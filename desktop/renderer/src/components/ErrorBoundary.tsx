import { Component, type ErrorInfo, type ReactNode } from 'react';
import i18n from '../i18n';

interface State {
  error: Error | null;
}

/** A render error in one page must not leave the whole window blank. */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('UI error', error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="fatal" role="alert">
        <div className="empty">
          <div className="empty-title">{i18n.t('errors.title')}</div>
          <div className="muted">{i18n.t('errors.hint')}</div>
          <pre className="server-error">{this.state.error.message}</pre>
          <button className="primary" onClick={() => window.location.reload()}>
            {i18n.t('errors.reload')}
          </button>
        </div>
      </div>
    );
  }
}
