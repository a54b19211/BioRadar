<script setup lang="ts">
import { computed } from "vue";
import { ElButton, ElTag } from "element-plus";
import type { ResearchAnalysisView } from "../../api/research";

type View = ResearchAnalysisView;
const props = defineProps<{ analysis: View }>();
const genericReviewRan = computed(() => props.analysis.calls.some(call => ["check", "recheck", "final_check"].includes(call.stage) && call.status === "received"));
const emit = defineEmits<{ source: [key: string] }>();
function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function list(value: unknown): unknown[] { return Array.isArray(value) ? value : []; }
function text(value: unknown) { return typeof value === "string" ? value : ""; }
const savedReport = computed(() => props.analysis.result?.report);
const feedbackRounds = computed(() => list(savedReport.value?.rounds).map(object));
const savedFeedback = computed(() => {
  const direct = object(savedReport.value?.quality_feedback);
  if (Object.keys(direct).length) return direct;
  const digest = savedReport.value?.text_sha256;
  return [...feedbackRounds.value].reverse().map(round => object(round.quality_feedback))
    .find(feedback => digest && feedback.text_sha256 === digest) ?? {};
});
const hasFeedback = computed(() => Object.keys(savedFeedback.value).length > 0);
const feedbackBinding = computed(() => {
  const digest = text(savedFeedback.value.text_sha256);
  return !digest || !savedReport.value?.text_sha256 ? "unrecorded"
    : digest === savedReport.value.text_sha256 ? "current" : "earlier";
});
const feedbackGaps = computed(() => list(savedFeedback.value.evidence_gaps));
const repairUnits = computed(() => list(savedFeedback.value.repair_units).filter((value): value is string => typeof value === "string"));
const reviewRequired = computed(() => list(savedFeedback.value.review_required));
const unsearchedGaps = computed(() => {
  if (Array.isArray(savedReport.value?.unsearched_gaps)) return savedReport.value.unsearched_gaps;
  const digest = savedReport.value?.text_sha256;
  const current = [...feedbackRounds.value].reverse().find(round =>
    digest && (object(round.quality_feedback).text_sha256 === digest || object(round.report).text_sha256 === digest));
  return list(current?.unsearched_gaps);
});
const separateUnsearched = computed(() => JSON.stringify(unsearchedGaps.value) !== JSON.stringify(feedbackGaps.value));
const feedbackReasons: Record<string, string> = {
  budget_or_review_required: "预算条件不足或仍有需核对事项；回执未进一步拆分原因。",
  quality_feedback_round_limit: "已达到核验后补查轮次上限。",
  baseline_fixed_sequence: "本版采用固定查询顺序，核验反馈未开启额外补查。",
  no_new_material_after_actual_queries: "实际补查后没有取得可用于新版成稿的新资料或原文选择。",
  analysis_model_budget: "模型调用次数预算已用尽。",
  analysis_token_budget: "模型 token 预算不足。",
  analysis_time_budget: "本轮时间预算已用尽。",
  analysis_query_budget: "查询次数预算已用尽。",
  analysis_tool_budget: "来源请求预算已用尽。",
  model_input_budget: "输入超过限制，请求未发送。",
};
const feedbackKinds: Record<string, string> = {
  evidence_selection: "需重选或补查原文",
  missing_support: "缺少支持",
  material_absence: "已读材料未提供",
};
const feedbackActions: Record<string, string> = {
  reselect_or_retrieve: "重选原文或补查",
  check_existing_then_retrieve: "先检查已读原文，再决定补查",
  assess_need_for_retrieval: "评估是否需要补查",
  retrieve: "补查资料",
};
const roundStates: Record<string, string> = {
  checked: "已完成本轮处理", running: "处理中", stopped_budget: "预算中止，保留此前正文",
  not_changed: "未形成修改稿", verification_needs_review: "修改稿核验仍待核对",
};
function feedbackReason(value: unknown) { return feedbackReasons[text(value)] || text(value); }
function feedbackText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "未记录具体原因";
  if (Array.isArray(value)) return value.map(feedbackText).join("；");
  if (typeof value !== "object") return String(value);
  const labels: Record<string, string> = { item: "待补内容", material: "材料", reason: "原因", error_code: "错误", source_id: "来源" };
  return Object.entries(object(value)).map(([key, item]) => (labels[key] || key) + "：" + feedbackText(item)).join("；");
}
function feedbackLocation(value: unknown) {
  const item = object(value);
  return [text(item.unit_id), typeof item.claim_index === "number" ? "陈述 " + (item.claim_index + 1) : "",
    feedbackKinds[text(item.kind)] || text(item.kind)].filter(Boolean).join(" · ");
}
function feedbackSpan(value: unknown) { return text(object(value).span); }
function feedbackItemReason(value: unknown) {
  const item = object(value);
  return feedbackText(item.reason ?? value);
}
function feedbackAction(value: unknown) {
  const action = text(object(value).action);
  return feedbackActions[action] || action;
}
const reportStatus: Record<string, string> = {
  PASS: "BioCheck 原核验通过", REVIEW: "报告待核", FAIL: "报告核验未通过",
  UNAVAILABLE: "核验服务暂不可用", FAILED: "报告核验未完成",
};
const supportAudit = computed(() => props.analysis.result?.report?.support_audit);
const supportAuditStatus = computed(() => supportAudit.value?.status.toUpperCase() || "NOT_RUN");
const auditLabels: Record<string, string> = {
  PASS: "所列片段支持复核通过", REVIEW: "所选支持片段待核", NOT_RUN: "所选片段复核未执行",
};
const supportVerdicts: Record<string, string> = {
  supported: "所选片段支持", not_supported: "所选片段不支持", uncertain: "支持关系待核",
};
const mechanismStatus: Record<string, string> = {
  supported: "所列机制证据支持", partial: "机制链仅部分支持", insufficient: "机制证据不足",
  not_requested: "本版未请求", unavailable: "评价方法暂不可用",
  needs_input: "评价仍缺输入", failed: "本次评价未完成", completed: "已完成机制评价",
  skipped_budget: "预算不足，本轮未执行",
};
const claimStatus: Record<string, string> = {
  supported: "所列依据支持", contradicted: "所列依据反对",
  mixed: "依据存在分歧", insufficient: "依据不足",
};
const relation: Record<string, string> = {
  supports: "支持", refutes: "反对", limits: "限定条件", context: "背景",
};
const updateStatus: Record<string, string> = {
  initial: "首次分析", maintained: "维持原分析", revised: "修订原分析",
  unable_to_compare: "暂不能比较",
};
function findingLocation(item: { path?: string | null; section?: string | null; claim_id?: string | null }) {
  const path = item.section ? "/" + item.section : (item.path || "");
  const labels: [string, string][] = [
    ["/conditional_answer/unknowns", "尚未确定的事项"],
    ["/conditional_answer/conditions", "成立条件"],
    ["/conditional_answer", "回答正文"],
    ["/gaps", "待查问题"],
    ["/update", "前后版本比较"],
    ["/explanations", "其他解释"],
    ["/claims", "具体陈述与依据"],
  ];
  const label = labels.find(([prefix]) => path === prefix || path.startsWith(prefix + "/"))?.[1] || "分析内容";
  return item.claim_id ? label + " · " + item.claim_id : label;
}
const reviewStatus: Record<string, string> = {
  grounded: "未发现明显引用问题", overstated: "表述过度",
  unsupported: "引用未支持", uncertain: "仍需核对",
};
</script>

<template>
  <div v-if="analysis.result" class="machine-result">
    <section v-if="analysis.result.report" class="research-report">
      <div class="candidate-meta">
        <h3>研究报告</h3>
        <ElTag :type="analysis.result.report.status.toUpperCase() === 'PASS' ? 'success' : 'warning'">
          {{ reportStatus[analysis.result.report.status.toUpperCase()] || analysis.result.report.status }}
        </ElTag>
      </div>
      <div v-if="analysis.result.report.final_text" class="report-document">{{ analysis.result.report.final_text }}</div>
      <p v-else class="notice">本版尚未取得可阅读的报告正文，请查看核验与执行记录。</p>
      <p class="muted">{{ analysis.result.report.provider || "报告生成服务" }} ·
        {{ analysis.result.report.status.toUpperCase() === "PASS" ? "原报告核验已通过；所选片段复核另见下方。" : "原报告核验尚未通过，当前正文已保留供检查。" }}
      </p>
      <details v-if="analysis.result.report.verification">
        <summary>原报告核验结果</summary>
        <pre class="source-text">{{ JSON.stringify(analysis.result.report.verification, null, 2) }}</pre>
      </details>
      <div class="support-audit">
        <div class="candidate-meta">
          <h4>所选支持片段复核</h4>
          <ElTag :type="supportAuditStatus === 'PASS' ? 'success' : 'warning'">
            {{ auditLabels[supportAuditStatus] || "片段复核结果待确认" }}
          </ElTag>
        </div>
        <p v-if="supportAudit?.not_run_reason || supportAuditStatus === 'NOT_RUN'" class="muted">
          {{ supportAudit?.not_run_reason || supportAudit?.reason || "本版未保存所选片段复核结果。" }}
        </p>
        <p v-if="supportAudit?.scope" class="muted">{{ supportAudit.scope }}</p>
        <p v-if="supportAudit?.verification_errors?.length" class="notice">片段复核的定位或回执仍有问题，具体原因见完整记录。</p>
        <details v-for="check in supportAudit?.checks || []" :key="check.unit_id + ':' + check.claim_index"
          :open="check.verdict !== 'supported'">
          <summary>{{ check.unit_id }} · 陈述 {{ check.claim_index + 1 }} · {{ supportVerdicts[check.verdict] || "支持关系待核" }}</summary>
          <p class="investigation-prose">{{ check.span }}</p>
          <p>{{ check.reason }}</p>
          <div v-for="(reference, index) in check.evidence || []" :key="index" class="analysis-citation">
            <p>{{ reference.evidence_id }} · {{ reference.segment_id }}
              <small v-if="reference.start != null && reference.end != null" class="muted"> · 位置 {{ reference.start }}–{{ reference.end }}</small>
            </p>
            <blockquote v-if="reference.quote">{{ reference.quote }}</blockquote>
            <ElButton class="no-print" text type="primary" @click="emit('source', reference.evidence_id)">阅读固定原文</ElButton>
          </div>
        </details>
        <details v-if="supportAudit">
          <summary>所选片段复核完整记录</summary>
          <pre class="source-text">{{ JSON.stringify(supportAudit, null, 2) }}</pre>
        </details>
      </div>
      <section class="report-feedback" aria-label="核验后反馈与未补齐问题">
        <h4>核验后反馈与未补齐问题</h4>
        <p class="muted">以下为机器核验反馈，须回原件核对；问题清单不等于事实、修订完成或证据覆盖已满足。</p>
        <template v-if="hasFeedback">
          <p v-if="feedbackBinding === 'current'" class="muted">反馈对应当前保存正文，已按正文哈希核对。</p>
          <p v-else-if="feedbackBinding === 'earlier'" class="notice">这份反馈对应此前稿件，不用于判定当前正文；原反馈保留供核对。</p>
          <p v-else class="notice">反馈与当前正文的对应哈希未记录，不能确认适用于当前稿。</p>
          <p class="feedback-counts">核验指出需补查或重选原文 {{ feedbackGaps.length }} 项；
            可修订单元 {{ repairUnits.length }} 项；需先核对的问题 {{ reviewRequired.length }} 项。
          </p>
          <p v-if="savedFeedback.error_code" class="notice">反馈处理状态：{{ feedbackReason(savedFeedback.error_code) }}</p>
          <template v-if="feedbackGaps.length">
            <h5>核验后提出的补查与原文选择问题</h5>
            <ol class="feedback-items">
              <li v-for="(item, index) in feedbackGaps" :key="index">
                <p v-if="feedbackLocation(item)"><strong>{{ feedbackLocation(item) }}</strong></p>
                <blockquote v-if="feedbackSpan(item)">{{ feedbackSpan(item) }}</blockquote>
                <p>{{ feedbackItemReason(item) }}</p>
                <p v-if="feedbackAction(item)" class="muted">建议处理：{{ feedbackAction(item) }}</p>
              </li>
            </ol>
          </template>
          <p v-if="repairUnits.length">可修订单元：{{ repairUnits.join("、") }}。这是处理建议，不代表已修改。</p>
          <template v-if="reviewRequired.length">
            <h5>需先核对的问题</h5>
            <ul><li v-for="(item, index) in reviewRequired" :key="index">{{ feedbackItemReason(item) }}</li></ul>
          </template>
          <p v-if="unsearchedGaps.length" class="notice"><strong>仍未补齐：{{ unsearchedGaps.length }} 项。</strong>
            <template v-if="!separateUnsearched">具体问题见上方核验反馈。</template>
          </p>
          <ul v-if="unsearchedGaps.length && separateUnsearched">
            <li v-for="(item, index) in unsearchedGaps" :key="index">
              {{ feedbackLocation(item) }}：{{ feedbackItemReason(item) }}
            </li>
          </ul>
        </template>
        <p v-else class="muted">本版未保存核验后补查与修订反馈，不能从缺少记录推断问题已经解决。</p>
        <p v-if="savedReport?.feedback_stop_reason">补查停止原因：{{ feedbackReason(savedReport.feedback_stop_reason) }}</p>
        <p v-if="savedReport?.repair_stop_reason">修订停止原因：{{ feedbackReason(savedReport.repair_stop_reason) }}</p>
        <div v-if="feedbackRounds.length" class="feedback-rounds">
          <h5>各轮处理与停止原因</h5>
          <article v-for="(round, index) in feedbackRounds" :key="index" class="feedback-round">
            <p><strong>第 {{ typeof round.index === "number" ? round.index + 1 : index + 1 }} 轮 ·
              {{ round.kind === "repair" ? "局部修订" : round.kind === "generation" ? "成稿" : round.kind }}</strong>
              · {{ roundStates[text(round.status)] || text(round.status) }}
            </p>
            <p>反馈缺口 {{ list(object(round.quality_feedback).evidence_gaps).length }} 项；
              保存的未补齐清单 {{ list(round.unsearched_gaps).length }} 项。</p>
            <p v-if="round.feedback_stop_reason"><strong>补查停止原因：</strong>{{ feedbackReason(round.feedback_stop_reason) }}</p>
            <p v-if="round.repair_stop_reason"><strong>修订停止原因：</strong>{{ feedbackReason(round.repair_stop_reason) }}</p>
            <p v-if="round.error_code" class="notice">处理未完成：{{ feedbackReason(round.error_code) }}</p>
            <details v-if="list(round.unsearched_gaps).length" class="no-print">
              <summary>该轮保存的未补齐问题</summary>
              <ul><li v-for="(item, itemIndex) in list(round.unsearched_gaps)" :key="itemIndex">
                {{ feedbackLocation(item) }}：{{ feedbackItemReason(item) }}
              </li></ul>
            </details>
          </article>
        </div>
        <p v-else-if="hasFeedback && !savedReport?.feedback_stop_reason && !savedReport?.repair_stop_reason" class="muted">
          本版未保存核验后停止处理的独立原因，不能仅从反馈推断已补查或已修订。
        </p>
        <details v-if="hasFeedback" class="no-print">
          <summary>核验后完整反馈回执</summary><pre class="source-text">{{ JSON.stringify(savedFeedback, null, 2) }}</pre>
        </details>
      </section>
    </section>
    <section v-if="analysis.result.mechanism_evaluation && analysis.result.mechanism_evaluation.status !== 'not_requested'" class="mechanism-summary">
      <h3>机制证据评价</h3>
      <ElTag>{{ mechanismStatus[analysis.result.mechanism_evaluation.overall?.verdict || analysis.result.mechanism_evaluation.status] || analysis.result.mechanism_evaluation.status }}</ElTag>
      <p v-if="analysis.result.mechanism_evaluation.status === 'skipped_budget'" class="muted">本轮预算优先用于最终报告，机制评价尚未执行。</p>
      <p v-if="analysis.result.mechanism_evaluation.overall?.rationale" class="investigation-prose">{{ analysis.result.mechanism_evaluation.overall.rationale }}</p>
      <p v-if="analysis.result.mechanism_evaluation.overall?.has_comparable_conflict" class="notice">所列材料存在可比较的证据冲突。</p>
      <p v-if="analysis.result.mechanism_evaluation.coverage?.status === 'partial'" class="notice">本次评价覆盖部分材料，以下结论须结合未纳入材料的缺口阅读。</p>
      <p v-if="analysis.result.mechanism_evaluation.missing_fields?.length" class="muted">仍缺输入：{{ analysis.result.mechanism_evaluation.missing_fields.join("、") }}</p>
      <p v-if="analysis.result.mechanism_evaluation.independence?.note" class="muted">{{ analysis.result.mechanism_evaluation.independence.note }}</p>
      <details v-if="analysis.result.mechanism_evaluation.critical_gaps?.length">
        <summary>机制链关键缺口</summary>
        <article v-for="(gap, index) in analysis.result.mechanism_evaluation.critical_gaps" :key="index" class="investigation-entry">
          <p>{{ gap.reason }}</p><p v-if="gap.missing_experiment"><strong>需要补查或验证：</strong>{{ gap.missing_experiment }}</p>
        </article>
      </details>
      <details><summary>本次机制评价完整记录</summary><pre class="source-text">{{ JSON.stringify(analysis.result.mechanism_evaluation, null, 2) }}</pre></details>
    </section>
    <details class="structured-analysis" :open="!analysis.result.report">
      <summary>{{ analysis.result.report ? "查看报告生成前的结构化分析与引文记录" : "查看本版分析与原文依据" }}</summary>
      <p class="muted">{{ analysis.result.report ? "这部分保留报告生成前的分析；上方核验状态对应上方报告正文。" : "本版分析与证据记录，研究人员判断另存。" }}</p>
    <p v-if="genericReviewRan && analysis.result.review.status === 'needs_review'" class="error-panel" role="alert">
      通用复核仍有待核问题。请查看下方逐项复核，以下草稿不能直接作为已核实结论。
    </p>
    <p v-if="genericReviewRan && analysis.state === 'needs_review' && analysis.result.review.status === 'passed'" class="notice" role="status">
      当前稿已完成通用复核，但本次流程仍有未完成事项。请查看版本比较和运行记录中的原因。
    </p>
    <p v-if="!genericReviewRan" class="muted">前期结构化分析未单独通用核验<template v-if="analysis.result.report">，最终正文核验见上方</template>。</p>
    <h3>{{ analysis.result.report ? "分析摘要" : "对研究问题的回答" }}</h3>
    <p class="investigation-prose">{{ analysis.result.conditional_answer.answer }}</p>
    <template v-if="analysis.result.conditional_answer.conditions.length">
      <h4>成立条件</h4>
      <ul><li v-for="item in analysis.result.conditional_answer.conditions" :key="item">{{ item }}</li></ul>
    </template>
    <template v-if="analysis.result.conditional_answer.unknowns.length">
      <h4>目前不能确定</h4>
      <ul><li v-for="item in analysis.result.conditional_answer.unknowns" :key="item">{{ item }}</li></ul>
    </template>

    <h3>逐条核对陈述与依据</h3>
    <article v-for="claim in analysis.result.claims" :key="claim.id" class="investigation-entry">
      <div class="candidate-meta">
        <ElTag>{{ claim.kind === "source_statement" ? "原文陈述" : "模型推断" }}</ElTag>
        <span>{{ claimStatus[claim.status] }} · {{ claim.id }}</span>
      </div>
      <p class="investigation-prose">{{ claim.text }}</p>
      <p><strong>适用范围：</strong>{{ claim.scope }}</p>
      <ul v-if="claim.conditions.length"><li v-for="condition in claim.conditions" :key="condition">{{ condition }}</li></ul>
      <div v-for="(link, index) in claim.evidence_links" :key="index" class="analysis-citation">
        <p><strong>{{ relation[link.role] }}：</strong>{{ link.reason }}</p>
        <blockquote>{{ link.quote }}</blockquote>
        <ElButton class="no-print" text type="primary" @click="emit('source', link.evidence_key)">
          阅读 {{ link.evidence_key }} 的固定原文
        </ElButton>
      </div>
      <p v-if="!claim.evidence_links.length" class="muted">没有可绑定的原文引用，不能据此视为已证实。</p>
    </article>

    <template v-if="analysis.result.explanations.length">
      <h3>需要比较的解释</h3>
      <article v-for="item in analysis.result.explanations" :key="item.id" class="investigation-entry">
        <p>{{ item.text }}</p>
        <p class="muted">支持所列解释的命题：{{ item.for_claim_ids.join("、") || "未列出" }}；
          与之冲突的命题：{{ item.against_claim_ids.join("、") || "未列出" }}</p>
        <p><strong>还要区分什么：</strong>{{ item.discriminating_evidence_needed }}</p>
      </article>
    </template>

    <h3>为什么维持或修改上一版</h3>
    <p v-if="analysis.calls.some((call) => call.stage === 'compare')" class="muted">
      这段比较来自本版分析记录；最终报告中采用的比较文字以报告正文及其核验结果为准。
    </p>
    <p><ElTag>{{ updateStatus[analysis.result.update.stance] }}</ElTag></p>
    <ul><li v-for="reason in analysis.result.update.reasons" :key="reason">{{ reason }}</li></ul>
    <ul v-if="analysis.result.update.changed_claims.length">
      <li v-for="item in analysis.result.update.changed_claims" :key="item.claim_id">
        {{ item.claim_id }}<template v-if="item.previous_claim_id">（对应上一版 {{ item.previous_claim_id }}）</template>：{{ item.reason }}
      </li>
    </ul>

    <template v-if="analysis.result.gaps.length">
      <h3>{{ analysis.result.report ? "报告生成前的结构化分析缺口" : "结构化分析中仍待补查的问题" }}</h3>
      <article v-for="item in analysis.result.gaps" :key="item.id" class="investigation-entry">
        <p>{{ item.question }}</p><p class="muted">{{ item.why_it_matters }}</p>
      </article>
    </template>
    <details v-if="genericReviewRan" :open="analysis.result.review.status === 'needs_review'">
      <summary>通用模型复核：{{ analysis.result.review.status === "passed" ? "未标出明显问题" : "有问题待核对" }}</summary>
      <p class="muted">同一配置模型的另一次检查，尚未经过独立的专业证据评价和结论核验。</p>
      <ul>
        <li v-for="item in analysis.result.review.claims" :key="item.claim_id">
          {{ item.claim_id }} · {{ reviewStatus[item.verdict] }}<span v-if="item.issue">：{{ item.issue }}</span>
        </li>
      </ul>
      <ul v-if="analysis.result.review.findings?.length">
        <li v-for="item in analysis.result.review.findings" :key="(item.section || item.path || '') + (item.claim_id || '') + item.issue">
          {{ item.issue }} <small class="muted">（{{ findingLocation(item) }}）</small>
          <small v-if="!item.evidence_keys.length" class="muted"> 此问题未附原文依据，需对照材料核实，不能仅凭批评判定有错。</small>
        </li>
      </ul>
      <ul><li v-for="item in analysis.result.review.overall_limitations" :key="item">{{ item }}</li></ul>
    </details>
    </details>
    <details class="report-evidence-list">
      <summary>本版固定来源材料（{{ analysis.evidence.length }} 项）</summary>
      <article v-for="item in analysis.evidence" :key="item.key" class="investigation-entry">
        <p><strong>{{ item.key }}</strong> · {{ item.title }}</p>
        <p class="muted">{{ item.source_id }} · {{ item.publication_time || "原始日期未提供" }}</p>
        <ElButton class="no-print" text type="primary" @click="emit('source', item.key)">阅读本版原文</ElButton>
      </article>
    </details>
    <ul v-if="analysis.result.limitations.length" class="muted">
      <li v-for="item in analysis.result.limitations" :key="item">{{ item }}</li>
    </ul>
  </div>
</template>

<style scoped>
.machine-result { overflow-wrap: anywhere; }
.research-report { margin: 24px 0; }
.research-report h3 { margin: 0; }
.report-document { white-space: pre-wrap; font-size: 15px; line-height: 1.9; color: var(--el-text-color-primary); padding: 20px 0; }
.machine-result h3 { margin: 1.4rem 0 .6rem; }
.machine-result h4 { margin: .9rem 0 .3rem; }
.machine-result li { margin: .4rem 0; line-height: 1.65; }
.analysis-citation { margin: .8rem 0; padding-left: .8rem; border-left: 3px solid var(--el-border-color); }
.analysis-citation blockquote { margin: .5rem 0; white-space: pre-wrap; line-height: 1.65; }
.machine-result details { margin: 1rem 0; }
.report-feedback { margin-top: 22px; border-top: 1px solid var(--el-border-color); padding-top: 4px; }
.feedback-items { padding-left: 24px; }
.feedback-items blockquote { margin: .4rem 0; padding-left: .8rem; border-left: 3px solid var(--el-border-color); }
.feedback-items p, .feedback-round p { margin: .4rem 0; }
.feedback-round { padding: .3rem 0; border-top: 1px solid var(--el-border-color-lighter); }
</style>
