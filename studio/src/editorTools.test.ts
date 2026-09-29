import { describe, expect, it } from 'vitest';
import {
  appendStage,
  arrangeWorkflow,
  createEditorHistory,
  recordHistory,
  redoDocument,
  undoDocument,
} from './editorTools';
import { classifyWorkflowEdit, cloneWorkflowDocument, createBlankWorkflow } from './workflow';
import type { TaskNode, WorkflowDocument, WorkflowEdge, WorkflowNode } from './types';

function task(id: string, overrides: Partial<TaskNode> = {}): TaskNode {
  return { ...createBlankWorkflow('example').nodes[0] as TaskNode, id, display_name: id, ...overrides };
}

function edge(source: string, target: string, outputMap: Record<string, string> = {}): WorkflowEdge {
  return { id: `${source}-to-${target}`, source, target, trigger: 'succeeded', output_map: outputMap };
}

function workflow(nodes: WorkflowNode[], edges: WorkflowEdge[] = []): WorkflowDocument {
  return {
    ...createBlankWorkflow('example'),
    document_revision: 8,
    semantic_revision: 5,
    nodes,
    edges,
    ui: { positions: Object.fromEntries(nodes.map((node, index) => [node.id, { x: index * 80, y: index * 50 }])) },
  };
}

describe('append a stage', () => {
  it('connects a terminal stage and retains existing entry flags without mutating its inputs', () => {
    const original = workflow([
      task('start', { entry: false, outputs: ['draft'] }),
      task('other', { entry: false }),
    ]);
    const before = cloneWorkflowDocument(original);
    const addition = task('review', { inputs: ['draft'] });
    const result = appendStage(original, 'start', addition);

    expect(result.nodes.map((node) => [node.id, node.entry])).toEqual([
      ['start', false], ['other', false], ['review', false],
    ]);
    expect(result.edges).toEqual([expect.objectContaining({
      source: 'start', target: 'review', trigger: 'succeeded', output_map: { draft: 'draft' },
    })]);
    expect(result.ui.positions.review!.x).toBeGreaterThan(result.ui.positions.start!.x);
    result.nodes[2]!.inputs.push('later');
    result.ui.positions.start!.x = 999;
    expect(addition.inputs).toEqual(['draft']);
    expect(original).toEqual(before);
    expect(result.document_revision).toBe(8);
    expect(result.semantic_revision).toBe(5);
  });

  it('uses the terminal node type to select its default trigger', () => {
    const validator: WorkflowNode = {
      ...task('check'), type: 'validator', skill_ref: null, validator_ref: 'paper-section',
      validator_config: null, outcomes: ['pass', 'fail', 'blocked'],
    };
    const condition: WorkflowNode = {
      ...task('choice'), type: 'condition', skill_ref: null,
      outcomes: ['approved', 'default'], condition_cases: [{ outcome: 'approved', when: { approved: true } }],
    };
    expect(appendStage(workflow([validator]), 'check', task('next')).edges[0]!.trigger).toBe('pass');
    expect(appendStage(workflow([condition]), 'choice', task('next')).edges[0]!.trigger).toBe('default');
  });

  it('inserts into a linear route while preserving renamed artifact mappings and the original trigger', () => {
    const source: WorkflowNode = {
      ...task('source', { outputs: ['raw'] }), type: 'condition', skill_ref: null,
      outcomes: ['ready', 'default'], condition_cases: [{ outcome: 'ready', when: { ready: true } }],
    };
    const original = workflow([
      source, task('finish', { entry: false, inputs: ['draft'] }),
    ], [{ ...edge('source', 'finish', { raw: 'draft' }), trigger: 'ready' }]);
    const result = appendStage(original, 'source', task('review', {
      inputs: ['draft'], outputs: ['draft'], write_scopes: ['draft'],
    }));

    expect(result.edges).toEqual([
      expect.objectContaining({ source: 'source', target: 'review', trigger: 'ready', output_map: { raw: 'draft' } }),
      expect.objectContaining({ source: 'review', target: 'finish', trigger: 'succeeded', output_map: { draft: 'draft' } }),
    ]);
    expect(result.nodes.find((node) => node.id === 'review')!.entry).toBe(false);
    expect(original.edges).toEqual([{ ...edge('source', 'finish', { raw: 'draft' }), trigger: 'ready' }]);
  });

  it('asks the user to select a branch instead of arbitrarily rewiring multiple successors', () => {
    const original = workflow([task('start'), task('left'), task('right')], [
      edge('start', 'left'), edge('start', 'right'),
    ]);
    expect(() => appendStage(original, 'start', task('new-step'))).toThrow(/选择.*分支/);
    expect(original.nodes).toHaveLength(3);
    expect(original.edges).toHaveLength(2);
  });

  it('preserves existing artifacts when a default unconfigured addition cannot bridge them', () => {
    const original = workflow([
      task('start', { outputs: ['raw'] }), task('finish', { entry: false, inputs: ['draft'] }),
    ], [edge('start', 'finish', { raw: 'draft' })]);
    const before = cloneWorkflowDocument(original);
    expect(() => appendStage(original, 'start', task('new-step'))).toThrow(/draft.*独立添加.*输入.*输出/);
    expect(original).toEqual(before);
  });

  it('does not overwrite an earlier artifact route when appending a stage with insufficient distinct outputs', () => {
    const original = workflow([
      task('start', { outputs: ['a', 'b'] }), task('finish', { entry: false, inputs: ['x', 'a'] }),
    ], [edge('start', 'finish', { a: 'x', b: 'a' })]);
    const before = cloneWorkflowDocument(original);
    expect(() => appendStage(original, 'start', task('middle', {
      inputs: ['x', 'a'], outputs: ['a'],
    }))).toThrow(/输出.*a.*多个.*产物/);
    expect(original).toEqual(before);
  });

  it('retains the existing insert safeguard around a shared successor', () => {
    const original = workflow([task('first'), task('second'), task('shared')], [
      edge('first', 'shared'), edge('second', 'shared'),
    ]);
    expect(() => appendStage(original, 'first', task('new-step'))).toThrow();
    expect(original.edges).toHaveLength(2);
  });

  it('rejects an absent source or duplicate node without touching the graph', () => {
    const original = workflow([task('start')]);
    expect(() => appendStage(original, 'missing', task('next'))).toThrow(/找不到.*missing/);
    expect(() => appendStage(original, 'start', task('start'))).toThrow();
    expect(original.nodes).toHaveLength(1);
  });
});

describe('arrange the workflow canvas', () => {
  it('places all nodes in deterministic left-to-right layers without changing workflow meaning or revisions', () => {
    const original = workflow([
      task('finish', { entry: false }),
      task('right', { entry: false, enabled: false }),
      task('start'),
      task('left', { entry: false }),
      task('isolated'),
    ], [
      edge('start', 'left'), edge('start', 'right'), edge('left', 'finish'), edge('right', 'finish'),
    ]);
    const before = cloneWorkflowDocument(original);
    const arranged = arrangeWorkflow(original);
    const positions = arranged.ui.positions;
    expect(positions.start!.x).toBeLessThan(positions.left!.x);
    expect(positions.left!.x).toBeLessThan(positions.finish!.x);
    expect(positions.left!.x).toBe(positions.right!.x);
    expect(positions.right!.y).toBeLessThan(positions.left!.y);
    expect(positions.isolated!.x).toBe(positions.start!.x);
    expect(positions.start!.y).toBeLessThan(positions.isolated!.y);
    expect(classifyWorkflowEdit(original, arranged)).toBe('visual');
    expect({ ...arranged, ui: original.ui }).toEqual(original);
    expect(arrangeWorkflow(arranged)).toEqual(arranged);
    arranged.nodes[0]!.inputs.push('new-input');
    expect(original).toEqual(before);
  });

  it('uses the longest dependency path when a node has direct and indirect predecessors', () => {
    const original = workflow([task('start'), task('middle'), task('finish')], [
      edge('start', 'finish'), edge('start', 'middle'), edge('middle', 'finish'),
    ]);
    const positions = arrangeWorkflow(original).ui.positions;
    expect(positions.start!.x).toBeLessThan(positions.middle!.x);
    expect(positions.middle!.x).toBeLessThan(positions.finish!.x);
  });

  it('accepts an empty canvas', () => {
    expect(arrangeWorkflow(workflow([])).ui.positions).toEqual({});
  });

  it('explains cycles and dangling connections instead of returning a partial layout', () => {
    const cyclic = workflow([task('a'), task('b', { enabled: false })], [edge('a', 'b'), edge('b', 'a')]);
    expect(() => arrangeWorkflow(cyclic)).toThrow(/循环/);
    expect(() => arrangeWorkflow(workflow([task('a')], [edge('a', 'missing')]))).toThrow(/missing/);
    expect(() => arrangeWorkflow(workflow([task('a')], [edge('missing', 'a')]))).toThrow(/missing/);
  });

  it('rejects missing or duplicate graph identifiers with actionable errors', () => {
    expect(() => arrangeWorkflow(workflow([task('a'), task('a')]))).toThrow(/标识.*重复/);
    expect(() => arrangeWorkflow(workflow([task('a'), task('b')], [{ ...edge('a', 'b'), id: '' }]))).toThrow(/连线.*标识/);
    expect(() => arrangeWorkflow(workflow([task('a'), task('b')], [edge('a', 'b'), edge('a', 'b')]))).toThrow(/连线.*重复/);
  });
});

describe('local editor history', () => {
  it('starts with empty undo and redo stacks', () => {
    const document = workflow([task('start')]);
    const history = createEditorHistory(document);
    expect(history).toEqual({ past: [], future: [] });
    expect(undoDocument(history, document)).toBeNull();
    expect(redoDocument(history, document)).toBeNull();
  });

  it('undoes and redoes content while preserving the current server-owned revision numbers', () => {
    const original = workflow([task('start')]);
    const changed = appendStage(original, 'start', task('next'));
    changed.document_revision = 12;
    changed.semantic_revision = 9;
    const history = recordHistory(createEditorHistory(original), original);
    const undone = undoDocument(history, changed)!;
    expect(undone.document.nodes.map((node) => node.id)).toEqual(['start']);
    expect(undone.document.document_revision).toBe(12);
    expect(undone.document.semantic_revision).toBe(9);
    const current = { ...undone.document, document_revision: 13, semantic_revision: 10 };
    const redone = redoDocument(undone.history, current)!;
    expect(redone.document.nodes.map((node) => node.id)).toEqual(['start', 'next']);
    expect(redone.document.document_revision).toBe(13);
    expect(redone.document.semantic_revision).toBe(10);
    expect(redone.history.past).toHaveLength(1);
    expect(redone.history.future).toEqual([]);
    expect(original.document_revision).toBe(8);
    expect(history.past).toHaveLength(1);
  });

  it('isolates stored snapshots, restored content, and each returned history object', () => {
    const original = workflow([task('start')]);
    const saved = cloneWorkflowDocument(original);
    const history = recordHistory(createEditorHistory(original), original);
    original.nodes[0]!.inputs.push('mutated-input');
    expect(history.past[0]).toEqual(saved);
    const laterHistory = recordHistory(history, original);
    laterHistory.past[0]!.nodes[0]!.inputs.push('mutated-history');
    expect(history.past[0]).toEqual(saved);
    const undone = undoDocument(history, original)!;
    undone.document.nodes[0]!.inputs.push('changed-undo');
    undone.history.future[0]!.nodes[0]!.inputs.push('changed-future');
    expect(history.past[0]).toEqual(saved);
    expect(original.nodes[0]!.inputs).toEqual(['mutated-input']);
  });

  it('clears redo after a new edit and keeps only the newest 50 undo snapshots', () => {
    const original = workflow([task('start')]);
    let history = createEditorHistory(original);
    for (let index = 0; index < 55; index += 1) {
      history = recordHistory(history, { ...original, workflow_id: `revision-${index}` });
    }
    expect(history.past).toHaveLength(50);
    expect(history.past[0]!.workflow_id).toBe('revision-5');
    expect(history.past[49]!.workflow_id).toBe('revision-54');
    const undone = undoDocument(history, original)!;
    expect(undone.history.future).toHaveLength(1);
    const edited = recordHistory(undone.history, undone.document);
    expect(edited.future).toEqual([]);
    expect(redoDocument(edited, original)).toBeNull();
    expect(undone.history.future).toHaveLength(1);
  });

  it('replays multiple edits in order', () => {
    const first = workflow([task('first')]);
    const second = appendStage(first, 'first', task('second'));
    const third = appendStage(second, 'second', task('third'));
    let state = { document: third, history: recordHistory(recordHistory(createEditorHistory(first), first), second) };
    state = undoDocument(state.history, state.document)!;
    state = undoDocument(state.history, state.document)!;
    expect(state.document.nodes.map((node) => node.id)).toEqual(['first']);
    state = redoDocument(state.history, state.document)!;
    expect(state.document.nodes.map((node) => node.id)).toEqual(['first', 'second']);
    state = redoDocument(state.history, state.document)!;
    expect(state.document.nodes.map((node) => node.id)).toEqual(['first', 'second', 'third']);
  });
});
