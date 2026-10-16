<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { ElButton, ElOption, ElSelect, ElTag } from "element-plus";
import EngineLogin from "../components/engine/EngineLogin.vue";
import DecisionResults from "../components/engine/DecisionResults.vue";
import { decisionExplanation } from "../api/decisionLabels";
import {
  engineRequest,
  EngineError,
  post,
  type DecisionRunCreate,
  type DecisionRunView,
  type SnapshotSummary,
} from "../api/engine";

const authenticated = ref(false);
const enabled = ref(true);
const loading = ref(false);
const busy = ref(false);
const loggingOut = ref(false);
const error = ref("");
const snapshots = ref<SnapshotSummary[]>([]);
const snapshotIds = ref<string[]>([]);
const draft = ref<Pick<DecisionRunCreate, "input" | "policy"> | null>(null);
const runs = ref<DecisionRunView[]>([]);
const selectedRun = ref<DecisionRunView | null>(null);
const nextCursor = ref<string | null>(null);
const sourceCursor = ref<string | null>(null);
const inputFile = ref<HTMLInputElement | null>(null);
let disposed = false;
let epoch = 0;
let refreshVersion = 0;
let activeId = "";
let timer: ReturnType<typeof setTimeout> | undefined;
const audienceLabels: Record<string, string> = {
  central: "中央层面",
  provincial: "省级层面",
  university: "学校",
  enterprise: "企业",
};
const stateLabels: Record<string, string> = {
  queued: "等待计算",
  running: "正在比较",
  completed: "执行结束",
  partial: "部分完成",
  failed: "比较失败",
};
const canRetry = computed(
  () =>
    selectedRun.value?.state === "failed" &&
    selectedRun.value.input_view_status === "current" &&
    selectedRun.value.policy_view_status === "current" &&
    ![
      "result_budget_exceeded",
      "method_version_changed",
      "policy_schema_changed",
      "decision_schema_changed",
      "decision_version_changed",
    ].includes(selectedRun.value.error?.code ?? ""),
);
function current(value: number) {
  return !disposed && value === epoch;
}
function showError(value: unknown) {
  error.value =
    value instanceof Error ? value.message : "研究任务未能完成，请稍后重试。";
  if (value instanceof EngineError && value.status === 401) {
    authenticated.value = false;
    selectedRun.value = null;
    activeId = "";
    clearTimeout(timer);
  }
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
    authenticated.value = session.authenticated;
    enabled.value = session.enabled;
    if (!session.authenticated) {
      selectedRun.value = null;
      activeId = "";
      clearTimeout(timer);
      return;
    }
    const [sources, history] = await Promise.all([
      engineRequest<{ items: SnapshotSummary[]; next_cursor?: string | null }>(
        "/snapshots?limit=100",
      ),
      engineRequest<{ items: DecisionRunView[]; next_cursor?: string | null }>(
        "/decision-runs",
      ),
    ]);
    if (!valid()) return;
    snapshots.value = sources.items;
    sourceCursor.value = sources.next_cursor ?? null;
    runs.value = history.items;
    nextCursor.value = history.next_cursor ?? null;
    if (!activeId && history.items[0]) await openRun(history.items[0].id);
  } catch (value) {
    if (valid()) showError(value);
  } finally {
    if (valid()) loading.value = false;
  }
}
async function openRun(id: string) {
  const generation = epoch;
  activeId = id;
  clearTimeout(timer);
  if (selectedRun.value?.id !== id) selectedRun.value = null;
  try {
    const run = await engineRequest<DecisionRunView>(
      `/decision-runs/${encodeURIComponent(id)}`,
    );
    if (!current(generation) || activeId !== id) return;
    if (
      selectedRun.value?.id === id &&
      selectedRun.value.revision > run.revision
    )
      return;
    selectedRun.value = run;
    const row = runs.value.find((item) => item.id === id);
    if (row) {
      row.state = run.state;
      row.revision = run.revision;
    }
    if (["queued", "running"].includes(run.state))
      timer = setTimeout(() => openRun(id), 2500);
  } catch (value) {
    if (current(generation) && activeId === id) showError(value);
  }
}
async function loadMore() {
  if (!nextCursor.value || loading.value) return;
  const generation = epoch;
  loading.value = true;
  try {
    const page = await engineRequest<{
      items: DecisionRunView[];
      next_cursor?: string | null;
    }>(`/decision-runs?cursor=${encodeURIComponent(nextCursor.value)}`);
    if (!current(generation)) return;
    const known = new Set(runs.value.map((item) => item.id));
    runs.value.push(...page.items.filter((item) => !known.has(item.id)));
    nextCursor.value = page.next_cursor ?? null;
  } catch (value) {
    if (current(generation)) showError(value);
  } finally {
    if (current(generation)) loading.value = false;
  }
}
async function loadSources() {
  if (!sourceCursor.value || loading.value) return;
  const generation = epoch;
  loading.value = true;
  try {
    const page = await engineRequest<{
      items: SnapshotSummary[];
      next_cursor?: string | null;
    }>(`/snapshots?limit=100&cursor=${encodeURIComponent(sourceCursor.value)}`);
    if (!current(generation)) return;
    const known = new Set(snapshots.value.map((item) => item.id));
    snapshots.value.push(...page.items.filter((item) => !known.has(item.id)));
    sourceCursor.value = page.next_cursor ?? null;
  } catch (value) {
    if (current(generation)) showError(value);
  } finally {
    if (current(generation)) loading.value = false;
  }
}
async function importDraft(event: Event) {
  const element = event.target as HTMLInputElement;
  const file = element.files?.[0];
  if (!file) return;
  if (loading.value || busy.value) {
    error.value = "资料列表或研究任务仍在加载，请稍后导入。";
    element.value = "";
    return;
  }
  const generation = epoch;
  error.value = "";
  try {
    if (file.size > 2_000_000)
      throw new Error("研究任务包超过 2 MB，请缩小本次比较范围。");
    const value = JSON.parse(await file.text());
    if (!current(generation)) return;
    if (
      value?.input?.schema_version !== "bioradar.decision.input.v1" ||
      value?.policy?.policy_version !== "bioradar.decision.policy.v1"
    )
      throw new Error(
        "请选择包含 input 和 policy 的决策任务包，格式见开发入口。",
      );
    if (
      !value.input.brief ||
      typeof value.input.brief.title !== "string" ||
      typeof value.input.brief.decision_question !== "string" ||
      !Object.hasOwn(audienceLabels, value.input.brief.audience) ||
      !Array.isArray(value.input.evidence_snapshot_hashes ?? [])
    )
      throw new Error(
        "研究任务缺少可显示的委托信息；完整字段将在提交时由服务器校验。",
      );
    if (
      ["outcomes", "constraints"].some(
        (key) => value.input[key] != null && !Array.isArray(value.input[key]),
      )
    )
      throw new Error("结果和约束必须为记录数组，完整格式见开发入口。");
    const intervals = [
      ...(value.input.outcomes ?? []).map(
        (item: { value?: unknown }) => item?.value,
      ),
      ...(value.input.constraints ?? []).map(
        (item: { threshold?: unknown }) => item?.threshold,
      ),
    ];
    if (
      intervals.some(
        (item) =>
          item != null &&
          (typeof item !== "object" ||
            !("lower" in item) ||
            !("upper" in item) ||
            typeof item.lower !== "string" ||
            typeof item.upper !== "string"),
      )
    )
      throw new Error(
        '为保留数值精度，请将区间端点写成十进制字符串，例如 "12.50"，再导入。',
      );
    draft.value = { input: value.input, policy: value.policy };
    const hashes = new Set(value.input.evidence_snapshot_hashes ?? []);
    snapshotIds.value = snapshots.value
      .filter((item) => hashes.has(item.snapshot_hash))
      .map((item) => item.id);
  } catch (value) {
    if (current(generation)) showError(value);
  } finally {
    element.value = "";
  }
}
async function calculate() {
  if (!draft.value || busy.value || loading.value) return;
  const expected = new Set(draft.value.input.evidence_snapshot_hashes ?? []);
  const selected = snapshots.value.filter((item) =>
    snapshotIds.value.includes(item.id),
  );
  const actual = new Set(selected.map((item) => item.snapshot_hash));
  if (
    selected.length !== snapshotIds.value.length ||
    actual.size !== expected.size ||
    [...expected].some((hash) => !actual.has(hash))
  ) {
    error.value =
      "所选资料与任务声明的来源不一致。请先采用所需资料，或加载更多可用资料并选择对应版本。";
    return;
  }
  const generation = epoch;
  busy.value = true;
  error.value = "";
  try {
    const body: DecisionRunCreate = {
      ...draft.value,
      snapshot_ids: snapshotIds.value,
    };
    const run = await post<DecisionRunView>("/decision-runs", body);
    if (!current(generation)) return;
    await openRun(run.id);
    if (current(generation)) await refresh();
  } catch (value) {
    if (current(generation)) showError(value);
  } finally {
    if (current(generation)) busy.value = false;
  }
}
async function retry() {
  if (!selectedRun.value || !canRetry.value || busy.value) return;
  const generation = epoch;
  const run = selectedRun.value;
  busy.value = true;
  try {
    await post<DecisionRunView>(
      `/decision-runs/${encodeURIComponent(run.id)}/actions`,
      {
        action: "retry_failed",
        expected_revision: run.revision,
      },
    );
    if (current(generation)) await openRun(run.id);
  } catch (value) {
    if (current(generation)) showError(value);
  } finally {
    if (current(generation)) busy.value = false;
  }
}
async function logout() {
  if (loggingOut.value) return;
  const generation = ++epoch;
  ++refreshVersion;
  loggingOut.value = true;
  loading.value = true;
  authenticated.value = false;
  busy.value = false;
  activeId = "";
  clearTimeout(timer);
  selectedRun.value = null;
  draft.value = null;
  runs.value = [];
  snapshots.value = [];
  snapshotIds.value = [];
  nextCursor.value = null;
  sourceCursor.value = null;
  try {
    await engineRequest("/session", { method: "DELETE" });
  } catch (value) {
    if (current(generation)) showError(value);
  } finally {
    if (current(generation)) {
      loading.value = false;
      loggingOut.value = false;
    }
  }
}
onMounted(refresh);
onUnmounted(() => {
  disposed = true;
  ++epoch;
  ++refreshVersion;
  clearTimeout(timer);
});
</script>

<template>
  <div class="page-heading">
    <div>
      <p class="eyebrow">DECISION RESEARCH</p>
      <h1>把证据用于具体选择</h1>
      <p class="description">
        明确要作出的决策，比较方案的条件、代价与未知，保留改变判断的依据。
      </p>
    </div>
    <div class="heading-actions">
      <ElButton :loading="loading" @click="refresh">刷新</ElButton>
      <ElButton v-if="authenticated" text @click="logout">退出空间</ElButton>
    </div>
  </div>
  <p v-if="error" class="error-panel" role="alert">{{ error }}</p>
  <p v-if="!enabled" class="notice">
    本地研究用户尚未配置，请先按开发入口完成配置。
  </p>
  <EngineLogin
    v-else-if="!authenticated && !loading"
    @authenticated="refresh"
  />
  <template v-if="authenticated">
    <section class="engine-panel decision-composer">
      <div class="section-heading">
        <h2>准备一项决策研究</h2>
        <ElButton :disabled="busy || loading" @click="inputFile?.click()"
          >导入研究任务包</ElButton
        >
        <input
          ref="inputFile"
          type="file"
          accept="application/json,.json"
          hidden
          @change="importDraft"
        />
      </div>
      <p class="muted">
        由研究人员确认问题、现状基线、备选方案、指标口径与假设后计算。资料可先在情报雷达采用；无证据的假设会保留声明身份。
      </p>
      <div v-if="!draft" class="decision-audiences">
        <div>
          <strong>中央层面</strong>
          <p>公共健康、关键供给与全国资源配置</p>
        </div>
        <div>
          <strong>省级层面</strong>
          <p>产业定位、地方能力与财政约束</p>
        </div>
        <div>
          <strong>学校</strong>
          <p>学科团队、公共平台与成果转化</p>
        </div>
        <div>
          <strong>企业</strong>
          <p>自研、合作、引进与暂缓的取舍</p>
        </div>
      </div>
      <template v-else>
        <div class="decision-draft-title">
          <ElTag v-if="draft.input.is_demo" type="warning"
            >测试夹具 · 非真实建议</ElTag
          >
          <ElTag>{{ audienceLabels[draft.input.brief.audience] }}</ElTag>
          <h3>{{ draft.input.brief.title }}</h3>
        </div>
        <p>{{ draft.input.brief.decision_question }}</p>
        <label class="muted" for="decision-sources"
          >关联的固定采用资料（最多 8 份，权限由服务器核对）</label
        >
        <div class="analysis-controls">
          <ElSelect
            id="decision-sources"
            v-model="snapshotIds"
            multiple
            :multiple-limit="8"
            placeholder="选择已授权采用的资料"
            aria-label="决策依据资料"
          >
            <ElOption
              v-for="item in snapshots"
              :key="item.id"
              :value="item.id"
              :label="`${item.is_demo ? '测试夹具' : '采用资料'} · ${item.source_instance} · ${item.record_count} 条 · ${item.snapshot_hash.slice(0, 10)}`"
            />
          </ElSelect>
          <ElButton
            type="primary"
            :disabled="loading"
            :loading="busy"
            @click="calculate"
            >比较方案</ElButton
          >
        </div>
        <ElButton
          v-if="sourceCursor"
          text
          :loading="loading"
          @click="loadSources"
          >加载更多可用资料</ElButton
        >
        <details class="run-policy">
          <summary>查看待提交的研究任务与计算条件</summary>
          <pre>{{ JSON.stringify(draft, null, 2) }}</pre>
        </details>
      </template>
    </section>
    <div class="engine-layout">
      <section class="decision-results">
        <template v-if="selectedRun">
          <div class="run-banner">
            <span>{{ stateLabels[selectedRun.state] }}</span>
            <ElTag v-if="selectedRun.is_demo" type="warning" size="small"
              >测试夹具 · 非真实建议</ElTag
            >
            <span>第 {{ selectedRun.attempt }} 次执行</span>
          </div>
          <h2 class="decision-title">{{ selectedRun.title }}</h2>
          <p v-if="selectedRun.dependency_note" class="notice">
            {{ selectedRun.dependency_note }}
          </p>
          <p v-if="selectedRun.error" class="error-panel">
            {{ selectedRun.error.message }}
          </p>
          <ElButton v-if="canRetry" :loading="busy" @click="retry"
            >重试固定输入</ElButton
          >
          <DecisionResults
            v-if="
              selectedRun.result &&
              selectedRun.decision_input &&
              selectedRun.result.execution_status !== 'unsupported'
            "
            :key="selectedRun.id"
            :run="selectedRun"
          />
          <section
            v-else-if="selectedRun.result?.execution_status === 'unsupported'"
            class="engine-panel decision-unsupported"
            role="status"
          >
            <h2>本次未执行方案比较</h2>
            <p>
              运行记录已保存，但当前策略不足以执行这份输入。请调整范围或计算限额后建立新任务。
            </p>
            <ul class="evidence-list">
              <li v-for="reason in selectedRun.result.data_gaps" :key="reason">
                {{ decisionExplanation(reason) }}
              </li>
            </ul>
          </section>
          <p
            v-else-if="
              selectedRun.input_view_status === 'historical_requires_new_run'
            "
            class="notice"
          >
            该任务保留了历史输入，请查看原始版本；按当前规则比较需要建立新任务。
          </p>
          <p v-else-if="!selectedRun.error" class="engine-empty">
            任务已保存，等待后台完成比较。
          </p>
          <details class="run-policy decision-frozen">
            <summary>查看固定输入、版本与结果</summary>
            <pre>{{
              JSON.stringify(
                {
                  input_hash: selectedRun.input_hash,
                  evidence_basis_hash: selectedRun.evidence_basis_hash,
                  submitted_by: selectedRun.submitted_by,
                  accepted_at: selectedRun.accepted_at,
                  sources: selectedRun.source_snapshots,
                  input: selectedRun.original_input,
                  policy: selectedRun.original_policy,
                  result: selectedRun.result,
                },
                null,
                2,
              )
            }}</pre>
          </details>
        </template>
        <div v-else class="engine-empty">
          <h3>从一个具体选择开始</h3>
          <p>
            准备问题、现状基线和至少一个替代方案。计算会区分条件满足、条件不满足、区间未决与输入未知。
          </p>
        </div>
      </section>
      <aside class="engine-aside">
        <section class="engine-panel">
          <h2>研究记录</h2>
          <button
            v-for="run in runs"
            :key="run.id"
            class="history-item decision-history"
            :class="{ active: run.id === selectedRun?.id }"
            @click="openRun(run.id)"
          >
            <span
              >{{ run.title
              }}<small
                >{{ audienceLabels[run.audience] }} ·
                {{ run.created_at.slice(0, 10) }}</small
              ></span
            >
            <small>{{ stateLabels[run.state] }}</small>
          </button>
          <p v-if="!runs.length" class="muted">暂无决策比较记录</p>
          <ElButton v-if="nextCursor" text :loading="loading" @click="loadMore"
            >加载更多</ElButton
          >
        </section>
        <section class="engine-panel">
          <h2>怎样阅读</h2>
          <p class="muted">
            结果是当前委托的决策准备草案。条件性比较保留被排除和未决方案，情景数量不代表发生概率。
          </p>
          <p class="muted">
            同一套事实可以支持不同职责下的选择；更改用途不会自动改变事实或开放资料权限。
          </p>
        </section>
      </aside>
    </div>
  </template>
</template>
