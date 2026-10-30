<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { ElButton, ElInput } from "element-plus";
import { useRoute, useRouter } from "vue-router";
import { engineRequest, EngineError, post } from "../api/engine";
import type { components } from "../api/generated/engine";
import EngineLogin from "../components/engine/EngineLogin.vue";
import TopicDefinitionForm from "../components/engine/TopicDefinitionForm.vue";
import TopicEvaluationDetail from "../components/engine/TopicEvaluationDetail.vue";
import TopicTrackingCard from "../components/engine/TopicTrackingCard.vue";

type Topic = components["schemas"]["TopicView"];
type Evaluation = components["schemas"]["TopicEvaluation"];
type Template = components["schemas"]["TopicTemplate"];
type Create = components["schemas"]["TopicCreate"];
const route = useRoute();
const router = useRouter();
const authenticated = ref(false);
const enabled = ref(true);
const loading = ref(false);
const busy = ref(false);
const error = ref("");
const topics = ref<Topic[]>([]);
const templates = ref<Template[]>([]);
const selected = ref<Topic | null>(null);
const tracking = computed(() => (selected.value as (Topic & { tracking?: unknown }) | null)?.tracking);
const trackingLifecycle = computed(() => String((tracking.value as { lifecycle?: unknown } | null)?.lifecycle || ""));
const lifecycleReason = ref("");
watch(() => selected.value?.id, () => { lifecycleReason.value = ""; });
const evaluations = ref<Evaluation[]>([]);
const evaluation = ref<Evaluation | null>(null);
const unavailable = ref(0);
const topicCursor = ref<string | null>(null);
const evaluationCursor = ref<string | null>(null);
const filter = ref("");
const form = ref<"new" | "edit" | null>(null);
const editTarget = ref<Topic | null>(null);
let formWriteKey: string | null = null;
const domains: Record<string, string> = {
  pipeline: "管线",
  research: "科研",
  business: "商业",
  public_health: "公共卫生",
  policy: "政策",
  regulatory: "监管与安全",
  supply: "制造与供应",
  regional: "区域布局",
  other: "其他专题",
};
const states: Record<string, string> = {
  paused: "已暂停",
  seeding: "正在补齐已采用资料",
  catching_up: "正在追赶更新",
  current: "已跟上采用记录",
  source_unavailable: "来源暂不可读",
};
const visibleTopics = computed(() =>
  topics.value.filter(
    (item) => !filter.value || item.definition.domain === filter.value,
  ),
);
let disposed = false;
let sessionEpoch = 0;
let selectionVersion = 0;
let refreshVersion = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
const current = (epoch: number) => !disposed && sessionEpoch === epoch;
function date(value?: string | null) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "尚无记录";
}
function upstreamSummary(value: unknown): string[] {
  if (!value || typeof value !== "object") return ["原站采集状态尚未核实。"];
  const health = value as {
    collection?: {
      state?: string;
      last_check_at?: string;
      last_recorded_success_at?: string;
    };
    watches?: {
      watch_id?: string;
      state?: string;
      scope?: string;
      last_success_at?: string;
      stale?: boolean;
    }[];
  };
  const lines: string[] = [];
  if (health.collection?.last_check_at)
    lines.push(
      "原采集任务最近检查：" +
        date(health.collection.last_check_at) +
        "；最近有记录的成功：" +
        date(health.collection.last_recorded_success_at) +
        "。",
    );
  for (const watch of health.watches ?? []) {
    const state = watch.stale
      ? "超过预定检查时间"
      : watch.state === "paging"
        ? "仍在继续读取本轮资料"
        : watch.state === "idle"
          ? "等待下一轮检查"
          : "请核对本轮读取状态";
    lines.push(
      (watch.scope || watch.watch_id || "固定查询") +
        "：" +
        state +
        "；最近完成检查：" +
        date(watch.last_success_at) +
        "。",
    );
  }
  return lines.length
    ? lines
    : ["尚无可核实的原站检查记录；读到 Fabric 数据不表示原站刚刚更新。"];
}
function clearContent() {
  formWriteKey = null;
  topics.value = [];
  selected.value = null;
  evaluations.value = [];
  evaluation.value = null;
  templates.value = [];
  topicCursor.value = null;
  evaluationCursor.value = null;
  form.value = null;
  editTarget.value = null;
}
function fail(value: unknown) {
  error.value =
    value instanceof Error ? value.message : "读取未完成，请稍后重试。";
  if (value instanceof EngineError && value.status === 403) {
    ++selectionVersion; ++refreshVersion;
    busy.value = false; loading.value = false;
    clearContent();
    unavailable.value = Math.max(1, unavailable.value);
    scheduleRefresh();
    return;
  }
  if (value instanceof EngineError && value.status === 401) {
    authenticated.value = false;
    ++sessionEpoch;
    ++selectionVersion;
    busy.value = false;
    loading.value = false;
    clearTimeout(timer);
    clearContent();
  }
}
function scheduleRefresh() {
  clearTimeout(timer);
  if (!disposed && authenticated.value) timer = setTimeout(refresh, 30000);
}
function startForm(mode: "new" | "edit") {
  if (busy.value || (mode === "edit" && !selected.value)) return;
  ++refreshVersion;
  ++selectionVersion;
  loading.value = false;
  editTarget.value = mode === "edit" ? selected.value : null;
  form.value = mode;
  formWriteKey = crypto.randomUUID();
  scheduleRefresh();
}
function cancelForm() {
  formWriteKey = null;
  form.value = null;
  editTarget.value = null;
  scheduleRefresh();
}
async function refresh() {
  if (disposed) return;
  if (busy.value || form.value) {
    clearTimeout(timer);
    timer = setTimeout(refresh, 30000);
    return;
  }
  const epoch = sessionEpoch,
    version = ++refreshVersion;
  const initialSelection = selectionVersion;
  const valid = () => current(epoch) && version === refreshVersion;
  loading.value = true;
  error.value = "";
  clearTimeout(timer);
  try {
    const session = await engineRequest<{
      enabled: boolean;
      authenticated: boolean;
    }>("/session");
    if (!valid()) return;
    authenticated.value = session.authenticated;
    enabled.value = session.enabled;
    if (!authenticated.value) {
      clearContent();
      return;
    }
    const [list, catalog] = await Promise.all([
      engineRequest<components["schemas"]["TopicList"]>("/topics?limit=50"),
      engineRequest<components["schemas"]["TopicTemplates"]>(
        "/topics/templates",
      ),
    ]);
    if (!valid()) return;
    if (busy.value || form.value || selectionVersion !== initialSelection)
      return;
    topics.value = list.items;
    templates.value = catalog.items;
    unavailable.value = list.unavailable_count ?? 0;
    topicCursor.value = list.next_cursor ?? null;
    const wanted =
      selected.value?.id ||
      (typeof route.query.id === "string" ? route.query.id : "") ||
      list.items[0]?.id;
    if (wanted) await openTopic(wanted, true);
    else {
      selected.value = null;
      evaluations.value = [];
      evaluation.value = null;
    }
  } catch (e) {
    if (valid()) fail(e);
  } finally {
    if (valid()) {
      loading.value = false;
      if (authenticated.value) scheduleRefresh();
    }
  }
}
async function openTopic(id: string, keepEvaluation = false) {
  if (!keepEvaluation) {
    ++refreshVersion;
    loading.value = false;
  }
  const epoch = sessionEpoch,
    version = ++selectionVersion;
  const valid = () => current(epoch) && version === selectionVersion;
  const previousEvaluation =
    keepEvaluation && selected.value?.id === id ? evaluation.value?.id : null;
  selected.value = null;
  evaluations.value = [];
  evaluation.value = null;
  evaluationCursor.value = null;
  if (!keepEvaluation) {
    form.value = null;
    editTarget.value = null;
  }
  try {
    const [topic, history] = await Promise.all([
      engineRequest<Topic>("/topics/" + encodeURIComponent(id)),
      engineRequest<components["schemas"]["TopicEvaluationList"]>(
        "/topics/" + encodeURIComponent(id) + "/evaluations?limit=20",
      ),
    ]);
    if (!valid()) return;
    selected.value = topic;
    evaluations.value = history.items;
    evaluationCursor.value = history.next_cursor ?? null;
    evaluation.value =
      history.items.find((item) => item.id === previousEvaluation) ??
      history.items[0] ??
      null;
    unavailable.value = Math.max(
      unavailable.value,
      history.unavailable_count ?? 0,
    );
    if (route.query.id !== id)
      void router.replace({ path: "/topics", query: { id } });
  } catch (e) {
    if (valid()) fail(e);
  } finally {
    if (valid()) scheduleRefresh();
  }
}
async function loadMore() {
  if (!topicCursor.value || busy.value) return;
  const epoch = sessionEpoch;
  busy.value = true;
  try {
    const list = await engineRequest<components["schemas"]["TopicList"]>(
      "/topics?limit=50&cursor=" + encodeURIComponent(topicCursor.value),
    );
    if (!current(epoch)) return;
    const ids = new Set(topics.value.map((item) => item.id));
    topics.value.push(...list.items.filter((item) => !ids.has(item.id)));
    topicCursor.value = list.next_cursor ?? null;
    unavailable.value += list.unavailable_count ?? 0;
  } catch (e) {
    if (current(epoch)) fail(e);
  } finally {
    if (current(epoch)) {
      busy.value = false;
      scheduleRefresh();
    }
  }
}
async function loadOlder() {
  const topic = selected.value;
  if (!topic || !evaluationCursor.value || busy.value) return;
  const epoch = sessionEpoch,
    version = selectionVersion;
  busy.value = true;
  try {
    const result = await engineRequest<
      components["schemas"]["TopicEvaluationList"]
    >(
      "/topics/" +
        encodeURIComponent(topic.id) +
        "/evaluations?limit=20&cursor=" +
        encodeURIComponent(evaluationCursor.value),
    );
    if (!current(epoch) || selectionVersion !== version) return;
    const ids = new Set(evaluations.value.map((item) => item.id));
    evaluations.value.push(...result.items.filter((item) => !ids.has(item.id)));
    evaluationCursor.value = result.next_cursor ?? null;
    unavailable.value += result.unavailable_count ?? 0;
  } catch (e) {
    if (current(epoch) && selectionVersion === version) fail(e);
  } finally {
    if (current(epoch)) {
      busy.value = false;
      scheduleRefresh();
    }
  }
}
async function save(body: Create) {
  if (busy.value) return;
  const epoch = sessionEpoch;
  const topic = editTarget.value;
  const editing = form.value === "edit";
  if (editing && !topic) {
    error.value = "编辑目标已失效，请重新选择专题。";
    return;
  }
  ++refreshVersion;
  loading.value = false;
  busy.value = true;
  error.value = "";
  try {
    const result =
      editing && topic
        ? await engineRequest<Topic>(
            "/topics/" + encodeURIComponent(topic.id),
            {
              method: "PUT",
              body: JSON.stringify({
                ...body,
                expected_revision: topic.control_revision,
              }),
              headers: {
                "Idempotency-Key": (formWriteKey ??= crypto.randomUUID()),
              },
            },
          )
        : await engineRequest<Topic>("/topics", {
            method: "POST",
            body: JSON.stringify(body),
            headers: {
              "Idempotency-Key": (formWriteKey ??= crypto.randomUUID()),
            },
          });
    if (!current(epoch)) return;
    form.value = null;
    editTarget.value = null;
    formWriteKey = null;
    const index = topics.value.findIndex((item) => item.id === result.id);
    if (index < 0) topics.value.unshift(result);
    else topics.value[index] = result;
    await openTopic(result.id);
  } catch (e) {
    if (current(epoch)) fail(e);
  } finally {
    if (current(epoch)) {
      busy.value = false;
      scheduleRefresh();
    }
  }
}
async function togglePause(lifecycle?: "closed" | "warming") {
  const topic = selected.value;
  if (!topic || busy.value) return;
  if (lifecycle && !lifecycleReason.value.trim()) {
    error.value = "请先记录调整跟踪状态的原因。";
    return;
  }
  ++refreshVersion;
  loading.value = false;
  const epoch = sessionEpoch,
    version = selectionVersion;
  busy.value = true;
  error.value = "";
  try {
    const result = await post<Topic>(
      "/topics/" + encodeURIComponent(topic.id) + "/control",
      {
        expected_revision: topic.control_revision,
        paused: lifecycle === "closed" ? true : lifecycle === "warming" ? false : !topic.paused,
        ...(lifecycle ? { lifecycle, reason: lifecycleReason.value.trim() } : {}),
      },
    );
    if (!current(epoch) || selectionVersion !== version) return;
    selected.value = result;
    const index = topics.value.findIndex((item) => item.id === result.id);
    if (index >= 0) topics.value[index] = result;
  } catch (e) {
    if (current(epoch) && selectionVersion === version) fail(e);
  } finally {
    if (current(epoch)) {
      busy.value = false;
      scheduleRefresh();
    }
  }
}
async function logout() {
  const epoch = ++sessionEpoch;
  ++selectionVersion;
  ++refreshVersion;
  clearTimeout(timer);
  authenticated.value = false;
  loading.value = false;
  busy.value = true;
  clearContent();
  try {
    await engineRequest("/session", { method: "DELETE" });
  } catch (e) {
    if (current(epoch)) fail(e);
  } finally {
    if (current(epoch)) {
      busy.value = false;
      scheduleRefresh();
    }
  }
}
onMounted(refresh);
onUnmounted(() => {
  disposed = true;
  ++sessionEpoch;
  ++selectionVersion;
  ++refreshVersion;
  clearTimeout(timer);
});
</script>

<template>
  <div class="topics-page">
    <div class="page-heading">
      <div>
        <p class="eyebrow">RESEARCH TOPICS</p>
        <h1>长期专题</h1>
        <p class="description">
          持续观察一个问题，积累证据，查看什么改变了、为什么需要继续调查。
        </p>
      </div>
      <div class="heading-actions">
        <ElButton :loading="loading" :disabled="busy" @click="refresh"
          >刷新</ElButton
        >
        <ElButton
          v-if="authenticated"
          type="primary"
          :disabled="busy || !templates.length"
          @click="startForm('new')"
          >新建专题</ElButton
        >
        <ElButton v-if="authenticated" text :disabled="busy" @click="logout"
          >退出空间</ElButton
        >
      </div>
    </div>
    <p v-if="error" class="error-panel" role="alert">{{ error }}</p>
    <p v-if="unavailable" class="notice">
      部分资料当前不可读取，相关专题或历史版本未显示。当前列表不代表全部资料。
    </p>
    <p v-if="!enabled" class="notice">请由管理员完成本地研究空间配置。</p>
    <EngineLogin
      v-else-if="!authenticated && !loading"
      @authenticated="refresh"
    />
    <template v-if="authenticated">
      <TopicDefinitionForm
        v-if="form && templates.length"
        :key="form + (form === 'edit' ? editTarget?.id : '')"
        :templates="templates"
        :initial="form === 'edit' ? editTarget?.definition : undefined"
        :is-demo="form === 'edit' ? editTarget?.is_demo : false"
        :editing="form === 'edit'"
        :busy="busy"
        @save="save"
        @cancel="cancelForm"
      />
      <p class="notice">
        已跟上采用记录，只表示处理完 Fabric
        已交付的资料；来源没采到、仍在补录或暂时中断，都不能解释成“没有变化”。
      </p>
      <div class="topic-layout">
        <aside class="topic-list">
          <label class="topic-filter"
            >观察方向
            <select v-model="filter">
              <option value="">全部方向</option>
              <option v-for="(name, key) in domains" :key="key" :value="key">
                {{ name }}
              </option>
            </select>
          </label>
          <button
            v-for="item in visibleTopics"
            :key="item.id"
            type="button"
            :class="['topic-card', { selected: selected?.id === item.id }]"
            :disabled="busy || Boolean(form)"
            @click="openTopic(item.id)"
          >
            <span class="topic-card-domain"
              >{{ domains[item.definition.domain]
              }}{{ item.is_demo ? " · 教学资料" : "" }}</span
            >
            <strong>{{ item.definition.title }}</strong>
            <small
              >{{ states[item.state] }} ·
              {{ item.observation_count }} 条已纳入资料</small
            >
          </button>
          <p v-if="!topics.length && !loading" class="muted">
            尚未建立专题。选择一个模板，写清关注的问题，就可以开始持续观察。
          </p>
          <ElButton v-if="topicCursor" :disabled="busy" @click="loadMore"
            >更多专题</ElButton
          >
        </aside>
        <section v-if="selected" class="topic-detail">
          <TopicTrackingCard v-if="tracking" :tracking="tracking" title="当前专题跟踪状态" :as-of="selected.updated_at" />
          <section class="engine-panel">
            <div class="section-heading">
              <h2>{{ selected.definition.title }}</h2>
              <span class="topic-badge"
                >{{ selected.is_demo ? "教学资料" : "真实资料" }} ·
                {{ states[selected.state] }}</span
              >
            </div>
            <p class="topic-question">{{ selected.definition.question }}</p>
            <div class="heading-actions">
              <ElButton :disabled="busy" @click="togglePause()">{{
                selected.paused ? "继续观察" : "暂停本专题"
              }}</ElButton>
              <ElButton :disabled="busy" @click="startForm('edit')"
                >修改范围</ElButton
              >
            </div>
            <details v-if="tracking" class="no-print lifecycle-control">
              <summary>人工调整跟踪状态</summary>
              <p class="muted">停止跟踪或重新关注均需记录原因；停止跟踪不会自动解除未解决的风险。</p>
              <ElInput v-model="lifecycleReason" type="textarea" :rows="2" maxlength="2000" :disabled="busy" placeholder="记录本次调整的证据与原因" aria-label="跟踪状态调整原因" />
              <div class="heading-actions">
                <ElButton v-if="trackingLifecycle !== 'closed'" :disabled="busy || !lifecycleReason.trim()" @click="togglePause('closed')">记录原因并结束跟踪</ElButton>
                <ElButton :disabled="busy || !lifecycleReason.trim()" @click="togglePause('warming')">记录原因并重新关注</ElButton>
              </div>
            </details>
            <dl class="topic-facts">
              <div>
                <dt>最近检查</dt>
                <dd>{{ date(selected.last_checked_at) }}</dd>
              </div>
              <div>
                <dt>最近形成记录</dt>
                <dd>{{ date(selected.last_evaluated_at) }}</dd>
              </div>
              <div>
                <dt>下次定期复查</dt>
                <dd>
                  {{
                    selected.paused
                      ? "已暂停"
                      : date(selected.next_evaluation_at)
                  }}
                </dd>
              </div>
              <div>
                <dt>范围版本</dt>
                <dd>第 {{ selected.scope_revision }} 版</dd>
              </div>
            </dl>
            <details>
              <summary>来源与处理进度</summary>
              <p class="muted">
                来源：{{ selected.definition.source_ids.join("、") }}；站点：{{
                  selected.definition.source_sites?.join("、") || "未另限定"
                }}。
              </p>
              <ul class="topic-source-list">
                <li v-for="source in selected.sources" :key="source.source_id">
                  {{ source.source_id }}：{{
                    source.error_code
                      ? "读取未完成"
                      : source.seed_complete
                        ? "已完成首轮已采用资料检查"
                        : "正在补齐已采用资料"
                  }}
                  <span class="muted">
                    · 最近成功 {{ date(source.last_success_at) }} · 位置
                    {{ source.after }} /
                    {{ source.scan_max_seq ?? "尚未确定" }}</span
                  >
                  <code v-if="source.error_code">{{ source.error_code }}</code>
                  <p
                    v-for="line in upstreamSummary(source.upstream_health)"
                    :key="line"
                    class="muted"
                  >
                    {{ line }}
                  </p>
                </li>
              </ul>
            </details>
          </section>
          <section class="engine-panel">
            <div class="section-heading">
              <h2>历次观察</h2>
              <span class="muted">旧记录保留当时依据</span>
            </div>
            <label v-if="evaluations.length" class="topic-evaluation-select"
              >选择记录
              <select
                :value="evaluation?.id"
                @change="
                  evaluation =
                    evaluations.find(
                      (item) =>
                        item.id === ($event.target as HTMLSelectElement).value,
                    ) ?? null
                "
              >
                <option
                  v-for="item in evaluations"
                  :key="item.id"
                  :value="item.id"
                >
                  {{ date(item.as_of) }} · 范围第 {{ item.scope_revision }} 版 ·
                  {{
                    item.state === "insufficient_data"
                      ? "资料不足"
                      : "有可描述的观察"
                  }}
                </option>
              </select>
            </label>
            <p v-else class="muted">
              尚未形成观察记录。后台先检查已采用资料，再按专题周期复查；资料不足会明确说明。
            </p>
            <ElButton
              v-if="evaluationCursor"
              text
              :disabled="busy"
              @click="loadOlder"
              >更早记录</ElButton
            >
          </section>
          <TopicEvaluationDetail
            v-if="evaluation"
            :key="evaluation.id"
            :topic-id="selected.id"
            :is-demo="selected.is_demo"
            :evaluation="evaluation"
            @failure="fail"
          />
        </section>
        <section v-else-if="topics.length" class="engine-panel">
          <p class="muted">选择一个专题，查看长期记录。</p>
        </section>
      </div>
    </template>
  </div>
</template>

<style scoped>
.topic-layout {
  display: grid;
  grid-template-columns: 250px minmax(0, 1fr);
  gap: 22px;
  align-items: start;
}
.topic-list,
.topic-detail {
  min-width: 0;
}
.topic-filter,
.topic-evaluation-select {
  display: grid;
  gap: 8px;
  font-size: 13px;
  margin-bottom: 16px;
}
select {
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  padding: 9px;
  border: 1px solid #cbd6c5;
  border-radius: 5px;
  background: white;
  color: #36543f;
}
.topic-card {
  display: block;
  text-align: left;
  width: 100%;
  padding: 16px;
  margin-bottom: 12px;
  background: white;
  border: 1px solid #dfe6da;
  border-radius: 7px;
  cursor: pointer;
  overflow-wrap: anywhere;
  color: #304d3c;
}
.topic-card.selected {
  border-color: #6e8d5e;
  background: #f0f5e9;
}
.topic-card strong {
  display: block;
  margin: 9px 0;
  line-height: 1.7;
  font-size: 14px;
}
.topic-card small,
.topic-card-domain {
  font-size: 11px;
  line-height: 1.7;
  color: #6d7c65;
}
.topic-question {
  font-size: 16px;
  line-height: 1.85;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.topic-badge {
  font-size: 12px;
  color: #607553;
}
.topic-facts {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
  margin: 24px 0;
}
.topic-facts dt {
  color: #75806f;
  font-size: 12px;
}
.topic-facts dd {
  margin: 7px 0 0;
  font-size: 13px;
}
details {
  font-size: 13px;
  line-height: 1.8;
}
summary {
  cursor: pointer;
}
.topic-source-list {
  padding-left: 20px;
  overflow-wrap: anywhere;
}
.topic-source-list code {
  display: block;
}
@media (max-width: 1000px) {
  .topic-layout {
    grid-template-columns: 1fr;
  }
  .topic-list {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }
  .topic-filter {
    grid-column: 1 / -1;
  }
  .topic-card {
    margin: 0;
  }
}
@media (max-width: 650px) {
  .topic-list,
  .topic-facts {
    grid-template-columns: 1fr;
  }
  .topics-page .page-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 14px;
  }
  .topics-page .section-heading {
    align-items: start;
    flex-wrap: wrap;
  }
  .heading-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .heading-actions .el-button {
    margin-left: 0;
  }
}
</style>
