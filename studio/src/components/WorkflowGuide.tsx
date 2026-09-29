interface WorkflowGuideProps {
  editable: boolean;
  active: boolean;
  dirty: boolean;
  checked: boolean;
  hintCount: number;
}

export function WorkflowGuide({ editable, active, dirty, checked, hintCount }: WorkflowGuideProps) {
  if (!editable) return <div className="workflow-guide workflow-guide--welcome">
    <strong>从现有流程开始编排</strong>
    <span>复制官方流程可保留已有阶段与连接；空白流程适合从少量阶段开始。日常研究可直接在 Codex 中使用默认流程。</span>
  </div>;
  const current = active ? 3 : checked ? 2 : 1;
  return <nav className="workflow-guide" aria-label="自定义流程使用步骤">
    <ol>
      {['编排与配置', '检查并启用', '回到 Codex 执行'].map((label, index) => <li key={label} aria-current={current === index + 1 ? 'step' : undefined} className={current > index + 1 ? 'is-complete' : ''}><span>{index + 1}</span>{label}</li>)}
    </ol>
    <p>{active ? '已启用；后续编辑保存在草稿中。' : hintCount > 0 ? `有 ${hintCount} 项配置提示，可在「流程检查」中定位。` : checked ? '检查已完成，可点击「验证并启用」继续。' : dirty ? '先选择阶段和 Skill，再检查连接；可随时保存草稿。' : '草稿已保存，点击「验证流程」检查配置。'}</p>
  </nav>;
}
