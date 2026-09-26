import type {
  ApiEnvelope,
  BootstrapData,
  CatalogData,
  CompileData,
  ProjectionData,
  ValidationData,
  WorkflowDocument,
  WorkflowData,
} from './types';

export const SESSION_STORAGE_KEY = 'workflow-studio-session';
const SESSION_TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;

export function consumeSessionToken(): string | null {
  const fragment = window.location.hash;
  const parameters = new URLSearchParams(fragment.startsWith('#') ? fragment.slice(1) : fragment);
  const tokenWasPresent = parameters.has('session');
  const fragmentToken = parameters.get('session');

  if (tokenWasPresent) {
    parameters.delete('session');
    const remainingFragment = parameters.toString();
    const cleanUrl = `${window.location.pathname}${window.location.search}${remainingFragment ? `#${remainingFragment}` : ''}`;
    window.history.replaceState(null, '', cleanUrl);
  }

  if (fragmentToken && SESSION_TOKEN_PATTERN.test(fragmentToken)) {
    window.sessionStorage.setItem(SESSION_STORAGE_KEY, fragmentToken);
    return fragmentToken;
  }
  if (tokenWasPresent) {
    window.sessionStorage.removeItem(SESSION_STORAGE_KEY);
  }
  const storedToken = window.sessionStorage.getItem(SESSION_STORAGE_KEY);
  return storedToken && SESSION_TOKEN_PATTERN.test(storedToken) ? storedToken : null;
}

export class ApiClientError extends Error {
  readonly kind: 'revision-conflict' | 'http' | 'protocol';

  constructor(
    message: string,
    readonly status: number,
    readonly payload: ApiEnvelope<unknown> | null,
  ) {
    super(message);
    this.name = 'ApiClientError';
    this.kind = status === 409 ? 'revision-conflict' : payload ? 'http' : 'protocol';
  }
}

export class ApiClient {
  private csrfToken = '';

  constructor(
    private readonly sessionToken: string,
    private readonly fetcher: typeof fetch = window.fetch.bind(window),
  ) {
    if (!SESSION_TOKEN_PATTERN.test(sessionToken)) {
      throw new Error('A valid Workflow Studio session token is required.');
    }
  }

  setCsrfToken(token: string): void {
    this.csrfToken = token;
  }

  getBootstrap(): Promise<ApiEnvelope<BootstrapData>> {
    return this.get('/api/bootstrap');
  }

  getCatalog(): Promise<ApiEnvelope<CatalogData>> {
    return this.get('/api/catalog');
  }

  getProjection(): Promise<ApiEnvelope<ProjectionData>> {
    return this.get('/api/projection');
  }

  getWorkflow(): Promise<ApiEnvelope<WorkflowData>> {
    return this.get('/api/workflow');
  }

  validateWorkflow(workflow: WorkflowDocument): Promise<ApiEnvelope<ValidationData>> {
    return this.write('/api/validate', 'POST', { workflow });
  }

  compileWorkflow(workflow: WorkflowDocument): Promise<ApiEnvelope<CompileData>> {
    return this.write('/api/compile', 'POST', { workflow });
  }

  saveWorkflow(workflow: WorkflowDocument, expectedDocumentRevision: number): Promise<ApiEnvelope<WorkflowData>> {
    return this.write('/api/workflow', 'PUT', {
      workflow,
      expected_document_revision: expectedDocumentRevision,
    });
  }

  activateWorkflow(input: {
    workflow_id: string;
    expected_document_revision: number;
    semantic_sha256: string;
    acknowledged_warning_codes: string[];
  }): Promise<ApiEnvelope<{ selection: Record<string, unknown>; run_id: string; document_revision: number }>> {
    return this.write('/api/activate', 'POST', input);
  }

  deactivateWorkflow(): Promise<ApiEnvelope<{ selection: Record<string, unknown> }>> {
    return this.write('/api/deactivate', 'POST', {});
  }

  shutdown(): Promise<ApiEnvelope<{ shutdown_requested: boolean }>> {
    return this.write('/api/shutdown', 'POST', {});
  }

  private get<T>(path: string): Promise<ApiEnvelope<T>> {
    return this.request(path, 'GET');
  }

  private write<T>(path: string, method: 'POST' | 'PUT', body: object): Promise<ApiEnvelope<T>> {
    return this.request(path, method, body);
  }

  private async request<T>(path: string, method: 'GET' | 'POST' | 'PUT', body?: object): Promise<ApiEnvelope<T>> {
    if (!path.startsWith('/api/') || path.startsWith('//')) {
      throw new Error('Workflow Studio requests must use a local API route.');
    }
    const headers = new Headers({ Authorization: `Bearer ${this.sessionToken}` });
    const init: RequestInit = { method, headers, cache: 'no-store', credentials: 'same-origin' };
    if (body !== undefined) {
      if (!this.csrfToken) {
        throw new Error('The CSRF token has not been initialized.');
      }
      headers.set('Content-Type', 'application/json');
      headers.set('X-Workflow-CSRF', this.csrfToken);
      init.body = JSON.stringify(body);
    }

    const response = await this.fetcher(path, init);
    const contentType = response.headers.get('Content-Type')?.toLowerCase() ?? '';
    if (!contentType.includes('application/json')) {
      throw new ApiClientError('The local server returned a non-JSON response.', response.status, null);
    }

    let payload: ApiEnvelope<T>;
    try {
      payload = (await response.json()) as ApiEnvelope<T>;
    } catch {
      throw new ApiClientError('The local server returned invalid JSON.', response.status, null);
    }
    if (!payload || !Array.isArray(payload.errors) || !Array.isArray(payload.warnings)) {
      throw new ApiClientError('The local server response did not match the API contract.', response.status, null);
    }
    if (!response.ok || payload.status === 'error') {
      const message = payload.errors[0]?.message ?? `Workflow Studio request failed (${response.status}).`;
      throw new ApiClientError(message, response.status, payload);
    }
    return payload;
  }
}
