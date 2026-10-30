<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { ElButton, ElInput, ElOption, ElSelect, ElTag } from "element-plus";
import { explainCode } from "../api/engineLabels";
import EngineLogin from "../components/engine/EngineLogin.vue";
import CandidateDetail from "../components/engine/CandidateDetail.vue";
import SnapshotComparison from "../components/engine/SnapshotComparison.vue";
import MonitorPanel from "../components/engine/MonitorPanel.vue";
import KddRunSummary from "../components/engine/KddRunSummary.vue";
import { allCandidates, candidateDecision, candidateEvaluation, kddDirections, kddLabel, methodLabel, makeKddPolicy, text, type KddDirection } from "../api/kdd";
import {
  engineRequest,
  EngineError,
  post,
  type Candidate,
  type FeedbackView,
  type RunView,
  type SnapshotSummary,
} from "../api/engine";
const authenticated = ref(false);
const enabled = ref(true);
const loading = ref(false);
const error = ref("");
const sourceAccessLimited = ref(false);
const snapshots = ref<SnapshotSummary[]>([]);
const runs = ref<RunView[]>([]);
const selectedSnapshot = ref("");
const selectedRun = ref<RunView | null>(null);
const selectedCandidate = ref<Candidate | null>(null);
const filter = ref("");
const candidateScope = ref<"selected" | "all">("selected");
const direction = ref<KddDirection | "legacy">("research");
const methodLabels = ref<Record<string, string>>({});
const busy = ref(false);
const loggingOut = ref(false);
const importFile = ref<HTMLInputElement | null>(null);
const showPolicy = ref(false);
const policyText = ref(
  JSON.stringify(
    {
      as_of: new Date().toISOString(),
      time_mode: "publication_retrospective",
      discovery_mode: "stock_and_incremental",
    },
    null,
    2,
  ),
);
let timer: ReturnType<typeof setTimeout> | undefined;
let disposed = false;
let activeRunId = "";
let sessionEpoch = 0;
let refreshVersion = 0;
function currentSession(epoch: number) {
  return !disposed && epoch === sessionEpoch;
}
const candidates = computed(() =>
  (candidateScope.value === "all" ? allCandidates(selectedRun.value?.result) : selectedRun.value?.result?.candidates ?? []).filter(
    (c) =>
      !filter.value ||
      `${c.title} ${c.method_id}`
        .toLowerCase()
        .includes(filter.value.toLowerCase()),
  ),
);
const methods = computed(() => selectedRun.value?.result?.method_results ?? []);
const generatedCandidates = computed(() => allCandidates(selectedRun.value?.result));
const selectedCandidateCanCreate = computed(() => Boolean(selectedRun.value?.result?.candidates.some(item => item.candidate_key === selectedCandidate.value?.candidate_key)));
function selectionLabel(key: string) {
  const decision = candidateDecision(selectedRun.value?.result, key);
  return decision ? kddLabel(text(decision.decision)) : "逐项筛选理由未记录";
}
const selectedData = computed(() =>
  snapshots.value.find((s) => s.id === selectedSnapshot.value),
);
const activeSnapshot = computed(() =>
  snapshots.value.find((s) => s.id === selectedRun.value?.snapshot_id),
);
const canRetry = computed(
  () =>
    selectedRun.value?.state === "failed" &&
    selectedRun.value.policy_view_status === "current" &&
    ![
      "result_budget_exceeded",
      "method_version_changed",
      "policy_schema_changed",
    ].includes(selectedRun.value.error?.code ?? ""),
);
const runnable = computed(
  () =>
    methods.value.filter(
      (m) =>
        m.execution_status === "success" || m.execution_status === "partial",
    ).length,
);
const kindLabels: Record<string, string> = {
  observed_change: "变化记录",
  association: "关联线索",
  hypothesis: "探索线索",
  data_quality: "数据核查",
};
const readinessLabels: Record<string, string> = {
  ready: "可计算",
  exploratory_only: "仅作探索",
  unsupported: "缺少输入",
};
const stateLabels: Record<string, string> = {
  queued: "等待计算",
  running: "正在分析",
  completed: "分析完成",
  partial: "部分完成",
  failed: "分析失败",
};
function handleError(e: unknown) {
  error.value = e instanceof Error ? e.message : "读取失败";
  if (e instanceof EngineError && e.status === 401) authenticated.value = false;
}
function rememberFeedback(feedback: FeedbackView) {
  const run = selectedRun.value;
  if (!run || run.id !== feedback.run_id) return;
  run.feedback ??= [];
  if (!run.feedback.some((item) => item.id === feedback.id))
    run.feedback.push(feedback);
}
async function refresh() {
  if (loggingOut.value) return;
  const epoch = sessionEpoch;
  const version = ++refreshVersion;
  const current = () => currentSession(epoch) && version === refreshVersion;
  loading.value = true;
  error.value = "";
  try {
    const session = await engineRequest<{
      enabled: boolean;
      authenticated: boolean;
    }>("/session");
    if (!current()) return;
    authenticated.value = session.authenticated;
    enabled.value = session.enabled;
    if (!session.authenticated) return;
    const [data, history, catalog] = await Promise.all([
      engineRequest<{ items: SnapshotSummary[]; unavailable_count?: number }>("/snapshots"),
      engineRequest<{ items: RunView[]; unavailable_count?: number }>("/runs"),
      engineRequest<{ method_id: string; label: string }[]>("/methods"),
    ]);
    if (!current()) return;
    snapshots.value = data.items;
    sourceAccessLimited.value = Boolean(data.unavailable_count || history.unavailable_count);
    if (sourceAccessLimited.value && !data.items.some((item) => item.id === selectedSnapshot.value))
      selectedSnapshot.value = "";
    if (sourceAccessLimited.value && selectedRun.value && !history.items.some((item) => item.id === selectedRun.value?.id)) {
      clearTimeout(timer);
      activeRunId = "";
      selectedRun.value = null;
      selectedCandidate.value = null;
    }
    runs.value = history.items;
    methodLabels.value = Object.fromEntries(
      catalog.map((m) => [m.method_id, m.label]),
    );
    if (!selectedSnapshot.value && data.items[0])
      selectedSnapshot.value = data.items[0].id;
    if (!selectedRun.value && history.items[0])
      await openRun(history.items[0].id);
  } catch (e) {
    if (current()) handleError(e);
  } finally {
    if (current()) loading.value = false;
  }
}
async function openRun(id: string) {
  const epoch = sessionEpoch;
  clearTimeout(timer);
  activeRunId = id;
  selectedCandidate.value = null;
  if (selectedRun.value?.id !== id) selectedRun.value = null;
  try {
    const run = await engineRequest<RunView>(`/runs/${encodeURIComponent(id)}`);
    if (!currentSession(epoch) || activeRunId !== id) return;
    selectedCandidate.value = null;
    selectedRun.value = run;
    const history = runs.value.find((item) => item.id === id);
    if (history) {
      history.state = run.state;
      history.revision = run.revision;
      history.attempt = run.attempt;
      history.completed_at = run.completed_at;
      history.candidate_count = run.candidate_count;
    }
    if (run.state === "queued" || run.state === "running")
      timer = setTimeout(() => openRun(id), 2500);
  } catch (e) {
    if (currentSession(epoch) && activeRunId === id) handleError(e);
  }
}
async function analyze() {
  if (!selectedSnapshot.value || busy.value) return;
  const epoch = sessionEpoch;
  busy.value = true;
  error.value = "";
  try {
    const policy = makeKddPolicy(JSON.parse(policyText.value), direction.value);
    const run = await post<RunView>("/runs", {
      snapshot_id: selectedSnapshot.value,
      policy,
    });
    if (!currentSession(epoch)) return;
    await openRun(run.id);
    if (currentSession(epoch)) await refresh();
  } catch (e) {
    if (currentSession(epoch)) handleError(e);
  } finally {
    if (currentSession(epoch)) busy.value = false;
  }
}
async function retryRun() {
  const run = selectedRun.value;
  if (!run || !canRetry.value || busy.value) return;
  const epoch = sessionEpoch;
  busy.value = true;
  error.value = "";
  try {
    const retried = await post<RunView>(
      `/runs/${encodeURIComponent(run.id)}/actions`,
      {
        action: "retry_failed",
        expected_revision: run.revision,
      },
    );
    if (!currentSession(epoch)) return;
    await openRun(retried.id);
    if (currentSession(epoch)) await refresh();
  } catch (e) {
    if (currentSession(epoch)) handleError(e);
  } finally {
    if (currentSession(epoch)) busy.value = false;
  }
}
async function importSnapshot(event: Event) {
  const target = event.target as HTMLInputElement;
  const file = target.files?.[0];
  if (!file) return;
  const epoch = sessionEpoch;
  busy.value = true;
  error.value = "";
  try {
    if (file.size > 8_000_000)
      throw new Error("采用包超过 8 MB，请拆分为有明确范围的资料包。");
    const payload = JSON.parse(await file.text());
    if (!currentSession(epoch)) return;
    const data = await post<SnapshotSummary>("/snapshots", payload);
    if (!currentSession(epoch)) return;
    selectedSnapshot.value = data.id;
    await refresh();
  } catch (e) {
    if (currentSession(epoch)) handleError(e);
  } finally {
    target.value = "";
    if (currentSession(epoch)) busy.value = false;
  }
}
async function logout() {
  if (loggingOut.value) return;
  loggingOut.value = true;
  const epoch = ++sessionEpoch;
  ++refreshVersion;
  clearTimeout(timer);
  activeRunId = "";
  loading.value = true;
  busy.value = false;
  authenticated.value = false;
  selectedRun.value = null;
  selectedCandidate.value = null;
  selectedSnapshot.value = "";
  snapshots.value = [];
  runs.value = [];
  try {
    await engineRequest("/session", { method: "DELETE" });
    if (!currentSession(epoch)) return;
  } catch (e) {
    if (currentSession(epoch)) handleError(e);
  } finally {
    if (currentSession(epoch)) {
      loading.value = false;
      loggingOut.value = false;
    }
  }
}
onMounted(async () => {
  const query = new URLSearchParams(window.location.search);
  const requestedRun = query.get("run");
  const requestedCandidate = query.get("candidate");
  await refresh();
  if (!disposed && authenticated.value && requestedRun) {
    await openRun(requestedRun);
    if (!disposed && selectedRun.value?.id === requestedRun && requestedCandidate) {
      candidateScope.value = "all";
      selectedCandidate.value = generatedCandidates.value.find(item => item.candidate_key === requestedCandidate) ?? null;
    }
  }
});
onUnmounted(() => {
  disposed = true;
  ++sessionEpoch;
  ++refreshVersion;
  clearTimeout(timer);
});
</script>
<template>
  <div class="page-heading">
    <div>
      <p class="eyebrow">DISCOVERY ENGINE</p>
      <h1>情报雷达</h1>
      <p class="description">
        从已有资料中发现线索，逐条查看计算依据，再决定深入调查什么。
      </p>
    </div>
    <div class="heading-actions">
      <ElButton :loading="loading" @click="refresh">刷新</ElButton
      ><ElButton v-if="authenticated" text @click="logout">退出空间</ElButton>
    </div>
  </div>
  <p v-if="error" class="error-panel" role="alert">{{ error }}</p>
  <p v-if="sourceAccessLimited" class="notice" role="status">本页部分来源当前不可读取，相关资料和运行未显示；普通资料仍可使用，当前列表不是全部采用记录。</p>
  <p v-if="!enabled" class="notice">
    本地研究用户尚未配置，请由管理员完成启动配置。
  </p>
  <EngineLogin
    v-else-if="!authenticated && !loading"
    @authenticated="refresh"
  />
  <template v-if="authenticated">
    <MonitorPanel @open-run="openRun" @unauthorized="handleError" />
    <section class="engine-panel analysis-composer">
      <div class="section-heading">
        <h2>手动分析（可选）</h2>
        <ElButton :disabled="busy" @click="importFile?.click()"
          >导入采用包</ElButton
        ><input
          ref="importFile"
          type="file"
          accept="application/json,.json"
          hidden
          @change="importSnapshot"
        />
      </div>
      <div class="analysis-controls">
        <label class="kdd-direction">分析方向
          <select v-model="direction" aria-label="分析方向">
            <option v-for="(label, value) in kddDirections" :key="value" :value="value">{{ label }}</option>
            <option value="legacy">原有方法（固定行为）</option>
          </select>
        </label>
        <ElSelect
          v-model="selectedSnapshot"
          aria-label="选择采用资料"
          placeholder="选择已采用的资料"
          class="snapshot-select"
          ><ElOption
            v-for="item in snapshots"
            :key="item.id"
            :value="item.id"
            :label="`${item.is_demo ? '测试夹具' : '采用资料'} · ${item.source_instance} · ${item.record_count} 条 · ${item.observed_at.slice(0, 10)}`" /></ElSelect
        ><ElButton
          type="primary"
          :loading="busy"
          :disabled="!selectedSnapshot"
          @click="analyze"
          >运行分析</ElButton
        >
      </div>
      <p v-if="selectedData" class="muted">
        {{
          selectedData.is_demo
            ? "算法测试资料，不作为真实情报。"
            : "仅在本次采用范围内进行探索。"
        }}
        覆盖方式：{{ selectedData.coverage_mode }} ·
        {{ selectedData.record_count }} 条记录
      </p>
      <p v-else class="muted">
        先导入授权、固定版本的资料包。清洗文本可直接开始探索，结构化字段会打开更多方法。
      </p>
      <ElButton text @click="showPolicy = !showPolicy"
        >{{ showPolicy ? "收起" : "查看和调整" }}时间范围与方法参数</ElButton
      >
      <div v-if="showPolicy" class="policy-editor">
        <p class="muted">
          截止时间固定在策略中；默认比较近 30 天与前 365
          天。存量文本探索不将今天采用的旧资料称为近期进展。
        </p>
        <ElInput
          v-model="policyText"
          type="textarea"
          :rows="7"
          aria-label="分析策略 JSON"
        />
      </div>
    </section>
    <SnapshotComparison :snapshots="snapshots" />
    <div class="engine-layout">
      <section class="engine-results">
        <div class="section-heading">
          <h2>
            待核实线索 <span class="muted">{{ candidates.length }}</span>
          </h2>
          <ElInput
            v-model="filter"
            placeholder="筛选标题或方法"
            aria-label="筛选线索"
            class="candidate-search"
            clearable
          />
        </div>
        <template v-if="selectedRun">
          <div class="run-banner">
            <ElTag
              v-if="
                selectedRun.result?.limitations?.includes(
                  'fixture_not_real_intelligence',
                ) || activeSnapshot?.is_demo
              "
              size="small"
              type="warning"
              >测试夹具 · 非真实情报</ElTag
            >
            <span>{{
              stateLabels[selectedRun.state] ?? selectedRun.state
            }}</span
            ><span
              >第 {{ selectedRun.attempt }} 次执行 ·
              {{ selectedRun.created_at.slice(0, 16).replace("T", " ") }}</span
            >
          </div>
          <p class="muted">
            本条分析采用：{{
              activeSnapshot?.source_instance ?? selectedRun.snapshot_id
            }}
            · {{ activeSnapshot?.record_count ?? "未知" }} 条记录 · 固定截止：{{
              selectedRun.original_policy.as_of
            }}
          </p>
          <details class="run-policy">
            <summary>查看本条分析的固定策略与结果版本</summary>
            <pre>{{
              JSON.stringify(
                {
                  policy: selectedRun.original_policy,
                  input_hash: selectedRun.input_hash,
                  compute_version: selectedRun.result?.compute_version,
                  result_hash: selectedRun.result?.result_hash,
                },
                null,
                2,
              )
            }}</pre>
          </details>
          <p
            v-if="
              selectedRun.policy_view_status === 'historical_requires_new_run'
            "
            class="notice"
          >
            这条记录保留了旧版算法的固定策略。可以继续查看原结果；使用当前算法请新建分析。
          </p>
          <p v-if="selectedRun.dependency_note" class="notice">
            {{ selectedRun.dependency_note }}
          </p>
          <ElButton v-if="canRetry" :loading="busy" @click="retryRun"
            >重试固定输入</ElButton
          >
          <p v-if="selectedRun.error" class="error-panel">
            {{ selectedRun.error.message }}
          </p>
          <KddRunSummary v-if="selectedRun.result" :result="selectedRun.result" :policy="selectedRun.original_policy" :method-labels="methodLabels" />
          <div v-if="selectedRun.result" class="candidate-scope">
            <label>显示范围
              <select v-model="candidateScope" aria-label="候选显示范围">
                <option value="selected">本轮入选（{{ selectedRun.result.candidates.length }}）</option>
                <option value="all">全部算法候选（{{ generatedCandidates.length }}）</option>
              </select>
            </label>
            <p class="muted">可抽查未入选依据与筛选理由。容量暂缓、相同依据暂缓和价值条件未通过分别保留。</p>
          </div>
          <p v-if="selectedRun.result" class="muted">
            {{ runnable }} /
            {{ methods.length }} 个方法实际计算。结果为候选，尚未经过专业核验。
          </p>
          <article
            v-for="candidate in candidates"
            :key="candidate.candidate_key"
            class="candidate-card"
          >
            <div class="candidate-meta">
              <ElTag
                size="small"
                :type="candidate.kind === 'data_quality' ? 'warning' : 'info'"
                >{{ kindLabels[candidate.kind] }}</ElTag
              ><span>{{
                methodLabel(candidate.method_id, methodLabels)
              }}</span
              ><ElTag
                v-if="candidate.urgency === 'immediate'"
                size="small"
                type="danger"
                >需及时核实</ElTag
              >
            </div>
            <h3>
              <button
                class="candidate-title"
                @click="selectedCandidate = candidate"
              >
                {{ candidate.title }}
              </button>
            </h3>
            <p>{{ explainCode(candidate.reasons[0] ?? "") }}</p>
            <p class="muted">{{ selectionLabel(candidate.candidate_key) }}</p>
            <div class="candidate-bottom">
              <span>{{ candidate.supports.length }} 条贡献资料 · 待核实</span
              ><ElButton
                text
                type="primary"
                @click="selectedCandidate = candidate"
                >查看依据与下一步 →</ElButton
              >
            </div>
          </article>
          <div
            v-if="!candidates.length && selectedRun.result"
            class="engine-empty"
          >
            当前筛选或分析范围内没有候选。请结合右侧方法状态判断是没有命中，还是输入不足。
          </div>
          <div
            v-else-if="!selectedRun.result && !selectedRun.error"
            class="engine-empty"
          >
            任务已保存，等待后台完成计算。此处会自动更新。
          </div>
        </template>
        <div v-else class="engine-empty">
          <div class="radar-symbol"><i></i><i></i><b></b></div>
          <h3>从一份真实资料开始</h3>
          <p>运行分析后，这里会呈现线索、适用范围和贡献资料。</p>
        </div>
      </section>
      <aside class="engine-aside">
        <section class="engine-panel">
          <h2>方法与数据条件</h2>
          <p v-if="!methods.length" class="muted">
            分析后逐项报告哪些方法可计算、哪些只能探索、哪些缺少输入。
          </p>
          <details
            v-for="method in methods"
            :key="method.method_id"
            class="method-item"
          >
            <summary>
              <span>{{
                methodLabel(method.method_id, methodLabels)
              }}</span
              ><ElTag
                size="small"
                :type="
                  method.readiness === 'ready'
                    ? 'success'
                    : method.readiness === 'unsupported'
                      ? 'info'
                      : 'warning'
                "
                >{{ readinessLabels[method.readiness] }}</ElTag
              >
            </summary>
            <p class="muted">执行状态：{{ method.execution_status }}</p>
            <ul>
              <li
                v-for="reason in [
                  ...(method.reasons ?? []),
                  ...(method.limitations ?? []),
                ]"
                :key="reason"
              >
                {{ explainCode(reason) }}
              </li>
            </ul>
          </details>
        </section>
        <section class="engine-panel">
          <h2>分析记录</h2>
          <button
            v-for="run in runs"
            :key="run.id"
            class="history-item"
            :class="{ active: run.id === selectedRun?.id }"
            @click="openRun(run.id)"
          >
            <span>{{ run.created_at.slice(0, 16).replace("T", " ") }}</span
            ><small>{{ stateLabels[run.state] ?? run.state }}</small>
          </button>
          <p v-if="!runs.length" class="muted">{{ sourceAccessLimited ? "本页没有可显示的分析，部分来源暂不可读。" : "暂无分析记录" }}</p>
        </section>
      </aside>
    </div>
    <CandidateDetail
      :candidate="selectedCandidate"
      :feedback="selectedRun?.feedback"
      :pattern-evaluation="candidateEvaluation(selectedRun?.result, selectedCandidate?.candidate_key)"
      :selection-decision="candidateDecision(selectedRun?.result, selectedCandidate?.candidate_key)"
      :investigation-allowed="selectedCandidateCanCreate"
      :run-id="selectedRun?.id ?? ''"
      :snapshot-id="selectedRun?.snapshot_id ?? ''"
      :result-hash="selectedRun?.result?.result_hash ?? ''"
      @close="selectedCandidate = null"
      @feedback-saved="rememberFeedback"
    />
  </template>
</template>

<style scoped>
.kdd-direction, .candidate-scope label { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.kdd-direction select, .candidate-scope select { max-width: 100%; border: 1px solid var(--line, #dce5e9); background: var(--el-bg-color, #fff); border-radius: 5px; padding: 8px; color: inherit; }
.candidate-scope { margin: 12px 0; }
</style>
