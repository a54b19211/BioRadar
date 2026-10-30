<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  ElButton,
  ElDrawer,
} from "element-plus";
import EngineLogin from "../components/engine/EngineLogin.vue";
import GraphInvestigationForm from "../components/engine/GraphInvestigationForm.vue";
import InvestigationRecord from "../components/engine/InvestigationRecord.vue";
import MachineAnalysisPanel from "../components/engine/MachineAnalysisPanel.vue";
import InvestigationEntryForm from "../components/engine/InvestigationEntryForm.vue";
import ResearchQuestionForm from "../components/engine/ResearchQuestionForm.vue";
import type {
  ResearchAskReceipt,
  ResearchAnalysisView,
  ResearchQuestionSubmission,
} from "../api/research";
import {
  engineRequest,
  EngineError,
  post,
  type InvestigationView,
  type InvestigationSummary,
  type InvestigationEntryCreate,
  type SnapshotSummary,
  type NormalizedSnapshot,
  type EvidenceSupport,
  type GraphInvestigationResult,
} from "../api/engine";

const route = useRoute();
const router = useRouter();
const reportOnly = computed(() => route.path === "/deliverables");
const preferredAnalysisId = computed(() =>
  typeof route.query.analysis === "string" ? route.query.analysis : undefined,
);
const authenticated = ref(false);
const enabled = ref(true);
const loading = ref(false);
const busy = ref(false);
const loggingOut = ref(false);
const error = ref("");
const sourceAccessLimited = ref(false);
const message = ref("");
const investigations = ref<InvestigationSummary[]>([]);
const selected = ref<InvestigationView | null>(null);
const snapshots = ref<SnapshotSummary[]>([]);
const sourceCursor = ref<string | null>(null);
const historyCursor = ref<string | null>(null);
const showCreate = ref(true);
const showGraph = ref(false);
const graphQuestion = ref("");
const graphForNew = ref(false);
const graphTarget = computed(() => (graphForNew.value ? null : selected.value));
const sourceOpen = ref(false);
const sourceRecord = ref<NormalizedSnapshot["records"][number] | null>(null);
const sourceError = ref("");
let disposed = false;
let epoch = 0;
let refreshVersion = 0;
let readVersion = 0;
let sourceVersion = 0;
let activeId = "";
let askIntent: { body: string; key: string } | null = null;
let graphAnalysisIntent: { id: string; revision: number; key: string } | null =
  null;
const statusLabels = {
  open: "继续跟进",
  paused: "暂停跟进",
  closed: "已停止跟进",
};

function current(generation: number) {
  return !disposed && epoch === generation;
}
function fail(value: unknown) {
  error.value =
    value instanceof Error ? value.message : "调查记录暂时无法读取或保存。";
  if (value instanceof EngineError && value.status === 401) {
    ++epoch;
    ++refreshVersion;
    ++readVersion;
    authenticated.value = false;
    showGraph.value = false;
    selected.value = null;
    investigations.value = [];
    snapshots.value = [];
    loading.value = false;
    busy.value = false;

    closeSource();
  } else if (value instanceof EngineError && value.status === 403) {
    ++readVersion;
    selected.value = null;
    closeSource();
  }
}
function closeSource() {
  ++sourceVersion;
  sourceOpen.value = false;
  sourceRecord.value = null;
  sourceError.value = "";
}
async function refresh() {
  if (loggingOut.value) return;
  const generation = epoch;
  const version = ++refreshVersion;
  const valid = () => current(generation) && version === refreshVersion;
  loading.value = true;
  error.value = "";
  try {
    const session = await engineRequest<{
      enabled: boolean;
      authenticated: boolean;
    }>("/session");
    if (!valid()) return;
    enabled.value = session.enabled;
    authenticated.value = session.authenticated;
    if (!session.authenticated) {
      selected.value = null;
      closeSource();
      return;
    }
    const [history, sources] = await Promise.all([
      engineRequest<{
        items: InvestigationSummary[];
        unavailable_count?: number;
        next_cursor?: string | null;
      }>("/investigations"),
      engineRequest<{
        items: SnapshotSummary[];
        unavailable_count?: number;
        next_cursor?: string | null;
      }>("/snapshots?limit=100"),
    ]);
    if (!valid()) return;
    sourceAccessLimited.value = Boolean(
      history.unavailable_count || sources.unavailable_count,
    );
    investigations.value = history.items;
    historyCursor.value = history.next_cursor ?? null;
    snapshots.value = sources.items;
    sourceCursor.value = sources.next_cursor ?? null;
    const id =
      typeof route.query.id === "string"
        ? route.query.id
        : activeId || history.items[0]?.id;
    if (id) await openInvestigation(id);
  } catch (value) {
    if (valid()) fail(value);
  } finally {
    if (valid()) loading.value = false;
  }
}
async function openInvestigation(id: string) {
  const generation = epoch;
  const version = ++readVersion;
  const changed = activeId !== id;
  activeId = id;
  error.value = "";
  message.value = "";
  closeSource();
  if (changed) {
    selected.value = null;
  }
  if (route.query.id !== id)
    void router.replace({
      path: reportOnly.value ? "/deliverables" : "/investigations",
      query: { id },
    });
  try {
    const detail = await engineRequest<InvestigationView>(
      "/investigations/" + encodeURIComponent(id),
    );
    if (!current(generation) || activeId !== id || version !== readVersion)
      return;
    if (selected.value?.id === id && selected.value.revision > detail.revision)
      return;
    if (
      selected.value?.id === id &&
      selected.value.revision !== detail.revision
    )
      message.value =
        "调查内容已更新，未保存的输入已清空，请按当前材料重新填写。";
    selected.value = detail;
  } catch (value) {
    if (current(generation) && activeId === id && version === readVersion) {
      selected.value = null;
      fail(value);
    }
  }
}
function selectAnalysis(id: string) {
  if (!selected.value || route.query.analysis === id) return;
  void router.replace({
    path: reportOnly.value ? "/deliverables" : "/investigations",
    query: { id: selected.value.id, analysis: id },
  });
}
async function createInvestigation(submission: ResearchQuestionSubmission) {
  if (busy.value || loading.value || !submission.question.trim()) return;
  const generation = epoch;
  const body = JSON.stringify({
    title: submission.question.slice(0, 160),
    question: submission.question,
    is_demo: false,
    source_ids: submission.source_ids,
    ...(submission.query_policy ? { query_policy: submission.query_policy } : {}),
    ...(submission.budget ? { budget: submission.budget } : {}),
  });
  if (!askIntent || askIntent.body !== body)
    askIntent = { body, key: crypto.randomUUID() };
  busy.value = true;
  error.value = "";
  message.value = "";
  try {
    const receipt = await engineRequest<ResearchAskReceipt>(
      "/investigations/ask",
      {
        method: "POST",
        body: askIntent.body,
        headers: { "Idempotency-Key": askIntent.key },
      },
    );
    if (!current(generation)) return;
    askIntent = null;
    showCreate.value = false;
    await openInvestigation(receipt.investigation_id);
    if (!current(generation)) return;
    selectAnalysis(receipt.analysis_id);
    await refresh();
    if (current(generation))
      message.value = "问题已保存，后台已受理研究。可在下方查看实际进度。";
  } catch (value) {
    if (!current(generation)) return;
    const retainedId =
      value instanceof EngineError ? value.details?.investigation_id : null;
    if (typeof retainedId === "string") {
      showCreate.value = false;
      await openInvestigation(retainedId);
      if (!current(generation)) return;
      await refresh();
      if (!current(generation)) return;
      message.value = "调查已保留，可在这项记录中继续处理。";
    }
    fail(value);
  } finally {
    if (current(generation)) busy.value = false;
  }
}
function openGraph(question = selected.value?.question || "", forNew = false) {
  graphQuestion.value = question;
  graphForNew.value = forNew;
  showGraph.value = true;
}
async function graphSaved(
  investigation: InvestigationView,
  result: GraphInvestigationResult,
) {
  if (disposed) return;
  const generation = epoch;
  selected.value = investigation;
  activeId = investigation.id;
  await openInvestigation(investigation.id);
  if (!current(generation)) return;
  await refresh();
  if (!current(generation)) return;
  if (result.completion_status === "awaiting_seed_selection") {
    message.value = "调查已保存，请在图谱候选中明确选择对象后继续。";
    return;
  }
  const view = selected.value;
  if (!view || view.id !== investigation.id || view.status !== "open") return;
  if (
    !graphAnalysisIntent ||
    graphAnalysisIntent.id !== view.id ||
    graphAnalysisIntent.revision !== view.revision
  ) {
    graphAnalysisIntent = {
      id: view.id,
      revision: view.revision,
      key: crypto.randomUUID(),
    };
  }
  busy.value = true;
  try {
    const analysis = await engineRequest<ResearchAnalysisView>(
      "/investigations/" + encodeURIComponent(view.id) + "/analyses",
      {
        method: "POST",
        headers: { "Idempotency-Key": graphAnalysisIntent.key },
        body: JSON.stringify({
          expected_revision: view.revision,
          question: graphQuestion.value || view.question,
          auto_retrieve: true,
        }),
      },
    );
    if (!current(generation) || selected.value?.id !== view.id) return;
    graphAnalysisIntent = null;
    selectAnalysis(analysis.id);
    showGraph.value = false;
    message.value = "图谱背景和查证记录已保存，研究已在同一调查中开始。";
    await openInvestigation(view.id);
  } catch (value) {
    if (current(generation)) fail(value);
  } finally {
    if (current(generation)) busy.value = false;
  }
}
async function loadMoreSources() {
  if (!sourceCursor.value || busy.value || loading.value) return;
  const generation = epoch;
  loading.value = true;
  try {
    const page = await engineRequest<{
      items: SnapshotSummary[];
      unavailable_count?: number;
      next_cursor?: string | null;
    }>("/snapshots?limit=100&cursor=" + encodeURIComponent(sourceCursor.value));
    if (!current(generation)) return;
    const known = new Set(snapshots.value.map((item) => item.id));
    snapshots.value.push(...page.items.filter((item) => !known.has(item.id)));
    sourceAccessLimited.value ||= Boolean(page.unavailable_count);
    sourceCursor.value = page.next_cursor ?? null;
  } catch (value) {
    if (current(generation)) fail(value);
  } finally {
    if (current(generation)) loading.value = false;
  }
}
async function loadMoreInvestigations() {
  if (!historyCursor.value || loading.value || busy.value) return;
  const generation = epoch;
  loading.value = true;
  try {
    const page = await engineRequest<{
      items: InvestigationSummary[];
      unavailable_count?: number;
      next_cursor?: string | null;
    }>("/investigations?cursor=" + encodeURIComponent(historyCursor.value));
    if (!current(generation)) return;
    const known = new Set(investigations.value.map((item) => item.id));
    investigations.value.push(
      ...page.items.filter((item) => !known.has(item.id)),
    );
    sourceAccessLimited.value ||= Boolean(page.unavailable_count);
    historyCursor.value = page.next_cursor ?? null;
  } catch (value) {
    if (current(generation)) fail(value);
  } finally {
    if (current(generation)) loading.value = false;
  }
}
async function appendEntry(body: InvestigationEntryCreate) {
  const investigation = selected.value;
  if (!investigation || busy.value || loading.value) return false;
  const generation = epoch;
  const id = investigation.id;
  busy.value = true;
  error.value = "";
  message.value = "";
  try {
    const saved = await post<InvestigationView>(
      "/investigations/" + encodeURIComponent(id) + "/entries",
      body,
    );
    if (!current(generation) || activeId !== id) return false;
    if (!selected.value || selected.value.revision <= saved.revision)
      selected.value = saved;
    const row = investigations.value.find((item) => item.id === saved.id);
    if (row && row.revision <= saved.revision) {
      row.status = saved.status;
      row.updated_at = saved.updated_at;
      row.revision = saved.revision;
      row.needs_review = saved.needs_review;
    }
    message.value = "记录已保存；原材料与历次判断保留。";
    return true;
  } catch (value) {
    if (current(generation) && activeId === id) fail(value);
    return false;
  } finally {
    if (current(generation)) busy.value = false;
  }
}
async function changeStatus(status: "open" | "paused" | "closed") {
  if (!selected.value) return;
  await appendEntry({
    expected_revision: selected.value.revision,
    kind: "status",
    status,
    text: "研究人员记录：" + statusLabels[status],
  });
}
async function changeQuestion(
  questionId: string,
  status: "pending" | "checking" | "resolved" | "blocked",
) {
  if (
    !selected.value ||
    selected.value.status === "closed" ||
    busy.value ||
    loading.value
  )
    return;
  await appendEntry({
    expected_revision: selected.value.revision,
    kind: "status",
    status: selected.value.status,
    text: "研究人员更新待查问题进度",
    question_updates: [{ question_id: questionId, status }],
  });
}
async function readSupport(
  snapshotId: string,
  snapshotHash: string,
  support: EvidenceSupport,
) {
  const generation = epoch;
  const id = activeId;
  const version = ++sourceVersion;
  sourceRecord.value = null;
  sourceError.value = "";
  sourceOpen.value = true;
  try {
    const data = await engineRequest<{
      snapshot_hash: string;
      snapshot: NormalizedSnapshot;
    }>("/snapshots/" + encodeURIComponent(snapshotId));
    if (!current(generation) || id !== activeId || sourceVersion !== version)
      return;
    const fields = [
      "provider",
      "object_type",
      "object_id",
      "adopted_version",
      "version_source",
      "content_hash",
      "authorization_ref",
    ] as const;
    const record = data.snapshot.records.find((item) =>
      fields.every(
        (field) => item.record_ref[field] === support.record_ref[field],
      ),
    );
    if (data.snapshot_hash !== snapshotHash || !record)
      throw new Error("引用与固定资料版本不匹配，不能显示正文。");
    sourceRecord.value = record;
  } catch (value) {
    if (current(generation) && sourceVersion === version)
      sourceError.value =
        value instanceof Error ? value.message : "读取固定资料失败。";
  }
}
async function printRecord() {
  if (!selected.value || busy.value || loading.value) return;
  const generation = epoch;
  const id = selected.value.id;
  const selectionVersion = readVersion;
  busy.value = true;
  error.value = "";
  try {
    const detail = await engineRequest<InvestigationView>(
      "/investigations/" + encodeURIComponent(id),
    );
    if (
      !current(generation) ||
      activeId !== id ||
      readVersion !== selectionVersion ||
      (selected.value && selected.value.revision > detail.revision)
    )
      return;
    selected.value = detail;
    closeSource();
    await nextTick();
    if (
      current(generation) &&
      activeId === id &&
      readVersion === selectionVersion
    )
      window.print();
  } catch (value) {
    if (
      current(generation) &&
      activeId === id &&
      readVersion === selectionVersion
    ) {
      selected.value = null;
      fail(value);
    }
  } finally {
    if (current(generation)) busy.value = false;
  }
}
async function logout() {
  if (loggingOut.value) return;
  const generation = ++epoch;
  ++refreshVersion;
  ++readVersion;
  loggingOut.value = true;
  loading.value = true;
  busy.value = false;
  authenticated.value = false;
  selected.value = null;
  investigations.value = [];
  snapshots.value = [];
  sourceCursor.value = null;
  historyCursor.value = null;
  activeId = "";
  closeSource();

  askIntent = null;
  graphAnalysisIntent = null;
  graphQuestion.value = "";
  showCreate.value = true;
  try {
    await engineRequest("/session", { method: "DELETE" });
  } catch (value) {
    if (current(generation)) fail(value);
  } finally {
    if (current(generation)) {
      loggingOut.value = false;
      loading.value = false;
    }
  }
}
watch(
  () => route.query.id,
  (id) => {
    if (authenticated.value && typeof id === "string" && id !== activeId)
      void openInvestigation(id);
  },
);
onMounted(refresh);
onUnmounted(() => {
  disposed = true;
  ++epoch;
  ++refreshVersion;
  ++readVersion;
  closeSource();
});
</script>

<template>
  <div class="investigation-page">
    <div class="page-heading no-print">
      <div>
        <p class="eyebrow">INVESTIGATION RECORDS</p>
        <h1>{{ reportOnly ? "研究报告" : "从一个问题开始调查" }}</h1>
        <p class="description">
          {{
            reportOnly
              ? "阅读保存的报告、核验结果和原文依据，按版本追溯变化。"
              : "提出问题，选择来源，查看实际研究进度与证据，再继续追问。"
          }}
        </p>
      </div>
      <div class="heading-actions">
        <ElButton :loading="loading" :disabled="busy" @click="refresh"
          >刷新</ElButton
        >
        <ElButton
          v-if="authenticated && !reportOnly"
          :disabled="loading || busy"
          @click="showCreate = !showCreate"
          >直接提出问题</ElButton
        >
        <ElButton v-if="authenticated" text @click="logout">退出空间</ElButton>
      </div>
    </div>
    <p v-if="error" class="error-panel no-print" role="alert">{{ error }}</p>
    <p v-if="sourceAccessLimited" class="notice no-print" role="status">
      本页部分来源当前不可读取，相关调查或材料未显示；普通资料仍可使用，当前列表不是全部研究记录。
    </p>
    <p v-if="message" class="success-inline no-print" role="status">
      {{ message }}
    </p>
    <p v-if="!enabled" class="notice no-print">
      本地研究用户尚未配置，请按开发说明完成启动。
    </p>
    <EngineLogin
      v-else-if="!authenticated && !loading"
      @authenticated="refresh"
    />
    <template v-if="authenticated">
      <section
        v-if="!reportOnly && showCreate"
        class="engine-panel investigation-form no-print"
      >
        <ResearchQuestionForm
          :disabled="busy || loading"
          :submitting="busy"
          allow-graph
          @submit="createInvestigation"
          @graph="openGraph($event, true)"
          @unauthorized="fail"
        />
      </section>
      <ElButton
        v-if="!reportOnly && selected"
        class="no-print"
        :disabled="busy || loading"
        @click="openGraph()"
        >用图谱补充当前调查</ElButton
      >
      <GraphInvestigationForm
        v-if="!reportOnly && showGraph"
        :investigation="graphTarget"
        :initial-question="graphQuestion"
        :loading="loading"
        @saved="graphSaved"
        @busy="busy = $event"
        @unauthorized="fail"
      />
      <div class="investigation-layout">
        <aside class="engine-panel investigation-list no-print">
          <h2>{{ reportOnly ? "按调查查看报告" : "已保存调查" }}</h2>
          <button
            v-for="item in investigations"
            :key="item.id"
            class="history-item"
            :disabled="busy || loading"
            :class="{ active: item.id === selected?.id }"
            @click="openInvestigation(item.id)"
          >
            <span
              >{{ item.title
              }}<small
                >{{ item.is_demo ? "教学演示 · " : ""
                }}{{ statusLabels[item.status]
                }}{{ item.needs_review ? " · 新材料待复查" : "" }}</small
              ></span
            >
          </button>
          <p v-if="!investigations.length" class="muted">
            尚无调查记录。可以从雷达候选保存，也可以直接提出问题。
          </p>
          <ElButton
            v-if="historyCursor"
            text
            :disabled="loading || busy"
            @click="loadMoreInvestigations"
            >加载更多调查</ElButton
          >
          <RouterLink to="/radar" class="text-link"
            >回雷达查看线索 →</RouterLink
          >
        </aside>
        <div v-if="selected" class="investigation-detail">
          <template v-if="reportOnly">
            <section class="engine-panel">
              <h2 class="investigation-title">{{ selected.title }}</h2>
              <p v-if="selected.title.trim() !== selected.question.trim()" class="investigation-question">{{ selected.question }}</p>
            </section>
            <MachineAnalysisPanel
              :investigation="selected"
              :busy="busy"
              :loading="loading"
              read-only
              :preferred-analysis-id="preferredAnalysisId"
              @selected="selectAnalysis"
              @changed="openInvestigation(selected.id)"
              @unauthorized="fail"
            />
          </template>
          <InvestigationRecord
            v-else
            :investigation="selected"
            :busy="busy"
            :loading="loading"
            @print="printRecord"
            @status="changeStatus"
            @question="changeQuestion"
            @read-source="readSupport"
          >
            <template #machine-analysis>
              <MachineAnalysisPanel
                :investigation="selected"
                :busy="busy"
                :loading="loading"
                :preferred-analysis-id="preferredAnalysisId"
                @selected="selectAnalysis"
                @changed="openInvestigation(selected.id)"
                @unauthorized="fail"
              />
            </template>
          </InvestigationRecord>
          <InvestigationEntryForm
            v-if="!reportOnly"
            :key="selected.id + ':' + selected.revision"
            :investigation="selected"
            :snapshots="snapshots"
            :busy="busy"
            :loading="loading"
            :source-cursor="sourceCursor"
            :save="appendEntry"
            @load-sources="loadMoreSources"
            @read-error="fail"
          />
        </div>
        <div v-else class="engine-empty no-print">
          <h3>
            {{
              loading
                ? "正在读取调查记录"
                : reportOnly
                  ? "选择一项调查阅读报告"
                  : "从一个具体问题开始"
            }}
          </h3>
          <p>
            {{
              reportOnly
                ? "报告与调查共用同一份保存记录，选择左侧调查后可查看各版结果。"
                : "在上方提出问题，或选择已保存调查继续研究。"
            }}
          </p>
        </div>
      </div>
    </template>
    <ElDrawer
      :model-value="sourceOpen"
      title="固定采用资料"
      size="min(820px, 96vw)"
      @close="closeSource"
    >
      <p v-if="sourceError" class="error-inline" role="alert">
        {{ sourceError }}
      </p>
      <template v-else-if="sourceRecord">
        <p class="source-id">
          {{ sourceRecord.record_ref.provider }} /
          {{ sourceRecord.record_ref.object_id }}
        </p>
        <p class="muted">
          引用匹配只说明版本与原文对应，不代表专业支持性已核验。
        </p>
        <dl class="metric-list">
          <dt>原始日期字段</dt>
          <dd>
            {{ sourceRecord.published_time_raw ?? "未提供" }} ·
            {{ sourceRecord.time_precision }} ·
            {{
              sourceRecord.publication_time_verified
                ? "上游公开时间语义已确认"
                : "含义未确认，不作公开时间"
            }}
          </dd>
          <dt>本次观察时间</dt>
          <dd>{{ sourceRecord.observed_at }}</dd>
          <dt>上游可获取时间</dt>
          <dd>{{ sourceRecord.available_at ?? "未提供" }}</dd>
          <dt>研究独立性</dt>
          <dd>
            {{
              sourceRecord.study_independence_status === "confirmed"
                ? "上游给出了确认研究簇"
                : "未确认"
            }}
          </dd>
          <dt>采用版本</dt>
          <dd>{{ sourceRecord.record_ref.adopted_version }}</dd>
        </dl>
        <p
          v-if="
            sourceRecord.record_flags.includes('retracted') ||
            sourceRecord.record_flags.includes('deleted')
          "
          class="notice"
        >
          采用记录带有撤回或删除标记。它可以用于核对历史变化，不能当作仍有效的专业支持。
        </p>
        <pre v-if="sourceRecord.normalized_text" class="source-text">{{
          sourceRecord.normalized_text
        }}</pre>
        <p v-else class="muted">
          没有可读正文；以下为本次固定采用的结构化记录。
        </p>
        <details>
          <summary>查看固定字段与来源版本</summary>
          <pre class="source-text">{{
            JSON.stringify(sourceRecord, null, 2)
          }}</pre>
        </details>
      </template>
      <p v-else class="muted">正在读取固定资料…</p>
    </ElDrawer>
  </div>
</template>
