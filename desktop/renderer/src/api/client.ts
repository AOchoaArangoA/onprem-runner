import type {
  EngineState,
  Graph,
  Interpreter,
  LogFilters,
  LogRow,
  PipelineSummary,
  RunDetail,
  RunSummary,
  SecretInfo,
  Settings,
} from './types';

const TOKEN_KEY = 'opr-token';

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

let token = '';

/** The engine hands the token to the page once, in the URL; keep it for the session only. */
export function initToken(): string {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get('token');
  if (fromUrl) {
    token = fromUrl;
    try {
      sessionStorage.setItem(TOKEN_KEY, fromUrl);
    } catch {
      /* storage may be unavailable; the token stays in memory */
    }
    params.delete('token');
    const query = params.toString();
    window.history.replaceState(
      null,
      '',
      window.location.pathname + (query ? `?${query}` : '') + window.location.hash,
    );
  } else {
    try {
      token = sessionStorage.getItem(TOKEN_KEY) ?? '';
    } catch {
      token = '';
    }
  }
  return token;
}

export function setToken(value: string): void {
  token = value;
}

export function hasToken(): boolean {
  return token !== '';
}

function queryString(params: Record<string, string | number | string[] | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === '') continue;
    if (Array.isArray(value)) {
      if (value.length) search.set(key, value.join(','));
    } else {
      search.set(key, String(value));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (cause) {
    throw new ApiError(0, cause instanceof Error ? cause.message : 'network error');
  }
  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!response.ok) {
    const message =
      data && typeof data === 'object' && 'error' in data
        ? String((data as { error: unknown }).error)
        : `HTTP ${response.status}`;
    throw new ApiError(response.status, message);
  }
  return data as T;
}

const file = (name: string) => `?file=${encodeURIComponent(name)}`;
const filterParams = (f: LogFilters) => ({
  step: f.step,
  level: f.level,
  stream: f.stream,
  q: f.q,
});

export const api = {
  state: () => request<EngineState>('GET', '/api/state'),
  pipelines: () => request<PipelineSummary[]>('GET', '/api/pipelines'),
  createPipeline: (name: string) =>
    request<{ file: string }>('POST', '/api/pipelines', { name }),
  graph: (name: string) => request<Graph>('GET', `/api/pipeline${file(name)}`),
  saveGraph: (name: string, graph: Graph, etag: string | null, force = false) =>
    request<{ etag: string }>('PUT', `/api/pipeline${file(name)}`, { graph, etag, force }),
  validate: (name: string, graph: Graph) =>
    request<{ ok: boolean; error: string | null }>('POST', `/api/pipeline/validate${file(name)}`, {
      graph,
    }),
  deletePipeline: (name: string) => request<{ ok: boolean }>('DELETE', `/api/pipeline${file(name)}`),
  run: (name: string, only?: string[]) =>
    request<{ run_id: string }>('POST', `/api/pipeline/run${file(name)}`, only ? { only } : {}),
  cancel: (runId: string) => request<{ ok: boolean }>('POST', `/api/runs/${runId}/cancel`),
  runs: (opts: { file?: string; status?: string; limit?: number; offset?: number } = {}) =>
    request<RunSummary[]>('GET', `/api/runs${queryString(opts)}`),
  runDetail: (runId: string) => request<RunDetail>('GET', `/api/runs/${runId}`),
  logs: (runId: string, filters: LogFilters = {}, after = 0, limit = 1000) =>
    request<LogRow[]>(
      'GET',
      `/api/runs/${runId}/logs${queryString({ ...filterParams(filters), after, limit })}`,
    ),
  exportUrl: (runId: string, format: 'txt' | 'csv' | 'jsonl', filters: LogFilters = {}) =>
    `/api/runs/${runId}/export${queryString({ ...filterParams(filters), format, token })}`,
  secrets: () => request<SecretInfo[]>('GET', '/api/secrets'),
  setSecret: (name: string, value: string) =>
    request<{ ok: boolean }>('PUT', `/api/secrets/${encodeURIComponent(name)}`, { value }),
  deleteSecret: (name: string) =>
    request<{ ok: boolean }>('DELETE', `/api/secrets/${encodeURIComponent(name)}`),
  settings: () => request<Settings>('GET', '/api/settings'),
  putSettings: (patch: Partial<Settings>) => request<Settings>('PUT', '/api/settings', patch),
  interpreters: () => request<Interpreter[]>('GET', '/api/interpreters'),
  checkInterpreter: (path: string) =>
    request<{ path: string; ok: boolean; version: string | null }>(
      'POST',
      '/api/interpreters/check',
      { path },
    ),
};

export const eventsUrl = () => `/api/events?token=${encodeURIComponent(token)}`;
