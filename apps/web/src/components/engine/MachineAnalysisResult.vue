<script setup lang="ts">
import { computed } from "vue";
import { ElButton, ElTag } from "element-plus";
import type { ResearchAnalysisView } from "../../api/research";

type View = ResearchAnalysisView;
const props = defineProps<{ analysis: View }>();
const genericReviewRan = computed(() => props.analysis.calls.some(call => ["check", "recheck", "final_check"].includes(call.stage) && call.status === "received"));
const emit = defineEmits<{ source: [key: string] }>();
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
      <h3>仍值得补查的问题</h3>
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
</style>
