import type {
  CatalogData,
  ConditionCase,
  JsonValue,
  ProjectionData,
  ProjectionNode,
  WorkflowDocument,
  WorkflowEdge,
  WorkflowNode,
  WorkflowPosition,
} from './types';

export type EditClassification = 'semantic' | 'visual';

export type NodePropertyPatch = Partial<{
  display_name: string;
  entry: boolean;
  enabled: boolean;
  skill_ref: string | null;
  validator_ref: string | null;
  validator_config: WorkflowNode['validator_config'];
  inputs: string[];
  outputs: string[];
  outcomes: string[];
  write_scopes: string[];
  failure_policy: WorkflowNode['failure_policy'];
  condition_cases: ConditionCase[];
  join_mode: WorkflowNode['join_mode'];
}>;

export type WorkflowSettingsPatch = Partial<{
  workflow_id: string;
  max_parallelism: number;
  external_inputs: string[];
}>;

export interface WorkflowEditState {
  document: WorkflowDocument;
  dirtyGeneration: number;
  lastClassification: EditClassification | null;
}

export interface LocalWorkflowHint {
  code: string;
  message: string;
  node_id: string;
}

export class WorkflowEditError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'WorkflowEditError';
  }
}

const IDENTIFIER = /^[a-z][a-z0-9_]*(?:-[a-z0-9_]+)*$/;
const PROJECTION_IDENTIFIER = /^[a-z][a-z0-9_]*(?:[.-][a-z0-9_]+)*$/;
const SHA256 = /^[0-9a-f]{64}$/;
const MAX_SCALAR = 4_000;
const MAX_PARALLELISM = 64;
const MAX_COORDINATE = 1_000_000;

function compareCanonicalText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function assertIdentifier(value: string, label: string): void {
  if (value.length > MAX_SCALAR || !IDENTIFIER.test(value)) {
    throw new WorkflowEditError(`${label} must be a normalized workflow identifier.`);
  }
}

function cloneJson<T extends JsonValue>(value: T): T {
  if (Array.isArray(value)) {
    return value.map((item) => cloneJson(item)) as T;
  }
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, cloneJson(item)]),
    ) as T;
  }
  return value;
}

function cloneNode(node: WorkflowNode): WorkflowNode {
  return {
    ...node,
    inputs: [...node.inputs],
    outputs: [...node.outputs],
    outcomes: [...node.outcomes],
    write_scopes: [...node.write_scopes],
    condition_cases: node.condition_cases.map((item) => ({
      outcome: item.outcome,
      when: cloneJson(item.when as JsonValue) as { [key: string]: JsonValue },
    })),
    validator_config: node.validator_config === null
      ? null
      : {
          input_roles: { ...node.validator_config.input_roles },
          options: Object.fromEntries(
            Object.entries(node.validator_config.options).map(([key, value]) => [key, cloneJson(value)]),
          ),
        },
  } as WorkflowNode;
}

/** Return an independent JSON-compatible workflow object. */
export function cloneWorkflowDocument(document: WorkflowDocument): WorkflowDocument {
  return {
    ...document,
    derived_from: document.derived_from === null ? null : { ...document.derived_from },
    external_inputs: [...document.external_inputs],
    nodes: document.nodes.map(cloneNode),
    edges: document.edges.map((edge) => ({ ...edge, output_map: { ...edge.output_map } })),
    ui: {
      positions: Object.fromEntries(
        Object.entries(document.ui.positions).map(([id, position]) => [id, { ...position }]),
      ),
    },
  };
}

function emptyNodeFields() {
  return {
    entry: false,
    enabled: true,
    origin_projection_node_id: null,
    inputs: [] as string[],
    outputs: [] as string[],
    outcomes: [] as string[],
    write_scopes: [] as string[],
    failure_policy: 'block' as const,
    condition_cases: [] as ConditionCase[],
    join_mode: 'all_active' as const,
  };
}

function defaultTask(id: string, displayName: string, entry: boolean): WorkflowNode {
  return {
    ...emptyNodeFields(),
    id,
    type: 'task',
    display_name: displayName,
    entry,
    skill_ref: null,
    validator_ref: null,
    validator_config: null,
    outcomes: ['succeeded'],
  };
}

/** Create a schema-saveable starter draft; the unbound task needs user configuration before activation. */
export function createBlankWorkflow(workflowId: string): WorkflowDocument {
  assertIdentifier(workflowId, 'workflow_id');
  const node = defaultTask('step-1', 'New step', true);
  return {
    schema_version: 'paper-workflow-custom-v1',
    workflow_id: workflowId,
    document_revision: 0,
    semantic_revision: 0,
    derived_from: null,
    max_parallelism: 1,
    external_inputs: [],
    nodes: [node],
    edges: [],
    ui: { positions: { [node.id]: { x: 180, y: 140 } } },
  };
}

function uniqueId(base: string, existing: ReadonlySet<string>, fallback: string): string {
  if (base.length <= MAX_SCALAR && IDENTIFIER.test(base) && !existing.has(base)) return base;
  let index = 1;
  while (existing.has(`${fallback}-${index}`)) index += 1;
  return `${fallback}-${index}`;
}

function positionForIndex(index: number): WorkflowPosition {
  return { x: 120 + (index % 8) * 220, y: 100 + Math.floor(index / 8) * 150 };
}

function assertPosition(position: WorkflowPosition): void {
  for (const axis of ['x', 'y'] as const) {
    const value = position[axis];
    if (typeof value !== 'number' || !Number.isFinite(value) || Math.abs(value) > MAX_COORDINATE) {
      throw new WorkflowEditError(`Position ${axis} must be finite and within the canvas limit.`);
    }
  }
}

function enabledIncoming(document: WorkflowDocument, nodeId: string): WorkflowEdge[] {
  const enabledIds = new Set(document.nodes.filter((node) => node.enabled).map((node) => node.id));
  return document.edges.filter(
    (edge) => edge.target === nodeId && enabledIds.has(edge.source) && enabledIds.has(edge.target),
  );
}

function markEnabledRoots(document: WorkflowDocument): WorkflowDocument {
  const nodes = document.nodes.map((node) => {
    if (node.enabled && enabledIncoming(document, node.id).length === 0 && !node.entry) {
      return { ...node, entry: true } as WorkflowNode;
    }
    return node;
  });
  return { ...document, nodes };
}

function cloneNewNode(document: WorkflowDocument, node: WorkflowNode): WorkflowNode {
  assertIdentifier(node.id, 'node id');
  if (document.nodes.some((existing) => existing.id === node.id)) {
    throw new WorkflowEditError(`Node already exists: ${node.id}`);
  }
  if (
    node.origin_projection_node_id !== null
    && document.nodes.some((existing) => existing.origin_projection_node_id === node.origin_projection_node_id)
  ) {
    throw new WorkflowEditError('A projected node origin can only be used once in a workflow.');
  }
  return cloneNode(node);
}

function nodeById(document: WorkflowDocument, nodeId: string): WorkflowNode {
  const node = document.nodes.find((candidate) => candidate.id === nodeId);
  if (!node) throw new WorkflowEditError(`Unknown node: ${nodeId}`);
  return node;
}

function assertUniqueEdgeId(document: WorkflowDocument, edgeId: string): void {
  if (document.edges.some((edge) => edge.id === edgeId)) {
    throw new WorkflowEditError(`Edge already exists: ${edgeId}`);
  }
}

function nextEdgeId(document: WorkflowDocument): string {
  const existing = new Set(document.edges.map((edge) => edge.id));
  return uniqueId('', existing, 'edge');
}

function acceptedArtifacts(node: WorkflowNode): Set<string> {
  const accepted = new Set(node.inputs);
  if (node.type === 'join') node.outputs.forEach((output) => accepted.add(output));
  return accepted;
}

function defaultOutputMap(source: WorkflowNode, target: WorkflowNode): Record<string, string> {
  const accepted = acceptedArtifacts(target);
  return Object.fromEntries(source.outputs.filter((output) => accepted.has(output)).map((output) => [output, output]));
}

function triggerFor(node: WorkflowNode): string {
  if (node.type === 'condition') return 'default';
  if (node.type === 'validator') return 'pass';
  return 'succeeded';
}

function legalTriggers(node: WorkflowNode): string[] {
  if (node.type === 'condition') return [...node.condition_cases.map((item) => item.outcome), 'default'];
  if (node.type === 'join') return ['succeeded'];
  return [...node.outcomes];
}

function makeEdge(
  id: string,
  source: WorkflowNode,
  target: WorkflowNode,
  trigger = triggerFor(source),
  outputMap = defaultOutputMap(source, target),
): WorkflowEdge {
  assertIdentifier(id, 'edge id');
  assertIdentifier(trigger, 'edge trigger');
  if (!legalTriggers(source).includes(trigger)) {
    throw new WorkflowEditError(`Trigger ${trigger} is not declared by source node ${source.id}.`);
  }
  return { id, source: source.id, target: target.id, trigger, output_map: { ...outputMap } };
}

function splitOutputMap(
  oldEdge: WorkflowEdge,
  source: WorkflowNode,
  inserted: WorkflowNode,
  target: WorkflowNode,
): { upstream: Record<string, string>; downstream: Record<string, string> } {
  const original = Object.keys(oldEdge.output_map).length > 0
    ? oldEdge.output_map
    : defaultOutputMap(source, target);
  const upstreamAccepted = acceptedArtifacts(inserted);
  const upstream: Record<string, string> = {};
  for (const [output, destination] of Object.entries(original)) {
    if (source.outputs.includes(output) && upstreamAccepted.has(destination)) upstream[output] = destination;
  }
  const downstream: Record<string, string> = {};
  for (const [oldOutput, destination] of Object.entries(original)) {
    const possibleOutput = inserted.outputs.includes(oldOutput) ? oldOutput : destination;
    if (inserted.outputs.includes(possibleOutput) && acceptedArtifacts(target).has(destination)) {
      downstream[possibleOutput] = destination;
    }
  }
  return { upstream, downstream };
}

function hasPath(document: WorkflowDocument, start: string, target: string): boolean {
  const pending = [start];
  const seen = new Set<string>();
  while (pending.length > 0) {
    const current = pending.pop()!;
    if (current === target) return true;
    if (seen.has(current)) continue;
    seen.add(current);
    for (const edge of document.edges) if (edge.source === current) pending.push(edge.target);
  }
  return false;
}

function projectedTaskSkill(node: ProjectionNode, catalog: CatalogData): string | null {
  if (node.suggested_skill_ids.length !== 1) return null;
  const suggestion = node.suggested_skill_ids[0];
  if (!suggestion) return null;
  const matches = catalog.skills.filter((skill) => skill.catalog_id === suggestion);
  return matches.length === 1 && !matches[0]!.ambiguous ? suggestion : null;
}

function projectedValidator(node: ProjectionNode, catalog: CatalogData): string | null {
  if (node.suggested_validator_ids.length !== 1) return null;
  const suggestion = node.suggested_validator_ids[0];
  if (!suggestion) return null;
  const matches = catalog.validators.filter((validator) => validator.validator_id === suggestion);
  return matches.length === 1 && matches[0]!.available !== false ? suggestion : null;
}

function projectionNodeToWorkflowNode(node: ProjectionNode, catalog: CatalogData): WorkflowNode {
  const common = {
    ...emptyNodeFields(),
    id: node.id,
    display_name: node.display_name,
    origin_projection_node_id: node.id,
    inputs: [...node.inputs],
    outputs: node.projection_kind === 'gate' ? [] : [...node.outputs],
    write_scopes: [...new Set([...node.write_scopes, ...node.outputs])],
  };
  if (node.projection_kind === 'gate') {
    return {
      ...common,
      type: 'validator',
      skill_ref: null,
      validator_ref: projectedValidator(node, catalog),
      validator_config: null,
      outcomes: ['pass', 'fail', 'blocked'],
    };
  }
  return {
    ...common,
    type: 'task',
    skill_ref: projectedTaskSkill(node, catalog),
    validator_ref: null,
    validator_config: null,
    outcomes: ['succeeded'],
  };
}

function assertAcyclic(nodes: WorkflowNode[], edges: WorkflowEdge[]): void {
  const indegree = new Map(nodes.map((node) => [node.id, 0]));
  for (const edge of edges) {
    if (!indegree.has(edge.source) || !indegree.has(edge.target)) {
      throw new WorkflowEditError(`Projection edge does not resolve: ${edge.id}`);
    }
    indegree.set(edge.target, indegree.get(edge.target)! + 1);
  }
  const pending = [...indegree].filter(([, degree]) => degree === 0).map(([id]) => id);
  let visited = 0;
  while (pending.length > 0) {
    const current = pending.pop()!;
    visited += 1;
    for (const edge of edges) {
      if (edge.source !== current) continue;
      const next = indegree.get(edge.target)! - 1;
      indegree.set(edge.target, next);
      if (next === 0) pending.push(edge.target);
    }
  }
  if (visited !== nodes.length) throw new WorkflowEditError('Projection graph must be acyclic.');
}

/** Copy the read-only projection into an editable, intentionally unbound custom draft. */
export function cloneProjection(
  projectionData: ProjectionData,
  catalog: CatalogData,
  workflowId: string,
): WorkflowDocument {
  assertIdentifier(workflowId, 'workflow_id');
  const projection = projectionData.projection;
  if (!PROJECTION_IDENTIFIER.test(projection.projection_id) || !SHA256.test(projectionData.sha256)) {
    throw new WorkflowEditError('Projection identity or digest is invalid.');
  }
  const nodeMap = new Map(projection.nodes.map((node) => [node.id, projectionNodeToWorkflowNode(node, catalog)]));
  if (nodeMap.size !== projection.nodes.length || nodeMap.size === 0) {
    throw new WorkflowEditError('Projection node IDs must be unique and non-empty.');
  }
  projection.nodes.forEach((node) => assertIdentifier(node.id, 'projection node id'));
  const incoming = new Set(projection.edges.map((edge) => edge.target));
  const nodes = projection.nodes.map((source) => ({
    ...nodeMap.get(source.id)!,
    entry: !incoming.has(source.id),
  }) as WorkflowNode);
  const edges = projection.edges.map((sourceEdge) => {
    const source = nodeMap.get(sourceEdge.source);
    const target = nodeMap.get(sourceEdge.target);
    if (!source || !target) throw new WorkflowEditError(`Projection edge does not resolve: ${sourceEdge.id}`);
    return makeEdge(sourceEdge.id, source, target);
  });
  if (new Set(edges.map((edge) => edge.id)).size !== edges.length) {
    throw new WorkflowEditError('Projection edge IDs must be unique.');
  }
  assertAcyclic(nodes, edges);
  const externalInputs = [...new Set(nodes.flatMap((node) => node.inputs.filter((input) => {
    return !edges.some((edge) => {
      if (edge.target !== node.id) return false;
      const source = nodeMap.get(edge.source)!;
      return source.outputs.some((output) => (edge.output_map[output] ?? output) === input);
    });
  })))].sort();
  return {
    schema_version: 'paper-workflow-custom-v1',
    workflow_id: workflowId,
    document_revision: 0,
    semantic_revision: 0,
    derived_from: { projection_id: projection.projection_id, projection_sha256: projectionData.sha256 },
    max_parallelism: 1,
    external_inputs: externalInputs,
    nodes,
    edges,
    ui: { positions: Object.fromEntries(nodes.map((_node, index) => [nodes[index]!.id, positionForIndex(index)])) },
  };
}

/** Add a detached node. New enabled roots are explicitly marked as entries. */
export function addNode(
  document: WorkflowDocument,
  node: WorkflowNode,
  position?: WorkflowPosition,
): WorkflowDocument {
  const addition = cloneNewNode(document, node);
  const location = position ?? positionForIndex(document.nodes.length);
  assertPosition(location);
  const added = {
    ...cloneWorkflowDocument(document),
    nodes: [...document.nodes.map(cloneNode), addition],
    ui: {
      positions: {
        ...Object.fromEntries(Object.entries(document.ui.positions).map(([id, value]) => [id, { ...value }])),
        [addition.id]: { ...location },
      },
    },
  };
  return markEnabledRoots(added);
}

/** Duplicate one node without copying projection provenance or incident edges. */
export function duplicateNode(document: WorkflowDocument, nodeId: string): WorkflowDocument {
  const original = nodeById(document, nodeId);
  const id = uniqueId(`${original.id}-copy`, new Set(document.nodes.map((node) => node.id)), 'node-copy');
  const duplicate = { ...cloneNode(original), id, origin_projection_node_id: null } as WorkflowNode;
  const position = document.ui.positions[nodeId] ?? positionForIndex(document.nodes.length);
  return addNode(document, duplicate, { x: position.x + 48, y: position.y + 48 });
}

/** Enable or disable a node while retaining its edges so it can be re-enabled later. */
export function setNodeEnabled(
  document: WorkflowDocument,
  nodeId: string,
  enabled: boolean,
): WorkflowDocument {
  nodeById(document, nodeId);
  if (typeof enabled !== 'boolean') throw new WorkflowEditError('enabled must be a boolean.');
  const updated = {
    ...cloneWorkflowDocument(document),
    nodes: document.nodes.map((node) => node.id === nodeId ? { ...cloneNode(node), enabled } as WorkflowNode : cloneNode(node)),
  };
  return markEnabledRoots(updated);
}

/** Remove one node and its incident edges, and make newly orphaned enabled nodes explicit roots. */
export function deleteNode(document: WorkflowDocument, nodeId: string): WorkflowDocument {
  nodeById(document, nodeId);
  const updated: WorkflowDocument = {
    ...cloneWorkflowDocument(document),
    nodes: document.nodes.filter((node) => node.id !== nodeId).map(cloneNode),
    edges: document.edges.filter((edge) => edge.source !== nodeId && edge.target !== nodeId).map((edge) => ({
      ...edge,
      output_map: { ...edge.output_map },
    })),
    ui: {
      positions: Object.fromEntries(
        Object.entries(document.ui.positions)
          .filter(([id]) => id !== nodeId)
          .map(([id, position]) => [id, { ...position }]),
      ),
    },
  };
  return markEnabledRoots(updated);
}

/** Change only canvas coordinates; workflow behavior and server revisions are preserved. */
export function moveNode(
  document: WorkflowDocument,
  nodeId: string,
  position: WorkflowPosition,
): WorkflowDocument {
  nodeById(document, nodeId);
  assertPosition(position);
  return {
    ...cloneWorkflowDocument(document),
    ui: {
      positions: {
        ...Object.fromEntries(Object.entries(document.ui.positions).map(([id, value]) => [id, { ...value }])),
        [nodeId]: { ...position },
      },
    },
  };
}

/** Connect two nodes with an explicit, schema-valid trigger and an identity output map where possible. */
export function connectNodes(
  document: WorkflowDocument,
  sourceId: string,
  targetId: string,
  trigger?: string,
): WorkflowDocument {
  const source = nodeById(document, sourceId);
  const target = nodeById(document, targetId);
  if (sourceId === targetId || hasPath(document, targetId, sourceId)) {
    throw new WorkflowEditError('Connections cannot create a cycle.');
  }
  const selectedTrigger = trigger ?? triggerFor(source);
  if (document.edges.some((edge) => edge.source === sourceId && edge.target === targetId && edge.trigger === selectedTrigger)) {
    throw new WorkflowEditError('This connection already exists.');
  }
  const id = nextEdgeId(document);
  assertUniqueEdgeId(document, id);
  const edge = makeEdge(id, source, target, selectedTrigger);
  const updated: WorkflowDocument = {
    ...cloneWorkflowDocument(document),
    nodes: document.nodes.map((node) => node.id === targetId
      ? { ...cloneNode(node), entry: false } as WorkflowNode
      : cloneNode(node)),
    edges: [...document.edges.map((item) => ({ ...item, output_map: { ...item.output_map } })), edge],
  };
  return updated;
}

/** Remove a connection without mutating the source; orphaned enabled targets become entries. */
export function disconnectEdge(document: WorkflowDocument, edgeId: string): WorkflowDocument {
  const edge = document.edges.find((candidate) => candidate.id === edgeId);
  if (!edge) throw new WorkflowEditError(`Unknown edge: ${edgeId}`);
  const updated: WorkflowDocument = {
    ...cloneWorkflowDocument(document),
    edges: document.edges.filter((candidate) => candidate.id !== edgeId).map((item) => ({
      ...item,
      output_map: { ...item.output_map },
    })),
  };
  return markEnabledRoots(updated);
}

function addInsertedNode(
  document: WorkflowDocument,
  node: WorkflowNode,
  position: WorkflowPosition,
): WorkflowDocument {
  const inserted = cloneNewNode(document, { ...cloneNode(node), entry: false } as WorkflowNode);
  assertPosition(position);
  return {
    ...cloneWorkflowDocument(document),
    nodes: [...document.nodes.map(cloneNode), inserted],
    ui: {
      positions: {
        ...Object.fromEntries(Object.entries(document.ui.positions).map(([id, point]) => [id, { ...point }])),
        [inserted.id]: { ...position },
      },
    },
  };
}

function midpoint(first: WorkflowPosition | undefined, second: WorkflowPosition | undefined, fallback: WorkflowPosition): WorkflowPosition {
  if (!first || !second) return fallback;
  return { x: (first.x + second.x) / 2, y: (first.y + second.y) / 2 };
}

/** Insert a node into an unbranched incoming segment and atomically rewire its two edges. */
export function insertBefore(
  document: WorkflowDocument,
  targetNodeId: string,
  node: WorkflowNode,
): WorkflowDocument {
  const target = nodeById(document, targetNodeId);
  const incoming = document.edges.filter((edge) => edge.target === targetNodeId);
  if (incoming.length !== 1) throw new WorkflowEditError('Insert before requires exactly one incoming edge.');
  const oldEdge = incoming[0]!;
  const source = nodeById(document, oldEdge.source);
  if (document.edges.filter((edge) => edge.source === source.id).length !== 1) {
    throw new WorkflowEditError('Insert before requires an unbranched predecessor.');
  }
  const inserted = cloneNewNode(document, { ...cloneNode(node), entry: false } as WorkflowNode);
  const maps = splitOutputMap(oldEdge, source, inserted, target);
  const insertedDoc = addInsertedNode(document, inserted, midpoint(
    document.ui.positions[source.id],
    document.ui.positions[targetNodeId],
    positionForIndex(document.nodes.length),
  ));
  const first = makeEdge(oldEdge.id, source, inserted, oldEdge.trigger, maps.upstream);
  const second = makeEdge(nextEdgeId(insertedDoc), inserted, target, triggerFor(inserted), maps.downstream);
  const result: WorkflowDocument = {
    ...insertedDoc,
    nodes: insertedDoc.nodes.map((item) => item.id === targetNodeId
      ? { ...cloneNode(item), entry: false } as WorkflowNode
      : cloneNode(item)),
    edges: [...insertedDoc.edges.filter((edge) => edge.id !== oldEdge.id), first, second],
  };
  return result;
}

/** Insert a node into an unbranched outgoing segment and atomically rewire its two edges. */
export function insertAfter(
  document: WorkflowDocument,
  sourceNodeId: string,
  node: WorkflowNode,
): WorkflowDocument {
  const source = nodeById(document, sourceNodeId);
  const outgoing = document.edges.filter((edge) => edge.source === sourceNodeId);
  if (outgoing.length !== 1) throw new WorkflowEditError('Insert after requires exactly one outgoing edge.');
  const oldEdge = outgoing[0]!;
  const target = nodeById(document, oldEdge.target);
  if (document.edges.filter((edge) => edge.target === target.id).length !== 1) {
    throw new WorkflowEditError('Insert after requires an unbranched successor.');
  }
  const inserted = cloneNewNode(document, { ...cloneNode(node), entry: false } as WorkflowNode);
  const maps = splitOutputMap(oldEdge, source, inserted, target);
  const insertedDoc = addInsertedNode(document, inserted, midpoint(
    document.ui.positions[sourceNodeId],
    document.ui.positions[target.id],
    positionForIndex(document.nodes.length),
  ));
  const first = makeEdge(oldEdge.id, source, inserted, oldEdge.trigger, maps.upstream);
  const second = makeEdge(nextEdgeId(insertedDoc), inserted, target, triggerFor(inserted), maps.downstream);
  return {
    ...insertedDoc,
    edges: [...insertedDoc.edges.filter((edge) => edge.id !== oldEdge.id), first, second],
  };
}

/** Update one node's user-editable properties while retaining its identity, type, and provenance. */
export function updateNode(
  document: WorkflowDocument,
  nodeId: string,
  patch: NodePropertyPatch,
): WorkflowDocument {
  const current = nodeById(document, nodeId);
  const allowed = new Set([
    'display_name', 'entry', 'enabled', 'skill_ref', 'validator_ref', 'validator_config',
    'inputs', 'outputs', 'outcomes', 'write_scopes', 'failure_policy', 'condition_cases', 'join_mode',
  ]);
  if (Object.keys(patch).some((key) => !allowed.has(key))) {
    throw new WorkflowEditError('Node identity, type, and projection provenance cannot be edited.');
  }
  const copiedPatch = cloneJson(patch as unknown as JsonValue) as unknown as NodePropertyPatch;
  const updated = {
    ...cloneWorkflowDocument(document),
    nodes: document.nodes.map((node) => node.id === nodeId
      ? { ...cloneNode(current), ...copiedPatch, id: current.id, type: current.type,
          origin_projection_node_id: current.origin_projection_node_id } as WorkflowNode
      : cloneNode(node)),
  };
  return markEnabledRoots(updated);
}

/** Update workflow identity, concurrency, or declared external artifact IDs. */
export function updateWorkflowSettings(
  document: WorkflowDocument,
  patch: WorkflowSettingsPatch,
): WorkflowDocument {
  const allowed = new Set(['workflow_id', 'max_parallelism', 'external_inputs']);
  if (Object.keys(patch).some((key) => !allowed.has(key))) {
    throw new WorkflowEditError('Only workflow ID, parallelism, and external inputs are editable settings.');
  }
  if (patch.workflow_id !== undefined) assertIdentifier(patch.workflow_id, 'workflow_id');
  if (
    patch.max_parallelism !== undefined
    && (!Number.isInteger(patch.max_parallelism) || patch.max_parallelism < 1 || patch.max_parallelism > MAX_PARALLELISM)
  ) {
    throw new WorkflowEditError(`max_parallelism must be an integer from 1 to ${MAX_PARALLELISM}.`);
  }
  if (patch.external_inputs !== undefined) {
    patch.external_inputs.forEach((input) => assertIdentifier(input, 'external input'));
    if (new Set(patch.external_inputs).size !== patch.external_inputs.length) {
      throw new WorkflowEditError('External input IDs must be unique.');
    }
  }
  return {
    ...cloneWorkflowDocument(document),
    ...patch,
    external_inputs: patch.external_inputs === undefined ? [...document.external_inputs] : [...patch.external_inputs],
  };
}

function semanticPayload(document: WorkflowDocument): unknown {
  return {
    schema_version: document.schema_version,
    workflow_id: document.workflow_id,
    derived_from: document.derived_from,
    max_parallelism: document.max_parallelism,
    external_inputs: [...document.external_inputs],
    nodes: [...document.nodes].sort((left, right) => compareCanonicalText(left.id, right.id)).map((node) => ({
      id: node.id,
      type: node.type,
      entry: node.entry,
      enabled: node.enabled,
      skill_ref: node.skill_ref,
      validator_ref: node.validator_ref,
      validator_config: node.validator_config,
      origin_projection_node_id: node.origin_projection_node_id,
      inputs: node.inputs,
      outputs: node.outputs,
      outcomes: node.outcomes,
      write_scopes: node.write_scopes,
      failure_policy: node.failure_policy,
      condition_cases: node.condition_cases,
      join_mode: node.join_mode,
    })),
    edges: [...document.edges].sort((left, right) => compareCanonicalText(left.id, right.id)).map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      trigger: edge.trigger,
      output_map: Object.fromEntries(Object.entries(edge.output_map).sort(([a], [b]) => compareCanonicalText(a, b))),
    })),
  };
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => compareCanonicalText(left, right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`)
      .join(',')}}`;
  }
  return JSON.stringify(value);
}

/** Match the backend's behavior-vs-canvas boundary; document revision is server-owned. */
export function classifyWorkflowEdit(
  before: WorkflowDocument,
  after: WorkflowDocument,
): EditClassification {
  return stableJson(semanticPayload(before)) === stableJson(semanticPayload(after)) ? 'visual' : 'semantic';
}

export function createWorkflowEditState(document: WorkflowDocument): WorkflowEditState {
  return { document: cloneWorkflowDocument(document), dirtyGeneration: 0, lastClassification: null };
}

/** Apply a pure operation to editor state and advance its non-persisted dirty generation once. */
export function applyWorkflowEdit(
  state: WorkflowEditState,
  operation: (document: WorkflowDocument) => WorkflowDocument,
): WorkflowEditState {
  const nextDocument = operation(state.document);
  return {
    document: nextDocument,
    dirtyGeneration: nextDirtyGeneration(state.dirtyGeneration),
    lastClassification: classifyWorkflowEdit(state.document, nextDocument),
  };
}

export function nextDirtyGeneration(generation: number): number {
  if (!Number.isSafeInteger(generation) || generation < 0 || generation === Number.MAX_SAFE_INTEGER) {
    throw new WorkflowEditError('Dirty generation must be a non-negative safe integer with room to advance.');
  }
  return generation + 1;
}

/** Fast, non-authoritative hints for obvious omissions while the user is editing. */
export function localWorkflowHints(document: WorkflowDocument): LocalWorkflowHint[] {
  const enabled = document.nodes.filter((node) => node.enabled);
  const hints: LocalWorkflowHint[] = [];
  if (enabled.length === 0) {
    hints.push({ code: 'local.no_enabled_nodes', message: '当前没有启用的阶段。', node_id: '' });
    return hints;
  }
  const entries = enabled.filter((node) => node.entry);
  if (entries.length !== 1) {
    hints.push({ code: 'local.entry_count', message: `当前标记了 ${entries.length} 个入口；建议检查流程起点。`, node_id: '' });
  }
  for (const node of enabled) {
    if (node.type === 'task' && !node.skill_ref) {
      hints.push({ code: 'local.task_unbound', message: '此任务阶段尚未绑定 Skill。', node_id: node.id });
    }
    if (node.type === 'validator' && (!node.validator_ref || !node.validator_config)) {
      hints.push({ code: 'local.validator_unconfigured', message: '此验证阶段尚未选择并配置内置验证器。', node_id: node.id });
    }
  }
  return hints;
}
