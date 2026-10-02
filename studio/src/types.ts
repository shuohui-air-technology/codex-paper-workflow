export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

export type FailurePolicy = 'block' | 'skip_branch';
export type JoinMode = 'all_active' | 'any_success';
export type WorkflowNodeType = 'task' | 'condition' | 'join' | 'validator';

export interface ConditionCase {
  outcome: string;
  when: { [key: string]: JsonValue };
}

export interface ValidatorConfig {
  input_roles: Record<string, string>;
  options: Record<string, JsonValue>;
}

interface NodeBase {
  id: string;
  display_name: string;
  entry: boolean;
  enabled: boolean;
  origin_projection_node_id: string | null;
  inputs: string[];
  outputs: string[];
  outcomes: string[];
  write_scopes: string[];
  failure_policy: FailurePolicy;
  condition_cases: ConditionCase[];
  join_mode: JoinMode;
  approval_source?: string | null;
}

export interface TaskNode extends NodeBase {
  type: 'task';
  skill_ref: string | null;
  validator_ref: null;
  validator_config: null;
}

export interface ValidatorNode extends NodeBase {
  type: 'validator';
  skill_ref: null;
  validator_ref: string | null;
  validator_config: ValidatorConfig | null;
}

export interface ConditionNode extends NodeBase {
  type: 'condition';
  skill_ref: null;
  validator_ref: null;
  validator_config: null;
}

export interface JoinNode extends NodeBase {
  type: 'join';
  skill_ref: null;
  validator_ref: null;
  validator_config: null;
}

export type WorkflowNode = TaskNode | ValidatorNode | ConditionNode | JoinNode;

export interface WorkflowEdge {
  id: string;
  source: string;
  target: string;
  trigger: string;
  output_map: Record<string, string>;
}

export interface WorkflowPosition {
  x: number;
  y: number;
}

export interface WorkflowDocument {
  schema_version: 'paper-workflow-custom-v1';
  workflow_id: string;
  document_revision: number;
  semantic_revision: number;
  derived_from: { projection_id: string; projection_sha256: string } | null;
  max_parallelism: number;
  external_inputs: string[];
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  ui: { positions: Record<string, WorkflowPosition> };
}

export interface ProjectionNode {
  id: string;
  display_name: string;
  projection_kind: 'task' | 'orchestrator' | 'delivery' | 'gate';
  official_stage_ids: string[];
  suggested_skill_ids: string[];
  suggested_validator_ids: string[];
  inputs: string[];
  outputs: string[];
  write_scopes: string[];
  control_tags: string[];
}

export interface ProjectionEdge {
  id: string;
  source: string;
  target: string;
  projection_note: string;
}

export interface WorkflowProjection {
  schema_version: string;
  projection_id: string;
  source_commit: string;
  nodes: ProjectionNode[];
  edges: ProjectionEdge[];
  stage_map: Record<string, string>;
  control_tags: string[];
  projection_notes: Array<Record<string, JsonValue>>;
}

export interface ProjectionData {
  projection: WorkflowProjection;
  sha256: string;
}

export interface SkillCatalogEntry {
  catalog_id: string;
  display_name: string;
  description: string;
  relative_path: string;
  skill_sha256: string;
  tree_sha256: string;
  locked: boolean;
  ambiguous: boolean;
}

export interface ValidatorInputRole {
  label: string;
  required: boolean;
  required_when: string | null;
}

export interface ValidatorOption {
  label: string;
  choices: JsonPrimitive[];
}

export interface ValidatorCatalogEntry {
  validator_id: string;
  script: string;
  sha256: string;
  adapter: string;
  input_schema: string;
  control_tags: string[];
  outcomes: string[];
  available?: boolean;
  unavailable_reason?: string;
  input_roles?: Record<string, ValidatorInputRole>;
  options?: Record<string, ValidatorOption>;
}

export interface WorkflowIssue {
  code: string;
  message: string;
  operation: string;
  recovery: string;
  node_id: string;
  edge_id: string;
}

export interface ApiEnvelope<T> {
  status: 'pass' | 'blocked' | 'error';
  data: T | null;
  errors: WorkflowIssue[];
  warnings: WorkflowIssue[];
  wrote_files: boolean;
}

export interface BootstrapData {
  project_label: string;
  project_root?: string;
  mode: 'official' | 'custom';
  active_workflow: ActiveWorkflowSummary | null;
  document_revision: number;
  csrf_token: string;
  max_json_body_bytes: number;
  approvals?: Array<{ node_id: string; source_node_id: string; state: string }>;
}

export interface ActiveWorkflowSummary {
  workflow_id: string;
  semantic_revision: number;
  semantic_sha256: string;
  run_id: string;
}

export interface SelectionData {
  mode: 'official' | 'custom';
  selection_revision: number;
  workflow_id: string;
  semantic_revision: number;
  semantic_sha256: string;
  acknowledged_warning_codes: string[];
  acknowledged_semantic_sha256: string;
  acknowledged_at: string;
}

export interface CatalogData {
  skills: SkillCatalogEntry[];
  validators: ValidatorCatalogEntry[];
}

export interface WorkflowData {
  workflow: WorkflowDocument | null;
  document_revision: number;
}

export interface ValidationData {
  document_sha256: string;
  semantic_sha256: string | null;
  required_warning_codes: string[];
}

export interface CompileData {
  workflow_id: string;
  semantic_revision: number;
  semantic_sha256: string;
  topological_order: string[];
  entry_nodes: string[];
  max_parallelism: number;
}

export interface ApiErrorPayload {
  status: 'error';
  errors: WorkflowIssue[];
  data: null;
  warnings: WorkflowIssue[];
  wrote_files: boolean;
}
