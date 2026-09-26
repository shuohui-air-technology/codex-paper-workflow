import type { Node } from '@xyflow/react';
import type { WorkflowNodeType } from '../types';

export interface WorkflowNodeData extends Record<string, unknown> {
  label: string;
  binding: string;
  enabled: boolean;
  summary: string;
}

export type WorkflowFlowNode = Node<WorkflowNodeData, WorkflowNodeType>;
