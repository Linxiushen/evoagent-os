export interface ClientOptions {
  baseUrl: string;
  token?: string;
  fetch?: typeof globalThis.fetch;
}

export interface RequestOptions {
  idempotencyKey?: string;
  signal?: AbortSignal;
}

export interface WorkspaceCreate {
  name: string;
  description?: string;
  tenant_id?: string;
}

export interface AgentCreate {
  workspace_id?: string;
  name: string;
  role: string;
  description?: string;
  model?: string;
  capabilities?: string[];
  tools?: string[];
  skills?: string[];
  system_prompt?: string;
  metadata?: Record<string, unknown>;
}

export interface RunCreate {
  workspace_id?: string;
  agent_id?: string;
  input: string;
  session_id?: string;
  wait?: boolean;
  metadata?: Record<string, unknown>;
}

export interface WorkflowNodeCreate {
  id: string;
  objective: string;
  depends_on?: string[];
  capabilities?: string[];
  input?: Record<string, unknown>;
  tokens?: number;
  cost_usd?: number;
  seconds?: number;
  approval_required?: boolean;
  max_attempts?: number;
}

export interface WorkflowCreate {
  workspace_id?: string;
  name: string;
  description?: string;
  nodes: WorkflowNodeCreate[];
  metadata?: Record<string, unknown>;
}

export interface ApprovalDecision {
  approved: boolean;
  actor?: string;
  reason?: string;
}

export interface DemoResult {
  workflow_id: string;
  run_id: string;
  approval_id: string;
  workspace_id: string;
  status: string;
  completed_nodes: string[];
  next_action: string;
}

export class EvoAgentError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    super(`EvoAgent OS request failed with HTTP ${status}`);
    this.name = "EvoAgentError";
    this.status = status;
    this.detail = detail;
  }
}

export class EvoAgentClient {
  readonly baseUrl: string;
  readonly token: string | undefined;
  readonly fetcher: typeof globalThis.fetch;

  constructor(options: ClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/$/, "");
    this.token = options.token;
    this.fetcher = options.fetch ?? globalThis.fetch;
  }

  overview<T = Record<string, unknown>>(): Promise<T> {
    return this.request<T>("GET", "/api/v1/overview");
  }

  workspaces<T = Record<string, unknown>[]>(): Promise<T> {
    return this.request<T>("GET", "/api/v1/workspaces");
  }

  createWorkspace<T = Record<string, unknown>>(body: WorkspaceCreate): Promise<T> {
    return this.request<T>("POST", "/api/v1/workspaces", body);
  }

  agents<T = Record<string, unknown>[]>(workspaceId?: string): Promise<T> {
    const query = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : "";
    return this.request<T>("GET", `/api/v1/agents${query}`);
  }

  createAgent<T = Record<string, unknown>>(body: AgentCreate): Promise<T> {
    return this.request<T>("POST", "/api/v1/agents", body);
  }

  runs<T = Record<string, unknown>[]>(): Promise<T> {
    return this.request<T>("GET", "/api/v1/runs");
  }

  createRun<T = Record<string, unknown>>(
    body: RunCreate,
    options: RequestOptions = {},
  ): Promise<T> {
    return this.request<T>("POST", "/api/v1/runs", body, options);
  }

  workflows<T = Record<string, unknown>[]>(): Promise<T> {
    return this.request<T>("GET", "/api/v1/workflows");
  }

  workflow<T = Record<string, unknown>>(workflowId: string): Promise<T> {
    return this.request<T>("GET", `/api/v1/workflows/${encodeURIComponent(workflowId)}`);
  }

  createWorkflow<T = Record<string, unknown>>(
    body: WorkflowCreate,
    options: RequestOptions = {},
  ): Promise<T> {
    return this.request<T>("POST", "/api/v1/workflows", body, options);
  }

  approvals<T = Record<string, unknown>[]>(): Promise<T> {
    return this.request<T>("GET", "/api/v1/approvals");
  }

  decideApproval<T = Record<string, unknown>>(
    approvalId: string,
    body: ApprovalDecision,
  ): Promise<T> {
    return this.request<T>(
      "POST",
      `/api/v1/approvals/${encodeURIComponent(approvalId)}`,
      body,
    );
  }

  events<T = Record<string, unknown>[]>(): Promise<T> {
    return this.request<T>("GET", "/api/v1/events");
  }

  skills<T = Record<string, unknown>[]>(): Promise<T> {
    return this.request<T>("GET", "/api/v1/skills");
  }

  artifacts<T = Record<string, unknown>[]>(workflowId?: string): Promise<T> {
    const query = workflowId ? `?workflow_id=${encodeURIComponent(workflowId)}` : "";
    return this.request<T>("GET", `/api/v1/artifacts${query}`);
  }

  launchDemo(options: RequestOptions = {}): Promise<DemoResult> {
    return this.request<DemoResult>("POST", "/api/v1/demo/launch", {}, options);
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
    options: RequestOptions = {},
  ): Promise<T> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const init: RequestInit = { method, headers };
    if (body !== undefined) init.body = JSON.stringify(body);
    if (options.signal) init.signal = options.signal;
    const response = await this.fetcher(`${this.baseUrl}${path}`, init);
    const contentType = response.headers.get("content-type") ?? "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : await response.text();
    if (!response.ok) throw new EvoAgentError(response.status, payload);
    return payload as T;
  }
}
