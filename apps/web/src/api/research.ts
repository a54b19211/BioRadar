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

export interface ResearchProgress {
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

type OriginalResult = components["schemas"]["AnalysisResult"];
export type ResearchResult = Omit<OriginalResult, "report" | "mechanism_evaluation" | "query_policy"> & {
  report?: ResearchReport | null;
  mechanism_evaluation?: ResearchMechanismEvaluation | null;
  query_policy?: QueryPolicyTrace | null;
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
