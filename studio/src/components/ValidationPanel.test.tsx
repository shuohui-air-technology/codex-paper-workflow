import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { ValidationPanel } from './ValidationPanel';

afterEach(() => cleanup());

describe('ValidationPanel status', () => {
  it('does not present local hints as completed server validation', () => {
    render(<ValidationPanel errors={[]} warnings={[]} projectionNotes={[]} lastValidated={false} busy={false} advisoryHints={[{ code: 'local.task_unbound', message: '尚未绑定 Skill。', node_id: 'step-1' }]} />);
    expect(screen.getByText('尚待服务端验证')).toBeInTheDocument();
    expect(screen.getByText(/请运行服务端验证/)).toBeInTheDocument();
    expect(screen.queryByText('服务端验证通过')).not.toBeInTheDocument();
  });

  it('shows success only after an authoritative server validation', () => {
    render(<ValidationPanel errors={[]} warnings={[]} projectionNotes={[]} lastValidated={true} busy={false} advisoryHints={[]} />);
    expect(screen.getByText('服务端验证通过')).toBeInTheDocument();
  });
});
