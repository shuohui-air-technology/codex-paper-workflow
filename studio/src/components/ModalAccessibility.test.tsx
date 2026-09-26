import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { RiskAcknowledgementDialog } from './RiskAcknowledgementDialog';
import { RevisionConflictDialog } from './RevisionConflictDialog';

afterEach(() => cleanup());

function RiskDialogHarness() {
  const [open, setOpen] = useState(false);
  return <>
    <button type="button" onClick={() => setOpen(true)}>打开风险确认</button>
    {open && <RiskAcknowledgementDialog requiredCodes={['risk.one']} warnings={[]} acknowledgedCodes={[]} busy={false} onToggle={vi.fn()} onBack={() => setOpen(false)} onActivate={vi.fn()} />}
  </>;
}

describe('modal keyboard access', () => {
  it('traps focus and maps Escape to returning from risk acknowledgement', async () => {
    const user = userEvent.setup();
    const onBack = vi.fn();
    render(<RiskAcknowledgementDialog requiredCodes={['risk.one']} warnings={[]} acknowledgedCodes={[]} busy={false} onToggle={vi.fn()} onBack={onBack} onActivate={vi.fn()} />);
    const checkbox = screen.getByRole('checkbox');
    const back = screen.getByRole('button', { name: '返回编辑' });
    expect(checkbox).toHaveFocus();
    await user.tab({ shift: true });
    expect(back).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(onBack).toHaveBeenCalledOnce();
  });

  it('maps Escape in the revision dialog to keeping the local draft locked', async () => {
    const user = userEvent.setup();
    const onKeepOpen = vi.fn();
    render(<RevisionConflictDialog open={true} busy={false} onLoadLatest={vi.fn()} onDownloadDraft={vi.fn()} onKeepOpen={onKeepOpen} />);
    expect(screen.getByRole('button', { name: /载入服务器最新草稿/ })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(onKeepOpen).toHaveBeenCalledOnce();
  });

  it('restores focus to the control that opened the risk dialog', async () => {
    const user = userEvent.setup();
    render(<RiskDialogHarness />);
    const opener = screen.getByRole('button', { name: '打开风险确认' });
    await user.click(opener);
    expect(screen.getByRole('checkbox')).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(opener).toHaveFocus();
  });
});
