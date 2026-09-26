import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { ConditionCaseEditor } from './ConditionCaseEditor';
import type { ConditionCase, JsonValue } from '../types';

afterEach(() => cleanup());

function decisionCase(value: JsonValue): ConditionCase[] {
  return [{ outcome: 'selected', when: { op: 'decision_is', name: 'choice', value } }];
}

function ControlledEditor({ initialCases, onUpdate = vi.fn() }: { initialCases: ConditionCase[]; onUpdate?: (cases: ConditionCase[]) => void }) {
  const [cases, setCases] = useState(initialCases);
  return <ConditionCaseEditor cases={cases} nodeIds={[]} disabled={false} onChange={(next) => { setCases(next); onUpdate(next); }} />;
}

describe('ConditionCaseEditor', () => {
  it.each([
    ['text', 'draft', 'string'],
    ['number', 2.5, 'number'],
    ['boolean', false, 'boolean'],
    ['null', null, 'null'],
  ] as const)('preserves the JSON %s scalar while editing another field', async (_label, value, type) => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ControlledEditor initialCases={decisionCase(value)} onUpdate={onChange} />);
    expect(screen.getByLabelText('决策值类型')).toHaveValue(type);
    const name = screen.getByLabelText('对象名称');
    await user.clear(name);
    await user.type(name, 'reviewer_choice');
    const latest = onChange.mock.calls.at(-1)?.[0] as ConditionCase[];
    expect(latest[0]?.when.value).toBe(value);
  });

  it('writes the selected decision scalar type without coercing it to text', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ControlledEditor initialCases={decisionCase('draft')} onUpdate={onChange} />);
    await user.selectOptions(screen.getByLabelText('决策值类型'), 'number');
    fireEvent.change(screen.getByLabelText('决策比较值'), { target: { value: '42' } });
    const latest = onChange.mock.calls.at(-1)?.[0] as ConditionCase[];
    expect(latest[0]?.when.value).toBe(42);
    expect(typeof latest[0]?.when.value).toBe('number');
  });

  it('keeps the active branch-name input focused as its editable key changes', async () => {
    const user = userEvent.setup();
    render(<ControlledEditor initialCases={[{ outcome: 'selected', when: { op: 'fact_is', name: 'ready', value: true } }]} />);
    const name = screen.getByLabelText('分支名称');
    await user.type(name, 'x');
    expect(name).toHaveFocus();
  });
});
