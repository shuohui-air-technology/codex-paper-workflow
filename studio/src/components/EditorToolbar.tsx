interface EditorToolbarProps {
  editable: boolean;
  busy: boolean;
  canUndo: boolean;
  canRedo: boolean;
  settingsSelected: boolean;
  onUndo: () => void;
  onRedo: () => void;
  onSettings: () => void;
  onArrange: () => void;
}

export function EditorToolbar(props: EditorToolbarProps) {
  return <div className="editor-toolbar" role="group" aria-label="编辑工具">
    <button type="button" className="button button--quiet button--small" aria-pressed={props.settingsSelected} onClick={props.onSettings}>流程设置</button>
    {props.editable && <>
      <span className="toolbar-divider" />
      <button type="button" className="button button--quiet button--small" onClick={props.onUndo} disabled={props.busy || !props.canUndo} title="撤销本次保存前的编辑（⌘/Ctrl + Z）">↶ 撤销</button>
      <button type="button" className="button button--quiet button--small" onClick={props.onRedo} disabled={props.busy || !props.canRedo} title="重做（⌘/Ctrl + Shift + Z）">↷ 重做</button>
      <button type="button" className="button button--quiet button--small" onClick={props.onArrange} disabled={props.busy} title="按阶段依赖整理位置，保留连接与执行顺序">自动整理画布</button>
      <small>撤销记录保留至下次保存</small>
    </>}
  </div>;
}
