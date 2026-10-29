import type { components } from "./generated/engine";

export type SnapshotSummary = components["schemas"]["SnapshotSummary"];
export type RunView = components["schemas"]["RunView"];
export type MiningPolicy = components["schemas"]["MiningPolicy"];
export type FeedbackView = components["schemas"]["FeedbackView"];
export type Candidate = components["schemas"]["Candidate"];
export type NormalizedSnapshot = components["schemas"]["NormalizedSnapshot"];
export type DecisionRunView = components["schemas"]["DecisionRunView"];
export type DecisionRunCreate = components["schemas"]["DecisionRunCreate"];
export type DecisionInput = DecisionRunCreate["input"];
export type DecisionResult = NonNullable<DecisionRunView["result"]>;
export type GraphPackageSummary = components["schemas"]["GraphPackageSummary"];
export type GraphPackageList = components["schemas"]["GraphPackageList"];
export type GraphInvestigationResult =
  components["schemas"]["GraphInvestigationResult"];
export type InvestigationView = components["schemas"]["InvestigationView"];
export type InvestigationSummary =
  components["schemas"]["InvestigationSummary"];
export type InvestigationEntry = components["schemas"]["InvestigationEntry"];
export type InvestigationEntryCreate =
  components["schemas"]["InvestigationEntryCreate"];
export type SnapshotComparison = components["schemas"]["SnapshotComparison"];
export type EvidenceSupport = components["schemas"]["EvidenceSupport"];

export class EngineError extends Error {
  constructor(
    public status: number,
    message: string,
    public code: string | null = null,
    public details: Record<string, unknown> | null = null,
  ) {
    super(message);
  }
}

export async function engineRequest<T>(
  path: string,
  options: RequestInit = {},
  responseType: "json" | "text" = "json",
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch("/api/engine" + path, {
    ...options,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (response.ok && responseType === "text")
    return (await response.text()) as T;
  const body =
    response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    throw new EngineError(
      response.status,
      body?.error?.message ||
        (response.status === 401
          ? "请登录研究空间后继续。"
          : "请求未完成（" + response.status + "），请查看输入或稍后重试。"),
      body?.error?.code ?? null,
      body?.error?.details ?? null,
    );
  }
  return body as T;
}

export function post<T>(path: string, body: unknown): Promise<T> {
  return engineRequest<T>(path, {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Idempotency-Key": crypto.randomUUID() },
  });
}
