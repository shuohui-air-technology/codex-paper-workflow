import { useEffect, useState } from 'react';

interface TagEditorProps {
  label: string;
  values: string[];
  disabled?: boolean;
  help?: string;
  onChange: (values: string[]) => void;
}

export function TagEditor({ label, values, disabled = false, help, onChange }: TagEditorProps) {
  const [text, setText] = useState(values.join(', '));
  useEffect(() => setText(values.join(', ')), [values]);

  function commit() {
    const next = [...new Set(text.split(',').map((value) => value.trim()).filter(Boolean))];
    onChange(next);
    setText(next.join(', '));
  }

  return (
    <label className="field tag-editor">
      <span className="field__label">{label}</span>
      <input
        value={text}
        disabled={disabled}
        onChange={(event) => setText(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); commit(); } }}
        placeholder="例如：research_brief, citations"
        aria-label={label}
      />
      {help && <small className="field__help">{help}</small>}
      <span className="tag-list" aria-label={`${label}列表`}>
        {values.map((value) => <span className="tag" key={value}>{value}</span>)}
        {values.length === 0 && <span className="tag-list__empty">尚未添加</span>}
      </span>
    </label>
  );
}
