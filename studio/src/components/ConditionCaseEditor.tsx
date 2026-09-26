import type { ConditionCase, JsonValue } from '../types';

type LeafOp = 'fact_is' | 'decision_is' | 'artifact_state_is' | 'outcome_is' | 'status_is';
type Predicate = { op: LeafOp; name?: string; node?: string; artifact?: string; value: JsonValue };
type Expression = Predicate | { op: 'not'; arg: Predicate } | { op: 'all' | 'any'; args: Array<Predicate | { op: 'not'; arg: Predicate }> };
type PredicateSource = 'fact_is' | 'decision_is' | 'artifact_state_is' | 'outcome_is' | 'status_is';

const SOURCE_LABEL: Record<PredicateSource, string> = {
  fact_is: '已记录的事实',
  decision_is: '用户决策',
  artifact_state_is: '产物状态',
  outcome_is: '阶段结果',
  status_is: '阶段状态',
};

interface PredicateDraft {
  source: PredicateSource;
  reference: string;
  value: string;
  valueType: 'string' | 'number' | 'boolean' | 'null';
  negate: boolean;
}

function parsePredicate(value: unknown): PredicateDraft | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const expression = value as Record<string, unknown>;
  if (expression.op === 'not') {
    const child = parsePredicate(expression.arg);
    return child ? { ...child, negate: true } : null;
  }
  if (!['fact_is', 'decision_is', 'artifact_state_is', 'outcome_is', 'status_is'].includes(String(expression.op))) return null;
  const source = expression.op as PredicateSource;
  const reference = String(expression.name ?? expression.node ?? expression.artifact ?? '');
  const rawValue = expression.value;
  if (source === 'decision_is') {
    if (rawValue === null) return { source, reference, value: '', valueType: 'null', negate: false };
    if (typeof rawValue === 'string') return { source, reference, value: rawValue, valueType: 'string', negate: false };
    if (typeof rawValue === 'boolean') return { source, reference, value: String(rawValue), valueType: 'boolean', negate: false };
    if (typeof rawValue === 'number' && Number.isFinite(rawValue)) return { source, reference, value: String(rawValue), valueType: 'number', negate: false };
    return null;
  }
  if (source === 'fact_is') {
    if (typeof rawValue !== 'boolean') return null;
    return { source, reference, value: String(rawValue), valueType: 'boolean', negate: false };
  }
  if (typeof rawValue !== 'string') return null;
  return { source, reference, value: rawValue, valueType: 'string', negate: false };
}

function parseExpression(value: unknown): { join: 'all' | 'any'; predicates: PredicateDraft[] } | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const expression = value as Record<string, unknown>;
  if (expression.op === 'all' || expression.op === 'any') {
    if (!Array.isArray(expression.args)) return null;
    const predicates = expression.args.map(parsePredicate);
    if (predicates.some((item) => item === null)) return null;
    return { join: expression.op, predicates: predicates as PredicateDraft[] };
  }
  const predicate = parsePredicate(value);
  return predicate ? { join: 'all', predicates: [predicate] } : null;
}

function makePredicate(draft: PredicateDraft): Predicate | { op: 'not'; arg: Predicate } {
  const referenceField = draft.source === 'artifact_state_is' ? 'artifact' : draft.source === 'outcome_is' || draft.source === 'status_is' ? 'node' : 'name';
  let value: JsonValue;
  if (draft.source === 'fact_is' || (draft.source === 'decision_is' && draft.valueType === 'boolean')) {
    value = draft.value === 'true';
  } else if (draft.source === 'decision_is' && draft.valueType === 'null') {
    value = null;
  } else if (draft.source === 'decision_is' && draft.valueType === 'number') {
    const number = Number(draft.value);
    if (!draft.value.trim() || !Number.isFinite(number)) throw new Error('决策值必须是有限数字。');
    value = number;
  } else {
    value = draft.value;
  }
  const leaf = { op: draft.source, [referenceField]: draft.reference || 'name', value } as Predicate;
  return draft.negate ? { op: 'not', arg: leaf } : leaf;
}

interface ConditionCaseEditorProps {
  cases: ConditionCase[];
  nodeIds: string[];
  disabled: boolean;
  onChange: (cases: ConditionCase[]) => void;
}

export function ConditionCaseEditor({ cases, nodeIds, disabled, onChange }: ConditionCaseEditorProps) {
  function updateCase(index: number, updated: ConditionCase) {
    onChange(cases.map((item, itemIndex) => itemIndex === index ? updated : item));
  }

  return (
    <div className="condition-case-list">
      {cases.map((item, index) => {
        const parsed = parseExpression(item.when);
        if (!parsed) {
          return (
            <div className="condition-card" key={`case-${index}`}>
              <div className="condition-card__header"><strong>分支 {index + 1}</strong><button type="button" className="text-button" disabled={disabled} onClick={() => onChange(cases.filter((_case, caseIndex) => caseIndex !== index))}>移除</button></div>
              <p className="warning-inline">此条件使用当前表单暂不支持的组合结构。删除并重新创建后可用图形表单编辑。</p>
            </div>
          );
        }
        const setPredicates = (predicates: PredicateDraft[]) => {
          const expressions = predicates.map(makePredicate);
          const when: Expression = expressions.length === 1 ? expressions[0]! : { op: parsed.join, args: expressions };
          updateCase(index, { ...item, when: when as unknown as Record<string, JsonValue> });
        };
        return (
          <div className="condition-card" key={`case-${index}`}>
            <div className="condition-card__header">
              <strong>分支 {index + 1}</strong>
              <button type="button" className="text-button" disabled={disabled} onClick={() => onChange(cases.filter((_case, caseIndex) => caseIndex !== index))}>移除</button>
            </div>
            <label className="field"><span className="field__label">分支名称</span><input value={item.outcome} disabled={disabled} onChange={(event) => updateCase(index, { ...item, outcome: event.target.value })} /></label>
            {parsed.predicates.map((predicate, predicateIndex) => (
              <div className="predicate-row" key={`predicate-${predicateIndex}`}>
                <label className="field"><span className="field__label">判断对象</span><select value={predicate.source} disabled={disabled} onChange={(event) => setPredicates(parsed.predicates.map((current, currentIndex) => {
                  if (currentIndex !== predicateIndex) return current;
                  const source = event.target.value as PredicateSource;
                  const valueType = source === 'fact_is' ? 'boolean' : source === 'decision_is' && current.valueType === 'boolean' ? 'boolean' : source === 'decision_is' && current.valueType === 'number' ? 'number' : source === 'decision_is' && current.valueType === 'null' ? 'null' : 'string';
                  const value = valueType === 'boolean' ? 'true' : valueType === 'number' ? '0' : valueType === 'null' ? '' : current.value;
                  return { ...current, source, reference: '', value, valueType };
                }))}>{Object.entries(SOURCE_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
                <label className="field"><span className="field__label">对象名称</span><input list={`node-options-${index}-${predicateIndex}`} value={predicate.reference} disabled={disabled} onChange={(event) => setPredicates(parsed.predicates.map((current, currentIndex) => currentIndex === predicateIndex ? { ...current, reference: event.target.value } : current))} placeholder="例如 ready" /><datalist id={`node-options-${index}-${predicateIndex}`}>{nodeIds.map((nodeId) => <option key={nodeId} value={nodeId} />)}</datalist></label>
                <div className="field">
                  <span className="field__label">等于</span>
                  {predicate.source === 'fact_is' ? <select aria-label="判断值" value={predicate.value} disabled={disabled} onChange={(event) => setPredicates(parsed.predicates.map((current, currentIndex) => currentIndex === predicateIndex ? { ...current, valueType: 'boolean', value: event.target.value } : current))}><option value="true">是</option><option value="false">否</option></select>
                    : predicate.source === 'decision_is' ? <div className="decision-value-editor">
                      <select aria-label="决策值类型" value={predicate.valueType} disabled={disabled} onChange={(event) => setPredicates(parsed.predicates.map((current, currentIndex) => {
                        if (currentIndex !== predicateIndex) return current;
                        const valueType = event.target.value as PredicateDraft['valueType'];
                        const value = valueType === 'boolean' ? 'true' : valueType === 'number' ? '0' : valueType === 'null' ? '' : current.value;
                        return { ...current, valueType, value };
                      }))}>
                        <option value="string">文字</option><option value="number">数字</option><option value="boolean">是／否</option><option value="null">空值（null）</option>
                      </select>
                      {predicate.valueType === 'boolean' ? <select aria-label="决策比较值" value={predicate.value} disabled={disabled} onChange={(event) => setPredicates(parsed.predicates.map((current, currentIndex) => currentIndex === predicateIndex ? { ...current, value: event.target.value } : current))}><option value="true">是</option><option value="false">否</option></select>
                        : predicate.valueType === 'null' ? <code className="decision-null-value">null</code>
                          : <input aria-label="决策比较值" type={predicate.valueType === 'number' ? 'number' : 'text'} step={predicate.valueType === 'number' ? 'any' : undefined} value={predicate.value} disabled={disabled} onChange={(event) => {
                            const entered = event.target.value;
                            setPredicates(parsed.predicates.map((current, currentIndex) => {
                              if (currentIndex !== predicateIndex) return current;
                              if (current.valueType === 'number' && entered.trim() && !Number.isFinite(Number(entered))) return current;
                              return { ...current, value: entered };
                            }));
                          }} placeholder={predicate.valueType === 'number' ? '0' : '输入文字'} />}
                    </div>
                      : <input aria-label="判断值" value={predicate.value} disabled={disabled} onChange={(event) => setPredicates(parsed.predicates.map((current, currentIndex) => currentIndex === predicateIndex ? { ...current, valueType: 'string', value: event.target.value } : current))} placeholder={predicate.source === 'status_is' ? 'succeeded' : '结果值'} />}
                </div>
                <label className="check-field"><input type="checkbox" checked={predicate.negate} disabled={disabled} onChange={(event) => setPredicates(parsed.predicates.map((current, currentIndex) => currentIndex === predicateIndex ? { ...current, negate: event.target.checked } : current))} /><span>取反</span></label>
                {parsed.predicates.length > 1 && <button type="button" className="icon-button predicate-remove" aria-label={`移除判断 ${predicateIndex + 1}`} disabled={disabled} onClick={() => setPredicates(parsed.predicates.filter((_current, currentIndex) => currentIndex !== predicateIndex))}>×</button>}
              </div>
            ))}
            <div className="condition-card__footer">
              <label className="field field--inline"><span className="field__label">组合方式</span><select value={parsed.join} disabled={disabled} onChange={(event) => {const join = event.target.value as 'all' | 'any'; const exprs = parsed.predicates.map(makePredicate); const when: Expression = exprs.length === 1 ? exprs[0]! : { op: join, args: exprs }; updateCase(index, { ...item, when: when as unknown as Record<string, JsonValue> });}}><option value="all">全部满足（AND）</option><option value="any">任一满足（OR）</option></select></label>
              <button type="button" className="text-button" disabled={disabled} onClick={() => setPredicates([...parsed.predicates, { source: 'fact_is', reference: '', value: 'true', valueType: 'boolean', negate: false }])}>＋ 添加判断</button>
            </div>
          </div>
        );
      })}
      <button type="button" className="button button--quiet button--small" disabled={disabled} onClick={() => onChange([...cases, { outcome: `branch_${cases.length + 1}`, when: { op: 'fact_is', name: 'ready', value: true } }])}>＋ 添加条件分支</button>
      <p className="field__help">没有命中任何条件时，请在画布中为“默认”出口连接后续阶段。</p>
    </div>
  );
}
