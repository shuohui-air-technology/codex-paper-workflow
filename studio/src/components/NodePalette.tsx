import { useMemo, useState } from 'react';
import type { SkillCatalogEntry, WorkflowNodeType } from '../types';

interface NodePaletteProps {
  skills: SkillCatalogEntry[];
  editable: boolean;
  onAddNode: (type: WorkflowNodeType, skillRef?: string) => void;
  onRefresh: () => void;
  refreshing: boolean;
}

const NODE_OPTIONS: Array<{ type: WorkflowNodeType; label: string; description: string; icon: string }> = [
  { type: 'task', label: '空白任务阶段', description: '先添加阶段，再从列表绑定 Skill', icon: '✦' },
  { type: 'condition', label: '条件分支', description: '根据已记录的事实选择路径', icon: '◇' },
  { type: 'join', label: '汇合阶段', description: '等待分支汇合或选择先完成的一支', icon: '⇉' },
  { type: 'validator', label: '验证阶段', description: '运行项目内置的安全检查', icon: '✓' },
];

export function NodePalette({ skills, editable, onAddNode, onRefresh, refreshing }: NodePaletteProps) {
  const [query, setQuery] = useState('');
  const visibleSkills = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return skills
      .filter((skill) => !skill.ambiguous)
      .filter((skill) => !normalized || `${skill.display_name} ${skill.catalog_id} ${skill.description}`.toLocaleLowerCase().includes(normalized));
  }, [query, skills]);

  return (
    <aside className="side-panel node-palette" aria-label="添加流程阶段">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">工作流积木</p>
          <h2>添加阶段</h2>
        </div>
      </div>
      {!editable && <p className="muted-note">官方流程仅供查看。复制为自定义流程后即可编排。</p>}
      <div className="palette-control-list">
        {NODE_OPTIONS.map((option) => (
          <button
            type="button"
            className="palette-control"
            key={option.type}
            disabled={!editable}
            onClick={() => onAddNode(option.type)}
          >
            <span className="palette-control__icon" aria-hidden="true">{option.icon}</span>
            <span><strong>{option.label}</strong><small>{option.description}</small></span>
            <span className="palette-control__add" aria-hidden="true">＋</span>
          </button>
        ))}
      </div>
      <div className="palette-section-heading">
        <h3>已安装的 Skills</h3>
        <button type="button" className="text-button" disabled={refreshing} onClick={onRefresh}>{refreshing ? '刷新中…' : '刷新'}</button>
      </div>
      {skills.some((skill) => skill.ambiguous) && <p className="warning-inline palette-warning">有 {skills.filter((skill) => skill.ambiguous).length} 个名称冲突的 Skill，暂不可选择。</p>}
      <label className="search-field">
        <span className="sr-only">搜索 Skill</span>
        <span aria-hidden="true">⌕</span>
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称或用途" disabled={!editable} />
      </label>
      <div className="skill-list">
        {visibleSkills.map((skill) => (
          <button type="button" className="skill-choice" key={skill.catalog_id} disabled={!editable} onClick={() => onAddNode('task', skill.catalog_id)}>
            <span className="skill-choice__symbol" aria-hidden="true">✧</span>
            <span className="skill-choice__copy">
              <strong>{skill.display_name}</strong>
              <small>{skill.description || skill.catalog_id}</small>
            </span>
            {skill.locked ? <span className="status-dot" title="已锁定版本身份" /> : <span className="unlocked-mark" title="本地版本未锁定">本地</span>}
          </button>
        ))}
        {visibleSkills.length === 0 && <p className="empty-state">没有匹配的 Skill。请确认已在本机安装。</p>}
      </div>
    </aside>
  );
}
