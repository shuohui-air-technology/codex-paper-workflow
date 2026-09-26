import { NodeCard } from './NodeCard';
import type { WorkflowFlowNode } from './types';
import type { NodeProps } from '@xyflow/react';

export function TaskNode(props: NodeProps<WorkflowFlowNode>) {
  return <NodeCard {...props} icon="✦" kind="任务" />;
}
