import { useEffect, useRef, useState } from 'react';
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  MarkerType,
  type Connection,
  type Edge,
  type ReactFlowInstance,
} from '@xyflow/react';
import type { SkillCatalogEntry, ValidatorCatalogEntry, WorkflowDocument } from '../types';
import { ConditionNode } from '../nodes/ConditionNode';
import { JoinNode } from '../nodes/JoinNode';
import { TaskNode } from '../nodes/TaskNode';
import { ValidatorNode } from '../nodes/ValidatorNode';
import type { WorkflowFlowNode } from '../nodes/types';

const NODE_TYPES = {
  task: TaskNode,
  condition: ConditionNode,
  join: JoinNode,
  validator: ValidatorNode,
};

interface WorkflowCanvasProps {
  workflow: WorkflowDocument | null;
  skills: SkillCatalogEntry[];
  validators: ValidatorCatalogEntry[];
  selectedNodeId: string | null;
  readOnly: boolean;
  onSelectNode: (nodeId: string) => void;
  onMoveNode: (nodeId: string, position: { x: number; y: number }) => void;
  onConnect: (connection: Connection) => void;
  onDeleteEdges: (edges: Edge[]) => void;
  onDeselect: () => void;
  focusRequest: number;
  layoutRequest: number;
}

export function WorkflowCanvas({
  workflow,
  skills,
  validators,
  selectedNodeId,
  readOnly,
  onSelectNode,
  onMoveNode,
  onConnect,
  onDeleteEdges,
  onDeselect,
  focusRequest,
  layoutRequest,
}: WorkflowCanvasProps) {
  const [flow, setFlow] = useState<ReactFlowInstance<WorkflowFlowNode, Edge> | null>(null);
  const canvasArea = useRef<HTMLDivElement>(null);
  const consumedFocus = useRef(0);
  useEffect(() => {
    if (!flow || !canvasArea.current) return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => { void flow.fitView({ padding: 0.2 }); });
    });
    observer.observe(canvasArea.current);
    return () => { observer.disconnect(); cancelAnimationFrame(frame); };
  }, [flow]);
  useEffect(() => {
    if (!flow || focusRequest === consumedFocus.current) return;
    consumedFocus.current = focusRequest;
    if (!selectedNodeId) return;
    void flow.fitView({ nodes: [{ id: selectedNodeId }], padding: 0.8, maxZoom: 1, duration: 180 });
  }, [flow, focusRequest, selectedNodeId]);
  useEffect(() => {
    if (!flow) return;
    const frame = requestAnimationFrame(() => { void flow.fitView({ padding: 0.2, duration: 180 }); });
    return () => cancelAnimationFrame(frame);
  }, [flow, layoutRequest]);
  const nodes: WorkflowFlowNode[] = workflow ? workflow.nodes.map((node) => {
    const skill = node.type === 'task' ? skills.find((item) => item.catalog_id === node.skill_ref) : undefined;
    const validator = node.type === 'validator' ? validators.find((item) => item.validator_id === node.validator_ref) : undefined;
    const binding = node.type === 'task'
      ? (skill?.display_name ?? node.skill_ref ?? '')
      : node.type === 'validator'
        ? (validator?.validator_id ?? node.validator_ref ?? '')
        : '';
    return {
      id: node.id,
      type: node.type,
      position: workflow.ui.positions[node.id] ?? { x: 80, y: 80 },
      selected: node.id === selectedNodeId,
      draggable: !readOnly,
      data: {
        label: node.display_name,
        binding,
        enabled: node.enabled,
        summary: node.entry ? '入口阶段' : '',
      },
    };
  }) : [];
  const edges: Edge[] = workflow ? workflow.edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    label: ({ succeeded: '完成', pass: '通过', fail: '未通过', blocked: '需处理', default: '默认' } as Record<string, string>)[edge.trigger] ?? edge.trigger,
    type: 'smoothstep',
    selectable: !readOnly,
    deletable: !readOnly,
    markerEnd: { type: MarkerType.ArrowClosed, color: '#8190a4' },
    style: { stroke: '#8b9aaf', strokeWidth: 1.7 },
    labelStyle: { fill: '#526171', fontWeight: 600, fontSize: 11 },
    labelBgStyle: { fill: '#f7f9fc', fillOpacity: 0.94 },
  })) : [];

  const handleConnect = (connection: Connection) => {
    if (!readOnly) onConnect(connection);
  };

  return (
    <section className="canvas-shell" aria-label="工作流画布">
      <div className="canvas-toolbar">
        <div>
          <p className="eyebrow">流程画布</p>
          <h2>{workflow?.workflow_id ?? '官方流程预览'}</h2>
        </div>
        <div className="canvas-legend" aria-label="阶段图例">
          <span><i className="legend-dot legend-dot--task" />任务</span>
          <span><i className="legend-dot legend-dot--condition" />分支</span>
          <span><i className="legend-dot legend-dot--validator" />验证</span>
          <span><i className="legend-dot legend-dot--join" />汇合</span>
        </div>
      </div>
      <div className="canvas-area" ref={canvasArea} role="region" aria-label="可视化工作流图">
        {!workflow || workflow.nodes.length === 0 ? (
          <div className="canvas-empty">
            <span aria-hidden="true">✧</span>
            <strong>从左侧添加一个阶段</strong>
            <p>将 Skills 与条件、验证和汇合阶段连接起来，组成适合自己的流程。</p>
          </div>
        ) : (
          <ReactFlow<WorkflowFlowNode, Edge>
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            nodesDraggable={!readOnly}
            nodesConnectable={!readOnly}
            elementsSelectable
            deleteKeyCode={readOnly ? null : ['Backspace', 'Delete']}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            minZoom={0.25}
            maxZoom={1.6}
            onNodeClick={(_event, node) => onSelectNode(node.id)}
            onInit={setFlow}
            onPaneClick={onDeselect}
            onNodeDragStop={(_event, node) => onMoveNode(node.id, node.position)}
            onConnect={handleConnect}
            onEdgesDelete={(deleted) => { if (!readOnly) onDeleteEdges(deleted); }}
            proOptions={{ hideAttribution: true }}
          >
            <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#d9e0ea" />
            {nodes.length > 5 && <MiniMap
              pannable
              zoomable
              nodeColor={(node) => ({ task: '#4f77d6', condition: '#c98b32', validator: '#259a7a', join: '#8a66bb' }[node.type ?? 'task'] ?? '#4f77d6')}
              maskColor="rgba(244,247,251,0.75)"
            />}
            <Controls showInteractive={false} />
          </ReactFlow>
        )}
      </div>
      <p className="canvas-hint">提示：拖动节点可调整布局；连接圆点可建立依赖。键盘用户也可在右侧面板用表单创建连接。</p>
    </section>
  );
}
