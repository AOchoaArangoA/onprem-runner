import {
  applyNodeChanges,
  Background,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Connection,
  type Edge,
  type NodeChange,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import {
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  FileCode2,
  LayoutDashboard,
  Play,
  Redo2,
  Save,
  Square,
  Terminal,
  Undo2,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import type { StepStatus } from '../api/types';
import { LogViewer } from '../components/LogViewer';
import { Link } from '../components/Link';
import { Empty, Spinner, StatusBadge } from '../components/ui';
import { useApp, errorMessage } from '../store/app';
import { useEditor } from '../store/editor';
import { Inspector } from './editor/Inspector';
import { StepNode, type StepFlowNode } from './editor/StepNode';

const nodeTypes = { step: StepNode };
const NO_DURATIONS: Record<string, number | null> = {}; // stable identity: used in effect deps
const EDGE_COLOR = { success: 'var(--ok)', failure: 'var(--bad)', always: 'var(--muted)' } as const;

/** Statuses to paint on the canvas: the live run if there is one, else the last finished run. */
function useRunOverlay(file: string) {
  const active = useApp((s) => Object.values(s.active).find((run) => run.file === file));
  const lastRunId = useApp((s) => s.pipelines.find((p) => p.file === file)?.last_run?.run_id ?? null);
  const lastStatus = useApp((s) => s.pipelines.find((p) => p.file === file)?.last_run?.status);
  const [last, setLast] = useState<{
    statuses: Record<string, StepStatus>;
    durations: Record<string, number | null>;
  }>({ statuses: {}, durations: {} });

  useEffect(() => {
    let cancelled = false;
    if (!lastRunId) {
      setLast({ statuses: {}, durations: {} });
      return;
    }
    api
      .runDetail(lastRunId)
      .then((detail) => {
        if (cancelled) return;
        setLast({
          statuses: Object.fromEntries(detail.steps.map((s) => [s.step_id, s.status])),
          durations: Object.fromEntries(detail.steps.map((s) => [s.step_id, s.duration_s])),
        });
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [lastRunId, lastStatus]);

  return {
    runId: active?.run_id ?? lastRunId,
    live: active !== undefined,
    statuses: active ? active.steps : last.statuses,
    durations: active ? NO_DURATIONS : last.durations,
  };
}

export function EditorPage({ file }: { file: string }) {
  return (
    <ReactFlowProvider>
      <EditorInner file={file} />
    </ReactFlowProvider>
  );
}

function EditorInner({ file }: { file: string }) {
  const { t } = useTranslation();
  const toast = useApp((s) => s.toast);
  const pipelines = useApp((s) => s.pipelines);
  const theme = useApp((s) => s.settings?.theme ?? 'system');
  const editor = useEditor();
  const { graph, selection, issues, serverError, saving, conflict, past, future } = editor;
  const overlay = useRunOverlay(file);
  const flow = useReactFlow();
  const wrapper = useRef<HTMLDivElement>(null);
  const [nodes, setNodes] = useState<StepFlowNode[]>([]);
  const [logsOpen, setLogsOpen] = useState(false);
  const dirty = graph !== null && editor.isDirty();
  const issueIds = useMemo(() => new Set(issues.map((i) => i.stepId).filter(Boolean) as string[]), [issues]);
  const summary = pipelines.find((p) => p.file === file);

  useEffect(() => {
    void useEditor.getState().load(file);
    return () => useEditor.getState().close();
  }, [file]);

  // Another program (or the user in a text editor) may change the file while it is open.
  useEffect(() => {
    if (saving || !graph) return;
    void useEditor
      .getState()
      .checkExternalChange()
      .then((result) => result === 'reloaded' && toast('info', t('editor.reloaded_from_disk')));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipelines]);

  // The engine does not watch files, so look for outside edits (a text editor, git) periodically.
  useEffect(() => {
    const id = setInterval(() => {
      if (document.hidden || useEditor.getState().saving) return;
      void useEditor
        .getState()
        .checkExternalChange()
        .then((result) => result === 'reloaded' && toast('info', t('editor.reloaded_from_disk')));
    }, 3000);
    return () => clearInterval(id);
  }, [toast, t]);

  useEffect(() => {
    if (!dirty) return;
    const handler = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [dirty]);

  // Canvas nodes: positions come from the document, except for a node being dragged right now.
  useEffect(() => {
    if (!graph) return;
    setNodes((previous) =>
      graph.steps.map((step) => {
        const old = previous.find((n) => n.id === step.id);
        const stored = graph.positions[step.id] ?? [0, 0];
        return {
          id: step.id,
          type: 'step',
          position: old?.dragging ? old.position : { x: stored[0], y: stored[1] },
          dragging: old?.dragging,
          selected: selection?.kind === 'node' && selection.id === step.id,
          data: {
            step,
            status: overlay.statuses[step.id],
            duration: overlay.durations[step.id],
            hasIssue: issueIds.has(step.id),
          },
        };
      }),
    );
  }, [graph, selection, overlay.statuses, overlay.durations, issueIds]);

  const edges = useMemo<Edge[]>(() => {
    if (!graph) return [];
    return graph.edges.map((edge) => {
      const color = EDGE_COLOR[edge.when];
      const isSelected = selection?.kind === 'edge' && selection.id === edge.id;
      return {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: 'smoothstep',
        label: edge.when === 'success' ? undefined : t(`editor.when_${edge.when}_short`),
        labelStyle: { fill: color, fontWeight: 600, fontSize: 11 },
        labelBgStyle: { fill: 'var(--surface)' },
        style: {
          stroke: color,
          strokeWidth: isSelected ? 3.5 : 2,
          strokeDasharray: edge.when === 'always' ? '7 5' : undefined,
        },
        markerEnd: { type: MarkerType.ArrowClosed, color },
        animated: overlay.statuses[edge.source] === 'running',
        selected: isSelected,
        interactionWidth: 24,
      };
    });
  }, [graph, selection, overlay.statuses, t]);

  const onNodesChange = useCallback(
    (changes: NodeChange<StepFlowNode>[]) => setNodes((current) => applyNodeChanges(changes, current)),
    [],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      if (!connection.source || !connection.target) return;
      const error = useEditor.getState().connect(connection.source, connection.target);
      if (error) toast('error', t(`editor.connect_error.${error}`));
    },
    [toast, t],
  );

  const addNode = (type: string) => {
    const rect = wrapper.current?.getBoundingClientRect();
    const center = flow.screenToFlowPosition({
      x: (rect?.left ?? 0) + (rect?.width ?? 600) / 2,
      y: (rect?.top ?? 0) + (rect?.height ?? 400) / 2,
    });
    const jitter = (useEditor.getState().graph?.steps.length ?? 0) % 6;
    useEditor.getState().addStep(type, [Math.round(center.x - 110 + jitter * 24), Math.round(center.y - 40 + jitter * 24)]);
  };

  const save = useCallback(async () => {
    const ok = await useEditor.getState().save();
    if (ok) toast('success', t('editor.saved'));
    else if (useEditor.getState().issues.length) toast('error', t('editor.cannot_save'));
  }, [toast, t]);

  const run = useCallback(
    async (only?: string[]) => {
      const state = useEditor.getState();
      if (state.isDirty() && !(await state.save())) {
        toast('error', t('editor.cannot_save'));
        return;
      }
      try {
        await api.run(file, only);
        setLogsOpen(true);
      } catch (error) {
        toast('error', errorMessage(error));
      }
    },
    [file, toast, t],
  );

  const stop = async () => {
    if (!overlay.live || !overlay.runId) return;
    try {
      await api.cancel(overlay.runId);
    } catch (error) {
      toast('error', errorMessage(error));
    }
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const typing = (event.target as HTMLElement | null)?.closest('input, textarea, select, [contenteditable]');
      const mod = event.ctrlKey || event.metaKey;
      if (mod && event.key.toLowerCase() === 's') {
        event.preventDefault();
        void save();
      } else if (mod && event.key === 'Enter') {
        event.preventDefault();
        void run();
      } else if (!typing && mod && event.key.toLowerCase() === 'z') {
        event.preventDefault();
        if (event.shiftKey) useEditor.getState().redo();
        else useEditor.getState().undo();
      } else if (!typing && mod && event.key.toLowerCase() === 'y') {
        event.preventDefault();
        useEditor.getState().redo();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [save, run]);

  if (editor.loadError) {
    return (
      <Empty
        title={t('editor.load_failed')}
        hint={editor.loadError}
        action={
          <Link to={{ name: 'pipelines' }} className="button">
            {t('editor.back')}
          </Link>
        }
      />
    );
  }
  if (!graph) return <Spinner label={t('common.loading')} />;

  const title = graph.name || summary?.name || file;
  return (
    <div className="editor">
      <div className="editor-top">
        <div className="editor-title">
          <Link to={{ name: 'pipelines' }} className="crumb">
            {t('nav.pipelines')}
          </Link>
          <span className="muted">/</span>
          <strong title={file}>{title}</strong>
          {dirty && <span className="dirty" title={t('editor.unsaved')} aria-label={t('editor.unsaved')} />}
        </div>
        <div className="toolbar">
          <button onClick={() => addNode('python')} title={t('editor.add_python')}>
            <FileCode2 size={15} /> {t('editor.add_python')}
          </button>
          <button onClick={() => addNode('shell')} title={t('editor.add_shell')}>
            <Terminal size={15} /> {t('editor.add_shell')}
          </button>
          <span className="sep" />
          <button className="icon-btn" disabled={past.length === 0} onClick={editor.undo} title={`${t('editor.undo')} (Ctrl+Z)`} aria-label={t('editor.undo')}>
            <Undo2 size={16} />
          </button>
          <button className="icon-btn" disabled={future.length === 0} onClick={editor.redo} title={`${t('editor.redo')} (Ctrl+Y)`} aria-label={t('editor.redo')}>
            <Redo2 size={16} />
          </button>
          <button className="icon-btn" onClick={editor.autoLayout} title={t('editor.auto_layout')} aria-label={t('editor.auto_layout')}>
            <LayoutDashboard size={16} />
          </button>
          <span className="sep" />
          <button className="primary" disabled={!dirty || saving} onClick={() => void save()} title="Ctrl+S">
            <Save size={15} /> {t('common.save')}
          </button>
          {overlay.live ? (
            <button className="danger" onClick={() => void stop()}>
              <Square size={15} /> {t('editor.stop')}
            </button>
          ) : (
            <button className="run" onClick={() => void run()} title="Ctrl+Enter">
              <Play size={15} /> {t('editor.run')}
            </button>
          )}
          {overlay.runId && (
            <StatusBadge status={overlay.live ? 'running' : (summary?.last_run?.status ?? 'pending')} small />
          )}
        </div>
      </div>

      {conflict && (
        <div className="banner warn" role="alert">
          <AlertTriangle size={16} aria-hidden />
          <span>{t('editor.conflict')}</span>
          <button onClick={() => void editor.reload()}>{t('editor.reload_disk')}</button>
          <button onClick={() => void editor.save(true)}>{t('editor.overwrite')}</button>
        </div>
      )}
      {(issues.length > 0 || serverError) && (
        <div className="banner error" role="alert">
          <AlertTriangle size={16} aria-hidden />
          <div>
            {issues.slice(0, 4).map((issue, i) => (
              <div key={i}>{t(`editor.issue.${issue.key}`, { step: issue.stepId })}</div>
            ))}
            {issues.length === 0 && serverError && <pre className="server-error">{serverError}</pre>}
          </div>
        </div>
      )}

      <div className="editor-main">
        <div className="canvas-wrap">
          <div className="canvas" ref={wrapper}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onConnect={onConnect}
            onNodeClick={(_, node) => editor.select({ kind: 'node', id: node.id })}
            onEdgeClick={(_, edge) => editor.select({ kind: 'edge', id: edge.id })}
            onPaneClick={() => editor.select(null)}
            onNodeDragStop={(_, __, dragged) =>
              editor.moveNodes(
                Object.fromEntries(dragged.map((n) => [n.id, [Math.round(n.position.x), Math.round(n.position.y)] as [number, number]])),
              )
            }
            onNodesDelete={(deleted) => deleted.forEach((n) => useEditor.getState().removeStepById(n.id))}
            onEdgesDelete={(deleted) => deleted.forEach((e) => useEditor.getState().removeEdgeById(e.id))}
            deleteKeyCode={['Delete', 'Backspace']}
            fitView
            fitViewOptions={{ padding: 0.3, maxZoom: 1 }}
            minZoom={0.2}
            snapToGrid
            snapGrid={[10, 10]}
            nodesConnectable
            colorMode={theme}
            aria-label={t('editor.canvas')}
          >
            <Background gap={20} size={1} />
            <Controls showInteractive={false} />
            <MiniMap pannable zoomable className="minimap" nodeColor="var(--node-mini)" />
          </ReactFlow>
          {graph.steps.length === 0 && (
            <div className="canvas-empty">
              <Empty title={t('editor.empty_title')} hint={t('editor.empty_hint')} />
            </div>
          )}
          </div>

          <div className={`log-drawer${logsOpen ? ' open' : ''}`}>
            <button className="drawer-handle" onClick={() => setLogsOpen(!logsOpen)} aria-expanded={logsOpen}>
              {logsOpen ? <ChevronDown size={16} /> : <ChevronUp size={16} />}
              <strong>{t('logs.title')}</strong>
              {overlay.runId ? (
                <span className="muted mono">{overlay.runId}</span>
              ) : (
                <span className="muted">{t('logs.no_runs_yet')}</span>
              )}
              {overlay.live && <StatusBadge status="running" small />}
            </button>
            {logsOpen && overlay.runId && (
              <LogViewer
                key={overlay.runId}
                runId={overlay.runId}
                live={overlay.live}
                steps={graph.steps.map((s) => s.id)}
              />
            )}
          </div>
        </div>
        <aside className="side">
          <Inspector graph={graph} running={overlay.live} onRun={(only) => void run(only)} />
        </aside>
      </div>
    </div>
  );
}
