import type { BootstrapData } from '../types';

interface StudioHeaderProps {
  bootstrap: BootstrapData;
  editable: boolean;
  dirty: boolean;
  hasSavedDraft: boolean;
  busy: boolean;
  conflictLocked: boolean;
  onCopyOfficial: () => void;
  onNewBlank: () => void;
  onOpenSavedDraft: () => void;
  onValidate: () => void;
  onSave: () => void;
  onValidateActivate: () => void;
  onDeactivate: () => void;
}

export function StudioHeader({
  bootstrap,
  editable,
  dirty,
  hasSavedDraft,
  busy,
  conflictLocked,
  onCopyOfficial,
  onNewBlank,
  onOpenSavedDraft,
  onValidate,
  onSave,
  onValidateActivate,
  onDeactivate,
}: StudioHeaderProps) {
  return (
    <header className="studio-header">
      <div className="studio-brand">
        <span className="studio-brand__mark" aria-hidden="true">P</span>
        <div>
          <p className="eyebrow">PAPER WORKFLOW ORCHESTRATOR</p>
          <h1>工作流编排器</h1>
        </div>
      </div>
      <div className="studio-header__context">
        <span className="project-label" title={bootstrap.project_label}>项目：{bootstrap.project_label}</span>
        <span className={`mode-badge${bootstrap.mode === 'custom' ? ' mode-badge--custom' : ''}`}>
          {bootstrap.mode === 'custom' ? '自定义流程已启用' : '官方流程 v1.0'}
        </span>
        {editable && bootstrap.mode === 'official' && (
          <span className="draft-editing-label">正在编辑自定义草稿</span>
        )}
        {!editable && <span className="readonly-label">只读预览</span>}
      </div>
      <div className="studio-header__actions" aria-label="工作流操作">
        {!editable ? (
          <>
            {hasSavedDraft && <button type="button" className="button button--quiet" onClick={onOpenSavedDraft}>打开已保存草稿</button>}
            <button type="button" className="button button--primary" onClick={onCopyOfficial}>复制为自定义流程</button>
            <button type="button" className="button button--quiet" onClick={onNewBlank}>新建空白流程</button>
          </>
        ) : (
          <>
            <span className={`save-indicator${dirty ? ' is-dirty' : ''}`} aria-live="polite">
              {conflictLocked ? '版本冲突：保存已锁定' : dirty ? '有未保存修改' : '草稿已保存'}
            </span>
            <button type="button" className="button button--quiet" onClick={onValidate} disabled={busy}>验证流程</button>
            <button type="button" className="button button--quiet" onClick={onSave} disabled={busy || !dirty || conflictLocked}>保存草稿</button>
            {bootstrap.mode === 'custom'
              ? <button type="button" className="button button--primary" onClick={onDeactivate} disabled={busy}>切回官方流程</button>
              : <button type="button" className="button button--primary" onClick={onValidateActivate} disabled={busy || conflictLocked || !hasSavedDraft && !dirty}>验证并启用</button>}
          </>
        )}
      </div>
    </header>
  );
}
