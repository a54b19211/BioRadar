// All backend calls live here. The types are written by hand to match services/bioradar
// (app.py, research/analyses.py view(), research/pipeline.py result(), research/monitor.py topic_view()).
import { reactive } from "vue";

// ---- types ---------------------------------------------------------------------------------

export type Direction = "pipeline" | "science" | "business" | "public_health";
export type Method = "planner" | "baseline";
export type State = "queued" | "running" | "completed" | "failed";
export type Level = "low" | "medium" | "high";
export type Alert = "none" | "normal" | "important" | "urgent";

export interface Options {
  sources: string[];
  demo_sources: string[];
  directions: Direction[];
  methods: Method[];
  configured: Record<"model" | "planner" | "biocheck" | "mechevidence" | "weknora", boolean>;
  limits: { max_steps: number; max_reads: number; max_llm_calls: number };
  monitor: { enabled: boolean; auto_analysis: boolean };
}

export interface AskBody {
  question: string;
  direction: Direction;
  method: Method;
  is_demo: boolean;
  cutoff?: string | null; // ISO time with a time zone
}

export interface Judgment {
  question: string;
  conclusion: string;
  confidence: "insufficient" | "possible" | "probable" | "confirmed";
  credibility: Level; // 真实性置信
  consequence: Level; // 潜在后果
  priority: Level | "urgent"; // 核实优先级
  grounds: string[]; // evidence refs, e.g. "E2"
  unproven: string;
  check?: "supported" | "contradicted" | "unverified"; // BioCheck on the conclusion; absent before T-13
}

export interface Check {
  id: string; // Q1, Q2 ...
  text: string;
  required: boolean;
  importance: number;
  status: "open" | "answered" | "conflict";
  evidence: string[];
}

export interface Computation {
  id: string;
  label: string;
  value: number;
  expression: string;
  unit: string;
  period: string;
  population: string;
  denominator: string;
  operands: { fact: string; value: number; ref: string }[];
}

// MechEvidence's own fields (overall, gaps, conflicts) are passed through as they come.
export interface Mechanism {
  status: "not_run" | "failed" | "completed";
  reason?: string;
  overall?: unknown;
  gaps?: unknown;
  conflicts?: unknown;
}

export interface Stop {
  decision: string;
  reason: string;
}
export interface Note {
  code: string;
  detail: string;
  stage: string;
}

export interface Result {
  report_status: "PASS" | "REVIEW" | "FAIL" | "NOT_RUN";
  draft: string;
  assessment: { alert: Alert; judgments: Judgment[] } | null;
  checks: Check[];
  computations: Computation[];
  mechanism: Mechanism | null;
  stop: Stop | null;
  reads: number;
  notes: Note[];
  usage: { llm_calls: number; requests: number; tokens: number };
  // BioCheck's sentence-by-sentence check; the sentence index is what a review refers to
  review?: { claims?: Claim[] | null } | null;
  // added later by the monitor when a cited record is corrected or retracted
  source_updates?: { ref: string; record_id: string; kind: "correction" | "retraction"; by_record: string; at: string }[];
}

export interface Claim {
  index: number;
  text: string;
  citations: string[];
  verdict: string;
  reason?: string;
  quotes?: { ref: string; quote: string; found: boolean }[];
}

export interface Analysis {
  id: string;
  investigation_id: string;
  parent_id: string | null;
  trigger: string;
  method: Method;
  state: State;
  stage: string;
  error_code: string | null;
  question: string;
  direction: Direction;
  is_demo: boolean;
  cutoff: string | null;
  created_at: string;
  completed_at: string | null;
  // null until the run ends; after an unexpected crash it holds only { trace }
  result: (Partial<Result> & { trace?: string }) | null;
}

export interface Action {
  kind: "search" | "fetch" | "next_page" | "read";
  source: string;
  args: { terms?: string[]; page?: number; record_id?: string };
  target_checks: string[];
  gain: number;
  status: "completed" | "failed";
  error?: string;
  ref?: string;
  found?: number;
  new?: number;
}

export interface Fact {
  id: string;
  ref: string;
  text: string;
  quote: string;
  checks: string[];
  stance: "supports" | "refutes" | "limits" | "context";
}

export interface Evidence {
  ref: string;
  source: string;
  record_id: string;
  version_id: string;
  title: string;
  published_at: string | null;
  available_at: string | null;
  material_kind: string | null;
  verified: boolean;
  url: string | null;
  withheld?: string; // the source cannot be read now (revoked, or Fabric's source list unreadable)
}

export interface EvidenceText {
  ref: string;
  source: string;
  record_id: string;
  title: string;
  text: string;
  version_id: string;
  url?: string | null;
  text_level?: string | null;
  withheld?: string;
}

export interface ModelCall {
  stage: string;
  service: string;
  model: string | null;
  tokens: number;
  requests: number;
  finish_reason: string | null;
  seconds: number;
  error: string | null;
}

export interface AnalysisFull extends Analysis {
  progress: {
    checks: Check[] | null;
    queries: { terms: string[]; checks: string[] }[] | null;
    actions: Action[] | null;
    facts: Fact[] | null;
    stop: Stop | null;
    notes: Note[] | null;
  };
  evidence: Evidence[];
  calls: ModelCall[];
  reviews: Review[];
}

export type TopicState = "emerging" | "rising" | "cooling" | "dormant";

export interface Topic {
  id: string;
  title: string;
  direction: Direction;
  is_demo: boolean;
  state: TopicState;
  alert: Alert;
  heat: number;
  awakenings: number;
  closed: boolean;
  first_seen_at: string;
  last_activity_at: string;
  last_analysis_id: string | null;
}

// An event is { at, kind } plus the keys of its detail, which depend on the kind.
export interface TopicEvent {
  at: string;
  kind: "created" | "observed" | "state" | "analysis" | "watch" | "series" | "alert";
  record?: string;
  title?: string;
  alert?: Alert;
  closure?: boolean;
  before?: string; // a TopicState, or an Alert for kind "alert"
  after?: string;
  by?: string; // kind "alert": who changed it
  reason?: string;
  analysis_id?: string;
}

export interface Observation {
  title: string;
  source: string;
  record_id: string;
  topic_id?: string | null; // not sent inside a topic
  available_at: string;
  alert: Alert;
  closure: boolean;
  reason: string;
}

export interface TopicFull extends Topic {
  events: TopicEvent[];
  observations: Observation[];
}

export interface TickSummary {
  records: number;
  backlog: boolean; // more new records wait in the feed than one round takes
  paused: string | null; // "daily_auto_tokens" when today's automatic ceiling is reached
  clock: string;
  topics: Record<string, string>; // topic id -> why it needs attention
  analyses: string[]; // ids of the analyses started
}

export interface UsageRow {
  day: string;
  service: string;
  calls: number;
  requests: number;
  tokens: number;
}

// ---- requests ------------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

/** null = not checked yet. The router guard fills it in, login/logout and any 401 update it. */
export type Role = "viewer" | "analyst" | "admin";
export const auth = reactive<{ signedIn: boolean | null; name: string; role: Role | null }>({
  signedIn: null,
  name: "",
  role: null,
});
// viewer reads; analyst also asks, follows up and reviews; admin also manages users
export const canReview = () => auth.role === "analyst" || auth.role === "admin";

// The backend answers errors in three shapes: {error_code, message}, {detail: "text"} and {detail: [{msg}]}.
async function errorText(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (body.error_code === "not_found") return `没有找到：${body.message}`;
    if (body.error_code === "invalid_input") return `输入不合法：${body.message}`;
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) return body.detail.map((d: { msg: string }) => d.msg).join("；");
    if (body.message) return String(body.message);
  } catch {
    // not JSON
  }
  return `请求失败（HTTP ${res.status}）`;
}

async function send(method: string, path: string, body?: unknown): Promise<Response> {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401) {
    auth.signedIn = false;
    if (location.pathname !== "/login") location.assign("/login");
  }
  if (!res.ok) throw new ApiError(await errorText(res), res.status);
  return res;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  return (await send(method, path, body)).json();
}

const demo = (isDemo: boolean) => `is_demo=${isDemo}`;

// ---- session -------------------------------------------------------------------------------

interface SessionState {
  signed_in: boolean;
  name?: string;
  role?: Role;
}
function setSession(s: SessionState) {
  auth.signedIn = s.signed_in;
  auth.name = s.name ?? "";
  auth.role = s.role ?? null;
}
export async function checkSession() {
  setSession(await request<SessionState>("GET", "/api/session"));
}
export async function login(name: string, credential: string) {
  setSession(await request<SessionState>("POST", "/api/session", { name, credential }));
}
export async function logout() {
  await request("DELETE", "/api/session");
  setSession({ signed_in: false });
}

// ---- human reviews -------------------------------------------------------------------------

export type ReviewAction = "confirm" | "reject" | "flag_source" | "set_alert";
export interface Review {
  id: number;
  by: string;
  action: ReviewAction;
  analysis_id: string | null;
  sentence: number | null;
  evidence_ref: string | null;
  topic_id: string | null;
  alert: Alert | null;
  note: string;
  created_at: string;
}
export const addReview = (body: {
  action: ReviewAction;
  analysis_id?: string;
  sentence?: number;
  evidence_ref?: string;
  topic_id?: string;
  alert?: Alert;
  note?: string;
}) => request<Review>("POST", "/api/reviews", body);

// ---- analyses ------------------------------------------------------------------------------

export const getOptions = () => request<Options>("GET", "/api/options");
export const ask = (body: AskBody) => request<Analysis>("POST", "/api/analyses", body);
export const followup = (id: string, question: string) =>
  request<Analysis>("POST", `/api/analyses/${id}/followup`, { question });
export const listAnalyses = () => request<Analysis[]>("GET", "/api/analyses");
export const getAnalysis = (id: string) => request<AnalysisFull>("GET", `/api/analyses/${id}`);
export const getEvidence = (id: string, ref: string) =>
  request<EvidenceText>("GET", `/api/analyses/${id}/evidence/${ref}`);
export async function exportAnalysis(id: string): Promise<string> {
  return (await send("GET", `/api/analyses/${id}/export`)).text();
}

// ---- topics and monitoring -----------------------------------------------------------------

export const listTopics = (isDemo: boolean) => request<Topic[]>("GET", `/api/topics?${demo(isDemo)}`);
export const getTopic = (id: string) => request<TopicFull>("GET", `/api/topics/${id}`);
export const tick = (isDemo: boolean) =>
  request<TickSummary>("POST", "/api/monitor/tick", { is_demo: isDemo });
export const listObservations = (isDemo: boolean) =>
  request<Observation[]>("GET", `/api/monitor/observations?${demo(isDemo)}`);
export const getUsage = () => request<UsageRow[]>("GET", "/api/usage");

export interface SourceStatus {
  source: string;
  collection: { state: string | null; last_recorded_success_at?: string | null; last_recorded_failure_at?: string | null; scope?: string | null; coverage?: string | null; error?: string };
  failed_tasks: { watch_id: string | null; scope: string | null; window_ended_at: string | null }[];
  last_new_at: string | null;
  latest_published_at: string | null;
  lag: number | null;
  note: string;
}
export interface SourceReport {
  sources: SourceStatus[];
  auto_tokens_today: number;
  daily_auto_tokens: number;
}
export const getSources = () => request<SourceReport>("GET", "/api/sources");
