import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClient, ApiClientError, consumeSessionToken, SESSION_STORAGE_KEY } from './api';
import type { ApiEnvelope, WorkflowDocument } from './types';

const workflow: WorkflowDocument = {
  schema_version: 'paper-workflow-custom-v1',
  workflow_id: 'demo-flow',
  document_revision: 1,
  semantic_revision: 1,
  derived_from: null,
  max_parallelism: 1,
  external_inputs: [],
  nodes: [],
  edges: [],
  ui: { positions: {} },
};

function successEnvelope<T>(data: T): ApiEnvelope<T> {
  return { status: 'pass', data, errors: [], warnings: [], wrote_files: false };
}

describe('Workflow Studio API client', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState(null, '', '/#session=' + 's'.repeat(64));
  });

  it('consumes the fragment once and removes it from browser history', () => {
    expect(consumeSessionToken()).toBe('s'.repeat(64));
    expect(window.location.hash).toBe('');
    expect(window.sessionStorage.getItem(SESSION_STORAGE_KEY)).toBe('s'.repeat(64));
  });

  it('uses the stored session after the one-time fragment has been cleared', () => {
    expect(consumeSessionToken()).toBe('s'.repeat(64));
    expect(consumeSessionToken()).toBe('s'.repeat(64));
    expect(window.location.hash).toBe('');
  });

  it('adds bearer, same-origin API path, and csrf headers to writes', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(successEnvelope({ workflow, document_revision: 1 })), {
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const client = new ApiClient('t'.repeat(64), fetchMock);
    client.setCsrfToken('c'.repeat(64));
    await client.saveWorkflow(workflow, 1);

    const [url, init] = fetchMock.mock.calls[0] ?? [];
    const headers = new Headers(init?.headers);
    expect(url).toBe('/api/workflow');
    expect(init?.method).toBe('PUT');
    expect(headers.get('Authorization')).toBe(`Bearer ${'t'.repeat(64)}`);
    expect(headers.get('X-Workflow-CSRF')).toBe('c'.repeat(64));
    expect(headers.get('Content-Type')).toBe('application/json');
    expect(init?.credentials).toBe('same-origin');
  });

  it('does not send JSON or CSRF headers on read-only requests', async () => {
    const bootstrap = {
      project_label: 'project',
      mode: 'official' as const,
      document_revision: 0,
      csrf_token: 'c'.repeat(64),
      max_json_body_bytes: 2 * 1024 * 1024,
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(successEnvelope(bootstrap)), {
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    await new ApiClient('t'.repeat(64), fetchMock).getBootstrap();
    const init = fetchMock.mock.calls[0]?.[1];
    const headers = new Headers(init?.headers);
    expect(init?.method).toBe('GET');
    expect(headers.get('Authorization')).toBe(`Bearer ${'t'.repeat(64)}`);
    expect(headers.has('X-Workflow-CSRF')).toBe(false);
    expect(headers.has('Content-Type')).toBe(false);
  });

  it('classifies HTTP 409 as a revision conflict without retrying', async () => {
    const conflict: ApiEnvelope<null> = {
      status: 'error',
      data: null,
      errors: [{
        code: 'store.revision_conflict',
        message: 'Draft is stale.',
        operation: 'save',
        recovery: 'Reload.',
        node_id: '',
        edge_id: '',
      }],
      warnings: [],
      wrote_files: false,
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(conflict), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const client = new ApiClient('t'.repeat(64), fetchMock);
    client.setCsrfToken('c'.repeat(64));
    await expect(client.saveWorkflow(workflow, 0)).rejects.toMatchObject({
      kind: 'revision-conflict',
      status: 409,
    } satisfies Partial<ApiClientError>);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
