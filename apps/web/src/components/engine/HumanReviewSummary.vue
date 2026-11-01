<script setup lang="ts">
import { computed } from "vue";
import type { InvestigationEntry } from "../../api/engine";
import type { ResearchAnalysisView } from "../../api/research";
import { object, reviewLabels } from "../../api/humanReview";
import AssessmentAxesPanel from "./AssessmentAxesPanel.vue";
const props = defineProps<{ analysis: ResearchAnalysisView; entries: InvestigationEntry[] }>();
const context = computed(() => props.analysis.human_review_context);
const fixed = computed(() => (context.value?.reviews ?? []).map(object));
const matched = computed(() => fixed.value.filter(entry => entry.binding_status === "exact_target"));
const generalCount = computed(() => context.value?.investigation_only_judgment_count ?? props.entries.filter(entry => entry.payload.kind === "judgment" && !object(entry.payload).review).length);
function target(entry: Record<string, unknown>) { return object(entry.target); }
</script>
<template>
  <section class="notice" aria-label="固定对象人工核实记录">
    <h4>人工核实与判断</h4>
    <p v-if="!matched.length">尚无针对本稿固定对象的人工核实记录，待人工核实。</p>
    <p v-if="generalCount > 0">调查中已记录一般人工判断；调查级自由判断不表示逐字批准本稿。</p>
    <article v-for="entry in fixed" :key="String(entry.entry_id)">
      <strong>{{ reviewLabels[String(entry.decision)] || entry.decision }} · {{ entry.binding_status === 'exact_target' ? '匹配本版固定对象' : entry.binding_status === 'target_changed' ? '目标已变化，旧审不覆盖本版' : '其他对象的判断，不覆盖本稿' }}</strong>
      <p>{{ entry.reviewer_actor }} · {{ entry.reviewed_at }} · 第 {{ entry.review_revision }} 版</p>
      <p>适用范围：{{ entry.scope }}</p><p>{{ entry.text }}</p>
      <p>条件与下一步：{{ entry.conditions || '未记录' }}</p>
      <details><summary>固定对象及依据</summary><p>对象类型：{{ target(entry).type }}；分析／候选：{{ target(entry).analysis_id || target(entry).candidate_key }}；命题：{{ target(entry).claim_id || '整份对象' }}</p><p>固定内容哈希：{{ target(entry).text_sha256 || target(entry).claim_sha256 || target(entry).result_hash }}</p><p>依据原件版本：{{ entry.basis_record_refs }}</p></details>
      <AssessmentAxesPanel :axes="entry.assessment_axes" />
    </article>
    <p class="muted">人工决定仅覆盖所列对象、固定版和范围，不改变全文核验或片段复核结果，不是对外警报签发；未证实且需要优先核实的信号仍待人工核实。</p>
  </section>
</template>
