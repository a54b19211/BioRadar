<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElButton, ElDrawer, ElInput, ElTag } from "element-plus";
import {
  engineRequest,
  post,
  type Candidate,
  type NormalizedSnapshot,
  type FeedbackView,
} from "../../api/engine";
import { explainCode } from "../../api/engineLabels";
import SourceAttentionPanel from "./SourceAttentionPanel.vue";
import AssessmentAxesPanel from "./AssessmentAxesPanel.vue";
import { object, strings, readable, kddLabel, fieldLabel, methodLabel, text } from "../../api/kdd";
const props = defineProps<{
  candidate: Candidate | null;
  runId: string;
  resultHash: string;
  snapshotId: string;
  feedback?: FeedbackView[];
  patternEvaluation?: unknown;
  selectionDecision?: unknown;
  investigationAllowed?: boolean;
}>();
const emit = defineEmits<{
  close: [];
  feedbackSaved: [feedback: FeedbackView];
}>();
const snapshot = ref<NormalizedSnapshot | null>(null);
const error = ref("");
const message = ref("");
const busy = ref(false);
const selectedSource = ref(0);
const note = ref("");
const savedFeedback = ref<FeedbackView[]>([]);
const investigationTitle = ref("");
const investigationQuestion = ref("");
const investigationBusy = ref(false);
const investigationId = ref("");
const investigationError = ref("");
const decisionLabels: Record<string, string> = {
  relevant: "值得调查",
  irrelevant: "不相关",
  duplicate: "重复",
  needs_check: "需要补证",
};
let requestVersion = 0;
watch(
  () =>
    [props.candidate, props.runId, props.snapshotId, props.resultHash] as const,
  async ([value]) => {
    const version = ++requestVersion;
    snapshot.value = null;
    error.value = "";
    message.value = "";
    selectedSource.value = 0;
    note.value = "";
    investigationTitle.value = value?.title ?? "";
    investigationQuestion.value =
      value?.next_questions?.[0] ?? value?.title ?? "";
    investigationId.value = "";
    investigationError.value = "";
    savedFeedback.value = (props.feedback ?? []).filter(
      (f) => f.candidate_key === value?.candidate_key,
    );
    if (!value) return;
    try {
      const data = await engineRequest<{ snapshot: NormalizedSnapshot }>(
        `/snapshots/${encodeURIComponent(props.snapshotId)}`,
      );
      if (requestVersion === version) snapshot.value = data.snapshot;
    } catch (e) {
      if (requestVersion === version)
        error.value = e instanceof Error ? e.message : "读取资料失败";
    }
  },
);
const pattern = computed(() => object(props.patternEvaluation));
const selection = computed(() => object(props.selectionDecision));
const support = computed(() => props.candidate?.supports[selectedSource.value]);
const recordRefFields = [
  "provider",
  "object_type",
  "object_id",
  "adopted_version",
  "version_source",
  "content_hash",
  "authorization_ref",
] as const;
const record = computed(() =>
  snapshot.value?.records.find(
    (r) =>
      support.value &&
      recordRefFields.every(
        (key) => r.record_ref[key] === support.value?.record_ref[key],
      ),
  ),
);
const sourceLink = computed(() => {
  try {
    const url = new URL(support.value?.source_url ?? "");
    return ["https:", "http:"].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
});
async function feedback(
  decision: "relevant" | "irrelevant" | "duplicate" | "needs_check",
) {
  if (!props.candidate || busy.value) return;
  const version = requestVersion;
  busy.value = true;
  error.value = "";
  try {
    const saved = await post<FeedbackView>(
      `/runs/${encodeURIComponent(props.runId)}/feedback`,
      {
        candidate_key: props.candidate.candidate_key,
        result_hash: props.resultHash,
        decision,
        reason: note.value.trim() || "研究工作台人工标注；尚未完成专业核验。",
      },
    );
    emit("feedbackSaved", saved);
    if (requestVersion === version) {
      savedFeedback.value.push(saved);
      message.value = "标注已保存。原分析结果保留，标注不代表专业核验通过。";
    }
  } catch (e) {
    if (requestVersion === version)
      error.value = e instanceof Error ? e.message : "保存失败";
  } finally {
    busy.value = false;
  }
}
async function saveInvestigation() {
  const candidate = props.candidate;
  if (
    !candidate ||
    props.investigationAllowed === false ||
    investigationBusy.value ||
    !props.runId ||
    !props.resultHash ||
    !investigationTitle.value.trim() ||
    !investigationQuestion.value.trim()
  )
    return;
  const version = requestVersion;
  investigationBusy.value = true;
  investigationError.value = "";
  try {
    const saved = await post<{ id: string }>("/investigations", {
      title: investigationTitle.value.trim(),
      question: investigationQuestion.value.trim(),
      questions: candidate.next_questions ?? [],
      origin: {
        run_id: props.runId,
        result_hash: props.resultHash,
        candidate_key: candidate.candidate_key,
      },
      is_demo: false,
    });
    if (requestVersion === version) investigationId.value = saved.id;
  } catch (value) {
    if (requestVersion === version)
      investigationError.value =
        value instanceof Error
          ? value.message
          : "调查未能保存，请检查记录后重试。";
  } finally {
    investigationBusy.value = false;
  }
}
</script>
<template>
  <ElDrawer
    :model-value="!!candidate"
    title="线索依据与待核实问题"
    size="min(820px, 96vw)"
    @close="emit('close')"
  >
    <template v-if="candidate">
      <ElTag type="warning">待核实线索</ElTag>
      <h2 class="candidate-detail-title">{{ candidate.title }}</h2>
      <p class="muted">
        {{ methodLabel(candidate.method_id) }} · {{ candidate.method_version }}
        <span v-if="candidate.metrics?.kdd_direction"> · {{ kddLabel(String(candidate.metrics.kdd_direction)) }}</span>
      </p>
      <SourceAttentionPanel v-if="candidate.metrics?.attention" :attention="candidate.metrics?.attention" />
      <AssessmentAxesPanel :axes="candidate.assessment_axes" />
      <h3>为什么进入候选</h3>
      <ul class="evidence-list">
        <li v-for="reason in candidate.reasons" :key="reason">
          {{ explainCode(reason) }}
        </li>
      </ul>
      <section v-if="pattern || selection" class="pattern-evaluation">
        <h3>模式价值与本轮筛选</h3>
        <p v-if="selection"><strong>{{ kddLabel(text(selection.decision)) }}</strong>：{{ readable(selection.reasons) }}</p>
        <template v-if="pattern">
          <p>{{ pattern.eligibility_kind === "exploratory_content_followup" ? "仅进入探索性内容补证，未确认有价值模式" : pattern.eligible === true ? "通过本轮描述性筛选条件" : pattern.eligible === false ? "未满足本轮价值条件" : "价值资格未记录" }}；
            规则评分 {{ pattern.value_score === null ? "本轮未赋数值评分" : pattern.value_score ?? "未记录" }}。</p>
          <p v-if="pattern.qualification">{{ kddLabel(text(pattern.qualification)) }}</p>
          <ul><li v-for="reason in strings(pattern.value_reasons)" :key="reason">{{ kddLabel(reason) }}</li></ul>
          <dl class="metric-list">
            <dt>新颖性依据</dt><dd>{{ readable(pattern.novelty) }}</dd>
            <dt>效应与变化幅度</dt><dd>{{ readable(pattern.effect) }}</dd>
            <dt>不确定性</dt><dd>{{ readable(pattern.uncertainty) }}</dd>
            <dt>研究优先级</dt><dd>{{ readable(pattern.importance) }}</dd>
            <dt>核实紧急度</dt><dd>{{ readable(pattern.urgency) }}</dd>
          </dl>
          <p v-if="pattern.qualification">{{ readable(pattern.qualification) }}</p>
          <p v-if="Array.isArray(pattern.required_evidence) && pattern.required_evidence.length"><strong>补证要求：</strong>{{ readable(pattern.required_evidence) }}</p>
        </template>
        <p class="muted">规则评分用于本轮筛选，不表示统计显著性或科研结论成立。容量暂缓不等于价值条件未通过。</p>
      </section>
      <details class="detail-block">
        <summary>实际计算与统计口径</summary>
        <dl class="metric-list">
          <template v-for="(value, key) in candidate.metrics" :key="key"
            ><dt>{{ fieldLabel(String(key)) }}</dt>
            <dd>
              {{ readable(value) }}
            </dd></template
          >
        </dl>
      </details>
      <h3>贡献资料 · {{ candidate.supports.length }} 条引用</h3>
      <p class="muted">
        引用数量不代表独立研究数量；以下内容来自本次固定版本。
      </p>
      <div class="source-tabs">
        <ElButton
          v-for="(item, index) in candidate.supports"
          :key="index"
          :type="selectedSource === index ? 'primary' : 'default'"
          @click="selectedSource = index"
          >资料 {{ index + 1 }}</ElButton
        >
      </div>
      <section v-if="support" class="source-reading">
        <p class="source-id">
          {{ support.record_ref.provider }} / {{ support.record_ref.object_id }}
        </p>
        <blockquote v-if="support.quote">{{ support.quote }}</blockquote>
        <p v-else class="muted">此项为结构化记录，请结合计算字段核实。</p>
        <p class="muted">采用版本：{{ support.record_ref.adopted_version }}</p>
        <p class="muted">
          引用状态：{{ support.quote_status }} · {{ support.locator.type }}
        </p>
        <dl v-if="record" class="metric-list provenance-list">
          <dt>来源日期字段</dt>
          <dd>
            {{ record.published_time_raw ?? "未知" }}（{{
              record.time_precision
            }}；{{
              record.publication_time_verified
                ? "公开时间含义已确认"
                : "含义未确认，不作为公开时间"
            }}）
          </dd>
          <dt>本次观察时间</dt>
          <dd>{{ record.observed_at }}</dd>
          <dt>上游首次可见</dt>
          <dd>{{ record.available_at ?? "未提供" }}</dd>
          <dt>资料处理状态</dt>
          <dd>
            {{ record.upstream_status?.extraction_status ?? "未提供抽取状态" }}
          </dd>
          <dt>研究独立性</dt>
          <dd>
            {{
              record.study_independence_status === "confirmed"
                ? "上游已给出确认研究簇"
                : "未确认，不能把引用数量当独立研究数量"
            }}
          </dd>
          <dt>文本规范化</dt>
          <dd>{{ record.normalization_version ?? "未知" }}</dd>
          <dt>引用在采用文本中的位置</dt>
          <dd>
            {{ support.normalized_start ?? "未提供" }}—{{
              support.normalized_end ?? "未提供"
            }}
          </dd>
          <dt>源定位</dt>
          <dd>
            {{ support.locator.type }} ·
            {{
              support.locator.start ??
              support.locator.paragraph_id ??
              support.locator.page ??
              support.locator.reason ??
              "未提供"
            }}{{ support.locator.end != null ? "—" + support.locator.end : "" }}
          </dd>
        </dl>
        <p v-else-if="snapshot" class="error-inline">
          此引用未与固定资料的完整版本匹配，无法提供正文预览。
        </p>
        <details v-if="record?.normalized_text">
          <summary>查看固定采用文本</summary>
          <pre class="source-text">{{ record.normalized_text }}</pre>
        </details>
        <a
          v-if="sourceLink"
          :href="sourceLink"
          target="_blank"
          rel="noopener noreferrer"
          class="text-link"
          >查看来源网页 ↗</a
        >
        <p v-if="!sourceLink" class="muted">
          本次资料未提供原站链接；可查看上面的固定采用文本。
        </p>
      </section>
      <h3>还缺什么</h3>
      <ul class="evidence-list">
        <li v-for="gap in candidate.data_gaps" :key="gap">
          {{ explainCode(gap) }}
        </li>
        <li v-if="!candidate.data_gaps?.length">
          请按下列问题核实线索；算法结果尚未经专业审阅。
        </li>
      </ul>
      <h3>下一步调查</h3>
      <ul class="evidence-list">
        <li v-for="question in candidate.next_questions" :key="question">
          {{ question }}
        </li>
      </ul>
      <ul class="evidence-list muted">
        <li
          v-for="requirement in candidate.next_data_requirements"
          :key="requirement"
        >
          {{ requirement }}
        </li>
      </ul>
      <p class="muted">保存调查后可继续补证，按问题使用适用的专业评价，再查看报告、停止原因和版本更新；以该调查的实际执行记录为准。</p>
      <p v-if="investigationAllowed === false" class="notice">这条算法候选未进入本轮审阅队列，可独立核对依据并留下意见。创建调查仅开放给本轮已选候选，未入选不代表没有价值。</p>
      <section v-else class="investigation-create">
        <h3>保存为调查</h3>
        <label for="candidate-investigation-title">调查标题</label>
        <ElInput
          id="candidate-investigation-title"
          v-model="investigationTitle"
          maxlength="200"
        />
        <label for="candidate-investigation-question">要查清楚的问题</label>
        <ElInput
          id="candidate-investigation-question"
          v-model="investigationQuestion"
          type="textarea"
          :rows="3"
          maxlength="2000"
        />
        <p class="muted">
          保存本条候选、原分析版本和待查问题；不会自动调用检索或作专业评价。
        </p>
        <ElButton
          type="primary"
          :loading="investigationBusy"
          :disabled="
            !!investigationId ||
            !investigationTitle.trim() ||
            !investigationQuestion.trim() ||
            !runId ||
            !resultHash
          "
          @click="saveInvestigation"
          >保存调查</ElButton
        >
        <p v-if="investigationId" class="success-inline" role="status">
          调查已保存。
          <RouterLink
            :to="{ path: '/investigations', query: { id: investigationId } }"
            class="text-link"
            >打开调查 →</RouterLink
          >
        </p>
        <p v-if="investigationError" class="error-inline" role="alert">
          {{ investigationError }}
        </p>
      </section>
      <h3>对这条候选留下意见</h3>
      <ElInput
        v-model="note"
        type="textarea"
        :rows="2"
        maxlength="2000"
        placeholder="补充判断理由或待核实事项（可选）"
        aria-label="标注理由"
      />
      <div class="feedback-actions">
        <ElButton :disabled="busy" @click="feedback('relevant')"
          >值得继续调查</ElButton
        ><ElButton :disabled="busy" @click="feedback('needs_check')"
          >需要补证</ElButton
        ><ElButton :disabled="busy" @click="feedback('irrelevant')"
          >不相关</ElButton
        ><ElButton :disabled="busy" @click="feedback('duplicate')"
          >重复线索</ElButton
        >
      </div>
      <ul v-if="savedFeedback.length" class="evidence-list muted">
        <li v-for="entry in savedFeedback" :key="entry.id">
          {{ decisionLabels[entry.decision] ?? entry.decision }} ·
          {{ entry.reason }}
        </li>
      </ul>
      <p v-if="message" class="success-inline" role="status">{{ message }}</p>
      <p v-if="error" class="error-inline" role="alert">{{ error }}</p>
    </template>
  </ElDrawer>
</template>
