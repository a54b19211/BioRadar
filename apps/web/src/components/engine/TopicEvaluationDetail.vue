<script setup lang="ts">
import { computed, onUnmounted, ref } from "vue";
import { ElButton } from "element-plus";
import { engineRequest } from "../../api/engine";
import { topicGapLabel } from "../../api/topicLabels";
import TopicTrackingCard from "./TopicTrackingCard.vue";
import { facts, readable } from "../../api/kdd";
import type { components } from "../../api/generated/engine";

type Evaluation = components["schemas"]["TopicEvaluation"];
const props = defineProps<{
  topicId: string;
  isDemo: boolean;
  evaluation: Evaluation;
}>();
const tracking = computed(() => (props.evaluation as Evaluation & { tracking?: unknown }).tracking);
const emit = defineEmits<{ failure: [error: unknown] }>();
const reading = ref("");
const original = ref<Record<string, unknown> | null>(null);
const downloading = ref(false);
let disposed = false;
let readVersion = 0;
const kinds: Record<string, string> = {
  text_pattern: "文字线索",
  state_change: "登记变化",
  milestone_check: "节点核查",
  metric_change: "数值变化",
  source_event: "来源事件",
  evidence_revision: "依据修订",
};
const automation: Record<string, string> = {
  not_needed: "本次未触发自动调查",
  deferred: "已保留，等待调查预算",
  saved: "已建立调查",
  needs_review: "已有调查需要复查",
  blocked: "调查暂未完成",
};
function path() {
  return (
    "/topics/" +
    encodeURIComponent(props.topicId) +
    "/evaluations/" +
    encodeURIComponent(props.evaluation.id)
  );
}
function date(value: string) {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}
async function read(key: string) {
  const version = ++readVersion;
  reading.value = key;
  original.value = null;
  try {
    const result = await engineRequest<Record<string, unknown>>(
      path() + "/evidence/" + encodeURIComponent(key),
    );
    if (disposed || version !== readVersion) return;
    original.value = result;
  } catch (error) {
    if (!disposed && version === readVersion) emit("failure", error);
  } finally {
    if (!disposed && version === readVersion) reading.value = "";
  }
}
async function download() {
  if (downloading.value) return;
  downloading.value = true;
  try {
    const record = await engineRequest<unknown>(path() + "/export");
    if (disposed) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(record, null, 2)], {
        type: "application/json;charset=utf-8",
      }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download =
      "专题观察-" +
      props.evaluation.as_of.slice(0, 10) +
      "-" +
      props.evaluation.id.slice(0, 8) +
      ".json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    if (!disposed) emit("failure", error);
  } finally {
    if (!disposed) downloading.value = false;
  }
}
function originalText(value: Record<string, unknown>) {
  const record =
    value.record && typeof value.record === "object"
      ? (value.record as Record<string, unknown>)
      : value;
  for (const field of ["raw_content", "normalized_text", "text"]) {
    if (typeof record[field] === "string") return record[field] as string;
  }
  return JSON.stringify(value, null, 2);
}
onUnmounted(() => {
  disposed = true;
  ++readVersion;
  original.value = null;
});
</script>

<template>
  <div class="topic-evaluation">
    <TopicTrackingCard v-if="tracking" :tracking="tracking" title="该版观察的证据与跟踪状态" :axes="evaluation.assessment_axes" :as-of="evaluation.as_of" />
    <section class="engine-panel">
      <div class="section-heading">
        <h2>这次看到了什么</h2>
        <span class="muted"
          >{{ date(evaluation.as_of) }} · 范围第
          {{ evaluation.scope_revision }} 版</span
        >
      </div>
      <p v-if="isDemo" class="notice">
        教学资料，用于检验程序行为，不能作为真实情报。
      </p>
      <p class="muted">本版问题：{{ evaluation.definition.question }}</p>
      <p class="topic-summary">{{ evaluation.summary }}</p>
      <p class="muted">
        以下是程序观察及待核问题。它不等于专家结论，也不是已经批准发布的报告。
      </p>
      <dl class="coverage-grid">
        <div>
          <dt>当前采用材料</dt>
          <dd>{{ evaluation.coverage.active_records }}</dd>
        </div>
        <div>
          <dt>缺少可用日期</dt>
          <dd>{{ evaluation.coverage.undated_records }}</dd>
        </div>
        <div>
          <dt>已撤回材料</dt>
          <dd>{{ evaluation.coverage.withdrawn_records }}</dd>
        </div>
        <div>
          <dt>首轮资料检查</dt>
          <dd>
            {{
              evaluation.coverage.seed_complete ? "已完成采用范围" : "仍在补齐"
            }}
          </dd>
        </div>
      </dl>
      <p class="muted">本版进入计算 {{ evaluation.coverage.evaluated_records }} 条；特征不足 {{ evaluation.coverage.limited_feature_records }} 条。进入窗口表示参与计算，不表示所有方法均适用。</p>
      <p
        v-if="!evaluation.coverage.original_source_coverage_known"
        class="notice"
      >
        原始网站的完整覆盖仍未确认。这里的数量仅指本专题采用的资料，不是整个赛道的规模。
      </p>
      <p v-if="(evaluation.coverage.omitted_records ?? 0) > 0" class="notice">
        {{ evaluation.coverage.omitted_records }} 条资料未进入本次有上限的计算。
        相关趋势只能按已说明的范围阅读，不能据此作全领域判断。
      </p>
      <p v-if="evaluation.coverage.missing_source_sites.length" class="notice">
        尚未见到这些站点的合格资料：{{
          evaluation.coverage.missing_source_sites.join("、")
        }}。
      </p>
      <article
        v-for="signal in evaluation.signals"
        :key="signal.key"
        class="topic-signal"
      >
        <span class="muted">{{ kinds[signal.kind] ?? signal.kind }}</span>
        <h3>{{ signal.title }}</h3>
        <p>{{ signal.reason }}</p>
        <ul v-if="signal.next_questions?.length">
          <li v-for="question in signal.next_questions" :key="question">
            {{ question }}
          </li>
        </ul>
        <p class="muted"><strong>适用资格：</strong>{{ readable(signal.qualification) }}</p>
        <dl v-if="facts(signal.details).length" class="coverage-grid">
          <div v-for="item in facts(signal.details)" :key="item.key"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></div>
        </dl>
        <details>
          <summary>原始计算字段与证据编号</summary>
          <p>
            用于该线索的材料单位：{{
              signal.support_units
            }}。独立性及统计资格见下列记录。
          </p>
          <pre>{{
            JSON.stringify(
              {
                qualification: signal.qualification,
                details: signal.details,
                evidence_keys: signal.evidence_keys,
              },
              null,
              2,
            )
          }}</pre>
        </details>
      </article>
      <p v-if="!evaluation.signals.length" class="muted">
        本次未形成新的待核线索。请结合资料覆盖和缺口阅读，不能据此判断该领域没有重要变化。
      </p>
      <div class="topic-followup">
        <span>{{ automation[evaluation.automation_status] }}</span>
        <RouterLink
          v-if="evaluation.investigation_id"
          :to="{
            path: '/investigations',
            query: { id: evaluation.investigation_id },
          }"
          class="text-link"
          >查看本次调查、查询策略与依据 →</RouterLink
        >
      </div>
    </section>

    <section v-if="evaluation.buckets.length" class="engine-panel">
      <h2>资料怎样积累</h2>
      <p class="muted">
        按资料的合格日期归入周期。数量增加可能来自补录或采集范围变化，不能直接解释为科研突破或行业进入上升周期。
      </p>
      <div class="topic-table-wrap">
        <table>
          <thead>
            <tr>
              <th>周期</th>
              <th>去重后的材料</th>
              <th>独立研究已确认</th>
              <th>独立性未确认</th>
              <th>时间周期</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="bucket in evaluation.buckets" :key="bucket.start">
              <td>{{ bucket.start }} 至 {{ bucket.end }}</td>
              <td>{{ bucket.exposures }}</td>
              <td>{{ bucket.confirmed_study_units }}</td>
              <td>{{ bucket.unresolved_units }}</td>
              <td>{{ bucket.complete_time_bucket ? "已结束" : "尚未结束" }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="muted">
        周期已结束不表示采集完整；独立研究未经确认时不把每份材料当成一次独立复现。
      </p>
    </section>

    <section v-if="evaluation.registered_states.length" class="engine-panel">
      <h2>最近已知登记状态</h2>
      <p class="muted">
        登记号指一项试验，不等于一个独立药物项目。计划日期、登记更新和临床实际发生时间分别核对。
      </p>
      <div class="topic-table-wrap">
        <table>
          <thead>
            <tr>
              <th>登记号</th>
              <th>登记状态</th>
              <th>阶段</th>
              <th>依据</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="state in evaluation.registered_states"
              :key="state.unit_id"
            >
              <td>{{ state.unit_id }}</td>
              <td>{{ state.status ?? "未知" }}</td>
              <td>{{ state.phase?.join("、") || "未知" }}</td>
              <td>
                <button
                  class="text-button"
                  :disabled="Boolean(reading)"
                  @click="read(state.evidence_key)"
                >
                  读取原登记
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="evaluation.metric_series.length" class="engine-panel">
      <h2>数值记录与计算资格</h2>
      <article
        v-for="series in evaluation.metric_series"
        :key="series.key"
        class="topic-series"
      >
        <h3>
          {{ series.metric_id }} <span class="muted">· {{ series.unit }}</span>
        </h3>
        <p
          v-if="
            !series.qualification.startsWith(
              'source_bound_literal_scope_descriptive',
            )
          "
          class="notice"
        >
          仅保留原值；这组资料尚未满足同口径比较条件。
        </p>
        <p class="muted">
          {{ series.region }} · {{ series.population }} · 指标口径
          {{ series.metric_version }}
        </p>
        <div class="topic-table-wrap">
          <table>
            <thead>
              <tr>
                <th>观察期间</th>
                <th>数值范围</th>
                <th>输入资格</th>
                <th>依据</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="point in series.points" :key="point.evidence_key">
                <td>{{ point.period_start }} 至 {{ point.period_end }}</td>
                <td>
                  {{
                    point.lower === null
                      ? "未知"
                      : point.lower === point.upper
                        ? point.lower
                        : point.lower + " 至 " + point.upper
                  }}
                </td>
                <td>
                  {{ point.qualified ? "已核对原件字段" : "未满足计算条件" }}
                </td>
                <td>
                  <button
                    class="text-button"
                    :disabled="Boolean(reading)"
                    @click="read(point.evidence_key)"
                  >
                    读取原件
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="series.descriptive_slope !== null" class="muted">
          同口径资料的描述性斜率：{{
            series.descriptive_slope
          }}。它不是因果判断或未来预测。
        </p>
        <details>
          <summary>对象和数值口径</summary>
          <pre>{{
            JSON.stringify(
              { subject: series.subject, qualification: series.qualification },
              null,
              2,
            )
          }}</pre>
        </details>
      </article>
    </section>

    <section class="engine-panel">
      <h2>留给研究人员的记录</h2>
      <div class="topic-draft">{{ evaluation.draft }}</div>
      <div v-if="evaluation.next_questions.length">
        <h3>下一步要核实</h3>
        <ul>
          <li v-for="question in evaluation.next_questions" :key="question">
            {{ question }}
          </li>
        </ul>
      </div>
      <details
        v-if="
          evaluation.data_gaps.length || evaluation.coverage.limitations.length
        "
      >
        <summary>资料缺口与计算限制</summary>
        <ul>
          <li
            v-for="gap in [
              ...new Set([
                ...evaluation.data_gaps,
                ...evaluation.coverage.limitations,
              ]),
            ]"
            :key="gap"
          >
            {{ topicGapLabel(gap) }}
          </li>
        </ul>
      </details>
      <ElButton :loading="downloading" @click="download"
        >下载本版研究记录</ElButton
      >
    </section>

    <section class="engine-panel">
      <h2>依据与原件</h2>
      <p class="muted">
        点击读取本版观察实际采用的原件。原件及字段匹配不代表专业结论已经得到确认。
      </p>
      <ol class="topic-evidence-list">
        <li v-for="item in evaluation.evidence" :key="item.key">
          <span
            >{{ item.source_site || item.source_id }} ·
            {{ item.source_record_id }}</span
          >
          <button
            class="text-button"
            :disabled="Boolean(reading)"
            @click="read(item.key)"
          >
            {{ reading === item.key ? "读取中…" : "读取固定原件" }}
          </button>
        </li>
      </ol>
      <p v-if="evaluation.evidence_truncated" class="notice">
        页面仅显示部分原件索引，不能把显示条数当成本版全部依据。
      </p>
      <section v-if="original" class="topic-original">
        <h3>固定原件</h3>
        <pre>{{ originalText(original) }}</pre>
      </section>
      <details>
        <summary>本版计算信息</summary>
        <p>
          计算版本 {{ evaluation.compute_version }}；证据采用版本
          {{ evaluation.observation_revision }}。
        </p>
        <pre>{{
          JSON.stringify(evaluation.coverage.source_watermarks, null, 2)
        }}</pre>
      </details>
    </section>
  </div>
</template>

<style scoped>
.topic-evaluation {
  min-width: 0;
}
.topic-evaluation h3 {
  font-size: 15px;
  margin-top: 14px;
}
.topic-summary {
  font-size: 16px;
  line-height: 1.85;
}
.coverage-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 18px;
  margin: 23px 0;
}
.coverage-grid dt {
  color: #75806f;
  font-size: 12px;
}
.coverage-grid dd {
  margin: 8px 0 0;
  font-size: 17px;
  color: #375b42;
}
.topic-signal {
  border-top: 1px solid #dfe7da;
  padding: 18px 0;
}
.topic-signal p,
li,
.topic-draft {
  font-size: 13px;
  line-height: 1.9;
  overflow-wrap: anywhere;
}
.topic-followup {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  align-items: center;
  font-size: 13px;
  padding-top: 15px;
}
.topic-table-wrap {
  overflow: auto;
  max-width: 100%;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
th,
td {
  padding: 10px 12px;
  text-align: left;
  vertical-align: top;
  border-bottom: 1px solid #e1e8db;
  overflow-wrap: anywhere;
}
th {
  color: #708068;
  font-weight: 500;
}
.topic-series {
  border-top: 1px solid #dfe7da;
  padding: 10px 0;
}
details {
  margin: 15px 0;
  font-size: 12px;
}
summary {
  cursor: pointer;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  overflow: auto;
  max-height: 360px;
  font-size: 12px;
  line-height: 1.8;
  padding: 14px;
  border-radius: 5px;
  background: #f0f4ec;
}
.topic-draft {
  white-space: pre-wrap;
  margin: 18px 0;
}
.topic-evidence-list {
  padding-left: 22px;
}
.topic-evidence-list li {
  margin: 12px 0;
}
.topic-evidence-list span {
  margin-right: 14px;
}
.text-button {
  padding: 0;
  background: none;
  border: 0;
  color: #3b6548;
  font: inherit;
  cursor: pointer;
  text-decoration: underline;
}
.text-button:disabled {
  opacity: 0.5;
}
@media (max-width: 650px) {
  .coverage-grid {
    grid-template-columns: 1fr 1fr;
  }
  .section-heading {
    align-items: start;
    flex-wrap: wrap;
  }
}
</style>
