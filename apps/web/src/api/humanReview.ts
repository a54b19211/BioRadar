import type { ResearchAnalysisView } from "./research";

export type ReviewTarget =
  | { type: "candidate"; run_id: string; result_hash: string; candidate_key: string }
  | { type: "report"; analysis_id: string; text_sha256: string }
  | { type: "claim"; analysis_id: string; claim_id: string; claim_sha256: string };
export interface FixedHumanReview {
  decision: "needs_verification" | "dismissed" | "accepted_within_scope";
  scope: string;
  target: ReviewTarget;
}
export const reviewLabels: Record<string, string> = {
  needs_verification: "待人工核实", dismissed: "范围内不采纳", accepted_within_scope: "范围内人工采纳",
};
export function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function ordered(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(ordered);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([key, item]) => [key, ordered(item)]));
  return value;
}
export async function claimDigest(claim: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify(ordered(claim)));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
}
export async function reviewTargets(analysis: ResearchAnalysisView | null | undefined): Promise<{ label: string; target: ReviewTarget }[]> {
  if (!analysis?.result || ["queued", "running"].includes(analysis.state)) return [];
  const targets: { label: string; target: ReviewTarget }[] = [];
  const report = analysis.result.report;
  if (report?.final_text && report.text_sha256) targets.push({ label: "本版整份报告", target: { type: "report", analysis_id: analysis.id, text_sha256: report.text_sha256 } });
  for (const claim of analysis.result.claims) targets.push({ label: "命题 " + claim.id + " · " + claim.text, target: { type: "claim", analysis_id: analysis.id, claim_id: claim.id, claim_sha256: await claimDigest(claim) } });
  return targets;
}
