import { create } from 'zustand';
import { api, ApiError, hasToken, initToken } from '../api/client';
import { connectEvents, type ConnectionState } from '../api/events';
import type {
  ActiveRun,
  EngineEvent,
  EngineState,
  Interpreter,
  LogRow,
  PipelineSummary,
  RunSummary,
  SecretInfo,
  Settings,
} from '../api/types';

export const MAX_LIVE_LINES = 5000;

export interface Toast {
  id: number;
  kind: 'info' | 'success' | 'error';
  message: string;
}

interface AppState {
  connection: ConnectionState;
  loaded: boolean;
  fatal: string | null;
  engine: EngineState | null;
  pipelines: PipelineSummary[];
  runs: RunSummary[];
  active: Record<string, ActiveRun>;
  liveLogs: Record<string, LogRow[]>;
  secrets: SecretInfo[];
  settings: Settings | null;
  interpreters: Interpreter[];
  toasts: Toast[];

  init: () => void;
  refreshAll: () => Promise<void>;
  refreshEngine: () => Promise<void>;
  refreshPipelines: () => Promise<void>;
  refreshRuns: () => Promise<void>;
  refreshSecrets: () => Promise<void>;
  refreshSettings: () => Promise<void>;
  loadInterpreters: () => Promise<void>;
  applyEvent: (event: EngineEvent) => void;
  toast: (kind: Toast['kind'], message: string) => void;
  dismissToast: (id: number) => void;
}

let toastId = 0;
let started = false;
let pendingLogs: LogRow[] = [];
let frame = 0;

function debounce<A extends unknown[]>(fn: (...args: A) => void, ms: number) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  return (...args: A) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

export const useApp = create<AppState>((set, get) => {
  const refreshPipelinesSoon = debounce(() => void get().refreshPipelines(), 150);
  const refreshRunsSoon = debounce(() => void get().refreshRuns(), 300);

  const flushLogs = () => {
    frame = 0;
    const batch = pendingLogs;
    pendingLogs = [];
    if (batch.length === 0) return;
    set((state) => {
      const next = { ...state.liveLogs };
      for (const row of batch) {
        const rows = next[row.run_id] ?? [];
        next[row.run_id] = rows.length >= MAX_LIVE_LINES ? [...rows.slice(rows.length - MAX_LIVE_LINES + 1), row] : [...rows, row];
      }
      return { liveLogs: next };
    });
  };

  return {
    connection: 'connecting',
    loaded: false,
    fatal: null,
    engine: null,
    pipelines: [],
    runs: [],
    active: {},
    liveLogs: {},
    secrets: [],
    settings: null,
    interpreters: [],
    toasts: [],

    init: () => {
      if (started) return;
      started = true;
      initToken();
      if (!hasToken()) {
        set({ fatal: 'missing_token' });
        return;
      }
      connectEvents(
        (event) => get().applyEvent(event),
        (connection) => set({ connection }),
      );
      void get()
        .refreshAll()
        .then(() => set({ loaded: true }))
        .catch((error: unknown) => {
          set({ fatal: error instanceof ApiError && error.status === 401 ? 'unauthorized' : 'unreachable' });
        });
    },

    refreshAll: async () => {
      const [engine, pipelines, runs, secrets, settings] = await Promise.all([
        api.state(),
        api.pipelines(),
        api.runs({ limit: 100 }),
        api.secrets(),
        api.settings(),
      ]);
      const active: Record<string, ActiveRun> = {};
      for (const run of engine.active) active[run.run_id] = run;
      set({ engine, pipelines, runs, secrets, settings, active });
    },

    refreshEngine: async () => set({ engine: await api.state() }),
    refreshPipelines: async () => {
      try {
        set({ pipelines: await api.pipelines() });
      } catch {
        /* the connection indicator already tells the user */
      }
    },
    refreshRuns: async () => {
      try {
        set({ runs: await api.runs({ limit: 100 }) });
      } catch {
        /* see above */
      }
    },
    refreshSecrets: async () => set({ secrets: await api.secrets() }),
    refreshSettings: async () => set({ settings: await api.settings() }),
    loadInterpreters: async () => set({ interpreters: await api.interpreters() }),

    applyEvent: (event) => {
      switch (event.type) {
        case 'run_started': {
          const { type: _type, ...run } = event;
          set((s) => ({ active: { ...s.active, [run.run_id]: run }, liveLogs: { ...s.liveLogs, [run.run_id]: [] } }));
          refreshPipelinesSoon();
          break;
        }
        case 'step':
          set((s) => {
            const run = s.active[event.run_id];
            if (!run) return s;
            return {
              active: {
                ...s.active,
                [event.run_id]: { ...run, steps: { ...run.steps, [event.step_id]: event.status } },
              },
            };
          });
          break;
        case 'log':
          pendingLogs.push(event);
          if (!frame) frame = requestAnimationFrame(flushLogs);
          break;
        case 'run_finished':
          flushLogs();
          set((s) => {
            const { [event.run_id]: _done, ...rest } = s.active;
            return { active: rest };
          });
          refreshPipelinesSoon();
          refreshRunsSoon();
          void get().refreshEngine();
          break;
        case 'pipelines_changed':
          refreshPipelinesSoon();
          break;
        case 'settings_changed':
          void get().refreshSettings();
          void get().refreshEngine();
          break;
        case 'secrets_changed':
          void get().refreshSecrets();
          refreshPipelinesSoon();
          break;
        case 'resync':
          void get().refreshAll();
          break;
        default:
          break;
      }
    },

    toast: (kind, message) => {
      const id = ++toastId;
      set((s) => ({ toasts: [...s.toasts, { id, kind, message }] }));
      setTimeout(() => get().dismissToast(id), kind === 'error' ? 8000 : 4000);
    },
    dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
  };
});

/** Turn any thrown value into a message fit for a toast. */
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
