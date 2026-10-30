<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from "vue";
import { ElButton, ElDrawer, ElTag } from "element-plus";
import { engineRequest, EngineError, type InvestigationView } from "../../api/engine";
import type { components } from "../../api/generated/engine";
import type {
  ResearchAnalysisView, ResearchAnalysisList, ResearchOptions,
  ResearchQuestionSubmission,
} from "../../api/research";
import MachineAnalysisResult from "./MachineAnalysisResult.vue";
import ResearchQuestionForm from "./ResearchQuestionForm.vue";
import ResearchProgress from "./ResearchProgress.vue";

type AnalysisView = ResearchAnalysisView;
type AnalysisList = ResearchAnalysisList;
type EvidenceRead = components["schemas"]["AnalysisEvidenceRead"];
const props = defineProps<{
  investigation: InvestigationView;
  busy: boolean;
  loading: boolean;
  readOnly?: boolean;
  preferredAnalysisId?: string;
}>();
const emit = defineEmits<{
  changed: [];
  unauthorized: [error: unknown];
  selected: [id: string];
}>();
const list = ref<AnalysisList | null>(null);
const selected = ref<AnalysisView | null>(null);
const source = ref<EvidenceRead | null>(null);
const sourceOpen = ref(false);
const sourceError = ref("");
const loading = ref(false);
const writing = ref(false);
const exporting = ref(false);
const printing = ref(false);
const error = ref("");
const followUpVersion = ref(0);
let disposed = false;
let epoch = 0;
let listVersion = 0;
let detailVersion = 0;
let sourceVersion = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
let activeAnalysisId = "";
let followLatest = true;
let writeIntent: { body: string; key: string } | null = null;

const stateLabels: Record<string, string> = {
  queued: "等待执行", running: "正在调查", completed: "研究已完成",
  needs_review: "研究结果待核", blocked: "需处理后继续", failed: "本次未完成",
};
function analysisTime(value: string) {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}
const sourceLabels: Record<string, string> = {
  clinicaltrials: "ClinicalTrials 临床登记", europe_pmc: "Europe PMC 文献",
  legacy_documents: "Fabric 已采用资料", fda_crl: "FDA 审评信函",
  who_don: "WHO 通报", external_archive: "已导入原始档案",
};
const queryLabels: Record<string, string> = {
  prepared: "准备读取", running: "正在读取", read: "已读取",
  empty: "本次未查到", failed: "读取失败",
  sent: "正在检索", unavailable: "来源暂不可用", unconfirmed: "请求结果尚未确认",
};
function queryProvider(query: AnalysisView["queries"][number]) {
  return query.provider === "weknora" ? "WeKnora 检索候选"
    : ["graph", "fabric_graph"].includes(String(query.provider)) ? "图谱背景查询" : "Fabric 固定资料检索";
}
function querySource(query: AnalysisView["queries"][number]) {
  const source = (query.plan as Record<string, unknown>).source_id;
  return typeof source === "string" ? sourceLabels[source] || source : "来源见策略动作";
}
function queryTerms(query: AnalysisView["queries"][number]) {
  const plan = query.plan as Record<string, unknown>;
  if (Array.isArray(plan.terms)) return plan.terms.filter((value) => typeof value === "string").join(" + ");
  if (typeof plan.query === "string") return plan.query;
  return "图谱动作参数见本版策略轨迹";
}
function queryReason(query: AnalysisView["queries"][number]) {
  const reason = (query.plan as Record<string, unknown>).reason;
  return typeof reason === "string" ? reason : "查询目的见本版策略轨迹。";
}
function queryState(query: AnalysisView["queries"][number]) {
  if (query.provider === "weknora" && ["read", "empty"].includes(query.status)) {
    return query.candidates?.length ? "候选检索完成" : "本次未检索到候选";
  }
  return queryLabels[query.status] || query.status;
}
const candidateLabels: Record<string, string> = {
  bound: "已绑定原件", unbound: "尚未绑定原件", not_authorized: "不在本轮来源范围",
  not_found: "未找到已发布原件", failed: "原件绑定未完成",
};
function stopReason(code?: string | null) {
  if (code === "analysis_provider_changed") return "本轮使用的方法版本发生变化，进度已保留；核对版本后可开始新的尝试。";
  if (code === "analysis_manual_budget") return "本小时研究额度已用尽，可待额度恢复后继续。";
  if (code?.includes("budget")) return "本轮预算不足，进度已保留；调整研究范围或预算后可开始新的尝试。";
  if (code?.includes("unconfirmed")) return "已有请求的结果尚未确认，请先核对调用回执，再决定是否继续。";
  if (code === "analysis_no_material") return "本轮未取得相关原文，可修改检索词或补充来源后继续。";
  if (code === "analysis_no_retrieval_scope" || code?.includes("source") || code?.includes("unavailable") || code === "history_not_connected") {
    return "资料来源暂不可读取，请核对来源服务与许可，恢复后继续这项调查。";
  }
  return "本轮尚未完成，请查看执行记录中的具体原因，处理后继续这项调查。";
}
const unavailableLabels: Record<string, string> = {
  disabled: "研究模型尚未启用。", not_configured: "尚未配置可调用模型。",
  semantic_model_disabled: "研究模型尚未启用。",
  semantic_model_not_configured: "尚未配置可调用模型。",
};
const canStart = computed(() =>
  !props.readOnly && list.value?.enabled && list.value.configured &&
  props.investigation.status === "open" && !props.busy && !props.loading && !writing.value &&
  !list.value.items.some((item) => ["queued", "running"].includes(item.state)),
);
const materialScope = computed(() => selected.value?.progress?.material_scope ?? null);
const unreadQueryHits = computed(() => materialScope.value?.unread_versions.filter(item => item.query_hit) ?? []);
const researchOptions = computed<ResearchOptions | null>(() => list.value ? {
  enabled: list.value.enabled,
  configured: list.value.configured,
  unavailable_reason: list.value.unavailable_reason,
  retrieval_sources: list.value.retrieval_sources ?? [],
  limits: list.value.limits ?? {},
  providers: list.value.providers,
} : null);
const safeSourceUrl = computed(() => {
  try {
    const url = new URL(source.value?.source_url || "");
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
});
function current(generation: number, id: string) {
  return !disposed && epoch === generation && props.investigation.id === id;
}
function endpoint(id = props.investigation.id) {
  return "/investigations/" + encodeURIComponent(id) + "/analyses";
}
function stopPolling() { if (timer) clearTimeout(timer); timer = undefined; }
function clearPrivate() {
  ++epoch; ++listVersion; ++detailVersion; ++sourceVersion;
  stopPolling();
  list.value = null; selected.value = null; source.value = null; sourceOpen.value = false;
  sourceError.value = ""; loading.value = false; writing.value = false; exporting.value = false;
  printing.value = false; writeIntent = null; activeAnalysisId = ""; followLatest = true;
  ++followUpVersion.value;
}
function fail(value: unknown) {
  const message = value instanceof Error ? value.message : "研究记录暂时无法读取。";
  if (value instanceof EngineError && [401, 403].includes(value.status)) {
    clearPrivate();
    emit("unauthorized", value);
  }
  error.value = message;
}
function schedulePoll() {
  stopPolling();
  if (list.value?.items.some((item) => ["queued", "running"].includes(item.state)) || (selected.value && ["queued", "running"].includes(selected.value.state))) {
    timer = setTimeout(() => { void refresh(); }, 3000);
  }
}
async function openAnalysis(id: string, userSelected = false) {
  const generation = epoch, investigationId = props.investigation.id, version = ++detailVersion;
  if (userSelected) followLatest = id === list.value?.items[0]?.id;
  if (activeAnalysisId !== id) { selected.value = null; closeSource(); }
  activeAnalysisId = id;
  try {
    const view = await engineRequest<AnalysisView>(endpoint(investigationId) + "/" + encodeURIComponent(id));
    if (!current(generation, investigationId) || version !== detailVersion || activeAnalysisId !== id) return;
    selected.value = view;
    if (userSelected) emit("selected", id);
  } catch (value) {
    if (current(generation, investigationId) && version === detailVersion) fail(value);
  }
}
async function refresh(preferId?: string) {
  const generation = epoch, id = props.investigation.id, version = ++listVersion;
  loading.value = true;
  try {
    const items = await engineRequest<AnalysisList>(endpoint(id));
    if (!current(generation, id) || version !== listVersion) return;
    const running = new Set(list.value?.items.filter((item) => ["queued", "running"].includes(item.state)).map((item) => item.id) || []);
    const requested = !activeAnalysisId ? props.preferredAnalysisId : undefined;
    const keepId = items.items.length
      ? preferId || requested || (followLatest ? items.items[0]?.id : activeAnalysisId) || items.items[0]?.id
      : undefined;
    if (requested && requested !== items.items[0]?.id) followLatest = false;
    list.value = items;
    if (!items.items.length) {
      ++detailVersion; activeAnalysisId = ""; selected.value = null; closeSource();
    }
    error.value = "";
    if (keepId) {
      const previousSelected = selected.value;
      await openAnalysis(keepId);
      if (!current(generation, id) || version !== listVersion) return;
      const selectedFinished = previousSelected?.id === selected.value?.id
        && previousSelected && ["queued", "running"].includes(previousSelected.state)
        && selected.value && !["queued", "running"].includes(selected.value.state);
      if (selectedFinished || items.items.some((item) => running.has(item.id) && !["queued", "running"].includes(item.state))) emit("changed");
    }
    schedulePoll();
  } catch (value) {
    if (current(generation, id) && version === listVersion) fail(value);
  } finally {
    if (current(generation, id) && version === listVersion) loading.value = false;
  }
}
async function loadMore() {
  const cursor = list.value?.next_cursor;
  if (!cursor || loading.value) return;
  const generation = epoch, id = props.investigation.id, version = ++listVersion;
  loading.value = true;
  try {
    const page = await engineRequest<AnalysisList>(endpoint(id) + "?cursor=" + encodeURIComponent(cursor));
    if (!current(generation, id) || version !== listVersion || !list.value) return;
    const existing = new Set(list.value.items.map((item) => item.id));
    list.value.items.push(...page.items.filter((item) => !existing.has(item.id)));
    list.value.next_cursor = page.next_cursor;
  } catch (value) { if (current(generation, id) && version === listVersion) fail(value); }
  finally { if (current(generation, id) && version === listVersion) loading.value = false; }
}
async function startAnalysis(retryId?: string, submission?: ResearchQuestionSubmission) {
  if (!canStart.value) return;
  const generation = epoch, id = props.investigation.id;
  const chosen = selected.value?.result ? selected.value : null;
  const parent = chosen || list.value?.items.find((item) => item.result_summary && ["completed", "needs_review"].includes(item.state));
  const requestedBudget = submission ? submission.budget : selected.value?.budget?.limits;
  const body = JSON.stringify({
    expected_revision: props.investigation.revision,
    question: submission?.question ?? selected.value?.question ?? props.investigation.question,
    source_ids: submission?.source_ids ?? selected.value?.search_scope ?? [],
    query_policy: submission?.query_policy ?? selected.value?.query_policy?.requested ?? selected.value?.result?.query_policy?.requested ?? undefined,
    ...(requestedBudget ? { budget: requestedBudget } : {}),
    parent_analysis_id: (retryId ? selected.value?.parent_analysis_id : parent?.id) ?? null,
    material_entry_ids: [],
    retry_of_analysis_id: retryId ?? null,
    auto_retrieve: true,
  });
  if (!writeIntent || writeIntent.body !== body) writeIntent = { body, key: crypto.randomUUID() };
  writing.value = true; error.value = ""; stopPolling();
  try {
    const result = await engineRequest<AnalysisView>(endpoint(id), {
      method: "POST", body: writeIntent.body,
      headers: { "Idempotency-Key": writeIntent.key },
    });
    if (!current(generation, id)) return;
    writeIntent = null;
    followLatest = true;
    ++followUpVersion.value;
    emit("selected", result.id);
    await refresh(result.id);
    if (current(generation, id)) emit("changed");
  } catch (value) {
    if (current(generation, id)) {
      fail(value);
      schedulePoll();
    }
  } finally { if (current(generation, id)) writing.value = false; }
}
function closeSource() {
  ++sourceVersion; sourceOpen.value = false; source.value = null; sourceError.value = "";
}
async function readSource(key: string) {
  const view = selected.value;
  if (!view) return;
  const generation = epoch, id = props.investigation.id, version = ++sourceVersion;
  source.value = null; sourceError.value = ""; sourceOpen.value = true;
  try {
    const result = await engineRequest<EvidenceRead>(endpoint(id) + "/" + encodeURIComponent(view.id) + "/evidence/" + encodeURIComponent(key));
    if (!current(generation, id) || version !== sourceVersion || selected.value?.id !== view.id) return;
    source.value = result;
  } catch (value) {
    if (current(generation, id) && version === sourceVersion) {
      sourceError.value = value instanceof Error ? value.message : "原文暂不可读取。";
      if (value instanceof EngineError && [401, 403].includes(value.status)) fail(value);
    }
  }
}
async function exportAnalysis(format: "json" | "markdown" = "json") {
  const view = selected.value;
  if (!view || exporting.value) return;
  const generation = epoch, id = props.investigation.id;
  exporting.value = true;
  try {
    const suffix = format === "markdown" ? "?format=markdown" : "";
    const result = await engineRequest<Record<string, unknown> | string>(
      endpoint(id) + "/" + encodeURIComponent(view.id) + "/export" + suffix, {},
      format === "markdown" ? "text" : "json",
    );
    if (!current(generation, id) || selected.value?.id !== view.id) return;
    const content = format === "markdown" ? String(result) : JSON.stringify(result, null, 2);
    const url = URL.createObjectURL(new Blob([content], { type: format === "markdown" ? "text/markdown;charset=utf-8" : "application/json" }));
    const link = document.createElement("a");
    link.href = url; link.download = "研究报告-" + view.id + (format === "markdown" ? ".md" : ".json");
    link.click(); URL.revokeObjectURL(url);
  } catch (value) { if (current(generation, id) && selected.value?.id === view.id) fail(value); }
  finally { if (current(generation, id)) exporting.value = false; }
}
async function printAnalysis() {
  const view = selected.value;
  if (!view || printing.value) return;
  const generation = epoch, id = props.investigation.id;
  printing.value = true;
  try {
    const fresh = await engineRequest<AnalysisView>(endpoint(id) + "/" + encodeURIComponent(view.id));
    if (!current(generation, id) || selected.value?.id !== view.id) return;
    selected.value = fresh;
    closeSource();
    await nextTick();
    if (current(generation, id) && selected.value?.id === view.id) window.print();
  } catch (value) { if (current(generation, id) && selected.value?.id === view.id) fail(value); }
  finally { if (current(generation, id)) printing.value = false; }
}
watch(() => props.investigation.id, () => { clearPrivate(); error.value = ""; void refresh(); }, { immediate: true });
watch(() => props.investigation.revision, () => { if (!writing.value) void refresh(); });
watch(() => props.preferredAnalysisId, (id) => {
  if (id && id !== activeAnalysisId) void openAnalysis(id, true);
});
onUnmounted(() => { disposed = true; clearPrivate(); });
</script>

<template>
  <section class="engine-panel machine-analysis-panel">
    <div class="analysis-heading">
      <div>
        <h2>{{ readOnly ? "已保存的研究报告" : "研究进度与报告" }}</h2>
        <p class="muted">从实际采用的原文追溯回答、反证和每一版变化。</p>
      </div>
      <div class="analysis-actions no-print">
        <ElButton :disabled="writing || loading" @click="refresh()">刷新</ElButton>
        <ElButton v-if="!readOnly && list?.items.length" :loading="writing" :disabled="!canStart" @click="startAnalysis()">按当前材料更新</ElButton>
      </div>
    </div>
    <p v-if="investigation.origin" class="muted">
      本调查源自一条固定计算候选；模式依据、价值筛选和审阅容量在原计算中保留。
      <RouterLink :to="{ path: '/radar', query: { run: investigation.origin.run_id, candidate: investigation.origin.candidate_key } }">返回该模式与本轮筛选</RouterLink>
    </p>
    <p v-if="error" class="error-panel no-print" role="alert">{{ error }}</p>
    <p v-if="list && (!list.enabled || !list.configured)" class="notice">
      {{ unavailableLabels[list.unavailable_reason || ""] || list.unavailable_reason || "研究模型当前不可用；已有材料和报告仍可阅读。" }}
    </p>
    <p v-if="investigation.status !== 'open'" class="notice">调查已暂停或停止，可继续阅读保存的结果。</p>
    <p v-if="list?.unavailable_count" class="notice">部分历史分析的来源当前不可读取，因此未显示。</p>
    <p v-if="list" class="muted no-print">
      当前可补查：{{ (list.retrieval_sources ?? []).map((item) => sourceLabels[item] || item).join("、") || "尚无可用来源" }}。
    </p>
    <div v-if="list?.items.length" class="analysis-history no-print" aria-label="研究版本">
      <button v-for="item in list.items" :key="item.id" class="history-item"
        :class="{ active: selected?.id === item.id }" :disabled="writing"
        @click="openAnalysis(item.id, true)">
        <span>{{ analysisTime(item.created_at) }}<small>{{ stateLabels[item.state] }}</small></span>
      </button>
      <ElButton v-if="list.next_cursor" text :disabled="loading" @click="loadMore">读取更早版本</ElButton>
    </div>
    <template v-if="selected">
      <div class="candidate-meta">
        <ElTag>{{ stateLabels[selected.state] }}</ElTag>
        <span>{{ selected.is_demo ? "教学材料" : "真实资料" }} · {{ selected.evidence_count }} 项采用资料</span>
        <div class="analysis-actions no-print">
          <ElButton text :loading="exporting" @click="exportAnalysis('markdown')">下载研报</ElButton>
          <ElButton text :loading="exporting" @click="exportAnalysis('json')">下载完整记录</ElButton>
          <ElButton text :loading="printing" @click="printAnalysis">打印本版</ElButton>
        </div>
      </div>
      <p class="investigation-prose"><strong>本版研究问题：</strong>{{ selected.question }}</p>
      <ResearchProgress :analysis="selected" />
      <div v-if="['blocked', 'failed', 'needs_review'].includes(selected.state)" class="notice">
        <p>{{ selected.state === "needs_review" ? "本版仍有待核问题。" : stopReason(selected.error_code) }}</p>
        <details v-if="selected.error_code"><summary>查看停止代码</summary><small>{{ selected.error_code }}</small></details>
        <ElButton v-if="!readOnly" class="no-print" :loading="writing" :disabled="!canStart" @click="startAnalysis(selected.id)">重新研究</ElButton>
      </div>
      <section v-if="materialScope" class="material-scope notice" aria-label="本轮资料阅读范围">
        <h3>本轮资料范围（系统记录）</h3>
        <p><strong>固定资料 {{ materialScope.fixed_version_count }} 个版本；实际读取其中 {{ materialScope.read_version_count }} 个版本的选定片段。</strong></p>
        <p class="material-scope-counts">未进入本轮原文片段：{{ materialScope.unread_versions.length }} 个版本；补查未完成：{{ materialScope.source_failures.length }} 次。</p>
        <p>这些数字不是全文阅读量、独立研究数或全领域覆盖率。未读材料与失败补查不代表没有相关证据。</p>
        <p v-if="unreadQueryHits.length" class="material-scope-counts">
          成功补查命中但未读：{{ unreadQueryHits.length }} 个固定版本。
          <span v-if="unreadQueryHits.some(item => item.adoption_stop_reason === 'analysis_evidence_budget')">本轮原文片段容量已满，这些命中未进入已读证据。</span>
        </p>
        <p v-if="materialScope.qualification" class="muted">{{ materialScope.qualification }}</p>
        <details v-if="materialScope.unread_versions.length || materialScope.source_failures.length" class="no-print">
          <summary>查看未读版本与未完成补查</summary>
          <ul v-if="materialScope.unread_versions.length">
            <li v-for="item in materialScope.unread_versions" :key="[item.source_id, item.source_record_id, item.version_id].join(':')">
              未进入原文片段 · {{ sourceLabels[item.source_id] || item.source_id }} · {{ item.source_record_id }} · 固定版本 {{ item.version_id }}
              <span v-if="item.query_hit"> · 成功补查命中，尚未进入已读片段</span>
              <small v-if="item.adoption_stop_reason"> · {{ item.adoption_stop_reason === 'analysis_evidence_budget' ? '本轮原文片段容量已满' : item.adoption_stop_reason }}</small>
            </li>
          </ul>
          <ul v-if="materialScope.source_failures.length">
            <li v-for="(item, index) in materialScope.source_failures" :key="index">
              补查未完成 · {{ sourceLabels[item.source_id || ''] || item.source_id || item.provider || '来源未记录' }}
              · {{ queryLabels[item.status] || item.status }}<template v-if="item.error_code"> · {{ item.error_code }}</template>
            </li>
          </ul>
        </details>
      </section>
      <MachineAnalysisResult :analysis="selected" @source="readSource" />
      <details v-if="selected.initial_draft" :open="!selected.result" class="initial-draft">
        <summary>修订前的分析与发现的问题</summary>
        <MachineAnalysisResult :analysis="{ ...selected, result: selected.initial_draft }" @source="readSource" />
      </details>
      <details :open="['queued', 'running', 'blocked', 'failed'].includes(selected.state)" class="research-queries">
        <summary>实际检索与补查（{{ selected.queries.length }} 项）</summary>
        <article v-for="query in selected.queries" :key="[query.provider, query.phase, query.index].join(':')" class="investigation-entry">
          <p><strong>{{ queryProvider(query) }}</strong>
            <small class="muted"> · {{ query.phase === "initial" ? "初查" : "补查" }}<template v-if="query.provider !== 'weknora'"> · {{ querySource(query) }}</template></small>
          </p>
          <p>{{ queryTerms(query) }}</p>
          <p>{{ queryReason(query) }}</p>
          <p v-if="'expected_discrimination' in query.plan && query.plan.expected_discrimination">用于区分：{{ query.plan.expected_discrimination }}</p>
          <p class="muted">
            {{ queryState(query) }}
            <template v-if="query.provider === 'weknora' && (['read', 'empty'].includes(query.status) || query.candidates?.length)">
              · 候选 {{ query.candidates?.length ?? 0 }} 项，成功绑定原件 {{ query.returned }} 份，新增采用 {{ query.new_versions }} 个版本
            </template>
            <template v-else-if="['read', 'empty'].includes(query.status)">
              · 返回 {{ query.returned }} 条，新增采用 {{ query.new_versions }} 个版本
            </template>
            <span v-if="query.has_more"> · 本次范围外仍有更多结果</span>
          </p>
          <template v-if="query.error_code">
            <p class="muted">{{ stopReason(query.error_code) }}</p>
            <details><summary>查看读取代码</summary><small>{{ query.error_code }}</small></details>
          </template>
          <details v-if="query.provider === 'weknora' && query.candidates?.length">
            <summary>候选与原件绑定情况</summary>
            <ul><li v-for="(candidate, index) in query.candidates" :key="index">
              {{ candidate.knowledge_title || candidate.knowledge_id || candidate.id || "候选" }} ·
              {{ candidateLabels[String(candidate.binding_status)] || "绑定状态待确认" }}
              <small v-if="candidate.binding_reason" class="muted">（{{ candidate.binding_reason }}）</small>
            </li></ul>
          </details>
          <div class="no-print"><ElButton v-for="key in query.evidence_keys" :key="key" text @click="readSource(key)">阅读 {{ key }}</ElButton></div>
        </article>
        <p v-if="!selected.queries.length" class="muted">本版尚无已记录的检索动作。</p>
      </details>
      <ul v-if="selected.limitations.length" class="muted"><li v-for="item in selected.limitations" :key="item">{{ item }}</li></ul>
      <details class="no-print">
        <summary>执行与版本记录</summary>
        <p>本版记录：{{ selected.id }} · {{ selected.created_at }}</p>
        <p>分析版本：{{ selected.version }}；提示版本：{{ selected.prompt_version }}</p>
        <p>实际补查范围：{{ selected.search_scope.map((item) => sourceLabels[item] || item).join("、") || "无" }}</p>
        <p v-if="selected.progress?.updated_at">最近更新：{{ selected.progress.updated_at }}</p>
        <p v-if="selected.error_code">未完成代码：{{ selected.error_code }}</p>
        <pre class="source-text">{{ JSON.stringify({ budget: selected.budget, calls: selected.calls }, null, 2) }}</pre>
      </details>
    </template>
    <p v-else-if="loading" class="muted">正在读取研究记录…</p>
    <p v-else class="muted">{{ readOnly ? "这项调查尚无已保存报告。打开调查后可开始研究。" : "提出研究问题，后台会取得材料并开展调查。" }}</p>
    <ResearchQuestionForm v-if="!readOnly && list && investigation.status === 'open'"
      :key="investigation.id + ':' + followUpVersion"
      :follow-up="Boolean(selected)" :initial-question="selected ? '' : investigation.question"
      :options="researchOptions" :disabled="!canStart" :submitting="writing"
      :is-demo="investigation.is_demo"
      @submit="startAnalysis(undefined, $event)" @unauthorized="fail" />
    <RouterLink v-if="readOnly" :to="{ path: '/investigations', query: { id: investigation.id, ...(selected ? { analysis: selected.id } : {}) } }" class="text-link no-print">继续这项调查 →</RouterLink>
    <ElDrawer :model-value="sourceOpen" title="本版实际采用的固定资料" size="min(820px, 96vw)" @close="closeSource">
      <p v-if="sourceError" class="error-panel" role="alert">{{ sourceError }}</p>
      <template v-else-if="source">
        <h3>{{ source.evidence.title }}</h3>
        <p class="muted">{{ source.warning }}</p>
        <p>本版引用：{{ source.evidence.key }} · {{ sourceLabels[source.evidence.source_id] || source.evidence.source_id }}</p>
        <p v-if="safeSourceUrl"><a :href="safeSourceUrl" target="_blank" rel="noopener noreferrer">打开来源网页</a></p>
        <ul v-if="source.evidence.limitations?.length" class="notice"><li v-for="item in source.evidence.limitations" :key="item">{{ item }}</li></ul>
        <pre class="source-text">{{ source.normalized_text }}</pre>
      </template>
      <p v-else class="muted">正在读取固定版本…</p>
    </ElDrawer>
  </section>
</template>

<style scoped>
.machine-analysis-panel { min-width: 0; overflow-wrap: anywhere; }
.analysis-heading { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 1rem; }
.analysis-heading h2 { margin-top: 0; }
.analysis-actions { display: inline-flex; flex-wrap: wrap; gap: 4px; }
.analysis-history { display: flex; flex-wrap: wrap; gap: .5rem; margin: 1rem 0; }
.analysis-history .history-item { max-width: 100%; width: auto; }
.analysis-history .history-item small { display: block; margin-top: .2rem; }
.research-queries { margin: 18px 0; }
.material-scope { margin: 20px 0; border-inline-start: 4px solid #a56414; break-inside: avoid; }
.material-scope h3 { margin-top: 0; }
.material-scope-counts { font-weight: 600; }
.machine-analysis-panel > .research-question-form { margin-top: 28px; padding-top: 24px; border-top: 1px solid var(--el-border-color); }
@media print { .machine-analysis-panel { border: 0; box-shadow: none; } }
</style>
