import { describe, expect, it } from 'vitest';
import {
  addNode,
  applyWorkflowEdit,
  classifyWorkflowEdit,
  cloneWorkflowDocument,
  cloneProjection,
  connectNodes,
  createBlankWorkflow,
  createWorkflowEditState,
  deleteNode,
  disconnectEdge,
  duplicateNode,
  insertAfter,
  insertBefore,
  localWorkflowHints,
  moveNode,
  setNodeEnabled,
  updateNode,
  updateWorkflowSettings,
  WorkflowEditError,
} from './workflow';
import type {
  CatalogData,
  ProjectionData,
  ProjectionNode,
  TaskNode,
  WorkflowDocument,
  WorkflowEdge,
} from './types';

function taskNode(
  id: string,
  overrides: Partial<TaskNode> = {},
): TaskNode {
  return {
    id,
    type: 'task',
    display_name: id,
    entry: true,
    enabled: true,
    skill_ref: null,
    validator_ref: null,
    validator_config: null,
    origin_projection_node_id: null,
    inputs: [],
    outputs: [],
    outcomes: ['succeeded'],
    write_scopes: [],
    failure_policy: 'block',
    condition_cases: [],
    join_mode: 'all_active',
    ...overrides,
  };
}

function linearDocument(): WorkflowDocument {
  const first = taskNode('first', { entry: true, outputs: ['draft'], write_scopes: ['draft'] });
  const second = taskNode('second', { entry: false, inputs: ['draft'], outputs: ['result'], write_scopes: ['result'] });
  const edge: WorkflowEdge = {
    id: 'first-to-second',
    source: first.id,
    target: second.id,
    trigger: 'succeeded',
    output_map: { draft: 'draft' },
  };
  return {
    ...createBlankWorkflow('demo-flow'),
    document_revision: 7,
    semantic_revision: 3,
    nodes: [first, second],
    edges: [edge],
    ui: { positions: { first: { x: 100, y: 120 }, second: { x: 500, y: 120 } } },
  };
}

function projectionNode(
  id: string,
  kind: ProjectionNode['projection_kind'],
  overrides: Partial<ProjectionNode> = {},
): ProjectionNode {
  return {
    id,
    display_name: id,
    projection_kind: kind,
    official_stage_ids: [id],
    suggested_skill_ids: [],
    suggested_validator_ids: [],
    inputs: [],
    outputs: [],
    write_scopes: [],
    control_tags: ['private-internal-only'],
    ...overrides,
  };
}

function projectionFixture(): ProjectionData {
  return {
    sha256: 'a'.repeat(64),
    projection: {
      schema_version: 'paper-workflow-studio-projection-v1',
      projection_id: 'official-v1.0',
      source_commit: 'e24c34255e3c72a329614e07c431bfa51a778c40',
      nodes: [
        projectionNode('intake', 'orchestrator', {
          inputs: ['user_request'],
          outputs: ['project_manifest'],
          write_scopes: ['project_manifest', 'progress'],
        }),
        projectionNode('audit', 'gate', {
          inputs: ['project_manifest'],
          outputs: ['audit_receipt'],
          suggested_validator_ids: ['final-edit-receipt'],
          write_scopes: ['audit_receipt'],
        }),
        projectionNode('delivery', 'delivery', {
          inputs: ['external_material', 'audit_receipt'],
          outputs: ['final_delivery'],
          write_scopes: ['final_delivery'],
        }),
        projectionNode('directions', 'task', {
          inputs: ['project_manifest'],
          outputs: ['direction_brief'],
          suggested_skill_ids: ['clarify-research-idea'],
          write_scopes: ['direction_brief'],
        }),
        projectionNode('choice', 'task', {
          suggested_skill_ids: ['paper-writer', 'academic-paper'],
        }),
        projectionNode('missing-skill', 'task', {
          suggested_skill_ids: ['not-installed'],
        }),
      ],
      edges: [
        { id: 'intake-to-audit', source: 'intake', target: 'audit', projection_note: 'first' },
        { id: 'audit-to-delivery', source: 'audit', target: 'delivery', projection_note: 'second' },
      ],
      stage_map: {},
      control_tags: ['citation'],
      projection_notes: [{ kind: 'feedback_as_new_revision', studio_behavior: 'Use a new revision.' }],
    },
  };
}

const catalog: CatalogData = {
  skills: [
    {
      catalog_id: 'clarify-research-idea', display_name: 'Clarify idea', description: '',
      relative_path: 'clarify/SKILL.md', skill_sha256: 'a'.repeat(64), tree_sha256: 'b'.repeat(64),
      locked: false, ambiguous: false,
    },
    {
      catalog_id: 'ambiguous-skill', display_name: 'Ambiguous', description: '',
      relative_path: 'ambiguous/SKILL.md', skill_sha256: 'c'.repeat(64), tree_sha256: 'd'.repeat(64),
      locked: false, ambiguous: true,
    },
  ],
  validators: [{
    validator_id: 'final-edit-receipt', script: 'scripts/final-edit-receipt.py', sha256: 'e'.repeat(64),
    adapter: 'receipt-v1', input_schema: 'receipt-v1', control_tags: [], outcomes: ['pass', 'fail', 'blocked'],
    available: true,
  }],
};

describe('workflow graph operations', () => {
  it('creates a blank, schema-shaped draft without inventing a Skill', () => {
    const draft = createBlankWorkflow('new-paper-flow');
    expect(draft.schema_version).toBe('paper-workflow-custom-v1');
    expect(draft.document_revision).toBe(0);
    expect(draft.semantic_revision).toBe(0);
    expect(draft.derived_from).toBeNull();
    expect(draft.nodes).toHaveLength(1);
    expect(draft.nodes[0]?.skill_ref).toBeNull();
    expect(draft.nodes[0]?.entry).toBe(true);
    expect(() => createBlankWorkflow('../outside')).toThrow(WorkflowEditError);
  });

  it('clones projection nodes into an editable draft with only unambiguous installed bindings', () => {
    const data = projectionFixture();
    const draft = cloneProjection(data, catalog, 'my-paper-flow');
    expect(draft.derived_from).toEqual({ projection_id: 'official-v1.0', projection_sha256: data.sha256 });
    expect(draft.document_revision).toBe(0);
    expect(draft.semantic_revision).toBe(0);
    expect(draft.nodes.find((node) => node.id === 'intake')?.skill_ref).toBeNull();
    expect(draft.nodes.find((node) => node.id === 'directions')?.skill_ref).toBe('clarify-research-idea');
    expect(draft.nodes.find((node) => node.id === 'choice')?.skill_ref).toBeNull();
    expect(draft.nodes.find((node) => node.id === 'missing-skill')?.skill_ref).toBeNull();
    const gate = draft.nodes.find((node) => node.id === 'audit');
    expect(gate?.type).toBe('validator');
    expect(gate?.validator_ref).toBe('final-edit-receipt');
    expect(gate?.validator_config).toBeNull();
    expect(gate?.outputs).toEqual([]);
    expect(draft.nodes.every((node) => !('control_tags' in node))).toBe(true);
    expect(draft.nodes.find((node) => node.id === 'intake')?.entry).toBe(true);
    expect(draft.external_inputs).toEqual([
      'audit_receipt', 'external_material', 'project_manifest', 'user_request',
    ]);
    expect(draft.edges).toHaveLength(2);
    expect(draft.edges[0]?.trigger).toBe('succeeded');
    expect(draft.edges[1]?.trigger).toBe('pass');
    expect(data.projection.projection_notes).toHaveLength(1);
  });

  it('adds both design checkpoints when the official successors share an edge', () => {
    const data = projectionFixture();
    const checkpointNodes = [
      projectionNode('topic', 'task'),
      projectionNode('design', 'task'),
      projectionNode('venue-outline', 'task'),
      projectionNode('experiments', 'task'),
      projectionNode('final-editorial-audit', 'task'),
      projectionNode('finalize', 'delivery'),
    ];
    data.projection.nodes.push(...checkpointNodes);
    data.projection.edges.push(
      { id: 'topic-to-design', source: 'topic', target: 'design', projection_note: '' },
      { id: 'design-to-venue-outline', source: 'design', target: 'venue-outline', projection_note: '' },
      { id: 'design-to-experiments', source: 'design', target: 'experiments', projection_note: '' },
      { id: 'final-audit-to-finalize', source: 'final-editorial-audit', target: 'finalize', projection_note: '' },
    );
    const draft = cloneProjection(data, catalog, 'checkpoint-flow');
    const approvalGates = draft.nodes.filter((node) => node.approval_source);
    expect(approvalGates).toHaveLength(4);
    expect(approvalGates.filter((node) => node.approval_source === 'design')).toHaveLength(2);
    expect(draft.edges.filter((edge) => edge.source === 'design')).toHaveLength(4);
  });

  it('adds and duplicates nodes immutably without copying projection authority or edges', () => {
    const original = linearDocument();
    const added = addNode(original, taskNode('third'), { x: 700, y: 180 });
    expect(added).not.toBe(original);
    expect(original.nodes).toHaveLength(2);
    expect(added.nodes).toHaveLength(3);
    expect(added.ui.positions.third).toEqual({ x: 700, y: 180 });
    expect(addNode(original, taskNode('auto-positioned')).ui.positions['auto-positioned']).toEqual({ x: 720, y: 100 });

    const duplicate = duplicateNode(original, 'first');
    const copied = duplicate.nodes.find((node) => node.id === 'first-copy');
    expect(copied?.origin_projection_node_id).toBeNull();
    expect(copied?.entry).toBe(true);
    expect(duplicate.edges).toHaveLength(1);
    expect(duplicate.ui.positions['first-copy']).toEqual({ x: 148, y: 168 });
    expect(() => addNode(original, taskNode('first'))).toThrow('already exists');
  });

  it('enables, disables, deletes, and preserves the original object graph', () => {
    const original = linearDocument();
    const disabled = setNodeEnabled(original, 'first', false);
    expect(disabled.nodes[0]?.enabled).toBe(false);
    expect(disabled.edges).toHaveLength(1);
    expect(original.nodes[0]?.enabled).toBe(true);
    expect(disabled.nodes.find((node) => node.id === 'second')?.entry).toBe(true);

    const reenabled = setNodeEnabled(disabled, 'first', true);
    expect(reenabled.nodes[0]?.enabled).toBe(true);
    const deleted = deleteNode(original, 'first');
    expect(deleted.nodes.map((node) => node.id)).toEqual(['second']);
    expect(deleted.edges).toEqual([]);
    expect(deleted.ui.positions.first).toBeUndefined();
    expect(deleted.nodes[0]?.entry).toBe(true);
    expect(original.edges).toHaveLength(1);
  });

  it('classifies movement and display-name edits as visual while behavior changes are semantic', () => {
    const original = linearDocument();
    const moved = moveNode(original, 'second', { x: 640, y: 320 });
    expect(classifyWorkflowEdit(original, moved)).toBe('visual');
    expect(moved.document_revision).toBe(original.document_revision);
    expect(moved.semantic_revision).toBe(original.semantic_revision);
    expect(moved.ui.positions.second).toEqual({ x: 640, y: 320 });
    const reordered = {
      ...original,
      nodes: [...original.nodes].reverse(),
      edges: [...original.edges].reverse(),
    };
    expect(classifyWorkflowEdit(original, reordered)).toBe('visual');

    const renamed = updateNode(original, 'second', { display_name: 'Final draft' });
    expect(classifyWorkflowEdit(original, renamed)).toBe('visual');
    expect(renamed.semantic_revision).toBe(3);
    const behaviorChanged = setNodeEnabled(original, 'second', false);
    expect(classifyWorkflowEdit(original, behaviorChanged)).toBe('semantic');
  });

  it('connects and disconnects nodes with output routing and root-entry updates', () => {
    const original = createBlankWorkflow('connect-flow');
    const withSecond = addNode(original, taskNode('second', { inputs: ['paper'], outputs: ['receipt'] }));
    const withOutput = updateNode(withSecond, 'step-1', { outputs: ['paper'], write_scopes: ['paper'] });
    const connected = connectNodes(withOutput, 'step-1', 'second');
    expect(connected.edges).toHaveLength(1);
    expect(connected.edges[0]?.output_map).toEqual({ paper: 'paper' });
    expect(connected.edges[0]?.trigger).toBe('succeeded');
    expect(connected.nodes.find((node) => node.id === 'second')?.entry).toBe(false);
    const disconnected = disconnectEdge(connected, connected.edges[0]!.id);
    expect(disconnected.edges).toEqual([]);
    expect(disconnected.nodes.find((node) => node.id === 'second')?.entry).toBe(true);
    expect(() => connectNodes(connected, 'missing', 'second')).toThrow('Unknown node');
    expect(() => connectNodes(connected, 'step-1', 'step-1')).toThrow('cycle');
  });

  it('rejects task outcome triggers that the server compiler cannot execute', () => {
    const starter = createBlankWorkflow('custom-trigger-flow');
    const withOutcome = updateNode(starter, 'step-1', { outcomes: ['succeeded', 'needs_review'] });
    const withTarget = addNode(withOutcome, taskNode('review'));
    expect(() => connectNodes(withTarget, 'step-1', 'review', 'needs_review')).toThrow('Trigger needs_review');
    expect(connectNodes(withTarget, 'step-1', 'review', 'succeeded').edges[0]?.trigger).toBe('succeeded');
  });

  it('inserts before and after only on simple one-edge segments and rewires atomically', () => {
    const original = linearDocument();
    const insertedBefore = insertBefore(original, 'second', taskNode('middle', {
      entry: false,
      inputs: ['draft'],
      outputs: ['draft'],
      write_scopes: ['draft'],
    }));
    expect(insertedBefore.nodes.map((node) => node.id)).toContain('middle');
    expect(insertedBefore.edges).toHaveLength(2);
    expect(insertedBefore.edges.map((edge) => [edge.source, edge.target])).toEqual([
      ['first', 'middle'], ['middle', 'second'],
    ]);
    expect(insertedBefore.edges[0]?.id).toBe('first-to-second');
    expect(insertedBefore.edges[0]?.output_map).toEqual({ draft: 'draft' });
    expect(insertedBefore.edges[1]?.output_map).toEqual({ draft: 'draft' });

    const insertedAfter = insertAfter(original, 'first', taskNode('middle', {
      entry: false,
      inputs: ['draft'],
      outputs: ['draft'],
      write_scopes: ['draft'],
    }));
    expect(insertedAfter.edges.map((edge) => [edge.source, edge.target])).toEqual([
      ['first', 'middle'], ['middle', 'second'],
    ]);
    expect(insertedAfter.edges).toHaveLength(2);
    expect(() => insertBefore(original, 'first', taskNode('other'))).toThrow('exactly one incoming');
    expect(() => insertAfter(original, 'second', taskNode('other'))).toThrow('exactly one outgoing');
  });

  it.each([
    { name: 'implicit identity', outputMap: {} },
    { name: 'explicit identity', outputMap: { draft: 'draft' } },
  ])('rejects insertion that would discard $name artifact flow, without mutation', ({ outputMap }) => {
    const original = linearDocument();
    original.edges[0]!.output_map = outputMap;
    const before = cloneWorkflowDocument(original);
    for (const addition of [
      taskNode('middle'),
      taskNode('middle', { inputs: ['draft'] }),
      taskNode('middle', { outputs: ['draft'] }),
    ]) {
      const additionBefore = { ...addition, inputs: [...addition.inputs], outputs: [...addition.outputs] };
      expect(() => insertBefore(original, 'second', addition)).toThrow(/draft.*独立添加.*输入.*输出/);
      expect(() => insertAfter(original, 'first', addition)).toThrow(/draft.*独立添加.*输入.*输出/);
      expect(addition).toEqual(additionBefore);
      expect(original).toEqual(before);
    }
  });

  it('protects implicit identity artifacts alongside an explicit rename, and accepts a complete bridge', () => {
    const original = linearDocument();
    original.nodes[0]!.outputs = ['raw', 'notes'];
    original.nodes[1]!.inputs = ['draft', 'notes'];
    original.edges[0]!.output_map = { raw: 'draft' };
    const before = cloneWorkflowDocument(original);
    expect(() => insertAfter(original, 'first', taskNode('middle', {
      inputs: ['draft'], outputs: ['draft'],
    }))).toThrow(/notes.*独立添加/);
    expect(() => insertBefore(original, 'second', taskNode('middle'))).toThrow(/draft.*独立添加/);

    const bridge = taskNode('middle', {
      inputs: ['draft', 'notes'], outputs: ['draft', 'notes'], write_scopes: ['draft', 'notes'],
    });
    for (const result of [insertBefore(original, 'second', bridge), insertAfter(original, 'first', bridge)]) {
      expect(result.edges).toEqual([
        expect.objectContaining({ source: 'first', target: 'middle', output_map: { raw: 'draft', notes: 'notes' } }),
        expect.objectContaining({ source: 'middle', target: 'second', output_map: { draft: 'draft', notes: 'notes' } }),
      ]);
      expect(result.document_revision).toBe(original.document_revision);
      expect(result.semantic_revision).toBe(original.semantic_revision);
    }
    expect(original).toEqual(before);
  });

  it('does not require an inserted node to forward outputs unused by the original target', () => {
    const original = linearDocument();
    original.nodes[0]!.outputs.push('unused');
    const result = insertAfter(original, 'first', taskNode('middle', {
      inputs: ['draft'], outputs: ['draft'],
    }));
    expect(result.edges[0]!.output_map).toEqual({ draft: 'draft' });
    expect(result.edges[1]!.output_map).toEqual({ draft: 'draft' });
  });

  it.each([
    ['a', 'b'],
    ['b', 'a'],
  ])('rejects reuse of one inserted output for different destinations (source order %s, %s)', (first, second) => {
    const original = linearDocument();
    original.nodes[0]!.outputs = [first, second];
    original.nodes[1]!.inputs = ['x', 'a'];
    original.edges[0]!.output_map = { a: 'x', b: 'a' };
    const addition = taskNode('middle', { inputs: ['x', 'a'], outputs: ['a'] });
    const before = cloneWorkflowDocument(original);
    expect(() => insertBefore(original, 'second', addition)).toThrow(/输出.*a.*多个.*产物/);
    expect(() => insertAfter(original, 'first', addition)).toThrow(/输出.*a.*多个.*产物/);
    expect(original).toEqual(before);
    expect(addition.inputs).toEqual(['x', 'a']);
    expect(addition.outputs).toEqual(['a']);
  });

  it('retains crossed renames when the inserted stage provides distinct outputs for both destinations', () => {
    const original = linearDocument();
    original.nodes[0]!.outputs = ['a', 'b'];
    original.nodes[1]!.inputs = ['x', 'a'];
    original.edges[0]!.output_map = { a: 'x', b: 'a' };
    const addition = taskNode('middle', { inputs: ['x', 'a'], outputs: ['a', 'b'] });
    const before = cloneWorkflowDocument(original);
    for (const result of [insertBefore(original, 'second', addition), insertAfter(original, 'first', addition)]) {
      expect(result.edges[0]!.output_map).toEqual({ a: 'x', b: 'a' });
      expect(result.edges[1]!.output_map).toEqual({ a: 'x', b: 'a' });
      expect(Object.values(result.edges[1]!.output_map).sort()).toEqual(['a', 'x']);
    }
    expect(original).toEqual(before);
  });

  it('updates node and workflow settings immutably with backend-owned revisions unchanged', () => {
    const original = linearDocument();
    const changed = updateNode(original, 'second', {
      failure_policy: 'skip_branch',
      inputs: ['new_artifact'],
    });
    expect(changed.nodes.find((node) => node.id === 'second')?.failure_policy).toBe('skip_branch');
    expect(original.nodes.find((node) => node.id === 'second')?.inputs).toEqual(['draft']);
    const settings = updateWorkflowSettings(changed, { max_parallelism: 3, external_inputs: ['new_artifact'] });
    expect(settings.max_parallelism).toBe(3);
    expect(settings.external_inputs).toEqual(['new_artifact']);
    expect(settings.document_revision).toBe(7);
    expect(settings.semantic_revision).toBe(3);
    expect(() => updateWorkflowSettings(settings, { max_parallelism: 65 })).toThrow(WorkflowEditError);
  });

  it('tracks a per-editor dirty generation separately from the persisted document', () => {
    const state = createWorkflowEditState(linearDocument());
    const next = applyWorkflowEdit(state, (document) => moveNode(document, 'second', { x: 610, y: 150 }));
    expect(next.dirtyGeneration).toBe(1);
    expect(next.lastClassification).toBe('visual');
    expect(next.document.document_revision).toBe(7);
    expect(next.document.semantic_revision).toBe(3);
    expect(state.dirtyGeneration).toBe(0);
  });

  it('reports only advisory hints for an unbound task and missing entry', () => {
    const draft = createBlankWorkflow('advisory-flow');
    const hints = localWorkflowHints({
      ...draft,
      nodes: draft.nodes.map((node) => ({ ...node, entry: false })),
    });
    expect(hints.map((hint) => hint.code)).toEqual(['local.entry_count', 'local.task_unbound']);
    expect(hints[1]?.node_id).toBe('step-1');
  });
});
