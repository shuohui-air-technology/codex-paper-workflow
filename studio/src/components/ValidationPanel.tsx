import type { WorkflowIssue } from '../types';
import type { LocalWorkflowHint } from '../workflow';

interface ValidationPanelProps {
  errors: WorkflowIssue[];
  warnings: WorkflowIssue[];
  projectionNotes: string[];
  lastValidated: boolean;
  busy: boolean;
  advisoryHints: LocalWorkflowHint[];
  onLocateIssue?: (issue: { node_id?: string; edge_id?: string }) => void;
}

export function ValidationPanel({ errors, warnings, projectionNotes, lastValidated, busy, advisoryHints, onLocateIssue }: ValidationPanelProps) {
  const hasResults = lastValidated || errors.length > 0 || warnings.length > 0 || advisoryHints.length > 0;
  const waitingForServer = !lastValidated && !busy && (errors.length > 0 || warnings.length > 0 || advisoryHints.length > 0);
  return (
    <section className="validation-panel" aria-labelledby="validation-title" aria-live="polite">
      <div className="validation-panel__header">
        <div>
          <p className="eyebrow">检查与风险</p>
          <h2 id="validation-title">流程检查</h2>
        </div>
        {busy ? <span className="validation-status validation-status--busy">正在检查…</span> : lastValidated ? (
          errors.length ? <span className="validation-status validation-status--error">{errors.length} 项需修正</span>
            : warnings.length ? <span className="validation-status validation-status--warning">检查完成 · 有风险提示</span>
              : <span className="validation-status validation-status--ok">服务端验证通过</span>
        ) : waitingForServer ? <span className="validation-status validation-status--warning">尚待服务端验证</span> : <span className="validation-status">尚未验证</span>}
      </div>
      {!hasResults && projectionNotes.length === 0 && <p className="muted-note">先编辑流程，再运行验证。只有服务端验证通过后，流程才具备启用条件。</p>}
      {waitingForServer && <p className="muted-note">当前内容包含编辑器提示或上次操作信息；请运行服务端验证，以确认正在编辑的版本。</p>}
      {errors.length > 0 && (
        <div className="issue-group issue-group--error">
          <h3>需要修正</h3>
          <ul>{errors.map((issue, index) => <li key={`${issue.code}-${issue.node_id}-${index}`}><strong>{issue.message}</strong><span>{issue.recovery}</span>{onLocateIssue && <button type="button" className="text-button issue-locate" onClick={() => onLocateIssue(issue)}>{issue.node_id || issue.edge_id ? '定位相关阶段' : '查看流程设置'}</button>}</li>)}</ul>
        </div>
      )}
      {warnings.length > 0 && (
        <div className="issue-group issue-group--warning">
          <h3>风险提示</h3>
          <ul>{warnings.map((issue, index) => <li key={`${issue.code}-${issue.node_id}-${index}`}><strong>{issue.message}</strong><span>{issue.recovery}</span>{onLocateIssue && (issue.node_id || issue.edge_id) && <button type="button" className="text-button issue-locate" onClick={() => onLocateIssue(issue)}>定位相关阶段</button>}</li>)}</ul>
        </div>
      )}
      {advisoryHints.length > 0 && (
        <div className="issue-group issue-group--note">
          <h3>编辑提示（仅供参考）</h3>
          <p className="muted-note">这些提示由编辑器快速检查生成，不会替代服务端验证，也不会单独阻止保存或启用。</p>
          <ul>{advisoryHints.map((hint) => <li key={`${hint.code}-${hint.node_id}`}><strong>{hint.message}</strong>{hint.node_id && <span>阶段：{hint.node_id}</span>}{onLocateIssue && <button type="button" className="text-button issue-locate" onClick={() => onLocateIssue(hint)}>{hint.node_id ? '配置此阶段' : '查看流程设置'}</button>}</li>)}</ul>
        </div>
      )}
      {projectionNotes.length > 0 && (
        <div className="issue-group issue-group--note">
          <h3>官方流程映射说明</h3>
          <ul>{projectionNotes.map((note, index) => <li key={`${index}-${note}`}><span>{note}</span></li>)}</ul>
        </div>
      )}
    </section>
  );
}
