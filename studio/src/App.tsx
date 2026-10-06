import { useEffect, useMemo, useState } from 'react';
import type { Connection, Edge } from '@xyflow/react';
import { ApiClient, ApiClientError, consumeSessionToken } from './api';
import {
  addApprovalGate,
  addNode,
  applyWorkflowEdit,
  cloneProjection,
  connectNodes,
  createBlankWorkflow,
  createWorkflowEditState,
  deleteNode,
  disconnectEdge,
  duplicateNode,
  insertAfter,
  insertBefore,
  moveNode,
  localWorkflowHints,
  updateNode,
  updateWorkflowSettings,
  type WorkflowEditState,
} from './workflow';
import type {
  BootstrapData,
  ActiveWorkflowSummary,
  CatalogData,
  ConditionNode,
  JoinNode,
  ProjectionData,
  SkillCatalogEntry,
  TaskNode,
  ValidationData,
  ValidatorCatalogEntry,
  ValidatorNode,
  WorkflowDocument,
  WorkflowEdge,
  WorkflowIssue,
  WorkflowNode,
  WorkflowNodeType,
  WorkflowData,
  SelectionData,
} from './types';
import { GraphOutline } from './components/GraphOutline';
import { NodeInspector } from './components/NodeInspector';
import { NodePalette } from './components/NodePalette';
import { StudioHeader } from './components/StudioHeader';
import { ValidationPanel } from './components/ValidationPanel';
import { WorkflowCanvas } from './components/WorkflowCanvas';
import { RiskAcknowledgementDialog } from './components/RiskAcknowledgementDialog';
import { RevisionConflictDialog } from './components/RevisionConflictDialog';
import { appendStage, arrangeWorkflow, createEditorHistory, recordHistory, undoDocument, redoDocument } from './editorTools';
import { EditorToolbar } from './components/EditorToolbar';
import { WorkflowGuide } from './components/WorkflowGuide';

interface AppProps {
  api?: ApiClient;
}

function slug(value: string): string {
  const candidate = value.toLocaleLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64);
  return /^[a-z]/.test(candidate) ? candidate : 'custom-paper-flow';
}

function nodeFor(type: WorkflowNodeType, id: string, skillRef?: string): WorkflowNode {
  const base = {
    id,
    display_name: type === 'task' ? '新任务阶段' : type === 'condition' ? '条件分支' : type === 'join' ? '分支汇合' : '验证阶段',
    entry: false,
    enabled: true,
    origin_projection_node_id: null,
    inputs: [] as string[],
    outputs: [] as string[],
    outcomes: [] as string[],
    write_scopes: [] as string[],
    failure_policy: 'block' as const,
    condition_cases: [],
    join_mode: 'all_active' as const,
  };
  if (type === 'task') return { ...base, type, skill_ref: skillRef ?? null, validator_ref: null, validator_config: null, outcomes: ['succeeded'] } satisfies TaskNode;
  if (type === 'validator') return { ...base, type, skill_ref: null, validator_ref: null, validator_config: null, outcomes: ['pass', 'fail', 'blocked'] } satisfies ValidatorNode;
  if (type === 'condition') return { ...base, type, skill_ref: null, validator_ref: null, validator_config: null, outcomes: [], condition_cases: [{ outcome: 'ready', when: { op: 'fact_is', name: 'ready', value: true } }] } satisfies ConditionNode;
  return { ...base, type, skill_ref: null, validator_ref: null, validator_config: null, outcomes: [] } satisfies JoinNode;
}

function nextNodeId(document: WorkflowDocument): string {
  const ids = new Set(document.nodes.map((node) => node.id));
  let index = 1;
  while (ids.has(`stage-${index}`)) index += 1;
  return `stage-${index}`;
}

function safeErrorMessage(error: unknown): string {
  if (error instanceof ApiClientError) {
    if (error.payload?.errors[0]?.code === 'run.already_active') return '当前已有自定义流程运行；请先切回官方流程，再启用新版本。';
    return error.message;
  }
  return error instanceof Error ? error.message : '操作未完成，请检查连接后重试。';
}

function issueFromError(error: unknown): WorkflowIssue {
  if (error instanceof ApiClientError && error.payload?.errors[0]) return error.payload.errors[0];
  return {
    code: error instanceof ApiClientError ? `http.${error.status}` : 'studio.request_failed',
    message: safeErrorMessage(error),
    operation: 'studio',
    recovery: '保留当前浏览器中的草稿内容，检查服务器状态后重试。',
    node_id: '',
    edge_id: '',
  };
}

interface ActivationPreview {
  workflow: WorkflowDocument;
  documentRevision: number;
  semanticSha256: string;
  requiredWarningCodes: string[];
  warnings: WorkflowIssue[];
}

function isWorkflowRevisionConflict(error: unknown): boolean {
  if (!(error instanceof ApiClientError) || error.status !== 409) return false;
  const code = error.payload?.errors[0]?.code;
  return code === 'store.revision_conflict'
    || code === 'studio.activation_conflict'
    || code === 'activation.acknowledgement_mismatch'
    || code === 'activation.draft_mismatch';
}

function selectionSummary(selection: SelectionData, runId: string): ActiveWorkflowSummary | null {
  if (selection.mode !== 'custom') return null;
  return {
    workflow_id: selection.workflow_id,
    semantic_revision: selection.semantic_revision,
    semantic_sha256: selection.semantic_sha256,
    run_id: runId,
  };
}

export function App({ api: providedApi }: AppProps) {
  const [sessionToken] = useState(() => providedApi ? null : consumeSessionToken());
  const api = useMemo(() => providedApi ?? (sessionToken ? new ApiClient(sessionToken) : null), [providedApi, sessionToken]);
  const [bootstrap, setBootstrap] = useState<BootstrapData | null>(null);
  const [catalog, setCatalog] = useState<CatalogData | null>(null);
  const [projection, setProjection] = useState<ProjectionData | null>(null);
  const [savedData, setSavedData] = useState<WorkflowData | null>(null);
  const [editor, setEditor] = useState<WorkflowEditState | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [errors, setErrors] = useState<WorkflowIssue[]>([]);
  const [warnings, setWarnings] = useState<WorkflowIssue[]>([]);
  const [lastValidation, setLastValidation] = useState<ValidationData | null>(null);
  const [advisoryHints, setAdvisoryHints] = useState<ReturnType<typeof localWorkflowHints>>([]);
  const [projectionNotes, setProjectionNotes] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [refreshingCatalog, setRefreshingCatalog] = useState(false);
  const [refreshingStatus, setRefreshingStatus] = useState(false);
  const [actionMessage, setActionMessage] = useState('');
  const [loadError, setLoadError] = useState<WorkflowIssue | null>(null);
  const [activationPreview, setActivationPreview] = useState<ActivationPreview | null>(null);
  const [acknowledgedCodes, setAcknowledgedCodes] = useState<string[]>([]);
  const [riskDialogOpen, setRiskDialogOpen] = useState(false);
  const [revisionConflict, setRevisionConflict] = useState(false);
  const [conflictDialogOpen, setConflictDialogOpen] = useState(false);
  const [history, setHistory] = useState<ReturnType<typeof createEditorHistory> | null>(null);
  const [placement, setPlacement] = useState<'after' | 'detached'>('after');
  const [focusRequest, setFocusRequest] = useState(0);
  const [layoutRequest, setLayoutRequest] = useState(0);
  const [handoffCopied, setHandoffCopied] = useState(false);

  useEffect(() => {
    if (!api) {
      setLoading(false);
      setLoadError({
        code: 'studio.session_missing',
        message: '此编辑器需要由本机 Workflow Studio 启动。请从 Orchestrator 的自定义流程入口重新打开。',
        operation: 'bootstrap',
        recovery: '关闭此页面，并从本机工作流入口启动编辑器。',
        node_id: '',
        edge_id: '',
      });
      return;
    }
    let active = true;
    async function load() {
      try {
        const bootstrapResult = await api!.getBootstrap();
        if (!bootstrapResult.data) throw new Error('服务器没有返回工作流会话信息。');
        api!.setCsrfToken(bootstrapResult.data.csrf_token);
        const [catalogResult, projectionResult, workflowResult] = await Promise.all([
          api!.getCatalog(), api!.getProjection(), api!.getWorkflow(),
        ]);
        if (!catalogResult.data || !projectionResult.data || !workflowResult.data) {
          throw new Error('服务器返回的工作流资料不完整。');
        }
        if (!active) return;
        setBootstrap(bootstrapResult.data);
        setCatalog(catalogResult.data);
        setProjection(projectionResult.data);
        setSavedData(workflowResult.data);
        setErrors(catalogResult.errors);
        setWarnings(catalogResult.warnings);
        const notes = projectionResult.data.projection.projection_notes.map((note) => {
          const text = note.studio_behavior ?? note.projection_note ?? note.kind;
          return typeof text === 'string' ? text : '官方流程含有无法直接映射为有向无环图的反馈行为。';
        });
        setProjectionNotes(notes);
        if (bootstrapResult.data.mode === 'custom' && workflowResult.data.workflow) {
          const next = createWorkflowEditState(workflowResult.data.workflow);
          setEditor(next);
          setHistory(createEditorHistory(next.document));
          setSelectedNodeId(next.document.nodes[0]?.id ?? null);
        }
      } catch (error) {
        if (active) setLoadError(issueFromError(error));
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => { active = false; };
  }, [api]);

  const officialPreview = useMemo(() => {
    if (!projection || !catalog) return null;
    try { return cloneProjection(projection, catalog, 'official-v1-preview'); }
    catch { return null; }
  }, [projection, catalog]);
  const workflow = editor?.document ?? officialPreview;
  const editable = editor !== null;
  const skillEntries: SkillCatalogEntry[] = catalog?.skills ?? [];
  const validatorEntries: ValidatorCatalogEntry[] = catalog?.validators ?? [];
  const selectedNode = workflow?.nodes.find((node) => node.id === selectedNodeId) ?? null;
  const dirty = Boolean(editor && editor.dirtyGeneration > 0);
  const newWorkflowId = `custom-${slug(bootstrap?.project_label ?? 'paper-flow')}-flow`;

  useEffect(() => {
    const protectReload = (event: BeforeUnloadEvent) => {
      if (!editor || editor.dirtyGeneration === 0) return;
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', protectReload);
    return () => window.removeEventListener('beforeunload', protectReload);
  }, [editor]);

  useEffect(() => {
    if (!editor) {
      setAdvisoryHints([]);
      return;
    }
    const snapshot = editor.document;
    const timer = window.setTimeout(() => setAdvisoryHints(localWorkflowHints(snapshot)), 300);
    return () => window.clearTimeout(timer);
  }, [editor]);

  function performEdit(operation: (document: WorkflowDocument) => WorkflowDocument): boolean {
    if (!editor) return false;
    try {
      const next = applyWorkflowEdit(editor, operation);
      setHistory(recordHistory(history ?? createEditorHistory(editor.document), editor.document));
      setEditor(next);
      setErrors([]);
      setWarnings([]);
      setLastValidation(null);
      setActivationPreview(null);
      setAcknowledgedCodes([]);
      setRiskDialogOpen(false);
      setActionMessage(next.lastClassification === 'visual' ? '布局已更新。' : '流程内容已修改，需要重新验证。');
      return true;
    } catch (error) {
      setActionMessage(safeErrorMessage(error));
      return false;
    }
  }

  function startEditing(document: WorkflowDocument) {
    const initial = createWorkflowEditState(document);
    const unsaved = applyWorkflowEdit(initial, (current) => current);
    setEditor(unsaved);
    setHistory(createEditorHistory(document));
    setLayoutRequest((value) => value + 1);
    setSelectedNodeId(document.nodes[0]?.id ?? null);
    setErrors([]);
    setWarnings([]);
    setLastValidation(null);
    setActionMessage('已在浏览器中创建草稿；保存前不会更改项目文件。');
  }

  function openSavedDraft() {
    if (!savedData?.workflow) return;
    setEditor(createWorkflowEditState(savedData.workflow));
    setHistory(createEditorHistory(savedData.workflow));
    setLayoutRequest((value) => value + 1);
    setSelectedNodeId(savedData.workflow.nodes[0]?.id ?? null);
    setActionMessage('已载入已保存的自定义草稿。官方流程仍保持原样。');
  }

  async function openFigureTemplate() {
    if (!api || busy || dirty || revisionConflict) return;
    setBusy(true);
    try {
      const result = await api.getFigureTemplate();
      if (!result.data?.workflow) throw new Error('未能读取绘图模板。');
      startEditing({ ...result.data.workflow,
        workflow_id: `reference-led-${slug(bootstrap?.project_label ?? 'figure')}` });
      setActionMessage('参考优先绘图模板已载入。请核对阶段、参考材料和已选 Python/R 后端，再验证并启用。');
    } catch (error) {
      setActionMessage(safeErrorMessage(error));
    } finally { setBusy(false); }
  }

  function addWorkflowNode(type: WorkflowNodeType, skillRef?: string) {
    if (!editor) return;
    const id = nextNodeId(editor.document);
    const node = nodeFor(type, id, skillRef);
    if (skillRef) node.display_name = skillEntries.find((skill) => skill.catalog_id === skillRef)?.display_name ?? skillRef;
    if (performEdit((document) => placement === 'after' && selectedNodeId
      ? appendStage(document, selectedNodeId, node)
      : addNode(document, node))) {
      setSelectedNodeId(id);
      setFocusRequest((value) => value + 1);
    }
  }

  function insertWorkflowNode(nodeId: string, type: WorkflowNodeType, direction: 'before' | 'after') {
    if (!editor) return;
    const node = nodeFor(type, nextNodeId(editor.document));
    if (performEdit((document) => direction === 'before' ? insertBefore(document, nodeId, node) : insertAfter(document, nodeId, node))) setSelectedNodeId(node.id);
  }

  function connect(source: string, target: string, trigger?: string) {
    if (!editor) return;
    const sourceNode = editor.document.nodes.find((node) => node.id === source);
    const actualTrigger = trigger ?? (sourceNode?.type === 'condition' ? (sourceNode.approval_source ? 'approved' : 'default') : sourceNode?.type === 'validator' ? 'pass' : 'succeeded');
    performEdit((document) => connectNodes(document, source, target, actualTrigger));
  }

  function onFlowConnect(connection: Connection) {
    if (!connection.source || !connection.target) return;
    connect(connection.source, connection.target);
  }

  function updateSelectedNode(nodeId: string, patch: Partial<WorkflowNode>) {
    performEdit((document) => updateNode(document, nodeId, patch as Parameters<typeof updateNode>[2]));
  }

  function updateSelectedEdge(edgeId: string, patch: Partial<WorkflowEdge>) {
    performEdit((document) => ({
      ...document,
      edges: document.edges.map((edge) => edge.id === edgeId ? { ...edge, ...patch } : edge),
    }));
  }

  function updateWorkflowSettingsFromInspector(updater: (workflow: WorkflowDocument) => WorkflowDocument) {
    performEdit((document) => {
      const next = updater(document);
      return updateWorkflowSettings(document, {
        workflow_id: next.workflow_id,
        max_parallelism: next.max_parallelism,
        external_inputs: next.external_inputs,
      });
    });
  }

  function handleOperationFailure(error: unknown, operation: string) {
    const issue = issueFromError(error);
    const apiError = error instanceof ApiClientError ? error : null;
    setErrors(apiError?.payload?.errors.length ? apiError.payload.errors : [issue]);
    setWarnings(apiError?.payload?.warnings ?? []);
    setLastValidation(null);
    if (isWorkflowRevisionConflict(error)) {
      setRevisionConflict(true);
      setConflictDialogOpen(true);
      setActionMessage(`${operation}遇到版本冲突。为保护服务器上的较新内容，保存和启用已锁定。`);
      return;
    }
    const writeState = apiError
      ? apiError.payload?.wrote_files ? '服务器报告本次操作曾写入文件；请按错误说明检查状态。' : '服务器报告本次操作未写入文件。'
      : '没有收到可靠的服务端结果；请先刷新状态，不要盲目重试。';
    setActionMessage(`${operation}未完成。${writeState}`);
  }

  async function validate() {
    if (!api || !editor || busy) return;
    setBusy(true);
    setActionMessage('正在使用服务端规则检查当前流程…');
    try {
      setActivationPreview(null);
      setAcknowledgedCodes([]);
      setRiskDialogOpen(false);
      const result = await api.validateWorkflow(editor.document);
      setErrors(result.errors);
      setWarnings(result.warnings);
      setLastValidation(result.data);
      setActionMessage(result.errors.length ? '检查完成：请修正阻断问题后再次验证。' : '检查完成：可保存草稿；启用前仍需满足全部安全条件。');
    } catch (error) {
      handleOperationFailure(error, '验证');
    } finally { setBusy(false); }
  }

  async function saveDraftDocument(document: WorkflowDocument): Promise<WorkflowDocument> {
    if (!api || !bootstrap) throw new Error('本机工作流服务尚未准备好。');
    const result = await api.saveWorkflow(document, savedData?.document_revision ?? bootstrap.document_revision);
    if (!result.data?.workflow) throw new Error('服务器没有返回已保存的草稿。');
    setSavedData(result.data);
    setBootstrap((current) => current ? { ...current, document_revision: result.data!.document_revision } : current);
    setEditor(createWorkflowEditState(result.data.workflow));
    setHistory(createEditorHistory(result.data.workflow));
    setErrors([]);
    setWarnings([]);
    setLastValidation(null);
    return result.data.workflow;
  }

  async function activateSavedPreview(preview: ActivationPreview, codes: string[]) {
    if (!api) throw new Error('本机工作流服务尚未准备好。');
    const result = await api.activateWorkflow({
      workflow_id: preview.workflow.workflow_id,
      expected_document_revision: preview.documentRevision,
      semantic_sha256: preview.semanticSha256,
      acknowledged_warning_codes: [...codes].sort(),
    });
    if (!result.data) throw new Error('服务器没有返回启用后的选择状态。');
    const activeWorkflow = selectionSummary(result.data.selection, result.data.run_id);
    setBootstrap((current) => current ? {
      ...current,
      mode: 'custom',
      active_workflow: activeWorkflow,
      document_revision: result.data!.document_revision,
    } : current);
    setSavedData({ workflow: preview.workflow, document_revision: result.data.document_revision });
    setEditor(createWorkflowEditState(preview.workflow));
    setHistory(createEditorHistory(preview.workflow));
    setHandoffCopied(false);
    setWarnings(result.warnings);
    setErrors([]);
    setActivationPreview(null);
    setAcknowledgedCodes([]);
    setRiskDialogOpen(false);
    setActionMessage(`自定义流程“${result.data.selection.workflow_id}”已启用（语义版本 ${result.data.selection.semantic_revision}）。请回到同一项目目录的 Codex 对话，要求继续已启用的自定义工作流。`);
  }

  async function validateAndActivate() {
    if (!api || !editor || !bootstrap || busy || revisionConflict || bootstrap.mode !== 'official') return;
    setBusy(true);
    setActionMessage('正在保存当前草稿并进行启用前检查…');
    setActivationPreview(null);
    setAcknowledgedCodes([]);
    setRiskDialogOpen(false);
    try {
      const savedWorkflow = dirty || !savedData?.workflow
        ? await saveDraftDocument(editor.document)
        : savedData.workflow;
      const validated = await api.validateWorkflow(savedWorkflow);
      setErrors(validated.errors);
      setWarnings(validated.warnings);
      setLastValidation(validated.data);
      if (validated.errors.length > 0) {
        setActionMessage('草稿已保存，但检查发现阻断问题；修正问题后再尝试启用。');
        return;
      }
      if (!validated.data?.semantic_sha256) {
        throw new Error('检查结果缺少语义版本哈希，不能安全启用。');
      }
      const requiredWarningCodes = [...new Set(validated.data.required_warning_codes)].sort();
      const preview: ActivationPreview = {
        workflow: savedWorkflow,
        documentRevision: savedWorkflow.document_revision,
        semanticSha256: validated.data.semantic_sha256,
        requiredWarningCodes,
        warnings: validated.warnings,
      };
      setActivationPreview(preview);
      if (requiredWarningCodes.length > 0) {
        setRiskDialogOpen(true);
        setActionMessage('检查通过。请逐项确认风险提示后再启用。');
      } else {
        await activateSavedPreview(preview, []);
      }
    } catch (error) {
      handleOperationFailure(error, '验证并启用');
    } finally { setBusy(false); }
  }

  async function confirmActivation() {
    if (!activationPreview || busy || revisionConflict) return;
    setBusy(true);
    setActionMessage('正在启用已确认的自定义流程…');
    try {
      await activateSavedPreview(activationPreview, acknowledgedCodes);
    } catch (error) {
      handleOperationFailure(error, '启用');
    } finally { setBusy(false); }
  }

  async function refreshCatalog() {
    if (!api || busy || refreshingCatalog) return;
    setBusy(true);
    setRefreshingCatalog(true);
    setLastValidation(null);
    setActivationPreview(null);
    setAcknowledgedCodes([]);
    setRiskDialogOpen(false);
    try {
      const result = await api.getCatalog();
      if (result.data) setCatalog(result.data);
      setErrors(result.errors);
      setWarnings(result.warnings);
      setActionMessage('本机 Skill 与验证器清单已刷新。');
    } catch (error) {
      setActionMessage(safeErrorMessage(error));
    } finally { setRefreshingCatalog(false); setBusy(false); }
  }

  async function refreshRuntimeStatus() {
    if (!api || refreshingStatus) return;
    setRefreshingStatus(true);
    try {
      const result = await api.getBootstrap();
      if (!result.data) throw new Error('服务器没有返回最新运行状态。');
      api.setCsrfToken(result.data.csrf_token);
      setBootstrap(result.data);
      setActionMessage('运行状态和确认关卡已刷新；当前草稿内容保持不变。');
    } catch (error) {
      setActionMessage(safeErrorMessage(error));
    } finally {
      setRefreshingStatus(false);
    }
  }

  async function save() {
    if (!api || !editor || !bootstrap || busy || revisionConflict) return;
    setBusy(true);
    setActionMessage('正在保存草稿…');
    try {
      await saveDraftDocument(editor.document);
      setActionMessage(bootstrap.mode === 'custom'
        ? '草稿已保存；当前运行版本未改变。要切换版本，请先切回官方流程。'
        : '草稿已保存在当前项目中；流程仍未启用。');
    } catch (error) {
      handleOperationFailure(error, '保存草稿');
    } finally { setBusy(false); }
  }

  async function loadLatestDraft() {
    if (!api) return;
    setBusy(true);
    try {
      const [bootstrapResult, workflowResult] = await Promise.all([api.getBootstrap(), api.getWorkflow()]);
      if (!bootstrapResult.data || !workflowResult.data) throw new Error('无法载入服务器上的最新状态。');
      api.setCsrfToken(bootstrapResult.data.csrf_token);
      setBootstrap(bootstrapResult.data);
      setSavedData(workflowResult.data);
      setEditor(workflowResult.data.workflow ? createWorkflowEditState(workflowResult.data.workflow) : null);
      setHistory(workflowResult.data.workflow ? createEditorHistory(workflowResult.data.workflow) : null);
      setLayoutRequest((value) => value + 1);
      setSelectedNodeId(workflowResult.data.workflow?.nodes[0]?.id ?? null);
      setRevisionConflict(false);
      setConflictDialogOpen(false);
      setErrors([]);
      setWarnings([]);
      setLastValidation(null);
      setActivationPreview(null);
      setAcknowledgedCodes([]);
      setRiskDialogOpen(false);
      setActionMessage('已载入服务器最新状态；请重新检查后再保存或启用。');
    } catch (error) {
      handleOperationFailure(error, '载入最新草稿');
    } finally { setBusy(false); }
  }

  function downloadBrowserDraft() {
    if (!editor) return;
    const body = JSON.stringify(editor.document, null, 2);
    const blob = new Blob([`${body}\n`], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${editor.document.workflow_id}-browser-draft.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
    setConflictDialogOpen(false);
    setActionMessage('已请求下载浏览器草稿备份；冲突锁仍保留，载入服务器最新草稿后才能继续保存或启用。');
  }

  async function deactivateWorkflow() {
    if (!api || !bootstrap || bootstrap.mode !== 'custom' || busy) return;
    const confirmed = window.confirm('这会停止当前自定义流程并切回官方流程 v1.0。已保存的自定义草稿会保留。是否继续？');
    if (!confirmed) return;
    setBusy(true);
    setActionMessage('正在停止当前运行并切回官方流程…');
    try {
      const result = await api.deactivateWorkflow();
      if (!result.data) throw new Error('服务器没有返回停用后的选择状态。');
      setBootstrap((current) => current ? { ...current, mode: 'official', active_workflow: null } : current);
      setActionMessage('已切回官方流程；自定义草稿仍保存在项目中。');
      setErrors([]);
      setWarnings([]);
    } catch (error) {
      handleOperationFailure(error, '停用自定义流程');
    } finally { setBusy(false); }
  }

  function travelHistory(direction: 'undo' | 'redo') {
    if (!editor || !history || busy) return;
    const restored = direction === 'undo' ? undoDocument(history, editor.document) : redoDocument(history, editor.document);
    if (!restored) return;
    setEditor(applyWorkflowEdit(editor, () => restored.document));
    setHistory(restored.history);
    if (!restored.document.nodes.some((node) => node.id === selectedNodeId)) setSelectedNodeId(restored.document.nodes[0]?.id ?? null);
    setErrors([]);
    setWarnings([]);
    setLastValidation(null);
    setActivationPreview(null);
    setAcknowledgedCodes([]);
    setRiskDialogOpen(false);
    setActionMessage(direction === 'undo' ? '已撤销上一步编辑。' : '已重做上一步编辑。');
  }

  useEffect(() => {
    function handleShortcut(event: KeyboardEvent) {
      const target = event.target;
      if (target instanceof HTMLElement && (target.isContentEditable || target.closest('input, textarea, select, [role="dialog"]'))) return;
      if (!(event.metaKey || event.ctrlKey) || event.altKey || event.key.toLowerCase() !== 'z' || busy || riskDialogOpen || conflictDialogOpen) return;
      const available = event.shiftKey ? history?.future.length : history?.past.length;
      if (!available) return;
      event.preventDefault();
      travelHistory(event.shiftKey ? 'redo' : 'undo');
    }
    window.addEventListener('keydown', handleShortcut);
    return () => window.removeEventListener('keydown', handleShortcut);
  });

  function locateIssue(issue: { node_id?: string; edge_id?: string }) {
    if (!workflow) return;
    const nodeId = issue.node_id || workflow.edges.find((edge) => edge.id === issue.edge_id)?.target;
    setSelectedNodeId(nodeId && workflow.nodes.some((node) => node.id === nodeId) ? nodeId : null);
    setFocusRequest((value) => value + 1);
  }

  useEffect(() => {
    if (!focusRequest || !window.matchMedia('(max-width: 1250px)').matches) return;
    document.querySelector<HTMLElement>('.inspector-panel')?.scrollIntoView({ block: 'nearest' });
  }, [focusRequest]);

  const handoffPrompt = `使用 paper-workflow-orchestrator 继续项目“${bootstrap?.project_label ?? ''}”（项目路径：${bootstrap?.project_root ?? '由当前 Codex 项目上下文确定'}）中已启用的自定义工作流“${bootstrap?.active_workflow?.workflow_id ?? ''}”（运行 ${bootstrap?.active_workflow?.run_id ?? ''}）。先检查当前模式、等待确认的关卡和可执行阶段，告诉我下一阶段需要提供什么；确认关卡必须展示对应产物并取得我的明确答复后再记录。`;

  async function copyHandoff() {
    try {
      await navigator.clipboard.writeText(handoffPrompt);
      setHandoffCopied(true);
    } catch {
      setActionMessage('自动复制不可用，请选中下方提示词复制到同一项目的 Codex 对话。');
    }
  }

  if (loading) return <main className="loading-screen"><div className="loading-mark">P</div><p>正在连接本机工作流…</p></main>;

  if (loadError || !bootstrap || !workflow) {
    const issue = loadError ?? { code: 'studio.bootstrap_failed', message: '无法载入工作流资料。', operation: 'bootstrap', recovery: '确认本机 Workflow Studio 正在运行后重新打开。', node_id: '', edge_id: '' };
    return <main className="error-screen"><p className="eyebrow">PAPER WORKFLOW ORCHESTRATOR</p><h1>无法打开工作流编排器</h1><div className="error-card"><strong>{issue.message}</strong><p>{issue.recovery}</p><code>{issue.code}</code></div></main>;
  }

  const readOnly = !editable;
  return (
    <main className="studio-app">
      <StudioHeader
        bootstrap={bootstrap}
        editable={editable}
        dirty={dirty}
        hasSavedDraft={Boolean(savedData?.workflow)}
        busy={busy}
        conflictLocked={revisionConflict}
        onCopyOfficial={() => { if (officialPreview && catalog) startEditing(cloneProjection(projection!, catalog, newWorkflowId)); }}
        onNewBlank={() => startEditing(createBlankWorkflow(newWorkflowId))}
        onOpenSavedDraft={openSavedDraft}
        onValidate={() => void validate()}
        onSave={() => void save()}
        onValidateActivate={() => void validateAndActivate()}
        onDeactivate={() => void deactivateWorkflow()}
      />
      {bootstrap.mode === 'custom' && <div className="active-workflow-banner">
        <span className="active-workflow-banner__dot" />
        <span>当前启用：{bootstrap.active_workflow?.workflow_id ?? '自定义流程'} · 语义版本 {bootstrap.active_workflow?.semantic_revision ?? '—'} · 哈希 {bootstrap.active_workflow?.semantic_sha256.slice(0, 12) ?? '—'}…</span>
        <span>修改只影响草稿；保存不会改变当前运行版本。</span>
      </div>}
      <WorkflowGuide editable={editable} active={bootstrap.mode === 'custom'} dirty={dirty} checked={lastValidation !== null && errors.length === 0} hintCount={advisoryHints.length} />
      <div className="saved-draft-banner"><span>从示例开始：参考设计 → Python/R 绘制 → 人工确认 → 视觉复核 → 图件验收。</span><button type="button" className="text-button" disabled={busy || dirty || revisionConflict} title={dirty ? '先保存当前草稿，再载入模板。' : '载入可编辑绘图草稿'} onClick={() => void openFigureTemplate()}>参考优先绘图模板</button></div>
      {bootstrap.mode === 'custom' && <details className="handoff-panel" open><summary>下一步：回到 Codex 执行流程</summary><div><p>{handoffPrompt}</p><button type="button" className="button button--quiet" onClick={() => void copyHandoff()}>{handoffCopied ? '提示词已复制' : '复制继续执行提示词'}</button></div></details>}
      {bootstrap.mode === 'custom' && (bootstrap.approvals ?? []).some((item) => item.state === 'awaiting_confirmation' || item.state === 'revision_requested') && <div className="active-workflow-banner" role="status">等待确认：{(bootstrap.approvals ?? []).filter((item) => item.state === 'awaiting_confirmation' || item.state === 'revision_requested').map((item) => `${item.node_id}（${item.state === 'revision_requested' ? '退回修改' : '待确认'}）`).join('、')}。回到 Codex 对话处理后刷新本页查看最新状态。<button type="button" className="text-button" disabled={refreshingStatus} onClick={() => void refreshRuntimeStatus()}>{refreshingStatus ? '刷新中…' : '刷新等待状态'}</button></div>}
      {revisionConflict && <div className="conflict-banner" role="status">
        <span>服务器中的流程版本或检查结果已变化。当前本地草稿仍保留，但保存与启用已锁定。</span>
        <button type="button" className="text-button" onClick={() => setConflictDialogOpen(true)}>处理版本冲突</button>
      </div>}
      {bootstrap.mode === 'official' && savedData?.workflow && !editor && <div className="saved-draft-banner"><span>检测到已保存的自定义草稿；默认官方流程仍处于启用状态。</span><button type="button" className="text-button" onClick={openSavedDraft}>打开草稿</button></div>}
      {actionMessage && <div className="action-message" role="status">{actionMessage}</div>}
      <div className="studio-layout">
        <NodePalette skills={skillEntries} editable={editable && !busy} onAddNode={addWorkflowNode} onRefresh={() => void refreshCatalog()} refreshing={refreshingCatalog || busy} placement={placement} onPlacementChange={setPlacement} selectedNodeName={selectedNode?.display_name ?? null} />
        <div className="studio-center-column">
          <EditorToolbar editable={editable} busy={busy} canUndo={Boolean(history?.past.length)} canRedo={Boolean(history?.future.length)} settingsSelected={selectedNodeId === null} onUndo={() => travelHistory('undo')} onRedo={() => travelHistory('redo')} onSettings={() => setSelectedNodeId(null)} onArrange={() => { if (performEdit(arrangeWorkflow)) setLayoutRequest((value) => value + 1); }} />
          <WorkflowCanvas
            workflow={workflow}
            skills={skillEntries}
            validators={validatorEntries}
            selectedNodeId={selectedNodeId}
            readOnly={readOnly || busy}
            onSelectNode={setSelectedNodeId}
            onDeselect={() => setSelectedNodeId(null)}
            focusRequest={focusRequest}
            layoutRequest={layoutRequest}
            onMoveNode={(nodeId, position) => performEdit((document) => moveNode(document, nodeId, position))}
            onConnect={onFlowConnect}
            onDeleteEdges={(deleted: Edge[]) => performEdit((document) => deleted.reduce((current, edge) => disconnectEdge(current, edge.id), document))}
          />
          <div className="bottom-panels">
            <GraphOutline workflow={workflow} selectedNodeId={selectedNodeId} onSelectNode={setSelectedNodeId} />
            <ValidationPanel
              errors={errors}
              warnings={warnings}
              projectionNotes={workflow.derived_from ? projectionNotes : []}
              lastValidated={lastValidation !== null}
              busy={busy}
              advisoryHints={advisoryHints}
              onLocateIssue={locateIssue}
            />
          </div>
        </div>
        <NodeInspector
          workflow={workflow}
          selectedNode={selectedNode}
          skills={skillEntries}
          validators={validatorEntries}
          readOnly={readOnly || busy}
          onUpdateWorkflow={updateWorkflowSettingsFromInspector}
          onUpdateNode={updateSelectedNode}
          onUpdateEdge={updateSelectedEdge}
          onConnect={connect}
          onDisconnect={(edgeId) => performEdit((document) => disconnectEdge(document, edgeId))}
          onMoveNode={(nodeId, position) => performEdit((document) => moveNode(document, nodeId, position))}
          onDuplicateNode={(nodeId) => {
            let duplicatedId: string | null = null;
            if (performEdit((document) => {
              const next = duplicateNode(document, nodeId);
              duplicatedId = next.nodes.at(-1)?.id ?? null;
              return next;
            })) setSelectedNodeId(duplicatedId);
          }}
          onDeleteNode={(nodeId) => { if (performEdit((document) => deleteNode(document, nodeId))) setSelectedNodeId(null); }}
          onInsertBefore={(nodeId, type) => insertWorkflowNode(nodeId, type, 'before')}
          onInsertAfter={(nodeId, type) => insertWorkflowNode(nodeId, type, 'after')}
          onAddApprovalGate={(sourceId, targetId) => { if (performEdit((document) => addApprovalGate(document, sourceId, [targetId]))) setActionMessage('已添加等待确认关卡；产物连线保持原样。'); }}
        />
      </div>
      {errors.length > 0 && <div className="screen-reader-only" role="alert">{errors[0]?.message}</div>}
      {editable && <p className="status-line" aria-live="polite">{editor?.lastClassification === 'visual' ? '最近编辑：仅画布位置' : editor?.lastClassification === 'semantic' ? '最近编辑：流程行为' : '选择阶段开始编辑'}{dirty ? ' · 尚未保存' : ''}</p>}
      {riskDialogOpen && activationPreview && <RiskAcknowledgementDialog
        requiredCodes={activationPreview.requiredWarningCodes}
        warnings={activationPreview.warnings}
        acknowledgedCodes={acknowledgedCodes}
        busy={busy}
        onToggle={(code, checked) => setAcknowledgedCodes((current) => checked
          ? [...new Set([...current, code])].sort()
          : current.filter((item) => item !== code))}
        onBack={() => { setRiskDialogOpen(false); setActivationPreview(null); setAcknowledgedCodes([]); }}
        onActivate={() => void confirmActivation()}
      />}
      <RevisionConflictDialog
        open={conflictDialogOpen}
        busy={busy}
        onLoadLatest={() => void loadLatestDraft()}
        onDownloadDraft={downloadBrowserDraft}
        onKeepOpen={() => { setConflictDialogOpen(false); setActionMessage('当前浏览器草稿仍保留；保存和启用保持锁定。你可继续编辑或下载备份。'); }}
      />
    </main>
  );
}
