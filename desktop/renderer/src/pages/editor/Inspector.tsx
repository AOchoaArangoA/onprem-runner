import { Play, Plus, SkipForward, Trash2, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { EdgeWhen, Graph, StepDef } from '../../api/types';
import { Field, Toggle } from '../../components/ui';
import { downstream, STEP_TYPES } from '../../lib/graphModel';
import { useApp } from '../../store/app';
import { useEditor } from '../../store/editor';
import { ScheduleEditor } from './ScheduleEditor';

interface InspectorProps {
  graph: Graph;
  running: boolean;
  onRun: (only?: string[]) => void;
}

export function Inspector({ graph, running, onRun }: InspectorProps) {
  const selection = useEditor((s) => s.selection);
  if (selection?.kind === 'node') {
    const step = graph.steps.find((s) => s.id === selection.id);
    if (step) return <StepInspector key={step.id} step={step} graph={graph} running={running} onRun={onRun} />;
  }
  if (selection?.kind === 'edge') {
    const edge = graph.edges.find((e) => e.id === selection.id);
    if (edge) return <EdgeInspector edgeId={edge.id} />;
  }
  return <PipelineInspector graph={graph} />;
}

// -- helpers ------------------------------------------------------------------------------------

const isEmpty = (value: unknown) =>
  value === '' ||
  value === undefined ||
  value === null ||
  (Array.isArray(value) && value.length === 0) ||
  (typeof value === 'object' && !Array.isArray(value) && Object.keys(value as object).length === 0);

function ListEditor({
  values,
  onChange,
  placeholder,
  addLabel,
}: {
  values: string[];
  onChange: (values: string[]) => void;
  placeholder: string;
  addLabel: string;
}) {
  const { t } = useTranslation();
  return (
    <div className="stack tight">
      {values.map((value, index) => (
        <div className="row" key={index}>
          <input
            value={value}
            placeholder={placeholder}
            aria-label={placeholder}
            onChange={(e) => onChange(values.map((v, i) => (i === index ? e.target.value : v)))}
          />
          <button
            className="icon-btn"
            aria-label={t('common.remove')}
            onClick={() => onChange(values.filter((_, i) => i !== index))}
          >
            <X size={14} />
          </button>
        </div>
      ))}
      <button className="link" onClick={() => onChange([...values, ''])}>
        <Plus size={14} /> {addLabel}
      </button>
    </div>
  );
}

function EnvEditor({
  env,
  onChange,
}: {
  env: Record<string, string>;
  onChange: (env: Record<string, string>) => void;
}) {
  const { t } = useTranslation();
  const [rows, setRows] = useState<[string, string][]>(Object.entries(env));
  const commit = (next: [string, string][]) => {
    setRows(next);
    onChange(Object.fromEntries(next.filter(([key]) => key.trim() !== '')));
  };
  return (
    <div className="stack tight">
      {rows.map(([key, value], index) => (
        <div className="row" key={index}>
          <input
            className="env-key"
            value={key}
            placeholder="NAME"
            aria-label={t('editor.env_name')}
            onChange={(e) => commit(rows.map((r, i) => (i === index ? [e.target.value, r[1]] : r)))}
          />
          <input
            value={value}
            placeholder={t('editor.env_value')}
            aria-label={t('editor.env_value')}
            onChange={(e) => commit(rows.map((r, i) => (i === index ? [r[0], e.target.value] : r)))}
          />
          <button
            className="icon-btn"
            aria-label={t('common.remove')}
            onClick={() => commit(rows.filter((_, i) => i !== index))}
          >
            <X size={14} />
          </button>
        </div>
      ))}
      <button className="link" onClick={() => setRows([...rows, ['', '']])}>
        <Plus size={14} /> {t('editor.add_env')}
      </button>
      <p className="hint">{t('editor.secret_hint')}</p>
    </div>
  );
}

function InterpreterField({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const { t } = useTranslation();
  const interpreters = useApp((s) => s.interpreters);
  const loadInterpreters = useApp((s) => s.loadInterpreters);
  useEffect(() => {
    if (interpreters.length === 0) void loadInterpreters().catch(() => undefined);
  }, [interpreters.length, loadInterpreters]);
  const known = interpreters.some((i) => i.path === value);
  const choice = value === '' ? '' : value === 'embedded' ? 'embedded' : known ? value : '__custom__';
  const [custom, setCustom] = useState(choice === '__custom__' ? value : '');
  return (
    <div className="stack tight">
      <select
        value={choice}
        onChange={(e) => {
          const next = e.target.value;
          if (next === '__custom__') onChange(custom || ' ');
          else onChange(next);
        }}
      >
        <option value="">{t('editor.python_default')}</option>
        <option value="embedded">{t('editor.python_embedded')}</option>
        {interpreters.map((i) => (
          <option key={i.path} value={i.path}>
            {i.version ? `Python ${i.version}` : 'Python'} — {i.path}
          </option>
        ))}
        <option value="__custom__">{t('editor.python_custom')}</option>
      </select>
      {choice === '__custom__' && (
        <input
          value={custom || value}
          placeholder="C:\\Python311\\python.exe"
          onChange={(e) => {
            setCustom(e.target.value);
            onChange(e.target.value);
          }}
        />
      )}
      {choice === 'embedded' && <p className="hint">{t('editor.python_embedded_hint')}</p>}
    </div>
  );
}

// -- step ---------------------------------------------------------------------------------------

function StepInspector({
  step,
  graph,
  running,
  onRun,
}: {
  step: StepDef;
  graph: Graph;
  running: boolean;
  onRun: (only?: string[]) => void;
}) {
  const { t } = useTranslation();
  const patchStep = useEditor((s) => s.patchStep);
  const rename = useEditor((s) => s.renameStep);
  const remove = useEditor((s) => s.removeStepById);
  const [draftId, setDraftId] = useState(step.id);
  const [idError, setIdError] = useState<string | null>(null);

  useEffect(() => setDraftId(step.id), [step.id]);

  const commitId = () => {
    if (draftId === step.id) return setIdError(null);
    const error = rename(step.id, draftId);
    if (error) setIdError(t(`editor.id_error.${error}`));
    else setIdError(null);
  };

  const setParam = (key: string, value: unknown) => {
    const params = { ...step.params };
    if (isEmpty(value)) delete params[key];
    else params[key] = value;
    patchStep(step.id, { params }, `p:${step.id}:${key}`);
  };
  const text = (key: string) => (typeof step.params[key] === 'string' ? (step.params[key] as string) : '');
  const known = (STEP_TYPES as readonly string[]).includes(step.type);

  return (
    <div className="inspector" aria-label={t('editor.step_properties')}>
      <h3>{t('editor.step_properties')}</h3>
      <Field label={t('editor.step_id')} error={idError}>
        <input
          value={draftId}
          spellCheck={false}
          onChange={(e) => setDraftId(e.target.value)}
          onBlur={commitId}
          onKeyDown={(e) => e.key === 'Enter' && e.currentTarget.blur()}
        />
      </Field>
      <Field label={t('editor.type')}>
        <select
          value={step.type}
          onChange={(e) => patchStep(step.id, { type: e.target.value, params: {} })}
          disabled={!known}
        >
          {known ? (
            STEP_TYPES.map((type) => (
              <option key={type} value={type}>
                {t(`editor.types.${type}`)}
              </option>
            ))
          ) : (
            <option value={step.type}>{step.type}</option>
          )}
        </select>
      </Field>

      {step.type === 'python' && (
        <>
          <Field label={t('editor.script')} hint={t('editor.script_hint')}>
            <input value={text('script')} spellCheck={false} onChange={(e) => setParam('script', e.target.value)} />
          </Field>
          <Field label={t('editor.args')} group>
            <ListEditor
              values={Array.isArray(step.params.args) ? (step.params.args as string[]) : []}
              onChange={(v) => setParam('args', v)}
              placeholder={t('editor.arg')}
              addLabel={t('editor.add_arg')}
            />
          </Field>
          <Field label={t('editor.interpreter')} group>
            <InterpreterField value={text('python')} onChange={(v) => setParam('python', v)} />
          </Field>
        </>
      )}
      {step.type === 'shell' && (
        <Field label={t('editor.command')} hint={t('editor.command_hint')}>
          <textarea
            rows={3}
            value={text('command')}
            spellCheck={false}
            onChange={(e) => setParam('command', e.target.value)}
          />
        </Field>
      )}
      {(step.type === 'python' || step.type === 'shell') && (
        <>
          <Field label={t('editor.cwd')} hint={t('editor.cwd_hint')}>
            <input value={text('cwd')} spellCheck={false} onChange={(e) => setParam('cwd', e.target.value)} />
          </Field>
          <Field label={t('editor.env')} group>
            <EnvEditor
              env={(step.params.env as Record<string, string> | undefined) ?? {}}
              onChange={(env) => setParam('env', env)}
            />
          </Field>
        </>
      )}
      {!known && <RawParams step={step} onChange={(params) => patchStep(step.id, { params })} />}

      <h4>{t('editor.execution')}</h4>
      <div className="grid-2">
        <Field label={t('editor.timeout')} hint={t('editor.seconds')}>
          <input
            type="number"
            min={1}
            value={step.timeout ?? ''}
            onChange={(e) => patchStep(step.id, { timeout: e.target.value === '' ? null : Number(e.target.value) })}
          />
        </Field>
        <Field label={t('editor.retries')}>
          <input
            type="number"
            min={0}
            value={step.retries}
            onChange={(e) => patchStep(step.id, { retries: Math.max(0, Math.floor(Number(e.target.value) || 0)) })}
          />
        </Field>
      </div>
      {step.retries > 0 && (
        <Field label={t('editor.retry_delay')} hint={t('editor.seconds')}>
          <input
            type="number"
            min={0}
            value={step.retry_delay}
            onChange={(e) => patchStep(step.id, { retry_delay: Math.max(0, Number(e.target.value) || 0) })}
          />
        </Field>
      )}
      <Toggle
        checked={step.ignore_failure}
        label={t('editor.ignore_failure')}
        onChange={(value) => patchStep(step.id, { ignore_failure: value })}
      />
      <p className="hint">{t('editor.ignore_failure_hint')}</p>

      <div className="inspector-actions">
        <button disabled={running} onClick={() => onRun([step.id])}>
          <Play size={14} /> {t('editor.run_only')}
        </button>
        <button disabled={running} onClick={() => onRun(downstream(graph, step.id))}>
          <SkipForward size={14} /> {t('editor.run_from_here')}
        </button>
        <button className="danger" onClick={() => remove(step.id)}>
          <Trash2 size={14} /> {t('editor.delete_step')}
        </button>
      </div>
    </div>
  );
}

function RawParams({ step, onChange }: { step: StepDef; onChange: (params: Record<string, unknown>) => void }) {
  const { t } = useTranslation();
  const [text, setText] = useState(JSON.stringify(step.params, null, 2));
  const [error, setError] = useState<string | null>(null);
  return (
    <Field label={t('editor.raw_params')} error={error} hint={t('editor.raw_params_hint')}>
      <textarea
        rows={6}
        value={text}
        spellCheck={false}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => {
          try {
            const parsed: unknown = JSON.parse(text);
            if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) throw new Error('object');
            onChange(parsed as Record<string, unknown>);
            setError(null);
          } catch {
            setError(t('editor.invalid_json'));
          }
        }}
      />
    </Field>
  );
}

// -- edge ---------------------------------------------------------------------------------------

function EdgeInspector({ edgeId }: { edgeId: string }) {
  const { t } = useTranslation();
  const edge = useEditor((s) => s.graph?.edges.find((e) => e.id === edgeId));
  const setWhen = useEditor((s) => s.setEdgeWhen);
  const remove = useEditor((s) => s.removeEdgeById);
  if (!edge) return null;
  const options: EdgeWhen[] = ['success', 'failure', 'always'];
  return (
    <div className="inspector" aria-label={t('editor.connection')}>
      <h3>{t('editor.connection')}</h3>
      <p className="route">
        <code>{edge.source}</code> → <code>{edge.target}</code>
      </p>
      <Field label={t('editor.when')} group>
        <div className="radio-cards">
          {options.map((option) => (
            <label key={option} className={`radio-card when-${option}${edge.when === option ? ' on' : ''}`}>
              <input
                type="radio"
                name="when"
                checked={edge.when === option}
                onChange={() => setWhen(edge.id, option)}
              />
              <strong>{t(`editor.when_${option}`)}</strong>
              <span>{t(`editor.when_${option}_hint`)}</span>
            </label>
          ))}
        </div>
      </Field>
      <div className="inspector-actions">
        <button className="danger" onClick={() => remove(edge.id)}>
          <Trash2 size={14} /> {t('editor.delete_connection')}
        </button>
      </div>
    </div>
  );
}

// -- pipeline -----------------------------------------------------------------------------------

function PipelineInspector({ graph }: { graph: Graph }) {
  const { t } = useTranslation();
  const patchMeta = useEditor((s) => s.patchMeta);
  return (
    <div className="inspector" aria-label={t('editor.pipeline_properties')}>
      <h3>{t('editor.pipeline_properties')}</h3>
      <Field label={t('editor.name')}>
        <input value={graph.name} onChange={(e) => patchMeta({ name: e.target.value }, 'name')} />
      </Field>
      <p className="muted file-path" title={graph.file}>
        {graph.file}
      </p>
      <Toggle
        checked={graph.fail_fast}
        label={t('editor.fail_fast')}
        onChange={(value) => patchMeta({ fail_fast: value })}
      />
      <p className="hint">{t('editor.fail_fast_hint')}</p>
      <Field label={t('editor.max_parallel')} hint={t('editor.max_parallel_hint')}>
        <input
          type="number"
          min={1}
          max={64}
          value={graph.max_parallel}
          onChange={(e) =>
            patchMeta({ max_parallel: Math.min(64, Math.max(1, Math.floor(Number(e.target.value) || 1))) }, 'parallel')
          }
        />
      </Field>
      <h4>{t('schedule.title')}</h4>
      <ScheduleEditor schedule={graph.schedule} onChange={(schedule) => patchMeta({ schedule })} />
      <h4>{t('editor.notifications')}</h4>
      {graph.notifiers.length === 0 ? (
        <p className="muted">{t('editor.no_notifiers')}</p>
      ) : (
        <ul className="plain">
          {graph.notifiers.map((n, i) => (
            <li key={i}>
              <code>{String(n.type)}</code>
              <span className="muted"> · {String(n.when ?? 'always')}</span>
            </li>
          ))}
        </ul>
      )}
      <p className="hint">{t('editor.notifiers_yaml')}</p>
    </div>
  );
}
