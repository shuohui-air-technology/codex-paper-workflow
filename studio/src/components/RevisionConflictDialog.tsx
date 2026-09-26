interface RevisionConflictDialogProps {
  open: boolean;
  busy: boolean;
  onLoadLatest: () => void;
  onDownloadDraft: () => void;
  onKeepOpen: () => void;
}

export function RevisionConflictDialog({
  open,
  busy,
  onLoadLatest,
  onDownloadDraft,
  onKeepOpen,
}: RevisionConflictDialogProps) {
  const dialogRef = useRef<HTMLElement>(null);
  useModalFocusTrap(open, dialogRef, () => { if (!busy) onKeepOpen(); });
  if (!open) return null;
  return (
    <div className="modal-backdrop">
      <section ref={dialogRef} className="modal-card conflict-dialog" role="dialog" aria-modal="true" aria-labelledby="conflict-dialog-title" aria-describedby="conflict-dialog-description" tabIndex={-1}>
        <p className="eyebrow">版本冲突</p>
        <h2 id="conflict-dialog-title">服务器中的流程状态已经变化</h2>
        <p id="conflict-dialog-description" className="modal-intro">文档版本或检查结果与当前预览不一致。为保护双方内容，工作流不会覆盖服务器内容；保存和启用已暂时锁定，请选择如何继续。</p>
        <div className="conflict-options">
          <button type="button" className="conflict-option" disabled={busy} onClick={onLoadLatest}>
            <strong>载入服务器最新草稿</strong>
            <span>放弃当前浏览器中的修改，并以服务器版本继续。</span>
          </button>
          <button type="button" className="conflict-option" disabled={busy} onClick={onDownloadDraft}>
            <strong>下载当前草稿备份</strong>
            <span>把浏览器中的当前内容保存为本地 JSON 文件；不会写入服务器。</span>
          </button>
          <button type="button" className="conflict-option" disabled={busy} onClick={onKeepOpen}>
            <strong>继续保留当前草稿</strong>
            <span>留在当前编辑器中；在重新载入服务器版本前，保存和启用仍不可用。</span>
          </button>
        </div>
      </section>
    </div>
  );
}
import { useRef } from 'react';
import { useModalFocusTrap } from './useModalFocusTrap';
