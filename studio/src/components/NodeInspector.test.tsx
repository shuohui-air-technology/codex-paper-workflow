import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NodeInspector } from './NodeInspector';
import { addNode, connectNodes, createBlankWorkflow, updateNode } from '../workflow';
import type { ValidatorCatalogEntry, WorkflowDocument, WorkflowNode } from '../types';

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

    expect(screen.getByText('高级设置').closest('details')).not.toHaveAttribute('open');
    await user.click(screen.getByText('高级设置'));
    await user.click(screen.getByRole('button', { name: '向右移动阶段' }));
    expect(onMoveNode).toHaveBeenCalledWith(node.id, { x: 220, y: 140 });
  });
});

function inspectorProps(workflow: WorkflowDocument, selectedNode: WorkflowNode | null = workflow.nodes[0]!) {
  return {
    workflow, selectedNode, skills: [], validators: [], readOnly: false,
    onUpdateWorkflow: vi.fn(), onUpdateNode: vi.fn(), onUpdateEdge: vi.fn(),
    onConnect: vi.fn(), onDisconnect: vi.fn(), onMoveNode: vi.fn(),
    onDuplicateNode: vi.fn(), onDeleteNode: vi.fn(), onInsertBefore: vi.fn(), onInsertAfter: vi.fn(),
  };
}

describe('NodeInspector guided editing', () => {
  it('allows clearing and typing a hyphenated workflow identifier before applying it on blur', async () => {
    const user = userEvent.setup();
    const workflow = createBlankWorkflow('old-name');
    const props = inspectorProps(workflow, null);
    render(<NodeInspector {...props} />);

    const identifier = screen.getByLabelText('流程标识');
    await user.clear(identifier);
    await user.type(identifier, 'introduction-review');
    expect(identifier).toHaveValue('introduction-review');
    expect(props.onUpdateWorkflow).not.toHaveBeenCalled();
    await user.tab();
    expect(props.onUpdateWorkflow).toHaveBeenCalledTimes(1);
    expect(props.onUpdateWorkflow.mock.calls[0]![0](workflow).workflow_id).toBe('introduction-review');
  });

  it('retains the current identifier for invalid input and applies a corrected value on Enter', async () => {
    const user = userEvent.setup();
    const workflow = createBlankWorkflow('current-name');
    const props = inspectorProps(workflow, null);
    render(<NodeInspector {...props} />);

    const identifier = screen.getByLabelText('流程标识');
    await user.clear(identifier);
    await user.type(identifier, 'unfinished-');
    await user.keyboard('{Enter}');
    expect(props.onUpdateWorkflow).not.toHaveBeenCalled();
    expect(identifier).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('alert')).toHaveTextContent('原标识“current-name”仍然生效');
    await user.type(identifier, 'flow');
    await user.keyboard('{Enter}');
    expect(props.onUpdateWorkflow).toHaveBeenCalledTimes(1);
    expect(props.onUpdateWorkflow.mock.calls[0]![0](workflow).workflow_id).toBe('unfinished-flow');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('resynchronizes buffered settings when a workflow is restored or loaded', async () => {
    const user = userEvent.setup();
    const workflow = createBlankWorkflow('original-name');
    const props = inspectorProps(workflow, null);
    const { rerender } = render(<NodeInspector {...props} />);
    const identifier = screen.getByLabelText('流程标识');
    await user.clear(identifier);
    await user.type(identifier, 'unfinished-');
    await user.keyboard('{Enter}');
    expect(screen.getByRole('alert')).toBeInTheDocument();

    rerender(<NodeInspector {...props} workflow={{ ...workflow, workflow_id: 'restored-name' }} />);
    expect(identifier).toHaveValue('restored-name');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(props.onUpdateWorkflow).not.toHaveBeenCalled();
  });

  it('explains unavailable insertion before users encounter a failed operation', async () => {
    const user = userEvent.setup();
    const workflow = createBlankWorkflow('guided-insert');
    const props = inspectorProps(workflow);
    render(<NodeInspector {...props} />);

    expect(screen.getByRole('button', { name: '插入之前' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '插入之后' })).toBeDisabled();
    expect(screen.getByText('前方没有连接。先添加阶段，再连接到当前阶段。')).toBeVisible();
    await user.click(screen.getByRole('button', { name: '插入之前' }));
    expect(props.onInsertBefore).not.toHaveBeenCalled();
  });

  it('allows insertion on a simple connection and disables the same operation at a branch', async () => {
    const user = userEvent.setup();
    const start = createBlankWorkflow('guided-branch');
    const second = { ...start.nodes[0]!, id: 'second', entry: false };
    const connected = connectNodes(addNode(start, second), 'step-1', 'second');
    const props = inspectorProps(connected);
    const { rerender } = render(<NodeInspector {...props} />);

    expect(screen.getByRole('button', { name: '插入之后' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: '插入之后' }));
    expect(props.onInsertAfter).toHaveBeenCalledWith('step-1', 'task');
    const branch = connectNodes(addNode(connected, { ...second, id: 'third' }), 'step-1', 'third');
    rerender(<NodeInspector {...props} workflow={branch} selectedNode={branch.nodes[0]!} />);
    expect(screen.getByRole('button', { name: '插入之后' })).toBeDisabled();
    expect(screen.getByText('后方存在分支。请先添加阶段，再手动调整连接。')).toBeVisible();
  });

  it('suggests connected, mapped artifacts and external inputs without modifying validator input values', () => {
    const start = createBlankWorkflow('validator-suggestions');
    const source = updateNode(start, 'step-1', { outputs: ['draft_text'] });
    const check: WorkflowNode = {
      ...source.nodes[0]!, id: 'check', display_name: '论文检查', type: 'validator', entry: false,
      skill_ref: null, validator_ref: 'paper-section',
      validator_config: { input_roles: { manuscript: 'manuscript' }, options: {} },
      inputs: ['manuscript'], outputs: [], outcomes: ['pass', 'fail', 'blocked'],
    };
    const connected = connectNodes(addNode(source, check), 'step-1', 'check');
    const workflow = {
      ...connected, external_inputs: ['provided_notes'],
      edges: [{ ...connected.edges[0]!, output_map: { draft_text: 'manuscript' } }],
    };
    const validator: ValidatorCatalogEntry = {
      validator_id: 'paper-section', script: 'paper_section_validator.py', sha256: 'a'.repeat(64),
      adapter: 'paper-section', input_schema: 'test', control_tags: [], outcomes: ['pass', 'fail', 'blocked'],
      input_roles: { manuscript: { label: '论文正文', required: true, required_when: null } }, options: {},
    };
    const props = inspectorProps(workflow, workflow.nodes[1]!);
    const { container } = render(<NodeInspector {...props} validators={[validator]} />);

    const input = screen.getByLabelText('论文正文');
    expect(input).toHaveValue('manuscript');
    const suggestions = container.querySelector(`datalist[id="${input.getAttribute('list')}"]`);
    expect([...suggestions!.querySelectorAll('option')].map((option) => option.value)).toEqual(['provided_notes', 'manuscript']);
    expect(props.onUpdateNode).not.toHaveBeenCalled();
    expect(screen.getByText('验证阶段')).toBeVisible();
  });
});

describe('NodeInspector connection contracts', () => {
  it('offers only the executable task trigger and edits join output mapping as a form', async () => {
    const user = userEvent.setup();
    const start = createBlankWorkflow('join-routing');
    const source = updateNode(start, 'step-1', {
      outputs: ['branch_a'], outcomes: ['succeeded', 'needs_review'],
    });
    const join: WorkflowNode = {
      ...source.nodes[0]!, id: 'merge', type: 'join', display_name: '汇合', entry: false,
      skill_ref: null, validator_ref: null, validator_config: null,
      inputs: [], outputs: ['merged'], outcomes: [], join_mode: 'any_success',
    };
    const workflow = connectNodes(addNode(source, join), 'step-1', 'merge');
    const onUpdateEdge = vi.fn();
    render(<NodeInspector
      workflow={workflow}
      selectedNode={workflow.nodes[1]!}
      skills={[]}
      validators={[]}
      readOnly={false}
      onUpdateWorkflow={vi.fn()}
      onUpdateNode={vi.fn()}
      onUpdateEdge={onUpdateEdge}
      onConnect={vi.fn()}
      onDisconnect={vi.fn()}
      onMoveNode={vi.fn()}
      onDuplicateNode={vi.fn()}
      onDeleteNode={vi.fn()}
      onInsertBefore={vi.fn()}
      onInsertAfter={vi.fn()}
    />);

    const triggerSelect = screen.getByRole('combobox', { name: 'step-1 到 merge 的触发结果' });
    expect(triggerSelect).toHaveTextContent('succeeded');
    expect(triggerSelect).not.toHaveTextContent('needs_review');
    const mappingSelect = screen.getByRole('combobox', { name: 'step-1 到 merge：branch_a 映射到' });
    expect(mappingSelect).toHaveTextContent('保持原名');
    await user.selectOptions(mappingSelect, 'merged');
    expect(onUpdateEdge).toHaveBeenCalledWith(workflow.edges[0]!.id, { output_map: { branch_a: 'merged' } });
  });

  it('prevents a second source output from taking the same target artifact', () => {
    const start = createBlankWorkflow('no-map-collision');
    const source = updateNode(start, 'step-1', { outputs: ['branch_a', 'branch_b'] });
    const join: WorkflowNode = {
      ...source.nodes[0]!, id: 'merge', type: 'join', display_name: '汇合', entry: false,
      skill_ref: null, validator_ref: null, validator_config: null,
      inputs: [], outputs: ['merged'], outcomes: [], join_mode: 'any_success',
    };
    const connected = connectNodes(addNode(source, join), 'step-1', 'merge');
    const workflow = {
      ...connected,
      edges: [{ ...connected.edges[0]!, output_map: { branch_a: 'merged' } }],
    };
    render(<NodeInspector
      workflow={workflow} selectedNode={workflow.nodes[1]!}
      skills={[]} validators={[]} readOnly={false}
      onUpdateWorkflow={vi.fn()} onUpdateNode={vi.fn()} onUpdateEdge={vi.fn()}
      onConnect={vi.fn()} onDisconnect={vi.fn()} onMoveNode={vi.fn()}
      onDuplicateNode={vi.fn()} onDeleteNode={vi.fn()}
      onInsertBefore={vi.fn()} onInsertAfter={vi.fn()}
    />);
    const second = screen.getByRole('combobox', { name: 'step-1 到 merge：branch_b 映射到' });
    expect(second.querySelector('option[value="merged"]')).toBeDisabled();
  });
});
