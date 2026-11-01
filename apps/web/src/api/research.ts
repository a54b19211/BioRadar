import type { components } from "./generated/engine";
import type { QueryPolicyTrace } from "./queryPolicy";

export interface ResearchBudget {
  max_calls?: number;
  max_tokens?: number;
  max_seconds?: number;
  max_queries?: number;
}

export interface ResearchProvider {
  status?: string;
  state?: string;
  configured?: boolean;
  available?: boolean;
  connection_checked?: boolean;
  is_demo?: boolean;
  qualification?: string;
  enabled?: boolean;
  reason?: string;
  message?: string;
  error_code?: string;
  [key: string]: unknown;
}

export interface ResearchOptions {
  enabled: boolean;
  configured: boolean;
  unavailable_reason?: string | null;
  retrieval_sources: string[];
  limits: Record<string, number>;
  limits_by_policy?: Partial<Record<"baseline" | "query_planner", Record<string, number>>>;
  providers?: Record<string, ResearchProvider>;
}

export interface ResearchQuestionSubmission {
  question: string;
  source_ids: string[];
  budget?: ResearchBudget;
  query_policy?: "baseline" | "query_planner";
}

export interface ResearchMaterialScope {
  fixed_version_count: number;
  read_version_count: number;
  read_scope: "selected_passages_not_full_documents";
  unread_versions: {
    source_id: string;
    source_record_id: string;
    version_id: string;
    query_hit?: boolean;
    adoption_stop_reason?: string | null;
  }[];
  source_failures: {
    provider?: string | null;
    source_id?: string | null;
    status: string;
    error_code?: string | null;
  }[];
  qualification: string;
}

export interface ResearchMaterialUpdateImpact {
  version: "dependency-aware-material-update-v1";
  parent_analysis_id: string;
  report_currentness?: "prior_fixed_materials";
  report_as_of?: string | null;
  checked_revision: number;
  status: "equivalent_inputs" | "affects_dependency" | "impact_pending";
  decisions: {
    entry_id: string;
    revision: number;
    old_ref: unknown;
    old_refs?: unknown[];
    new_ref: unknown;
    status: "equivalent_inputs" | "affects_dependency" | "impact_pending";
    reason: string;
    affected_claim_ids: string[];
  }[];
}

export interface ResearchAnalysisDiagnostics {
  analysis_id?: string | null;
  investigation_id?: string | null;
  state?: string | null;
  error_code?: string | null;
  report_status?: string | null;
  execution_limits: Record<string, number>;
  queries: Record<string, unknown>[];
  reading_repairs: Record<string, unknown>[];
  biocheck_issues: Record<string, unknown>[];
  numeric_candidates: Record<string, unknown>[];
  usage: {
    total_tokens: number | null;
    known_total_tokens: number;
    unknown_usage_calls: number;
    verified_pass_report: boolean;
    verified_pass_total_tokens: number | null;
    stages: Record<string, unknown>[];
    calls: Record<string, unknown>[];
  };
}

export interface ResearchProgress {
  diagnostics?: ResearchAnalysisDiagnostics | null;
  material_update_impact?: ResearchMaterialUpdateImpact | null;
  waiting_fetch?: { reason: string; action_id?: string | null; next_poll_at: string } | null;
  budget_pool?: "manual" | "monitor" | "topic" | "material_update";
  budget_wait?: {
    reason: string;
    next_budget_at: string;
    started_at: string;
    pool: "manual" | "monitor" | "topic" | "material_update";
  } | null;
  material_scope?: ResearchMaterialScope | null;
  stage?: string;
  started_at?: string | null;
  updated_at?: string | null;
  waiting_reason?: string | null;
}

export interface ResearchBudgetView {
  limits: ResearchBudget & Record<string, number>;
  usage: {
    calls?: number;
    total_tokens?: number | null;
    prompt_tokens?: number | null;
    completion_tokens?: number | null;
    unknown_usage_calls?: number;
    known_total_tokens?: number;
    estimated_tokens?: number;
    budgeted_tokens?: number;
    queries?: number;
    elapsed_seconds?: number;
    monetary_cost?: number | null;
  };
}

export interface ResearchSupportCheck {
  unit_id: string;
  claim_index: number;
  span?: string;
  verdict: string;
  reason?: string;
  evidence?: {
    evidence_id: string;
    segment_id: string;
    version?: string;
    start?: number;
    end?: number;
    quote?: string;
  }[];
}

export interface ResearchSupportAudit {
  status: string;
  scope?: string;
  reason?: string;
  not_run_reason?: string;
  error_code?: string | null;
  checks?: ResearchSupportCheck[];
  verification_errors?: unknown[];
  [key: string]: unknown;
}

export interface ResearchQualityFeedback {
  text_sha256?: string;
  evidence_gaps?: unknown[];
  repair_units?: string[];
  review_required?: unknown[];
  qualification?: string;
  error_code?: string | null;
  [key: string]: unknown;
}

export interface ResearchReportRound {
  index?: number;
  kind?: string;
  status?: string;
  quality_feedback?: ResearchQualityFeedback | null;
  unsearched_gaps?: unknown[];
  feedback_stop_reason?: string | null;
  repair_stop_reason?: string | null;
  error_code?: string | null;
  report?: ResearchReport | null;
}

export interface ResearchReport {
  generation_error?: string | null;
  verification_error?: string | null;
  error_code?: string | null;
  text_sha256?: string;
  quality_feedback?: ResearchQualityFeedback | null;
  unsearched_gaps?: unknown[];
  feedback_stop_reason?: string | null;
  repair_stop_reason?: string | null;
  rounds?: ResearchReportRound[];
  final_text: string;
  status: string;
  provider?: string;
  verification?: Record<string, unknown> | null;
  support_audit?: ResearchSupportAudit | null;
  [key: string]: unknown;
}

export interface ResearchMechanismEvaluation {
  status: string;
  overall?: {
    verdict?: string;
    rationale?: string;
    valid_witness_ids?: string[];
    has_comparable_conflict?: boolean;
  } | null;
  critical_gaps?: { reason: string; missing_experiment?: string; claim_ids?: string[]; join_ids?: string[] }[];
  material_assessability?: { status?: string; limitations?: string[]; material_count?: number; passage_count?: number };
  independence?: { independent_cohorts?: number | null; note?: string };
  coverage?: { status?: string; missing_fields?: string[] };
  missing_fields?: string[];
  error_code?: string | null;
  [key: string]: unknown;
}

export interface ResearchWarningAssessment {
  decision: "verify_now" | "alert" | "watch";
  supporting_claim_ids: string[];
  opposing_claim_ids: string[];
  critical_unknowns: string[];
  next_actions: string[];
  reason: string;
}
export interface ResearchHypothesisJudgment {
  least_refuted_hypothesis_id: string;
  reason: string;
  next_discriminating_evidence: string;
  qualification: "这是模型判断，未证实";
}

export interface ResearchCompetingHypothesis {
  id: string;
  explanation: string;
  supporting_claim_ids: string[];
  opposing_claim_ids: string[];
  discriminating_evidence_needed: string;
  untested_conditions: string[];
}
export interface ResearchAssessmentBinding {
  status: "bound" | "unbound" | "not_provided";
  scope: string;
  can_use_bound_claim_refs: boolean;
  unbound_claim_ids: string[];
  issues: { path: (string | number)[]; reason: string; claim_id?: string }[];
  qualification: string;
}
export interface ResearchReadingManifestItem {
  source_id: string;
  source_record_id: string;
  version_id: string;
  status: "read" | "unread" | "unavailable";
  priority: "critical" | "normal";
  reason: "correction" | "counterevidence" | "scope";
  covers: string[];
  window_start?: number;
}

type OriginalResult = components["schemas"]["AnalysisResult"];
export type ResearchResult = Omit<OriginalResult, "report" | "mechanism_evaluation" | "query_policy" | "assessment_binding"> & {
  report?: ResearchReport | null;
  mechanism_evaluation?: ResearchMechanismEvaluation | null;
  query_policy?: QueryPolicyTrace | null;
  warning_assessment?: ResearchWarningAssessment | null;
  competing_hypotheses?: ResearchCompetingHypothesis[] | null;
  assessment_axes?: unknown;
  current_judgment?: ResearchHypothesisJudgment | null;
  assessment_binding?: ResearchAssessmentBinding | null;
  coverage_issues?: { check_id: string; source: string; status: "unknown" }[];
  reading_manifest?: ResearchReadingManifestItem[];
};

export interface ResearchCall {
  call_id?: string;
  stage: string;
  status: string;
  receipt?: {
    usage?: {
      prompt_tokens?: number | null;
      completion_tokens?: number | null;
      total_tokens?: number | null;
    };
    request_bytes?: number | null;
    elapsed_ms?: number;
    error_code?: string | null;
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

type OriginalView = components["schemas"]["AnalysisView"];
export type ResearchAnalysisView = Omit<
  OriginalView,
  "result" | "initial_draft" | "progress" | "budget" | "providers" | "calls" | "query_policy"
> & {
  result: ResearchResult | null;
  initial_draft?: ResearchResult | null;
  calls: ResearchCall[];
  human_review_context?: { status: string; analysis_id?: string; reviews: Record<string, unknown>[]; investigation_only_judgment_count: number; qualification: string } | null;
  progress?: ResearchProgress;
  budget?: ResearchBudgetView;
  providers?: Record<string, ResearchProvider>;
  query_policy?: QueryPolicyTrace | null;
};

export type ResearchAnalysisList = components["schemas"]["AnalysisList"] &
  Partial<ResearchOptions>;

export interface ResearchAskReceipt {
  investigation_id: string;
  analysis_id: string;
  analysis: ResearchAnalysisView;
}
