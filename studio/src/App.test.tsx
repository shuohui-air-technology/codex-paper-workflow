import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from './App';
import { ApiClientError } from './api';
import type { ApiClient } from './api';
import { createBlankWorkflow } from './workflow';
import type {
  ApiEnvelope,
  BootstrapData,
  CatalogData,
  ProjectionData,
  WorkflowDocument,
  WorkflowData,
  ValidationData,
  WorkflowIssue,
} from './types';

afterEach(() => cleanup());

function envelope<T>(data: T, errors: ApiEnvelope<T>['errors'] = [], warnings: ApiEnvelope<T>['warnings'] = []): ApiEnvelope<T> {
  return { status: errors.length ? 'blocked' : 'pass', data, errors, warnings, wrote_files: false };
}

const bootstrap: BootstrapData = {
  project_label: 'Demo Project',
  mode: 'official',
  active_workflow: null,
  document_revision: 0,
  csrf_token: 'c'.repeat(64),
  max_json_body_bytes: 2 * 1024 * 1024,
};

const catalog: CatalogData = {
  skills: [{
    catalog_id: 'clarify-research-idea',
    display_name: 'Clarify Research Idea',
    description: 'Refines an initial research idea.',
    relative_path: 'clarify-research-idea/SKILL.md',
    skill_sha256: 'a'.repeat(64),
    tree_sha256: 'b'.repeat(64),
    locked: true,
    ambiguous: false,
  }],
  validators: [],
};

const projection: ProjectionData = {
  sha256: 'c'.repeat(64),
  projection: {
    schema_version: 'paper-workflow-studio-projection-v1',
    projection_id: 'official-v1.0',
    source_commit: 'e24c34255e3c72a329614e07c431bfa51a778c40',
    nodes: [
      {
        id: 'intake', display_name: 'Project intake', projection_kind: 'orchestrator',
        official_stage_ids: ['intake'], suggested_skill_ids: [], suggested_validator_ids: [],
        inputs: ['user_request'], outputs: ['research_brief'], write_scopes: ['research_brief'], control_tags: [],
      },
      {
        id: 'directions', display_name: 'Explore directions', projection_kind: 'task',
        official_stage_ids: ['directions'], suggested_skill_ids: ['clarify-research-idea'], suggested_validator_ids: [],
        inputs: ['research_brief'], outputs: ['direction_candidates'], write_scopes: ['direction_candidates'], control_tags: [],
      },
    ],
    edges: [{ id: 'intake-to-directions', source: 'intake', target: 'directions', projection_note: 'Sequence' }],
    stage_map: { intake: 'intake', directions: 'directions' },
    control_tags: [],
    projection_notes: [{ kind: 'feedback_as_new_revision', studio_behavior: 'Feedback is represented as a new workflow revision.' }],
  },
};

function makeApi(options: {
  mode?: 'official' | 'custom';
  savedWorkflow?: WorkflowDocument | null;
  validateResult?: ApiEnvelope<ValidationData>;
} = {}) {
  const workflowData: WorkflowData = {
    workflow: options.savedWorkflow ?? null,
    document_revision: options.savedWorkflow?.document_revision ?? 0,
  };
  const api = {
    setCsrfToken: vi.fn(),
    getBootstrap: vi.fn().mockResolvedValue(envelope({ ...bootstrap, mode: options.mode ?? 'official', active_workflow: options.mode === 'custom' ? {
      workflow_id: options.savedWorkflow?.workflow_id ?? 'custom-demo-project-flow',
      semantic_revision: options.savedWorkflow?.semantic_revision ?? 1,
      semantic_sha256: 'e'.repeat(64),
      run_id: 'run-active',
    } : null })),
    getCatalog: vi.fn().mockResolvedValue(envelope(catalog)),
    getProjection: vi.fn().mockResolvedValue(envelope(projection)),
    getWorkflow: vi.fn().mockResolvedValue(envelope(workflowData)),
    validateWorkflow: vi.fn().mockImplementation(async (workflow: WorkflowDocument) => options.validateResult ?? envelope({
      document_sha256: 'd'.repeat(64),
      semantic_sha256: workflow.nodes.length ? 'e'.repeat(64) : null,
      required_warning_codes: [],
    })),
    saveWorkflow: vi.fn().mockImplementation(async (workflow: WorkflowDocument) => envelope({
      workflow: { ...workflow, document_revision: 1, semantic_revision: 1 },
      document_revision: 1,
    })),
    activateWorkflow: vi.fn().mockImplementation(async () => envelope({
      selection: {
        mode: 'custom' as const, selection_revision: 1, workflow_id: 'custom-demo-project-flow', semantic_revision: 1,
        semantic_sha256: 'e'.repeat(64), acknowledged_warning_codes: [], acknowledged_semantic_sha256: 'e'.repeat(64),
        acknowledged_at: '2026-09-27T00:00:00Z',
      },
      run_id: 'run-custom-1', document_revision: 1,
    })),
    deactivateWorkflow: vi.fn().mockResolvedValue(envelope({
      selection: {
        mode: 'official' as const, selection_revision: 2, workflow_id: '', semantic_revision: 0,
        semantic_sha256: '', acknowledged_warning_codes: [], acknowledged_semantic_sha256: '', acknowledged_at: '',
      },
    })),
  };
  return api as unknown as ApiClient & typeof api;
}

describe('Workflow Studio app', () => {
  it('saves the workflow identifier entered immediately before clicking save', async () => {
    const api = makeApi();
    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(await screen.findByRole('button', { name: '新建空白流程' }));
    await user.click(screen.getByRole('button', { name: '流程设置' }));
    const identifier = screen.getByLabelText('流程标识');
    await user.clear(identifier);
    await user.type(identifier, 'introduction-review');
    expect(identifier).toHaveValue('introduction-review');
    await user.click(screen.getByRole('button', { name: '保存草稿' }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalledOnce());
    expect((api.saveWorkflow.mock.calls[0]![0] as WorkflowDocument).workflow_id).toBe('introduction-review');
    expect(screen.getByLabelText('流程标识')).toHaveValue('introduction-review');
  });

  it('requires a new workflow check after refreshing the installed Skill list', async () => {
    const api = makeApi();
    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(await screen.findByRole('button', { name: '新建空白流程' }));
    await user.click(screen.getByRole('button', { name: '验证流程' }));
    expect(await screen.findByText('服务端验证通过')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '刷新' }));
    await waitFor(() => expect(api.getCatalog).toHaveBeenCalledTimes(2));
    expect(screen.queryByText('服务端验证通过')).not.toBeInTheDocument();
    expect(screen.queryByText('检查已完成，可点击「验证并启用」继续。')).not.toBeInTheDocument();
  });

  it('appends linked stages, undoes deletion with its connections, and resets history after saving', async () => {
    const api = makeApi();
    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(await screen.findByRole('button', { name: '新建空白流程' }));
    await user.selectOptions(screen.getByLabelText('主要 Skill'), 'clarify-research-idea');
    await user.click(screen.getByRole('button', { name: /空白任务阶段/ }));
    await user.selectOptions(screen.getByLabelText('主要 Skill'), 'clarify-research-idea');
    expect(screen.getByText('2 阶段')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '删除阶段' }));
    expect(screen.getByText('1 阶段')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /撤销$/ }));
    expect(screen.getByText('2 阶段')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /重做$/ }));
    expect(screen.getByText('1 阶段')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /撤销$/ }));
    await user.click(screen.getByRole('button', { name: '保存草稿' }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalledOnce());
    const saved = api.saveWorkflow.mock.calls[0]![0] as WorkflowDocument;
    expect(saved.edges).toMatchObject([{ source: 'step-1', target: 'stage-1', trigger: 'succeeded' }]);
    expect(saved.nodes.find((node) => node.id === 'stage-1')?.entry).toBe(false);
    expect(screen.getByRole('button', { name: /撤销$/ })).toBeDisabled();
  });

  it('opens workflow settings without deleting a stage and locates a stage from validation errors', async () => {
    const error: WorkflowIssue = { code: 'catalog.skill_missing', message: '找不到对应 Skill', node_id: 'step-1', edge_id: '', operation: 'validate', recovery: '请选择已安装的 Skill。' };
    const api = makeApi({ validateResult: envelope({ document_sha256: 'd'.repeat(64), semantic_sha256: null, required_warning_codes: [] }, [error]) });
    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(await screen.findByRole('button', { name: '新建空白流程' }));
    await user.click(screen.getByRole('button', { name: '流程设置' }));
    expect(screen.getByRole('heading', { name: '流程设置' })).toBeInTheDocument();
    expect(screen.getByText('1 阶段')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '验证流程' }));
    await user.click(await screen.findByRole('button', { name: '定位相关阶段' }));
    expect(screen.getByLabelText('阶段名称')).toHaveValue('第一个任务阶段');
  });

  it('keeps independent addition explicit and does not intercept undo inside text fields', async () => {
    const api = makeApi();
    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(await screen.findByRole('button', { name: '新建空白流程' }));
    await user.selectOptions(screen.getByLabelText('新阶段添加位置'), 'detached');
    await user.click(screen.getByRole('button', { name: /空白任务阶段/ }));
    await user.click(screen.getByLabelText('阶段名称'));
    await user.keyboard('{Control>}z{/Control}');
    expect(screen.getByText('2 阶段')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '保存草稿' }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalledOnce());
    expect((api.saveWorkflow.mock.calls[0]![0] as WorkflowDocument).edges).toEqual([]);
  });

  it('opens the official workflow as a read-only preview without saving or activating anything', async () => {
    const api = makeApi();
    render(<App api={api} />);

    expect(await screen.findByText('官方流程 v1.0')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /空白任务阶段/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: '复制为自定义流程' })).toBeEnabled();
    expect(screen.getByText('Feedback is represented as a new workflow revision.')).toBeInTheDocument();
    expect(api.saveWorkflow).not.toHaveBeenCalled();
  });

  it('creates an in-browser custom copy and keeps the default mode unchanged until an explicit save', async () => {
    const api = makeApi();
    const user = userEvent.setup();
    render(<App api={api} />);

    await user.click(await screen.findByRole('button', { name: '复制为自定义流程' }));
    expect(screen.getByText('官方流程 v1.0')).toBeInTheDocument();
    expect(screen.getByText('正在编辑自定义草稿')).toBeInTheDocument();
    expect(screen.getByText('有未保存修改')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存草稿' })).toBeEnabled();
    await user.selectOptions(screen.getByLabelText('新阶段添加位置'), 'detached');
    await user.click(within(screen.getByRole('complementary', { name: '添加流程阶段' })).getByRole('button', { name: /Clarify Research Idea/ }));
    expect(screen.getByText('3 阶段')).toBeInTheDocument();
    expect(screen.getAllByText('Clarify Research Idea').length).toBeGreaterThan(0);
    expect(api.saveWorkflow).not.toHaveBeenCalled();
    expect(api.getBootstrap).toHaveBeenCalledTimes(1);
  });

  it('keeps the selected stage when inserting before the only entry cannot succeed', async () => {
    const api = makeApi();
    const user = userEvent.setup();
    render(<App api={api} />);

    await user.click(await screen.findByRole('button', { name: '新建空白流程' }));
    expect(screen.getByRole('heading', { name: '阶段设置' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '插入之前' })).toBeDisabled();
    expect(screen.getByRole('heading', { name: '阶段设置' })).toBeInTheDocument();
    expect(screen.getByText(/前方没有连接/)).toBeInTheDocument();
  });

  it('validates and saves only after explicit user actions', async () => {
    const api = makeApi();
    const user = userEvent.setup();
    render(<App api={api} />);

    await user.click(await screen.findByRole('button', { name: '新建空白流程' }));
    await user.click(within(screen.getByRole('banner')).getByRole('button', { name: '验证流程' }));
    await waitFor(() => expect(api.validateWorkflow).toHaveBeenCalledTimes(1));
    expect(screen.getByText('服务端验证通过')).toBeInTheDocument();

    await user.click(within(screen.getByRole('banner')).getByRole('button', { name: '保存草稿' }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalledTimes(1));
    expect(api.saveWorkflow.mock.calls[0]?.[1]).toBe(0);
    expect(await screen.findByText('草稿已保存')).toBeInTheDocument();
  });

  it('saves, validates, requires every warning acknowledgement, and then activates the exact saved revision', async () => {
    const risk: WorkflowIssue = {
      code: 'risk.control_removed.integrity',
      message: 'The projected integrity control is not covered.',
      operation: 'validate',
      recovery: 'Add an equivalent check or proceed only after reviewing this risk.',
      node_id: '',
      edge_id: '',
    };
    const api = makeApi({ validateResult: envelope({
      document_sha256: 'd'.repeat(64),
      semantic_sha256: 'e'.repeat(64),
      required_warning_codes: [risk.code],
    }, [], [risk]) });
    const user = userEvent.setup();
    render(<App api={api} />);

    await user.click(await screen.findByRole('button', { name: '新建空白流程' }));
    await user.click(screen.getByRole('button', { name: '验证并启用' }));
    const dialog = await screen.findByRole('dialog', { name: '请逐项确认流程变更' });
    expect(api.saveWorkflow).toHaveBeenCalledTimes(1);
    expect(api.validateWorkflow).toHaveBeenCalledTimes(1);
    expect(within(dialog).getByRole('button', { name: '启用自定义流程' })).toBeDisabled();

    await user.click(within(dialog).getByRole('checkbox', { name: /projected integrity control/i }));
    expect(within(dialog).getByRole('button', { name: '启用自定义流程' })).toBeEnabled();
    await user.click(within(dialog).getByRole('button', { name: '启用自定义流程' }));
    await waitFor(() => expect(api.activateWorkflow).toHaveBeenCalledTimes(1));
    expect(api.activateWorkflow.mock.calls[0]?.[0]).toMatchObject({
      workflow_id: 'custom-demo-project-flow',
      expected_document_revision: 1,
      semantic_sha256: 'e'.repeat(64),
      acknowledged_warning_codes: [risk.code],
    });
    expect(await screen.findByText(/当前启用：custom-demo-project-flow/)).toBeInTheDocument();
  });

  it('locks saving after a revision conflict and offers only non-overwriting recovery', async () => {
    const api = makeApi();
    const conflict: ApiEnvelope<null> = {
      status: 'error', data: null,
      errors: [{ code: 'store.revision_conflict', message: 'Draft is stale.', operation: 'save', recovery: 'Reload.', node_id: '', edge_id: '' }],
      warnings: [], wrote_files: false,
    };
    api.saveWorkflow.mockRejectedValue(new ApiClientError('Draft is stale.', 409, conflict));
    const user = userEvent.setup();
    render(<App api={api} />);

    await user.click(await screen.findByRole('button', { name: '新建空白流程' }));
    await user.click(screen.getByRole('button', { name: '保存草稿' }));
    const dialog = await screen.findByRole('dialog', { name: '服务器中的流程状态已经变化' });
    expect(within(dialog).getAllByRole('button')).toHaveLength(3);
    await user.click(within(dialog).getByRole('button', { name: /继续保留当前草稿/ }));
    expect(screen.getByRole('button', { name: '处理版本冲突' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存草稿' })).toBeDisabled();
    expect(api.saveWorkflow).toHaveBeenCalledTimes(1);
  });

  it('requires confirmation to deactivate and preserves the saved draft', async () => {
    const saved = createBlankWorkflow('custom-saved-flow');
    const api = makeApi({ mode: 'custom', savedWorkflow: saved });
    const confirmation = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const user = userEvent.setup();
    render(<App api={api} />);

    await screen.findByText(/当前启用：custom-saved-flow/);
    await user.click(screen.getByRole('button', { name: '切回官方流程' }));
    await waitFor(() => expect(api.deactivateWorkflow).toHaveBeenCalledTimes(1));
    expect(confirmation).toHaveBeenCalledTimes(1);
    expect(screen.getByText('官方流程 v1.0')).toBeInTheDocument();
    expect(screen.getByText(/自定义草稿仍保存在项目中/)).toBeInTheDocument();
    expect(api.getWorkflow).toHaveBeenCalledTimes(1);
    confirmation.mockRestore();
  });
});
