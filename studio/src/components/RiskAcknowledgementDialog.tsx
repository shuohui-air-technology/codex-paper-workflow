import { useRef } from 'react';
import type { WorkflowIssue } from '../types';
import { useModalFocusTrap } from './useModalFocusTrap';

interface RiskAcknowledgementDialogProps {
  requiredCodes: string[];
  warnings: WorkflowIssue[];
  acknowledgedCodes: string[];
  busy: boolean;
  onToggle: (code: string, checked: boolean) => void;
  onBack: () => void;
  onActivate: () => void;
}

export function RiskAcknowledgementDialog({
  requiredCodes,
  warnings,
  acknowledgedCodes,
  busy,
  onToggle,
  onBack,
  onActivate,
}: RiskAcknowledgementDialogProps) {
  const allAcknowledged = requiredCodes.every((code) => acknowledgedCodes.includes(code));
  const dialogRef = useRef<HTMLElement>(null);
  useModalFocusTrap(true, dialogRef, () => { if (!busy) onBack(); });

  return (
    <div className="modal-backdrop">
      <section ref={dialogRef} className="modal-card risk-dialog" role="dialog" aria-modal="true" aria-labelledby="risk-dialog-title" aria-describedby="risk-dialog-description" tabIndex={-1}>
        <p className="eyebrow">启用前的风险确认</p>
        <h2 id="risk-dialog-title">请逐项确认流程变更</h2>
        <p id="risk-dialog-description" className="modal-intro">下列项目来自当前已保存版本的服务端检查。只有逐项确认后，才能启用这个自定义流程。</p>
        <div className="risk-list">
          {requiredCodes.map((code) => {
            const related = warnings.filter((warning) => warning.code === code);
            const checked = acknowledgedCodes.includes(code);
            return (
              <label className="risk-item" key={code}>
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={busy}
                  aria-label={`${related[0]?.message ?? code}（${code}）`}
                  onChange={(event) => onToggle(code, event.target.checked)}
                />
                <span className="risk-item__copy">
                  <strong>{related[0]?.message ?? '需要确认此流程检查项'}</strong>
                  <code>{code}</code>
                  {related.some((item) => item.node_id) && (
                    <small>相关阶段：{[...new Set(related.map((item) => item.node_id).filter(Boolean))].join('、')}</small>
                  )}
                  {related.map((item, index) => item.recovery && (
                    <small key={`${item.node_id}-${index}`}>建议：{item.recovery}</small>
                  ))}
                </span>
              </label>
            );
          })}
          {requiredCodes.length === 0 && <p className="muted-note">当前版本没有需要逐项确认的风险代码。</p>}
        </div>
        <div className="modal-actions">
          <button type="button" className="button button--quiet" disabled={busy} onClick={onBack}>返回编辑</button>
          <button type="button" className="button button--primary" disabled={busy || !allAcknowledged} onClick={onActivate}>
            {busy ? '正在启用…' : '启用自定义流程'}
          </button>
        </div>
      </section>
    </div>
  );
}
