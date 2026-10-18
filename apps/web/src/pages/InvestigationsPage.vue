<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  ElButton,
  ElCheckbox,
  ElDrawer,
  ElInput,
  ElOption,
  ElSelect,
  ElTag,
} from "element-plus";
import EngineLogin from "../components/engine/EngineLogin.vue";
import GraphInvestigationForm from "../components/engine/GraphInvestigationForm.vue";
import InvestigationRecord from "../components/engine/InvestigationRecord.vue";
import InvestigationEntryForm from "../components/engine/InvestigationEntryForm.vue";
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
} from "../api/engine";

const route = useRoute();
const router = useRouter();
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
const showCreate = ref(false);
const showGraph = ref(false);
const title = ref("");
const question = ref("");
const questionLines = ref("");
const demo = ref(false);
const sourceOpen = ref(false);
const sourceRecord = ref<NormalizedSnapshot["records"][number] | null>(null);
const sourceError = ref("");
let disposed = false;
let epoch = 0;
let refreshVersion = 0;
let readVersion = 0;
let sourceVersion = 0;
let activeId = "";
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
      engineRequest<{ items: SnapshotSummary[]; unavailable_count?: number; next_cursor?: string | null }>(
        "/snapshots?limit=100",
      ),
    ]);
    if (!valid()) return;
    sourceAccessLimited.value = Boolean(history.unavailable_count || sources.unavailable_count);
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
    void router.replace({ path: "/investigations", query: { id } });
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
async function createInvestigation() {
  if (
    busy.value ||
    loading.value ||
    !title.value.trim() ||
    !question.value.trim()
  )
    return;
  const generation = epoch;
  busy.value = true;
  error.value = "";
  try {
    const created = await post<InvestigationView>("/investigations", {
      title: title.value.trim(),
      question: question.value.trim(),
      questions: questionLines.value
        .split("\n")
        .map((item) => item.trim())
        .filter(Boolean),
      origin: null,
      is_demo: demo.value,
    });
    if (!current(generation)) return;
    showCreate.value = false;
    title.value = "";
    question.value = "";
    questionLines.value = "";
    demo.value = false;
    await openInvestigation(created.id);
    if (current(generation)) await refresh();
  } catch (value) {
    if (current(generation)) fail(value);
  } finally {
    if (current(generation)) busy.value = false;
  }
}
async function graphSaved(investigation: InvestigationView) {
  if (disposed) return;
  selected.value = investigation;
  activeId = investigation.id;
  message.value = "图谱背景材料与查证记录已保存，未自动生成研究人员判断。";
  await openInvestigation(investigation.id);
  if (!disposed) await refresh();
}
async function loadMoreSources() {
  if (!sourceCursor.value || busy.value || loading.value) return;
  const generation = epoch;
  loading.value = true;
  try {
    const page = await engineRequest<{
      items: SnapshotSummary[]; unavailable_count?: number;
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

  title.value = "";
  question.value = "";
  questionLines.value = "";
  showCreate.value = false;
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
        <h1>把线索接着查下去</h1>
        <p class="description">
          保存问题、支持与反面材料，留下研究人员的判断及变化。
        </p>
      </div>
      <div class="heading-actions">
        <ElButton :loading="loading" :disabled="busy" @click="refresh"
          >刷新</ElButton
        >
        <ElButton
          v-if="authenticated"
          :disabled="loading || busy"
          @click="showCreate = !showCreate"
          >直接提出问题</ElButton
        >
        <ElButton v-if="authenticated" text @click="logout">退出空间</ElButton>
      </div>
    </div>
    <p v-if="error" class="error-panel no-print" role="alert">{{ error }}</p>
    <p v-if="sourceAccessLimited" class="notice no-print" role="status">本页部分来源当前不可读取，相关调查或材料未显示；普通资料仍可使用，当前列表不是全部研究记录。</p>
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
      <ElButton class="no-print" :disabled="busy || loading" @click="showGraph = !showGraph">从图谱继续调查</ElButton>
      <GraphInvestigationForm v-if="showGraph" :investigation="selected" :loading="loading"
        @saved="graphSaved" @busy="busy = $event" @unauthorized="fail" />
      <section
        v-if="showCreate"
        class="engine-panel investigation-form no-print"
      >
        <h2>从一个问题开始</h2>
        <label for="new-investigation-title">调查标题</label
        ><ElInput
          id="new-investigation-title"
          v-model="title"
          maxlength="200"
        />
        <label for="new-investigation-question">研究问题与范围</label
        ><ElInput
          id="new-investigation-question"
          v-model="question"
          type="textarea"
          :rows="3"
          maxlength="2000"
          placeholder="要查什么技术、疾病、人群或具体条件？"
        />
        <label for="new-investigation-questions"
          >接着要核对什么（每行一项，可暂空）</label
        ><ElInput
          id="new-investigation-questions"
          v-model="questionLines"
          type="textarea"
          :rows="3"
          maxlength="8000"
        />
        <ElCheckbox v-model="demo">这是教学演示，不作真实情报</ElCheckbox>
        <ElButton
          type="primary"
          :loading="busy"
          :disabled="loading || !title.trim() || !question.trim()"
          @click="createInvestigation"
          >保存调查</ElButton
        >
      </section>
      <div class="investigation-layout">
        <aside class="engine-panel investigation-list no-print">
          <h2>已保存调查</h2>
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
          <InvestigationRecord
            :investigation="selected"
            :busy="busy"
            :loading="loading"
            @print="printRecord"
            @status="changeStatus"
            @question="changeQuestion"
            @read-source="readSupport"
          />
          <InvestigationEntryForm
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
          <h3>{{ loading ? "正在读取调查记录" : "从一个具体问题开始" }}</h3>
          <p>
            选择已保存调查，或从雷达候选保存一项新调查。缺证据时会保留未知。
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
