import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NodeInspector } from './NodeInspector';
import { createBlankWorkflow } from '../workflow';

afterEach(() => cleanup());

describe('NodeInspector keyboard layout controls', () => {
  it('moves a selected stage using accessible buttons without dragging the canvas', async () => {
    const user = userEvent.setup();
    const workflow = createBlankWorkflow('keyboard-layout');
    const node = workflow.nodes[0]!;
    const onMoveNode = vi.fn();
    render(<NodeInspector
      workflow={workflow}
      selectedNode={node}
      skills={[]}
      validators={[]}
      readOnly={false}
      onUpdateWorkflow={vi.fn()}
      onUpdateNode={vi.fn()}
      onUpdateEdge={vi.fn()}
      onConnect={vi.fn()}
      onDisconnect={vi.fn()}
      onMoveNode={onMoveNode}
      onDuplicateNode={vi.fn()}
      onDeleteNode={vi.fn()}
      onInsertBefore={vi.fn()}
      onInsertAfter={vi.fn()}
    />);

    await user.click(screen.getByRole('button', { name: '向右移动阶段' }));
    expect(onMoveNode).toHaveBeenCalledWith(node.id, { x: 220, y: 140 });
  });
});
