export type StepStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'failed'
  | 'timeout'
  | 'skipped'
  | 'cancelled';
export type RunStatus = 'running' | 'success' | 'failed' | 'cancelled';
export type EdgeWhen = 'success' | 'failure' | 'always';
export type Frequency = 'minutes' | 'hourly' | 'daily' | 'weekly' | 'startup';
export type Weekday = 'MON' | 'TUE' | 'WED' | 'THU' | 'FRI' | 'SAT' | 'SUN';

export interface StepDef {
  id: string;
  type: string;
  params: Record<string, unknown>;
  timeout: number | null;
  retries: number;
  retry_delay: number;
  ignore_failure: boolean;
}

export interface EdgeDef {
  id: string;
  source: string;
  target: string;
  when: EdgeWhen;
}

export interface Schedule {
  frequency: Frequency;
  at: string | null;
  every: number;
  days: Weekday[];
}

export type Position = [number, number];

export interface Graph {
  file: string;
  etag: string;
  name: string;
  fail_fast: boolean;
  max_parallel: number;
  steps: StepDef[];
  edges: EdgeDef[];
  positions: Record<string, Position>;
  auto_layout: boolean;
  schedule: Schedule | null;
  notifiers: Record<string, unknown>[];
}

export interface RunSummary {
  run_id: string;
  pipeline_name: string;
  status: RunStatus;
  trigger: string;
  started_at: string;
  finished_at: string | null;
  duration_s: number | null;
  file: string | null;
}

export interface StepRun {
  run_id: string;
  step_id: string;
  type: string;
  status: StepStatus;
  exit_code: number | null;
  attempts: number;
  ignored: boolean;
  started_at: string | null;
  finished_at: string | null;
  duration_s: number | null;
}

export interface RunDetail extends RunSummary {
  steps: StepRun[];
}

export interface PipelineSummary {
  file: string;
  name: string;
  steps: number;
  schedule: Schedule | null;
  error: string | null;
  last_run: RunSummary | null;
  running_run_id: string | null;
  secrets_missing: string[];
}

export interface LogRow {
  run_id: string;
  step_id: string;
  seq: number;
  ts: string;
  stream: 'stdout' | 'stderr';
  level: string | null;
  message: string;
  fields: Record<string, unknown> | null;
}

export interface ActiveRun {
  run_id: string;
  file: string;
  pipeline: string;
  trigger: string;
  started_at: string;
  steps: Record<string, StepStatus>;
}

export interface SecretInfo {
  name: string;
  exists: boolean;
  used_by: string[];
}

export interface Settings {
  language: 'auto' | 'es' | 'en';
  theme: 'system' | 'light' | 'dark';
  workspace: string | null;
  default_python: string | null;
  retention_days: number;
  max_lines_per_run: number;
  close_to_tray: boolean;
  start_with_windows: boolean;
}

export interface Interpreter {
  path: string;
  version: string | null;
  source: string;
}

export interface Stats {
  runs_total: number;
  runs_24h: number;
  running: number;
  success_rate: number | null;
  log_lines: number;
  db_bytes: number;
}

export interface EngineState {
  workspace: string;
  settings: Settings;
  stats: Stats;
  active: ActiveRun[];
}

export type EngineEvent =
  | { type: 'hello' }
  | { type: 'resync' }
  | ({ type: 'run_started' } & ActiveRun)
  | {
      type: 'step';
      run_id: string;
      file: string;
      step_id: string;
      status: StepStatus;
      exit_code: number | null;
      duration: number | null;
    }
  | ({ type: 'log' } & LogRow)
  | { type: 'run_finished'; run_id: string; file: string; status: RunStatus }
  | { type: 'pipelines_changed' }
  | { type: 'settings_changed' }
  | { type: 'secrets_changed' };

export interface LogFilters {
  step?: string;
  level?: string[];
  stream?: string;
  q?: string;
}
