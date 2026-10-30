export interface QueryPolicyArtifactRef {
  id: string;
  version: string;
}

export interface QueryPolicyArtifact {
  id: string;
  version: string;
  kind: string;
  source_artifact_id?: string;
  source_version?: string;
  qualified_count?: number;
  record_count?: number;
  scope?: { direction?: string; mode?: string; cutoff?: string };
  policy?: unknown;
  provider?: string;
  status?: string;
}

export interface QueryPolicyCheck {
  id: string;
  question?: string;
  required?: boolean;
  qualification?: string;
  artifact_refs?: QueryPolicyArtifactRef[];
  status: "passed" | "failed" | "unknown";
  evidence_ids?: string[];
  reason?: string;
}

export interface QueryPolicyAction {
  id: string;
  kind: string;
  arguments: {
    provider?: string;
    source_id?: string;
    terms?: string[];
    query?: string;
    phase?: string;
    index?: number;
    [key: string]: unknown;
  };
  covers?: string[];
  estimated_cost?: Record<string, number | null>;
}

export interface QueryPolicyHistory {
  action_id: string;
  status: string;
  query_index?: number;
  evidence_ids?: string[];
  new_evidence_ids?: string[];
  new_artifact_refs?: QueryPolicyArtifactRef[];
  check_updates?: unknown[];
}

export interface QueryPolicyDecision {
  step: number;
  decision: string;
  action?: QueryPolicyAction | null;
  reason?: string;
  policy?: {
    version?: string;
    selector?: string;
    stop?: string;
    training_status?: string;
  };
  state_summary?: {
    observed_history?: QueryPolicyHistory[];
    checks?: QueryPolicyCheck[];
    pending_action_ids?: string[];
    excluded_candidates?: { id: string; reason: string }[];
    warnings?: string[];
    [key: string]: unknown;
  };
  receipt?: {
    query_index?: number;
    status: string;
    error_code?: string | null;
    evidence_ids?: string[];
    new_artifact_refs?: QueryPolicyArtifactRef[];
    check_updates?: unknown[];
    cost?: Record<string, number | null>;
  } | null;
}

export interface QueryPolicyTrace {
  execution_version: string;
  schema_version?: string;
  direction?: string;
  direction_basis?: string;
  artifacts?: QueryPolicyArtifact[];
  artifact_results?: Record<string, unknown>;
  execution?: { action_id: string; kind: string; input_refs: QueryPolicyArtifactRef[]; output_refs: QueryPolicyArtifactRef[] }[];
  requested: string;
  effective: string;
  version?: string;
  source_sha256?: string;
  selector?: string;
  stop?: string;
  training_status?: string;
  status: string;
  reason?: string;
  evidence_time?: { mode: string; cutoff: string; study_id?: string; run_id?: string | null };
  checks?: QueryPolicyCheck[];
  decisions: QueryPolicyDecision[];
  pending_action?: QueryPolicyAction | null;
  budget?: {
    limits?: Record<string, number | null>;
    usage?: Record<string, number | null>;
    remaining?: Record<string, number | null>;
  };
}

export interface PolicyQueryReceipt {
  action_id?: string | null;
  kind?: string;
  artifact_refs?: Record<string, unknown>[];
  provider?: string;
  phase?: string;
  index?: number;
  status: string;
  returned?: number;
  new_versions?: number;
}
