import type { EdgeDef, EdgeWhen, Graph, Position, StepDef } from '../api/types';

export const STEP_TYPES = ['python', 'shell'] as const;
export const ID_PATTERN = /^[A-Za-z0-9_.-]+$/;
export const LAYER_WIDTH = 300;
export const ROW_HEIGHT = 130;

export const NODE_WIDTH = 224;
export const NODE_HEIGHT = 96;
const GAP = 24;

/** Nearest free spot to `desired`: rows below, then above, then further right. */
export function freePosition(graph: Graph, desired: Position): Position {
  const taken = Object.values(graph.positions);
  const overlaps = (x: number, y: number) =>
    taken.some(([px, py]) => Math.abs(px - x) < NODE_WIDTH + GAP && Math.abs(py - y) < NODE_HEIGHT + GAP);
  const [x, y] = desired;
  if (!overlaps(x, y)) return desired;
  const stepY = NODE_HEIGHT + GAP;
  const stepX = NODE_WIDTH + GAP * 2;
  for (let column = 0; column < 40; column += 1) {
    for (let row = 1; row < 40; row += 1) {
      for (const dy of [row * stepY, -row * stepY]) {
        const candidate: Position = [x + column * stepX, y + dy];
        if (!overlaps(candidate[0], candidate[1])) return candidate;
      }
    }
  }
  return desired;
}

export const edgeId = (source: string, target: string) => `${source}->${target}`;

export function defaultParams(type: string): Record<string, unknown> {
  if (type === 'python') return { script: 'scripts/new_script.py' };
  if (type === 'shell') return { command: '' };
  return {};
}

export function uniqueId(graph: Graph, base: string): string {
  const taken = new Set(graph.steps.map((s) => s.id));
  if (!taken.has(base)) return base;
  let n = 1;
  while (taken.has(`${base}_${n}`)) n += 1;
  return `${base}_${n}`;
}

export function addStep(graph: Graph, type: string, position: Position): { graph: Graph; id: string } {
  const id = uniqueId(graph, type);
  const step: StepDef = {
    id,
    type,
    params: defaultParams(type),
    timeout: null,
    retries: 0,
    retry_delay: 0,
    ignore_failure: false,
  };
  return {
    id,
    graph: {
      ...graph,
      steps: [...graph.steps, step],
      positions: { ...graph.positions, [id]: position },
    },
  };
}

export function removeStep(graph: Graph, id: string): Graph {
  const positions = { ...graph.positions };
  delete positions[id];
  return {
    ...graph,
    steps: graph.steps.filter((s) => s.id !== id),
    edges: graph.edges.filter((e) => e.source !== id && e.target !== id),
    positions,
  };
}

export type RenameError = 'empty' | 'invalid' | 'duplicate';

export function renameStep(
  graph: Graph,
  oldId: string,
  newId: string,
): { graph: Graph } | { error: RenameError } {
  if (newId === oldId) return { graph };
  if (!newId) return { error: 'empty' };
  if (!ID_PATTERN.test(newId)) return { error: 'invalid' };
  if (graph.steps.some((s) => s.id === newId)) return { error: 'duplicate' };
  const positions: Graph['positions'] = {};
  for (const [id, pos] of Object.entries(graph.positions)) positions[id === oldId ? newId : id] = pos;
  const edges = graph.edges.map((e) => {
    const source = e.source === oldId ? newId : e.source;
    const target = e.target === oldId ? newId : e.target;
    return { ...e, source, target, id: edgeId(source, target) };
  });
  return {
    graph: {
      ...graph,
      steps: graph.steps.map((s) => (s.id === oldId ? { ...s, id: newId } : s)),
      edges,
      positions,
    },
  };
}

export function updateStep(graph: Graph, id: string, patch: Partial<StepDef>): Graph {
  return { ...graph, steps: graph.steps.map((s) => (s.id === id ? { ...s, ...patch } : s)) };
}

export function setPosition(graph: Graph, id: string, position: Position): Graph {
  return { ...graph, positions: { ...graph.positions, [id]: position } };
}

/** True if adding source -> target would make the dependency graph circular. */
export function wouldCreateCycle(graph: Graph, source: string, target: string): boolean {
  if (source === target) return true;
  // A cycle appears if `source` is reachable from `target` following existing edges.
  const next = new Map<string, string[]>();
  for (const e of graph.edges) next.set(e.source, [...(next.get(e.source) ?? []), e.target]);
  const seen = new Set<string>();
  const stack = [target];
  while (stack.length) {
    const current = stack.pop() as string;
    if (current === source) return true;
    if (seen.has(current)) continue;
    seen.add(current);
    stack.push(...(next.get(current) ?? []));
  }
  return false;
}

export type ConnectError = 'self' | 'duplicate' | 'cycle' | 'unknown';

export function connect(
  graph: Graph,
  source: string,
  target: string,
  when: EdgeWhen = 'success',
): { graph: Graph } | { error: ConnectError } {
  const ids = new Set(graph.steps.map((s) => s.id));
  if (!ids.has(source) || !ids.has(target)) return { error: 'unknown' };
  if (source === target) return { error: 'self' };
  if (graph.edges.some((e) => e.source === source && e.target === target)) return { error: 'duplicate' };
  if (wouldCreateCycle(graph, source, target)) return { error: 'cycle' };
  const edge: EdgeDef = { id: edgeId(source, target), source, target, when };
  return { graph: { ...graph, edges: [...graph.edges, edge] } };
}

export function removeEdge(graph: Graph, id: string): Graph {
  return { ...graph, edges: graph.edges.filter((e) => e.id !== id) };
}

export function setEdgeWhen(graph: Graph, id: string, when: EdgeWhen): Graph {
  return { ...graph, edges: graph.edges.map((e) => (e.id === id ? { ...e, when } : e)) };
}

/** Layered layout: each step sits one column right of its deepest dependency. */
export function autoLayout(graph: Graph): Graph {
  const incoming = new Map<string, string[]>();
  for (const e of graph.edges) incoming.set(e.target, [...(incoming.get(e.target) ?? []), e.source]);
  const depth = new Map<string, number>();
  const visit = (id: string, trail: Set<string>): number => {
    const known = depth.get(id);
    if (known !== undefined) return known;
    if (trail.has(id)) return 0; // defensive: cycles are rejected elsewhere
    trail.add(id);
    const value = 1 + Math.max(-1, ...(incoming.get(id) ?? []).map((p) => visit(p, trail)));
    trail.delete(id);
    depth.set(id, value);
    return value;
  };
  const rows = new Map<number, number>();
  const positions: Graph['positions'] = {};
  for (const step of graph.steps) {
    const column = visit(step.id, new Set());
    const row = rows.get(column) ?? 0;
    rows.set(column, row + 1);
    positions[step.id] = [column * LAYER_WIDTH, row * ROW_HEIGHT];
  }
  return { ...graph, positions, auto_layout: false };
}

export type IssueKey =
  | 'no_steps'
  | 'empty_id'
  | 'bad_id'
  | 'duplicate_id'
  | 'missing_script'
  | 'missing_command'
  | 'empty_name';

export interface Issue {
  key: IssueKey;
  stepId?: string;
}

/** Cheap checks done while editing; the engine runs the authoritative validation. */
export function validateClient(graph: Graph): Issue[] {
  const issues: Issue[] = [];
  if (!graph.name.trim()) issues.push({ key: 'empty_name' });
  if (graph.steps.length === 0) issues.push({ key: 'no_steps' });
  const seen = new Set<string>();
  for (const step of graph.steps) {
    if (!step.id) issues.push({ key: 'empty_id' });
    else if (!ID_PATTERN.test(step.id)) issues.push({ key: 'bad_id', stepId: step.id });
    if (seen.has(step.id)) issues.push({ key: 'duplicate_id', stepId: step.id });
    seen.add(step.id);
    if (step.type === 'python' && !String(step.params.script ?? '').trim())
      issues.push({ key: 'missing_script', stepId: step.id });
    if (step.type === 'shell' && !String(step.params.command ?? '').trim())
      issues.push({ key: 'missing_command', stepId: step.id });
  }
  return issues;
}

/** What identifies the document for "unsaved changes"; ignores etag and layout bookkeeping. */
export function comparable(graph: Graph): string {
  const { etag: _etag, auto_layout: _auto, ...rest } = graph;
  return JSON.stringify(rest);
}

export const isDirty = (current: Graph, saved: Graph) => comparable(current) !== comparable(saved);

export function stepSummary(step: StepDef): string {
  const value = step.type === 'python' ? step.params.script : step.params.command;
  return typeof value === 'string' ? value : '';
}

/** Steps that can run once `id` is selected for "run from here": it and everything after it. */
export function downstream(graph: Graph, id: string): string[] {
  const next = new Map<string, string[]>();
  for (const e of graph.edges) next.set(e.source, [...(next.get(e.source) ?? []), e.target]);
  const seen = new Set<string>([id]);
  const stack = [id];
  while (stack.length) {
    for (const child of next.get(stack.pop() as string) ?? []) {
      if (!seen.has(child)) {
        seen.add(child);
        stack.push(child);
      }
    }
  }
  return graph.steps.map((s) => s.id).filter((s) => seen.has(s));
}
