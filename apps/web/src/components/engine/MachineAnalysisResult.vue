<script setup lang="ts">
import { ElButton, ElTag } from "element-plus";
import type { components } from "../../api/generated/engine";

type View = components["schemas"]["AnalysisView"];
defineProps<{ analysis: View }>();
const emit = defineEmits<{ source: [key: string] }>();
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
    <p class="notice">
      这是模型根据本次资料形成的分析草稿，尚未经过专业审核。下方“研究人员判断”单独保存。
    </p>
    <p v-if="analysis.result.review.status === 'needs_review'" class="error-panel" role="alert">
      通用复核仍有待核问题。请查看下方逐项复核，以下草稿不能直接作为已核实结论。
    </p>
    <p v-if="analysis.state === 'needs_review' && analysis.result.review.status === 'passed'" class="notice" role="status">
      当前稿已完成通用复核，但本次流程仍有未完成事项。请查看版本比较和运行记录中的原因。
    </p>
    <h3>对研究问题的回答</h3>
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
      当前稿的通用复核不涵盖这段版本比较，仍需结合原件核对。旧稿不会因为被用于比较而获得认可。
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
    <details :open="analysis.result.review.status === 'needs_review'">
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
    <ul v-if="analysis.result.limitations.length" class="muted">
      <li v-for="item in analysis.result.limitations" :key="item">{{ item }}</li>
    </ul>
  </div>
</template>

<style scoped>
.machine-result { overflow-wrap: anywhere; }
.machine-result h3 { margin: 1.4rem 0 .6rem; }
.machine-result h4 { margin: .9rem 0 .3rem; }
.machine-result li { margin: .4rem 0; line-height: 1.65; }
.analysis-citation { margin: .8rem 0; padding-left: .8rem; border-left: 3px solid var(--el-border-color); }
.analysis-citation blockquote { margin: .5rem 0; white-space: pre-wrap; line-height: 1.65; }
.machine-result details { margin: 1rem 0; }
</style>
