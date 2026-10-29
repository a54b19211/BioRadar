<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { ElButton, ElCheckbox, ElInput, ElOption, ElSelect, ElTag } from "element-plus";
import {
  EngineError,
  engineRequest,
  post,
  type GraphInvestigationResult,
  type GraphPackageList,
  type GraphPackageSummary,
  type InvestigationView,
} from "../../api/engine";

const props = defineProps<{ investigation: InvestigationView | null; loading: boolean; initialQuestion?: string }>();
const emit = defineEmits<{
  saved: [investigation: InvestigationView, result: GraphInvestigationResult];
  busy: [value: boolean];
  unauthorized: [error: unknown];
}>();
const source = ref("primekg_graph");
const demo = ref(false);
const packages = ref<GraphPackageSummary[]>([]);
const unavailableCount = ref(0);
const packageKey = ref("");
const cursor = ref<string | null>(null);
const asOf = ref<string | null>(null);
const fetching = ref(false);
const running = ref(false);
const error = ref("");
const seedName = ref("");
const seedId = ref("");
const selectedCandidate = ref("");
const topic = ref("");
const question = ref(props.initialQuestion ?? props.investigation?.question ?? "");
const appendCurrent = ref(Boolean(props.investigation));
const searchClinical = ref(true);
const result = ref<GraphInvestigationResult | null>(null);
let generation = 0;
let disposed = false;
const disabled = computed(() => props.loading || fetching.value || running.value);
const selectedPackage = computed(() => packages.value.find((item) => key(item) === packageKey.value));
const candidates = computed(() => result.value?.seed_resolutions?.flatMap((value) => value.candidates ?? []) ?? []);
function key(item: GraphPackageSummary) {
  return JSON.stringify([item.source_record_id, item.version_id]);
}
function current(version: number) {
  return !disposed && version === generation;
}
function explain(value: unknown) {
  return value instanceof Error ? value.message : "本次图谱读取或保存未确认，请先核对已有记录。";
}
async function loadPackages(more = false) {
  if (running.value || fetching.value || (more && !cursor.value)) return;
  const version = ++generation;
  fetching.value = true;
  error.value = "";
  const params = new URLSearchParams({
    source_id: source.value, is_demo: String(demo.value), limit: "20",
  });
  if (more && cursor.value && asOf.value) {
    params.set("cursor", cursor.value);
    params.set("cutoff", asOf.value);
  }
  try {
    const data = await engineRequest<GraphPackageList>("/graph/packages?" + params);
    if (!current(version)) return;
    const known = new Set(more ? packages.value.map(key) : []);
    packages.value = [
      ...(more ? packages.value : []),
      ...data.items.filter((item) => !known.has(key(item))),
    ];
    unavailableCount.value = (more ? unavailableCount.value : 0) + (data.unavailable_packages?.length ?? 0);
    cursor.value = data.next_cursor;
    asOf.value = data.as_of;
    if (!selectedPackage.value && packages.value[0]) packageKey.value = key(packages.value[0]);
  } catch (value) {
    if (!current(version)) return;
    error.value = explain(value);
    if (value instanceof EngineError && (value.status === 401 || value.status === 403)) {
      packages.value = [];
      packageKey.value = "";
      result.value = null;
      emit("unauthorized", value);
    }
  } finally {
    if (current(version)) fetching.value = false;
  }
}
async function submit() {
  const graph = selectedPackage.value;
  if (!graph || disabled.value || !question.value.trim()) return;
  const identity = selectedCandidate.value || seedId.value.trim();
  if (!identity && !seedName.value.trim()) return;
  const version = ++generation;
  running.value = true;
  emit("busy", true);
  error.value = "";
  const pending = result.value?.completion_status === "awaiting_seed_selection"
    ? result.value.investigation : null;
  const target = pending || (appendCurrent.value ? props.investigation : null);
  const body = {
    title: "图谱引导调查：" + (seedName.value.trim() || identity).slice(0, 120),
    question: question.value.trim(),
    source_id: graph.source_id,
    source_record_id: graph.source_record_id,
    graph_version_id: graph.version_id,
    is_demo: graph.is_demo,
    ...(identity ? { seed_ids: [identity] } : { seed_names: [seedName.value.trim()] }),
    topic_terms: topic.value.split(/[,，\n]/).map((value) => value.trim()).filter(Boolean),
    search_clinicaltrials: searchClinical.value,
    ...(target ? { investigation_id: target.id, expected_revision:
      props.investigation?.id === target.id ? props.investigation.revision : target.revision } : {}),
  };
  try {
    const saved = await post<GraphInvestigationResult>("/investigations/graph", body);
    if (!current(version)) return;
    result.value = saved;
    emit("saved", saved.investigation, saved);
  } catch (value) {
    if (!current(version)) return;
    error.value = explain(value) + " 本次没有自动重发。";
    if (value instanceof EngineError && (value.status === 401 || value.status === 403)) {
      result.value = null;
      packages.value = [];
      packageKey.value = "";
      emit("unauthorized", value);
    }
  } finally {
    if (current(version)) {
      running.value = false;
      emit("busy", false);
    }
  }
}
watch(() => props.investigation?.id, (id) => {
  if (result.value && result.value.investigation.id !== id) {
    result.value = null;
    selectedCandidate.value = "";
  }
  if (running.value) {
    ++generation;
    running.value = false;
    emit("busy", false);
    error.value = "当前调查已切换；前次执行结果请在已保存调查中核对，不会自动重发。";
  }
});
watch([source, demo], () => {
  ++generation;
  packages.value = [];
  unavailableCount.value = 0;
  packageKey.value = "";
  cursor.value = null;
  asOf.value = null;
  result.value = null;
  selectedCandidate.value = "";
  fetching.value = false;
});
watch([seedName, packageKey], () => {
  selectedCandidate.value = "";
  result.value = null;
});
watch(() => props.initialQuestion, (value) => { if (value != null && !running.value) question.value = value; });
onMounted(() => void loadPackages());
onUnmounted(() => { disposed = true; ++generation; });
const reviewLabels: Record<string, string> = { approved: "来源人工审核", auto_approved: "来源自动审核", pending: "待审核", unreviewed: "未审核", background: "背景资料" };
const bindingLabels: Record<string, string> = { bound: "已核对原文", unbound: "原文未绑定", background: "背景关联" };
const statusLabels: Record<GraphInvestigationResult["completion_status"], string> = {
  bounded_context_saved: "已保存有限背景查证",
  partial: "部分完成，已保留材料与缺口",
  read_failed: "已保存读取失败记录",
  empty_neighborhood: "本包未取得邻域关系",
  awaiting_seed_selection: "名称未唯一定位，等待明确选择",
};
</script>

<template>
  <section class="engine-panel graph-form no-print">
    <h2>从图谱继续调查</h2>
    <p class="muted">从真实关系提出待查问题，回读原件并保存背景材料。本轮材料会保存到同一调查，再继续研究和报告。</p>
    <div class="graph-fields">
      <label>图谱来源
        <ElSelect v-model="source" :disabled="disabled">
          <ElOption label="PrimeKG 当前背景图" value="primekg_graph" />
          <ElOption label="已发布对象与关系" value="reviewed_graph" />
        </ElSelect>
      </label>
      <label>图谱资料
        <ElSelect v-model="packageKey" :disabled="disabled" placeholder="先读取已获准图谱资料">
          <ElOption v-for="item in packages" :key="key(item)" :value="key(item)"
            :label="item.title + ' · ' + item.version_id.slice(0, 10)" />
        </ElSelect>
      </label>
      <label>名称（在所选版本内查候选）
        <ElInput v-model="seedName" :disabled="disabled" placeholder="例如 EGFR" maxlength="120" />
      </label>
      <label>保留的主题检索词（最多2个，逗号分隔）
        <ElInput v-model="topic" :disabled="disabled" placeholder="例如 EGFR" maxlength="240" />
      </label>
    </div>
    <div class="graph-actions">
      <ElCheckbox v-model="demo" :disabled="disabled">仅教学测试资料</ElCheckbox>
      <ElButton :disabled="disabled" @click="loadPackages(false)">刷新图谱资料</ElButton>
      <ElButton v-if="cursor" :disabled="disabled" @click="loadPackages(true)">继续读取下一页</ElButton>
    </div>
    <p v-if="unavailableCount" class="notice">已读页面中有 {{ unavailableCount }} 个旧版资料暂不能读取，保留原版但不允许选择；其余可用资料可继续读取。</p>
    <p class="muted">列表是当前许可下的一页已发布图谱资料，不表示全图或全专题完整覆盖。图源不可用时仍可使用普通调查功能。</p>
    <p v-if="selectedPackage" class="graph-scope">
      {{ selectedPackage.scope_description }}<br />
      版本：{{ selectedPackage.version_id }}
      <ElTag v-if="selectedPackage.selection_truncated" type="warning">源选择已截断</ElTag>
      <ElTag v-if="selectedPackage.is_demo" type="warning">合成演示</ElTag>
    </p>
    <label>这次想弄清什么
      <ElInput v-model="question" type="textarea" :rows="3" :disabled="disabled" maxlength="4000" />
    </label>
    <div class="graph-actions">
      <ElCheckbox v-model="searchClinical" :disabled="disabled">按实际邻居名称查现存 ClinicalTrials 标题（每问最多3条）</ElCheckbox>
      <ElCheckbox v-if="investigation" v-model="appendCurrent" :disabled="disabled">
        追加到当前调查（保留旧判断）
      </ElCheckbox>
    </div>
    <details>
      <summary>高级：使用来源身份ID</summary>
      <label>明确的 namespace|id（填写后优先使用，不按名称猜测）
        <ElInput v-model="seedId" :disabled="disabled" placeholder="来源返回的 namespace|id" />
      </label>
    </details>
    <p v-if="error" class="error-panel" role="alert">{{ error }}</p>
    <template v-if="result">
      <p role="status">{{ statusLabels[result.completion_status] }}</p>
      <label v-if="candidates.length && result.completion_status === 'awaiting_seed_selection'">
        请核对候选后明确选择
        <ElSelect v-model="selectedCandidate" :disabled="disabled" placeholder="不会自动选择第一项">
          <ElOption v-for="candidate in candidates" :key="candidate.namespace + '|' + candidate.id"
            :value="candidate.namespace + '|' + candidate.id"
            :label="candidate.name + ' · ' + candidate.entity_type + ' · ' + candidate.namespace + '|' + candidate.id
              + ' · ' + (reviewLabels[candidate.review.status] ?? candidate.review.status)" />
        </ElSelect>
      </label>
      <ol v-if="result.followups?.length">
        <li v-for="item in result.followups ?? []" :key="item.edge_id">
          {{ item.question }}<br /><span class="muted">原关系：{{ item.relation_type }}；审核：{{ reviewLabels[item.provider_review_status] ?? item.provider_review_status }}；原文：{{ bindingLabels[item.binding_status] ?? item.binding_status }}</span>
        </li>
      </ol>
      <p class="muted">图谱资料、原文入口、查证范围与未知已保存在调查材料和笔记中，可继续人工核对或打印。</p>
    </template>
    <ElButton type="primary" :loading="running" :disabled="disabled || !selectedPackage || !question.trim()"
      @click="submit">{{ result?.completion_status === 'awaiting_seed_selection' ? "按所选身份继续这项调查" : "按图谱取证并开始研究" }}</ElButton>
  </section>
</template>

<style scoped>
.graph-form { min-width: 0; overflow-wrap: anywhere; }
.graph-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.graph-form label:not(.el-checkbox) { display: grid; gap: 6px; margin: 12px 0; min-width: 0; }
.graph-form :deep(.el-select) { width: 100%; min-width: 0; }
.graph-actions { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 12px 0; }
.graph-actions :deep(.el-checkbox) { max-width: 100%; height: auto; white-space: normal; }
.graph-actions :deep(.el-checkbox__label) { white-space: normal; }
.graph-scope { font-size: 13px; line-height: 1.7; }
.graph-form li { margin: 12px 0; }
@media (max-width: 760px) { .graph-fields { grid-template-columns: minmax(0, 1fr); } }
</style>
