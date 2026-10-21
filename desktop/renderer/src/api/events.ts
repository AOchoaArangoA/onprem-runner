import { eventsUrl } from './client';
import type { EngineEvent } from './types';

export type ConnectionState = 'connecting' | 'online' | 'offline';

/**
 * Subscribe to the engine's live events. EventSource reconnects by itself; after any gap we
 * emit `resync` so the UI reloads what it may have missed.
 */
export function connectEvents(
  onEvent: (event: EngineEvent) => void,
  onState: (state: ConnectionState) => void,
): () => void {
  let source: EventSource | null = null;
  let hadError = false;
  let closed = false;

  const open = () => {
    if (closed) return;
    onState('connecting');
    source = new EventSource(eventsUrl());
    source.onopen = () => {
      onState('online');
      if (hadError) {
        hadError = false;
        onEvent({ type: 'resync' });
      }
    };
    source.onmessage = (message) => {
      try {
        onEvent(JSON.parse(message.data) as EngineEvent);
      } catch {
        /* ignore a malformed event */
      }
    };
    source.onerror = () => {
      hadError = true;
      onState('offline');
      if (source && source.readyState === EventSource.CLOSED) {
        // The browser gave up (for example a 401); retry ourselves with a delay.
        source.close();
        setTimeout(open, 2000);
      }
    };
  };
  open();
  return () => {
    closed = true;
    source?.close();
  };
}
