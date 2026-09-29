import type { WorkflowDocument, WorkflowNode, WorkflowPosition } from './types';
import {
  addNode,
  cloneWorkflowDocument,
  connectNodes,
  insertAfter,
  WorkflowEditError,
} from './workflow';

const LAYER_GAP = 340;
const ROW_GAP = 200;
const HISTORY_LIMIT = 50;

/** Append at a terminal stage, or insert into its single outgoing route. */
export function appendStage(
  document: WorkflowDocument,
  sourceId: string,
  node: WorkflowNode,
): WorkflowDocument {
  const source = document.nodes.find((candidate) => candidate.id === sourceId);
  if (!source) throw new WorkflowEditError(`找不到阶段“${sourceId}”，请重新选择添加位置。`);
  const outgoing = document.edges.filter((edge) => edge.source === sourceId);
  if (outgoing.length > 1) {
    throw new WorkflowEditError('此阶段有多个后续分支，请选择“独立添加，稍后连接”，再在阶段连接中接入需要的分支。');
  }
  if (outgoing.length === 1) return insertAfter(document, sourceId, node);

  const sourcePosition = document.ui.positions[sourceId];
  const position = sourcePosition
    ? { x: sourcePosition.x + LAYER_GAP, y: sourcePosition.y }
    : undefined;
  const connected = connectNodes(addNode(document, node, position), sourceId, node.id);
  // addNode normalizes roots; appending must retain all existing entry choices.
  const entries = new Map(document.nodes.map((existing) => [existing.id, existing.entry]));
  connected.nodes = connected.nodes.map((existing) => entries.has(existing.id)
    ? { ...existing, entry: entries.get(existing.id)! }
    : existing);
  return connected;
}

/** Arrange every node, including disabled stages, without changing workflow semantics. */
export function arrangeWorkflow(document: WorkflowDocument): WorkflowDocument {
  const indegree = new Map(document.nodes.map((node) => [node.id, 0]));
  if (indegree.size !== document.nodes.length) {
    throw new WorkflowEditError('阶段标识存在重复，请先修正后再整理布局。');
  }
  if (document.nodes.some((node) => !node.id)) {
    throw new WorkflowEditError('阶段缺少标识，请先补全后再整理布局。');
  }
  const outgoing = new Map(document.nodes.map((node) => [node.id, [] as string[]]));
  const edgeIds = new Set<string>();
  for (const edge of document.edges) {
    if (!edge.id) throw new WorkflowEditError('连线缺少标识，请先修正后再整理布局。');
    if (edgeIds.has(edge.id)) {
      throw new WorkflowEditError(`连线标识“${edge.id}”重复，请先修正后再整理布局。`);
    }
    edgeIds.add(edge.id);
    for (const endpoint of [edge.source, edge.target]) {
      if (!indegree.has(endpoint)) {
        throw new WorkflowEditError(`连线“${edge.id}”引用了不存在的阶段“${endpoint}”，请修正连线。`);
      }
    }
    indegree.set(edge.target, indegree.get(edge.target)! + 1);
    outgoing.get(edge.source)!.push(edge.target);
  }

  const pending = document.nodes.filter((node) => indegree.get(node.id) === 0).map((node) => node.id);
  const layers = new Map(document.nodes.map((node) => [node.id, 0]));
  for (let cursor = 0; cursor < pending.length; cursor += 1) {
    const current = pending[cursor]!;
    for (const target of outgoing.get(current)!) {
      layers.set(target, Math.max(layers.get(target)!, layers.get(current)! + 1));
      const remaining = indegree.get(target)! - 1;
      indegree.set(target, remaining);
      if (remaining === 0) pending.push(target);
    }
  }
  if (pending.length !== document.nodes.length) {
    throw new WorkflowEditError('流程中存在循环连线，请断开循环后再整理布局。');
  }

  // Document order determines row order, independent of edge traversal order.
  const rows = new Map<number, number>();
  const positions: Record<string, WorkflowPosition> = {};
  for (const node of document.nodes) {
    const layer = layers.get(node.id)!;
    const row = rows.get(layer) ?? 0;
    positions[node.id] = { x: 120 + layer * LAYER_GAP, y: 100 + row * ROW_GAP };
    rows.set(layer, row + 1);
  }
  return { ...cloneWorkflowDocument(document), ui: { positions } };
}

export interface EditorHistory {
  past: WorkflowDocument[];
  future: WorkflowDocument[];
}

export interface EditorHistoryResult {
  history: EditorHistory;
  document: WorkflowDocument;
}

/** Start a local editing session. Call again after saving or loading a document. */
export function createEditorHistory(_document: WorkflowDocument): EditorHistory {
  return { past: [], future: [] };
}

/** Record the state before an edit; a new edit invalidates the redo branch. */
export function recordHistory(history: EditorHistory, previous: WorkflowDocument): EditorHistory {
  return {
    past: [...history.past.slice(-(HISTORY_LIMIT - 1)), previous].map(cloneWorkflowDocument),
    future: [],
  };
}

function restoreContent(snapshot: WorkflowDocument, current: WorkflowDocument): WorkflowDocument {
  return {
    ...cloneWorkflowDocument(snapshot),
    document_revision: current.document_revision,
    semantic_revision: current.semantic_revision,
  };
}

/** Restore content only: server revisions always belong to the current session. */
export function undoDocument(history: EditorHistory, current: WorkflowDocument): EditorHistoryResult | null {
  const previous = history.past.at(-1);
  if (!previous) return null;
  return {
    document: restoreContent(previous, current),
    history: {
      past: history.past.slice(0, -1).map(cloneWorkflowDocument),
      future: [...history.future.slice(-(HISTORY_LIMIT - 1)), current].map(cloneWorkflowDocument),
    },
  };
}

export function redoDocument(history: EditorHistory, current: WorkflowDocument): EditorHistoryResult | null {
  const next = history.future.at(-1);
  if (!next) return null;
  return {
    document: restoreContent(next, current),
    history: {
      past: [...history.past.slice(-(HISTORY_LIMIT - 1)), current].map(cloneWorkflowDocument),
      future: history.future.slice(0, -1).map(cloneWorkflowDocument),
    },
  };
}
