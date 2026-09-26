import { useEffect, useMemo, useState } from 'react';
import type {
  ConditionCase,
  JsonPrimitive,
  SkillCatalogEntry,
  ValidatorCatalogEntry,
  WorkflowDocument,
  WorkflowEdge,
  WorkflowNode,
  WorkflowPosition,
} from '../types';
import { ConditionCaseEditor } from './ConditionCaseEditor';
import { TagEditor } from './TagEditor';

interface NodeInspectorProps {
  workflow: WorkflowDocument | null;
  selectedNode: WorkflowNode | null;
  skills: SkillCatalogEntry[];
  validators: ValidatorCatalogEntry[];
  readOnly: boolean;
  onUpdateWorkflow: (updater: (workflow: WorkflowDocument) => WorkflowDocument) => void;
  onUpdateNode: (nodeId: string, patch: Partial<WorkflowNode>) => void;
  onUpdateEdge: (edgeId: string, patch: Partial<WorkflowEdge>) => void;
  onConnect: (source: string, target: string, trigger: string) => void;
  onDisconnect: (edgeId: string) => void;
  onMoveNode: (nodeId: string, position: WorkflowPosition) => void;
  onDuplicateNode: (nodeId: string) => void;
  onDeleteNode: (nodeId: string) => void;
  onInsertBefore: (nodeId: string, type: 'task' | 'condition' | 'join' | 'validator') => void;
  onInsertAfter: (nodeId: string, type: 'task' | 'condition' | 'join' | 'validator') => void;
}

function sourceTriggers(node: WorkflowNode): string[] {
  if (node.type === 'condition') return [...node.condition_cases.map((item) => item.outcome), 'default'];
  if (node.type === 'validator') return ['pass', 'fail', 'blocked'];
  if (node.type === 'join') return ['succeeded'];
  return node.outcomes.length ? node.outcomes : ['succeeded'];
}

function defaultValidatorConfig(entry: ValidatorCatalogEntry | undefined, nodeId: string) {
  if (!entry) return { input_roles: {}, options: {} };
  const options = Object.fromEntries(Object.entries(entry.options ?? {}).map(([name, option]) => [name, option.choices[0] ?? ''])) as Record<string, JsonPrimitive>;
  const inputRoles = Object.fromEntries(
    Object.entries(entry.input_roles ?? {})
      .filter(([, role]) => !role.required_when || role.required_when !== 'phase=final' || options.phase === 'final')
      .map(([role]) => [role, `${nodeId}_${role}`]),
  );
  return { input_roles: inputRoles, options };
}

export function NodeInspector({
  workflow,
  selectedNode,
  skills,
  validators,
  readOnly,
  onUpdateWorkflow,
  onUpdateNode,
  onUpdateEdge,
  onConnect,
  onDisconnect,
  onMoveNode,
  onDuplicateNode,
  onDeleteNode,
  onInsertBefore,
  onInsertAfter,
}: NodeInspectorProps) {
  const [sourceId, setSourceId] = useState(selectedNode?.id ?? '');
  const [targetId, setTargetId] = useState('');
  const [insertType, setInsertType] = useState<'task' | 'condition' | 'join' | 'validator'>('task');
  const sourceNode = workflow?.nodes.find((node) => node.id === sourceId) ?? selectedNode;
  const triggers = sourceNode ? sourceTriggers(sourceNode) : [];
  const [trigger, setTrigger] = useState(triggers[0] ?? 'succeeded');

  useEffect(() => {
    setSourceId(selectedNode?.id ?? '');
    setTrigger(selectedNode ? sourceTriggers(selectedNode)[0] ?? 'succeeded' : 'succeeded');
  }, [selectedNode?.id]);

  useEffect(() => {
    if (sourceNode && !triggers.includes(trigger)) setTrigger(triggers[0] ?? 'succeeded');
  }, [sourceNode, trigger, triggers]);

  const touchingEdges = useMemo(
    () => workflow?.edges.filter((edge) => edge.source === selectedNode?.id || edge.target === selectedNode?.id) ?? [],
    [workflow, selectedNode?.id],
  );

  if (!workflow) {
    return <aside className="side-panel inspector-panel" aria-label="阶段属性"><p className="empty-state">工作流数据正在载入。</p></aside>;
  }

  function updateWorkflowSettings(patch: Partial<WorkflowDocument>) {
    onUpdateWorkflow((current) => ({ ...current, ...patch }));
  }

  function updateNode(patch: Partial<WorkflowNode>) {
    if (!selectedNode) return;
    onUpdateNode(selectedNode.id, patch);
  }

  function moveSelectedNode(dx: number, dy: number) {
    if (!workflow || !selectedNode) return;
    const position = workflow.ui.positions[selectedNode.id] ?? { x: 80, y: 80 };
    const bound = (value: number) => Math.max(-1_000_000, Math.min(1_000_000, value));
    onMoveNode(selectedNode.id, { x: bound(position.x + dx), y: bound(position.y + dy) });
  }

  function updateInputs(values: string[]) {
    if (!selectedNode) return;
    if (selectedNode.type === 'validator' && selectedNode.validator_config) {
      const keys = Object.keys(selectedNode.validator_config.input_roles);
      const roles = Object.fromEntries(keys.map((key, index) => [key, values[index] ?? `${selectedNode.id}_${key}`]));
      updateNode({ inputs: Object.values(roles), validator_config: { ...selectedNode.validator_config, input_roles: roles } });
    } else updateNode({ inputs: values });
  }

  return (
    <aside className="side-panel inspector-panel" aria-label="阶段属性">
      {selectedNode ? (
        <>
          <div className="panel-heading">
            <div><p className="eyebrow">属性编辑</p><h2>阶段设置</h2></div>
            <span className="node-type-pill">{selectedNode.type}</span>
          </div>
          <div className="inspector-scroll">
            <div className="inspector-identity">
              <span className="inspector-identity__id">{selectedNode.id}</span>
              {selectedNode.origin_projection_node_id && <span className="provenance-chip">源自官方：{selectedNode.origin_projection_node_id}</span>}
            </div>
            {!readOnly && <section className="layout-controls" aria-labelledby="layout-controls-title">
              <div className="form-section__heading"><strong id="layout-controls-title">画布位置</strong><small>每次移动 40 像素；也可使用方向键按钮调整布局。</small></div>
              <div className="layout-controls__grid" role="group" aria-label="键盘调整阶段布局">
                <span />
                <button type="button" className="icon-button" aria-label="向上移动阶段" onClick={() => moveSelectedNode(0, -40)}>↑</button>
                <span />
                <button type="button" className="icon-button" aria-label="向左移动阶段" onClick={() => moveSelectedNode(-40, 0)}>←</button>
                <span className="layout-controls__hint" aria-hidden="true">移动</span>
                <button type="button" className="icon-button" aria-label="向右移动阶段" onClick={() => moveSelectedNode(40, 0)}>→</button>
                <span />
                <button type="button" className="icon-button" aria-label="向下移动阶段" onClick={() => moveSelectedNode(0, 40)}>↓</button>
                <span />
              </div>
            </section>}
            <label className="field"><span className="field__label">阶段名称</span><input value={selectedNode.display_name} disabled={readOnly} onChange={(event) => updateNode({ display_name: event.target.value })} /></label>
            <label className="check-field check-field--card"><input type="checkbox" checked={selectedNode.enabled} disabled={readOnly} onChange={(event) => updateNode({ enabled: event.target.checked })} /><span><strong>启用此阶段</strong><small>停用后仍保留在草稿中，但不会进入执行计划。</small></span></label>
            <label className="check-field"><input type="checkbox" checked={selectedNode.entry} disabled={readOnly} onChange={(event) => updateNode({ entry: event.target.checked })} /><span>作为流程入口</span></label>

            {selectedNode.type === 'task' && (
              <label className="field">
                <span className="field__label">主要 Skill</span>
                <select
                  value={selectedNode.skill_ref ?? ''}
                  disabled={readOnly}
                  onChange={(event) => updateNode({ skill_ref: event.target.value || null })}
                >
                  <option value="">请选择已安装的 Skill</option>
                  {selectedNode.skill_ref && !skills.some((skill) => skill.catalog_id === selectedNode.skill_ref) && <option value={selectedNode.skill_ref}>缺失：{selectedNode.skill_ref}</option>}
                  {skills.filter((skill) => !skill.ambiguous).map((skill) => <option key={skill.catalog_id} value={skill.catalog_id}>{skill.display_name}{skill.locked ? ' · 已锁定' : ' · 本地版本'}</option>)}
                </select>
                <small className="field__help">每个任务阶段绑定一个主要 Skill；缺失或冲突的 Skill 无法通过验证。</small>
              </label>
            )}

            {selectedNode.type === 'validator' && (() => {
              const activeValidator = validators.find((entry) => entry.validator_id === selectedNode.validator_ref);
              const config = selectedNode.validator_config;
              const selectedAvailable = activeValidator?.available !== false;
              return (
                <div className="form-section">
                  <label className="field"><span className="field__label">内置验证器</span><select
                    value={selectedNode.validator_ref ?? ''}
                    disabled={readOnly}
                    onChange={(event) => {
                      const entry = validators.find((item) => item.validator_id === event.target.value);
                      const value = event.target.value || null;
                      if (!value) updateNode({ validator_ref: null, validator_config: null, inputs: [] });
                      else if (entry?.available === false) updateNode({ validator_ref: value, validator_config: null, inputs: [] });
                      else {
                        const nextConfig = defaultValidatorConfig(entry, selectedNode.id);
                        updateNode({ validator_ref: value, validator_config: nextConfig, inputs: Object.values(nextConfig.input_roles) });
                      }
                    }}>
                    <option value="">请选择安全的内置验证器</option>
                    {validators.map((entry) => <option key={entry.validator_id} value={entry.validator_id} disabled={entry.available === false}>{entry.validator_id}{entry.available === false ? ' · 首版不可用' : ''}</option>)}
                  </select></label>
                  {selectedNode.validator_ref === 'humanizer-preflight' && <p className="warning-inline">Humanizer Preflight 在首版自定义流程中不可用。</p>}
                  {activeValidator && config && selectedAvailable && (
                    <>
                      <div className="form-section__heading"><strong>输入角色</strong><small>每个输入都必须映射到一个上游产物。</small></div>
                      {Object.entries(activeValidator.input_roles ?? {}).filter(([role, metadata]) => Object.hasOwn(config.input_roles, role) || !metadata.required_when || metadata.required_when !== 'phase=final' || config.options.phase === 'final').map(([role, metadata]) => (
                        <label className="field" key={role}><span className="field__label">{metadata.label}</span><input value={config.input_roles[role] ?? ''} disabled={readOnly} placeholder="产物 ID" onChange={(event) => {
                          const roles = { ...config.input_roles, [role]: event.target.value };
                          if (!event.target.value) delete roles[role];
                          updateNode({ validator_config: { ...config, input_roles: roles }, inputs: Object.values(roles) });
                        }} /></label>
                      ))}
                      {Object.entries(activeValidator.options ?? {}).map(([name, option]) => (
                        <label className="field" key={name}><span className="field__label">{option.label}</span><select value={String(config.options[name] ?? '')} disabled={readOnly} onChange={(event) => {
                          const choice = option.choices.find((item) => String(item) === event.target.value);
                          if (choice === undefined) return;
                          const options = { ...config.options, [name]: choice };
                          let roles = config.input_roles;
                          if (name === 'phase') {
                            if (choice === 'final' && !Object.hasOwn(roles, 'semantic_receipt')) roles = { ...roles, semantic_receipt: `${selectedNode.id}_semantic_receipt` };
                            if (choice !== 'final' && Object.hasOwn(roles, 'semantic_receipt')) { roles = { ...roles }; delete roles.semantic_receipt; }
                          }
                          updateNode({ validator_config: { input_roles: roles, options }, inputs: Object.values(roles) });
                        }}>{option.choices.map((choice) => <option key={String(choice)} value={String(choice)}>{String(choice)}</option>)}</select></label>
                      ))}
                    </>
                  )}
                </div>
              );
            })()}

            {selectedNode.type === 'condition' && (
              <div className="form-section"><div className="form-section__heading"><strong>条件分支</strong><small>通过可视化表单创建事实判断，不需要编写表达式。</small></div><ConditionCaseEditor cases={selectedNode.condition_cases} nodeIds={workflow.nodes.map((node) => node.id)} disabled={readOnly} onChange={(condition_cases: ConditionCase[]) => updateNode({ condition_cases })} /></div>
            )}

            {selectedNode.type === 'join' && <label className="field"><span className="field__label">汇合策略</span><select value={selectedNode.join_mode} disabled={readOnly} onChange={(event) => updateNode({ join_mode: event.target.value as 'all_active' | 'any_success' })}><option value="all_active">等待所有启用分支</option><option value="any_success">任一分支成功即可</option></select><small className="field__help">“任一成功”需要每条输入连线明确映射相同的输出产物。</small></label>}

            <div className="form-section">
              <div className="form-section__heading"><strong>数据与产物</strong><small>使用产物 ID 表达阶段之间交换的内容。</small></div>
              {!(selectedNode.type === 'validator' && selectedNode.validator_config) && <TagEditor label="输入产物" values={selectedNode.inputs} disabled={readOnly} help="以逗号分隔；输入需来自上游阶段或外部输入。" onChange={updateInputs} />}
              {selectedNode.type !== 'validator' && <TagEditor label="输出产物" values={selectedNode.outputs} disabled={readOnly} onChange={(outputs) => updateNode({ outputs })} />}
              {selectedNode.type === 'task' && <TagEditor label="任务结果" values={selectedNode.outcomes} disabled={readOnly} help="下游连线依据这些结果选择路径。" onChange={(outcomes) => updateNode({ outcomes })} />}
              <TagEditor label="写入范围" values={selectedNode.write_scopes} disabled={readOnly} help="并行阶段不能同时修改重叠范围。" onChange={(write_scopes) => updateNode({ write_scopes })} />
            </div>

            {selectedNode.type !== 'condition' && selectedNode.type !== 'join' && <label className="field"><span className="field__label">失败时</span><select value={selectedNode.failure_policy} disabled={readOnly} onChange={(event) => updateNode({ failure_policy: event.target.value as 'block' | 'skip_branch' })}><option value="block">阻止后续流程</option><option value="skip_branch">跳过当前分支</option></select></label>}

            <section className="connections-section" aria-labelledby="connections-title">
              <div className="form-section__heading"><strong id="connections-title">阶段连接</strong><small>决定哪个阶段完成后进入下一步。</small></div>
              {touchingEdges.length > 0 ? <ul className="connection-list">{touchingEdges.map((edge) => {
                const from = workflow.nodes.find((item) => item.id === edge.source);
                const to = workflow.nodes.find((item) => item.id === edge.target);
                return <li key={edge.id} className="connection-item"><span><strong>{from?.display_name ?? edge.source}</strong><small>→ {to?.display_name ?? edge.target}</small></span><select aria-label={`${edge.source} 到 ${edge.target} 的触发结果`} value={edge.trigger} disabled={readOnly} onChange={(event) => onUpdateEdge(edge.id, { trigger: event.target.value })}>{[...new Set([...sourceTriggers(from ?? selectedNode), edge.trigger])].map((value) => <option key={value} value={value}>{value === 'default' ? '默认' : value}</option>)}</select><button type="button" className="icon-button" aria-label={`删除连接 ${edge.source} 到 ${edge.target}`} disabled={readOnly} onClick={() => onDisconnect(edge.id)}>×</button></li>;
              })}</ul> : <p className="empty-state empty-state--small">此阶段尚未连接其他阶段。</p>}
              <div className="connection-form">
                <label className="field"><span className="field__label">从阶段</span><select value={sourceId} disabled={readOnly} onChange={(event) => {setSourceId(event.target.value); const node = workflow.nodes.find((candidate) => candidate.id === event.target.value); setTrigger(node ? sourceTriggers(node)[0] ?? 'succeeded' : 'succeeded');}}>{workflow.nodes.map((node) => <option value={node.id} key={node.id}>{node.display_name}</option>)}</select></label>
                <label className="field"><span className="field__label">触发结果</span><select value={trigger} disabled={readOnly} onChange={(event) => setTrigger(event.target.value)}>{triggers.map((item) => <option value={item} key={item}>{item === 'default' ? '默认' : item}</option>)}</select></label>
                <label className="field"><span className="field__label">连接到</span><select value={targetId} disabled={readOnly} onChange={(event) => setTargetId(event.target.value)}><option value="">选择目标阶段</option>{workflow.nodes.filter((node) => node.id !== sourceId).map((node) => <option value={node.id} key={node.id}>{node.display_name}</option>)}</select></label>
                <button type="button" className="button button--quiet button--small" disabled={readOnly || !sourceId || !targetId} onClick={() => {onConnect(sourceId, targetId, trigger); setTargetId('');}}>添加连接</button>
              </div>
            </section>

            {!readOnly && <div className="inspector-actions">
              <label className="field field--inline"><span className="field__label">插入类型</span><select value={insertType} onChange={(event) => setInsertType(event.target.value as typeof insertType)}><option value="task">任务</option><option value="condition">条件分支</option><option value="join">汇合</option><option value="validator">验证器</option></select></label>
              <button type="button" className="text-button" onClick={() => onInsertBefore(selectedNode.id, insertType)}>插入之前</button>
              <button type="button" className="text-button" onClick={() => onInsertAfter(selectedNode.id, insertType)}>插入之后</button>
              <button type="button" className="text-button" onClick={() => onDuplicateNode(selectedNode.id)}>复制阶段</button>
              <button type="button" className="text-button text-button--danger" onClick={() => onDeleteNode(selectedNode.id)}>删除阶段</button>
            </div>}
          </div>
        </>
      ) : (
        <>
          <div className="panel-heading"><div><p className="eyebrow">流程属性</p><h2>流程设置</h2></div></div>
          <div className="inspector-scroll">
            <p className="muted-note">选择阶段以编辑属性。流程设置影响并行执行与用户提供的输入。</p>
            <label className="field"><span className="field__label">流程标识</span><input value={workflow.workflow_id} disabled={readOnly} onChange={(event) => updateWorkflowSettings({ workflow_id: event.target.value })} /><small className="field__help">使用小写字母、数字、连字符或下划线。</small></label>
            <label className="field"><span className="field__label">最大并行阶段数</span><input type="number" min={1} max={64} value={workflow.max_parallelism} disabled={readOnly} onChange={(event) => updateWorkflowSettings({ max_parallelism: Number(event.target.value) })} /></label>
            <TagEditor label="外部输入" values={workflow.external_inputs} disabled={readOnly} help="声明由使用者在工作流启动时提供的产物 ID；这里不会浏览或登记文件。" onChange={(external_inputs) => updateWorkflowSettings({ external_inputs })} />
            {readOnly && <p className="notice-box">这是官方 v1.0 工作流的只读结构预览。复制为自定义流程后，可在不改变默认流程的前提下编辑。</p>}
          </div>
        </>
      )}
    </aside>
  );
}
