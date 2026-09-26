import { Handle, Position, type NodeProps } from '@xyflow/react';
import type { WorkflowFlowNode } from './types';

export function NodeCard({
  data,
  selected,
  icon,
  kind,
}: NodeProps<WorkflowFlowNode> & { icon: string; kind: string }) {
  return (
    <article className={`workflow-node workflow-node--${kind}${data.enabled ? '' : ' is-disabled'}${selected ? ' is-selected' : ''}`}>
      <Handle type="target" position={Position.Left} aria-label={`Connect to ${data.label}`} />
      <div className="workflow-node__heading">
        <span className="workflow-node__icon" aria-hidden="true">{icon}</span>
        <span className="workflow-node__kind">{kind}</span>
        {!data.enabled && <span className="workflow-node__status">已停用</span>}
      </div>
      <strong className="workflow-node__title">{data.label}</strong>
      <span className="workflow-node__binding">{data.binding || '尚未配置'}</span>
      {data.summary && <span className="workflow-node__summary">{data.summary}</span>}
      <Handle type="source" position={Position.Right} aria-label={`Connect from ${data.label}`} />
    </article>
  );
}
