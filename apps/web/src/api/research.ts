import type { components } from "./generated/engine";

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
}

export interface ResearchProgress {
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

export interface ResearchReport {
  final_text: string;
  status: string;
  provider?: string;
  verification?: Record<string, unknown> | null;
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
export type ResearchResult = Omit<OriginalResult, "report" | "mechanism_evaluation"> & {
  report?: ResearchReport | null;
  mechanism_evaluation?: ResearchMechanismEvaluation | null;
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
  "result" | "initial_draft" | "progress" | "budget" | "providers" | "calls"
> & {
  result: ResearchResult | null;
  initial_draft?: ResearchResult | null;
  calls: ResearchCall[];
  progress?: ResearchProgress;
  budget?: ResearchBudgetView;
  providers?: Record<string, ResearchProvider>;
};

export type ResearchAnalysisList = components["schemas"]["AnalysisList"] &
  Partial<ResearchOptions>;

export interface ResearchAskReceipt {
  investigation_id: string;
  analysis_id: string;
  analysis: ResearchAnalysisView;
}
