import { create } from 'zustand';
import { api, ApiError } from '../api/client';
import type { EdgeWhen, Graph, Position, StepDef } from '../api/types';
import {
  addStep,
  autoLayout,
  connect,
  freePosition,
  isDirty,
  removeEdge,
  removeStep,
  renameStep,
  setEdgeWhen,
  setPosition,
  updateStep,
  validateClient,
  type ConnectError,
  type Issue,
  type RenameError,
} from '../lib/graphModel';

const HISTORY_LIMIT = 100;
const COALESCE_MS = 900;

export type Selection = { kind: 'node' | 'edge'; id: string } | null;

interface EditorState {
  file: string | null;
  saved: Graph | null;
  graph: Graph | null;
  past: Graph[];
  future: Graph[];
  selection: Selection;
  loading: boolean;
  loadError: string | null;
  saving: boolean;
  /** The file changed on disk while there were unsaved edits. */
  conflict: boolean;
  /** Authoritative validation result from the engine for the current draft. */
  serverError: string | null;
  issues: Issue[];

  load: (file: string) => Promise<void>;
  reload: () => Promise<void>;
  checkExternalChange: () => Promise<'unchanged' | 'reloaded' | 'conflict'>;
  close: () => void;
  select: (selection: Selection) => void;
  commit: (next: Graph, coalesceKey?: string) => void;
  undo: () => void;
  redo: () => void;
  isDirty: () => boolean;

  addStep: (type: string, position: Position) => string | null;
  deleteSelection: () => void;
  removeStepById: (id: string) => void;
  renameStep: (oldId: string, newId: string) => RenameError | null;
  patchStep: (id: string, patch: Partial<StepDef>, coalesceKey?: string) => void;
  moveNodes: (moves: Record<string, Position>) => void;
  connect: (source: string, target: string, when?: EdgeWhen) => ConnectError | null;
  setEdgeWhen: (id: string, when: EdgeWhen) => void;
  removeEdgeById: (id: string) => void;
  patchMeta: (patch: Partial<Graph>, coalesceKey?: string) => void;
  autoLayout: () => void;

  save: (force?: boolean) => Promise<boolean>;
}

let lastKey = '';
let lastAt = 0;
let validationTimer: ReturnType<typeof setTimeout> | undefined;
let validationSeq = 0;

export const useEditor = create<EditorState>((set, get) => {
  const scheduleValidation = () => {
    clearTimeout(validationTimer);
    const { graph, file } = get();
    if (!graph || !file) return;
    const issues = validateClient(graph);
    set({ issues });
    const seq = ++validationSeq;
    validationTimer = setTimeout(async () => {
      try {
        const verdict = await api.validate(file, graph);
        if (seq === validationSeq) set({ serverError: verdict.ok ? null : verdict.error });
      } catch {
        /* offline: the next edit retries */
      }
    }, 500);
  };

  const apply = (next: Graph, record: boolean, key?: string) => {
    const { graph, past } = get();
    if (!graph) return;
    let nextPast = past;
    if (record) {
      const now = Date.now();
      const coalesce = key !== undefined && key === lastKey && now - lastAt < COALESCE_MS;
      if (!coalesce) nextPast = [...past, graph].slice(-HISTORY_LIMIT);
      lastKey = key ?? '';
      lastAt = now;
    }
    set({ graph: next, past: nextPast, future: record ? [] : get().future });
    scheduleValidation();
  };

  const adopt = (graph: Graph) => {
    set({
      saved: graph,
      graph,
      past: [],
      future: [],
      selection: null,
      conflict: false,
      serverError: null,
      loadError: null,
      issues: validateClient(graph),
    });
    lastKey = '';
    scheduleValidation();
  };

  return {
    file: null,
    saved: null,
    graph: null,
    past: [],
    future: [],
    selection: null,
    loading: false,
    loadError: null,
    saving: false,
    conflict: false,
    serverError: null,
    issues: [],

    load: async (file) => {
      set({ file, loading: true, loadError: null, saved: null, graph: null });
      try {
        const graph = await api.graph(file);
        if (get().file !== file) return; // the user already moved on
        adopt(graph);
      } catch (error) {
        set({ loadError: error instanceof Error ? error.message : String(error) });
      } finally {
        set({ loading: false });
      }
    },

    reload: async () => {
      const file = get().file;
      if (file) await get().load(file);
    },

    checkExternalChange: async () => {
      const { file, saved } = get();
      if (!file || !saved) return 'unchanged';
      try {
        const fresh = await api.graph(file);
        if (fresh.etag === saved.etag) return 'unchanged';
        if (!get().isDirty()) {
          adopt(fresh);
          return 'reloaded';
        }
        set({ conflict: true });
        return 'conflict';
      } catch {
        return 'unchanged';
      }
    },

    close: () => {
      clearTimeout(validationTimer);
      set({ file: null, saved: null, graph: null, past: [], future: [], selection: null, conflict: false });
    },

    select: (selection) => set({ selection }),
    commit: (next, key) => apply(next, true, key),

    undo: () => {
      const { past, graph, future } = get();
      const previous = past.at(-1);
      if (!previous || !graph) return;
      set({ graph: previous, past: past.slice(0, -1), future: [graph, ...future] });
      lastKey = '';
      scheduleValidation();
    },
    redo: () => {
      const { past, graph, future } = get();
      const next = future[0];
      if (!next || !graph) return;
      set({ graph: next, past: [...past, graph], future: future.slice(1) });
      lastKey = '';
      scheduleValidation();
    },
    isDirty: () => {
      const { graph, saved } = get();
      return graph !== null && saved !== null && isDirty(graph, saved);
    },

    addStep: (type, position) => {
      const graph = get().graph;
      if (!graph) return null;
      const result = addStep(graph, type, freePosition(graph, position));
      apply(result.graph, true);
      set({ selection: { kind: 'node', id: result.id } });
      return result.id;
    },
    removeStepById: (id) => {
      const graph = get().graph;
      if (!graph) return;
      apply(removeStep(graph, id), true);
      if (get().selection?.id === id) set({ selection: null });
    },
    removeEdgeById: (id) => {
      const graph = get().graph;
      if (!graph) return;
      apply(removeEdge(graph, id), true);
      if (get().selection?.id === id) set({ selection: null });
    },
    deleteSelection: () => {
      const { selection } = get();
      if (!selection) return;
      if (selection.kind === 'node') get().removeStepById(selection.id);
      else get().removeEdgeById(selection.id);
    },
    renameStep: (oldId, newId) => {
      const graph = get().graph;
      if (!graph) return null;
      const result = renameStep(graph, oldId, newId);
      if ('error' in result) return result.error;
      apply(result.graph, true, `rename:${oldId}`);
      if (get().selection?.id === oldId) set({ selection: { kind: 'node', id: newId } });
      return null;
    },
    patchStep: (id, patch, key) => {
      const graph = get().graph;
      if (graph) apply(updateStep(graph, id, patch), true, key ?? `step:${id}`);
    },
    moveNodes: (moves) => {
      let graph = get().graph;
      if (!graph) return;
      for (const [id, position] of Object.entries(moves)) graph = setPosition(graph, id, position);
      apply(graph, true);
    },
    connect: (source, target, when = 'success') => {
      const graph = get().graph;
      if (!graph) return null;
      const result = connect(graph, source, target, when);
      if ('error' in result) return result.error;
      apply(result.graph, true);
      return null;
    },
    setEdgeWhen: (id, when) => {
      const graph = get().graph;
      if (graph) apply(setEdgeWhen(graph, id, when), true);
    },
    patchMeta: (patch, key) => {
      const graph = get().graph;
      if (graph) apply({ ...graph, ...patch }, true, key ?? `meta:${Object.keys(patch).join(',')}`);
    },
    autoLayout: () => {
      const graph = get().graph;
      if (graph) apply(autoLayout(graph), true);
    },

    save: async (force = false) => {
      const { file, graph, saved } = get();
      if (!file || !graph || !saved) return false;
      if (validateClient(graph).length > 0) {
        set({ issues: validateClient(graph) });
        return false;
      }
      set({ saving: true });
      try {
        const result = await api.saveGraph(file, graph, saved.etag, force);
        const next = { ...graph, etag: result.etag, auto_layout: false };
        set({ saved: next, graph: next, conflict: false, serverError: null });
        return true;
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) set({ conflict: true });
        else set({ serverError: error instanceof Error ? error.message : String(error) });
        return false;
      } finally {
        set({ saving: false });
      }
    },
  };
});
