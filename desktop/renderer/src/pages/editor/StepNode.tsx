import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';
import { AlertTriangle, Box, FileCode2, RotateCw, Terminal, Timer } from 'lucide-react';
import { memo } from 'react';
import { useTranslation } from 'react-i18next';
import type { StepDef, StepStatus } from '../../api/types';
import { formatDuration } from '../../lib/format';
import { stepSummary } from '../../lib/graphModel';

export type StepNodeData = {
  step: StepDef;
  status?: StepStatus;
  duration?: number | null;
  hasIssue: boolean;
};
export type StepFlowNode = Node<StepNodeData, 'step'>;

function Icon({ type }: { type: string }) {
  if (type === 'python') return <FileCode2 size={16} aria-hidden />;
  if (type === 'shell') return <Terminal size={16} aria-hidden />;
  return <Box size={16} aria-hidden />;
}

function StepNodeView({ data, selected }: NodeProps<StepFlowNode>) {
  const { t } = useTranslation();
  const { step, status, duration, hasIssue } = data;
  const summary = stepSummary(step);
  return (
    <div
      className={`step-node${selected ? ' selected' : ''}${status ? ` ns-${status}` : ''}${hasIssue ? ' issue' : ''}`}
      aria-label={`${t('editor.step')} ${step.id}`}
    >
      <Handle type="target" position={Position.Left} />
      <div className="step-head">
        <Icon type={step.type} />
        <span className="step-id" title={step.id}>
          {step.id}
        </span>
        {status && (
          <span className={`step-state s-${status}`} title={t(`status.${status}`)}>
            {status === 'running' && <span className="spinner" aria-hidden />}
          </span>
        )}
      </div>
      <div className="step-sub" title={summary}>
        <span className="type-tag">{step.type}</span>
        {summary || <span className="muted">{t('editor.not_configured')}</span>}
      </div>
      <div className="step-foot">
        {step.retries > 0 && (
          <span className="mini" title={t('editor.retries')}>
            <RotateCw size={11} aria-hidden /> {step.retries}
          </span>
        )}
        {step.timeout !== null && (
          <span className="mini" title={t('editor.timeout')}>
            <Timer size={11} aria-hidden /> {step.timeout}s
          </span>
        )}
        {step.ignore_failure && (
          <span className="mini warn" title={t('editor.ignore_failure')}>
            <AlertTriangle size={11} aria-hidden />
          </span>
        )}
        {hasIssue && <span className="mini error">{t('editor.fix_me')}</span>}
        {duration !== undefined && duration !== null && (
          <span className="mini right">{formatDuration(duration)}</span>
        )}
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

export const StepNode = memo(StepNodeView);
