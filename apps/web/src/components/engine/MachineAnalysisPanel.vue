<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { ElButton, ElDrawer, ElTag } from "element-plus";
import { engineRequest, EngineError, type InvestigationView } from "../../api/engine";
import type { components } from "../../api/generated/engine";
import MachineAnalysisResult from "./MachineAnalysisResult.vue";

type AnalysisView = components["schemas"]["AnalysisView"];
type AnalysisList = components["schemas"]["AnalysisList"];
type EvidenceRead = components["schemas"]["AnalysisEvidenceRead"];
const props = defineProps<{ investigation: InvestigationView; busy: boolean; loading: boolean }>();
const emit = defineEmits<{ changed: []; unauthorized: [error: unknown] }>();
const list = ref<AnalysisList | null>(null);
const selected = ref<AnalysisView | null>(null);
const source = ref<EvidenceRead | null>(null);
const sourceOpen = ref(false);
const sourceError = ref("");
const loading = ref(false);
const writing = ref(false);
const exporting = ref(false);
const error = ref("");
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
  queued: "等待执行", running: "正在解读与补查", completed: "分析已完成",
  needs_review: "分析有待核问题", blocked: "已停止，需处理原因", failed: "本次未完成",
};
const sourceLabels: Record<string, string> = {
  clinicaltrials: "ClinicalTrials 临床登记", europe_pmc: "Europe PMC 文献",
  legacy_documents: "Fabric 已采用库存", fda_crl: "FDA 审评信函",
  who_don: "WHO 通报", external_archive: "已导入原始档案",
};
const queryLabels: Record<string, string> = {
  prepared: "尚待读取", read: "已读取", empty: "本次未查到", failed: "读取失败",
};
const unavailableLabels: Record<string, string> = {
  disabled: "语义分析尚未启用。", not_configured: "尚未配置可调用模型。",
  semantic_model_disabled: "语义分析尚未启用。",
  semantic_model_not_configured: "尚未配置可调用模型。",
};
const canStart = computed(() =>
  list.value?.enabled && list.value.configured && props.investigation.status === "open"
  && !props.busy && !props.loading && !writing.value
  && !list.value.items.some((item) => ["queued", "running"].includes(item.state)),
);
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
  writeIntent = null; activeAnalysisId = ""; followLatest = true;
}
function fail(value: unknown) {
  const message = value instanceof Error ? value.message : "机器分析暂时无法读取。";
  if (value instanceof EngineError && [401, 403].includes(value.status)) {
    clearPrivate();
    emit("unauthorized", value);
  }
  error.value = message;
}
function schedulePoll() {
  stopPolling();
  if (list.value?.items.some((item) => ["queued", "running"].includes(item.state))) {
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
    const keepId = items.items.length
      ? preferId || (followLatest ? items.items[0]?.id : activeAnalysisId) || items.items[0]?.id
      : undefined;
    list.value = items;
    if (!items.items.length) {
      ++detailVersion; activeAnalysisId = ""; selected.value = null; closeSource();
    }
    error.value = "";
    if (keepId) {
      await openAnalysis(keepId);
      if (!current(generation, id) || version !== listVersion) return;
      if (items.items.some((item) => running.has(item.id) && !["queued", "running"].includes(item.state))) emit("changed");
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
async function startAnalysis(retryId?: string) {
  if (!canStart.value) return;
  const generation = epoch, id = props.investigation.id;
  const parent = list.value?.items.find((item) => item.result_summary
    && ["completed", "needs_review"].includes(item.state));
  const body = JSON.stringify({
    expected_revision: props.investigation.revision,
    parent_analysis_id: parent?.id ?? null,
    material_entry_ids: [],
    retry_of_analysis_id: retryId ?? null,
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
    await refresh(result.id);
    if (current(generation, id)) emit("changed");
  } catch (value) { if (current(generation, id)) fail(value); }
  finally { if (current(generation, id)) writing.value = false; }
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
    const result = await engineRequest<EvidenceRead>(
      endpoint(id) + "/" + encodeURIComponent(view.id) + "/evidence/" + encodeURIComponent(key),
    );
    if (!current(generation, id) || version !== sourceVersion || selected.value?.id !== view.id) return;
    source.value = result;
  } catch (value) {
    if (current(generation, id) && version === sourceVersion) {
      sourceError.value = value instanceof Error ? value.message : "原文暂不可读取。";
      if (value instanceof EngineError && [401, 403].includes(value.status)) fail(value);
    }
  }
}
async function exportAnalysis() {
  const view = selected.value;
  if (!view || exporting.value) return;
  const generation = epoch, id = props.investigation.id;
  exporting.value = true;
  try {
    const result = await engineRequest<Record<string, unknown>>(endpoint(id) + "/" + encodeURIComponent(view.id) + "/export");
    if (!current(generation, id) || selected.value?.id !== view.id) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "机器分析-" + view.id + ".json";
    link.click(); URL.revokeObjectURL(url);
  } catch (value) { if (current(generation, id)) fail(value); }
  finally { if (current(generation, id)) exporting.value = false; }
}
watch(() => props.investigation.id, () => {
  clearPrivate(); error.value = ""; void refresh();
}, { immediate: true });
watch(() => props.investigation.revision, () => { if (!writing.value) void refresh(); });
onUnmounted(() => { disposed = true; clearPrivate(); });
</script>

<template>
  <section class="engine-panel machine-analysis-panel">
    <div class="analysis-heading">
      <div><h2>机器分析与调查过程</h2><p class="muted">读材料、查缺口、比较解释，并说明为什么维持或修改判断。</p></div>
      <div class="no-print">
        <ElButton :disabled="writing || loading" @click="refresh()">刷新分析</ElButton>
        <ElButton type="primary" :loading="writing" :disabled="!canStart" @click="startAnalysis()">
          {{ list?.items.length ? "根据当前材料更新分析" : "开展材料分析" }}
        </ElButton>
      </div>
    </div>
    <p v-if="error" class="error-panel no-print" role="alert">{{ error }}</p>
    <p v-if="list && (!list.enabled || !list.configured)" class="notice">
      {{ unavailableLabels[list.unavailable_reason || ""] || "语义分析当前不可用；已有材料与研究记录仍可阅读。" }}
    </p>
    <p v-if="investigation.status !== 'open'" class="notice">调查已暂停或停止，后台不会继续新增分析动作。</p>
    <p v-if="list?.unavailable_count" class="notice">部分历史分析的来源当前不可读取，因此未显示。</p>
    <p v-if="list" class="muted">
      本次可补查：{{ list.retrieval_sources.map((item) => sourceLabels[item] || item).join("、") || "尚无可用来源" }}。
      仅查询 Fabric 已采用版本；不表示全网资料已经查全。
    </p>
    <div v-if="list?.items.length" class="analysis-history no-print">
      <button v-for="item in list.items" :key="item.id" class="history-item"
        :class="{ active: selected?.id === item.id }" :disabled="writing"
        @click="openAnalysis(item.id, true)">
        <span>{{ item.created_at }}<small>{{ stateLabels[item.state] }} · 基于调查第 {{ item.based_on_revision }} 版</small></span>
      </button>
      <ElButton v-if="list.next_cursor" text :disabled="loading" @click="loadMore">读取更早分析</ElButton>
    </div>
    <template v-if="selected">
      <div class="candidate-meta">
        <ElTag>{{ stateLabels[selected.state] }}</ElTag>
        <span>{{ selected.is_demo ? "教学材料" : "真实资料" }} · 尚未专业审核 · {{ selected.evidence_count }} 项采用资料</span>
        <ElButton text class="no-print" :loading="exporting" @click="exportAnalysis">下载本版记录</ElButton>
      </div>
      <p class="investigation-prose"><strong>本版研究问题：</strong>{{ selected.question }}</p>
      <p v-if="['queued', 'running'].includes(selected.state)" class="notice" role="status">
        <template v-if="selected.state === 'queued'">
          已排队，等待后台执行。暂停、功能停用或额度用完时，需相应条件恢复后才继续。
        </template>
        <template v-else>后台正在处理。关闭页面后任务仍会继续；页面会自动读取进度。</template>
      </p>
      <div v-if="['blocked', 'failed'].includes(selected.state)" class="notice">
        <p>本次分析未完成。先核对来源许可、调查状态和执行记录；旧结果仍保留。</p>
        <ElButton class="no-print" :disabled="!canStart" @click="startAnalysis(selected.id)">另起一次尝试</ElButton>
        <p class="muted">这会发起新的模型调用，仍受次数限制；不会覆盖旧尝试。</p>
      </div>
      <MachineAnalysisResult :analysis="selected" @source="readSource" />
      <details v-if="selected.initial_draft" :open="!selected.result" class="initial-draft">
        <summary>修订前的草稿与发现的问题</summary>
        <p class="notice">保留第一次复核指出的问题，方便核对修订过程。这份初稿不代表最终判断。</p>
        <MachineAnalysisResult :analysis="{ ...selected, result: selected.initial_draft }" @source="readSource" />
      </details>
      <h3>实际补查了什么</h3>
      <article v-for="query in selected.queries" :key="query.index" class="investigation-entry">
        <p><strong>{{ sourceLabels[query.plan.source_id] || query.plan.source_id }}：</strong>{{ query.plan.terms.join(" + ") }}</p>
        <p>{{ query.plan.reason }}</p>
        <p>用于区分：{{ query.plan.expected_discrimination }}</p>
        <p class="muted">
          {{ queryLabels[query.status] }}
          <template v-if="['read', 'empty'].includes(query.status)">
            · 返回 {{ query.returned }} 条，本次新增 {{ query.new_versions }} 个版本
            <span v-if="query.has_more"> · 仍有更多，受本轮读取范围限制</span>
          </template>
          <template v-else> · 尚未取得有效结果，不能判断命中数量</template>
        </p>
        <div class="no-print">
          <ElButton v-for="key in query.evidence_keys" :key="key" text @click="readSource(key)">阅读 {{ key }}</ElButton>
        </div>
        <details v-if="query.error_code"><summary>读取未完成的原因</summary>{{ query.error_code }}</details>
      </article>
      <p v-if="!selected.queries.length" class="muted">本版尚无实际追加查找记录；不能把提出问题当作已经查过。</p>
      <ul v-if="selected.limitations.length" class="muted">
        <li v-for="item in selected.limitations" :key="item">{{ item }}</li>
      </ul>
      <details class="no-print">
        <summary>执行与版本记录</summary>
        <p>分析版本：{{ selected.version }}；提示版本：{{ selected.prompt_version }}</p>
        <p>固定补查来源：{{ selected.search_scope.map((item) => sourceLabels[item] || item).join("、") }}</p>
        <p v-if="selected.error_code">未完成代码：{{ selected.error_code }}</p>
        <pre class="source-text">{{ JSON.stringify(selected.calls, null, 2) }}</pre>
      </details>
    </template>
    <p v-else-if="loading" class="muted">正在读取分析记录…</p>
    <p v-else class="muted">还没有机器分析。先采用与问题有关的材料，再开展分析；原有人工判断不受影响。</p>
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
.analysis-history { display: flex; flex-wrap: wrap; gap: .5rem; margin: 1rem 0; }
.analysis-history .history-item { max-width: 100%; width: auto; }
</style>
