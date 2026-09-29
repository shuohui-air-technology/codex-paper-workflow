import type { WorkflowDocument } from '../types';

interface GraphOutlineProps {
  workflow: WorkflowDocument | null;
  selectedNodeId: string | null;
  onSelectNode: (nodeId: string) => void;
}

export function GraphOutline({ workflow, selectedNodeId, onSelectNode }: GraphOutlineProps) {
  return (
    <section className="outline-panel" aria-labelledby="outline-title">
      <div className="panel-heading panel-heading--compact">
        <div><p className="eyebrow">点击阶段即可编辑</p><h2 id="outline-title">流程大纲</h2></div>
        <span className="count-pill">{workflow?.nodes.length ?? 0} 阶段</span>
      </div>
      {!workflow || workflow.nodes.length === 0 ? (
        <p className="empty-state">添加阶段后，这里会显示流程结构。</p>
      ) : (
        <ol className="outline-list">
          {workflow.nodes.map((node, index) => {
            const outgoing = workflow.edges.filter((edge) => edge.source === node.id).map((edge) => workflow.nodes.find((candidate) => candidate.id === edge.target)?.display_name ?? edge.target);
            const selected = selectedNodeId === node.id;
            return (
              <li key={node.id}>
                <button
                  type="button"
                  className={`outline-item${selected ? ' is-selected' : ''}${node.enabled ? '' : ' is-disabled'}`}
                  aria-pressed={selected}
                  onClick={() => onSelectNode(node.id)}
                >
                  <span className="outline-item__number">{String(index + 1).padStart(2, '0')}</span>
                  <span className="outline-item__main">
                    <strong>{node.display_name}</strong>
                    <small>{{ task: '任务', validator: '验证', condition: '分支', join: '汇合' }[node.type]}{outgoing.length ? ` → ${outgoing.join(', ')}` : ' · 结束阶段'}</small>
                  </span>
                  {!node.enabled && <span className="outline-item__state">停用</span>}
                </button>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
