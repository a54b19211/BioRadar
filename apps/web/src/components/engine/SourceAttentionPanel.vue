<script setup lang="ts">
import { computed } from "vue";
import { strings, text } from "../../api/kdd";
function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
const props = defineProps<{ attention: unknown }>();
const value = computed(() => object(props.attention));
const truth = computed(() => object(value.value.truth_confidence));
const consequence = computed(() => object(value.value.potential_consequence));
const priority = computed(() => object(value.value.verification_priority));
const truthLabels: Record<string, string> = { unverified: "尚未证实", unknown: "未知", source_reported: "来源声称，未作事实确认" };
const consequenceLabels: Record<string, string> = { potentially_serious: "若说法成立，潜在后果可能严重", undetermined: "尚不能判断" };
const importance: Record<string, string> = { high: "高", medium: "中", low: "低", undetermined: "未定" };
const urgency: Record<string, string> = { immediate: "立即核实", soon: "尽快核实", routine: "常规核实", undetermined: "未定" };
const reasonLabels: Record<string, string> = {
  specific_potentially_serious_claim_requires_verification_truth_unresolved: "具体严重说法值得优先核实；来源与真实性仍未确认",
  specific_attributed_claim_for_verification: "已找到可归属来源、可进一步核对的具体说法",
  no_checkable_entity_quantity_event_claim: "尚无足够具体的实体、数量或事件说法",
  source_bound_metric_not_population_or_causal_proof: "有固定来源的数值，不代表总体率或因果证明",
  source_linked_correction_or_reviewed_counterevidence: "有来源关联的更正或已审反证",
  specific_event_with_independent_or_source_confirmed_support: "具体事件具有独立来源或来源核实记录",
  specific_serious_event_requires_immediate_verification_not_truth_approval: "严重事件说法值得立即核实，尚非真实性批准",
  fixed_typed_state_or_graph_claim_for_verification: "固定结构化状态或图谱说法可进一步核实",
};
const claims = computed(() => Array.isArray(value.value.checkable_claims) ? value.value.checkable_claims.map(object) : []);
</script>

<template>
  <section v-if="value.version" class="source-attention notice" aria-label="真实性后果与核实优先级">
    <h3>真实性、潜在后果与核实优先级</h3>
    <dl class="attention-grid">
      <div><dt>原始说法真实性</dt><dd>{{ truthLabels[text(truth.status)] || truth.status || "未记录" }}</dd></div>
      <div><dt>潜在后果</dt><dd>{{ consequenceLabels[text(consequence.level)] || consequence.level || "未记录" }}</dd></div>
      <div><dt>核实优先级</dt><dd>{{ importance[text(priority.importance)] || priority.importance || "未记录" }} · {{ urgency[text(priority.urgency)] || priority.urgency || "未记录" }}</dd></div>
    </dl>
    <p class="muted">来源转述准确、传播量大或值得立即核实，均不等于原始说法已证实。后果仅在说法成立的条件下讨论，不是疾病风险估计；事后不成立的说法，当时也可能值得核实。未证实高风险仍待人工核实，不自动升级为警报。</p>
    <ul><li v-for="reason in strings(value.attention_reasons)" :key="reason">{{ reasonLabels[reason] || reason }}</li></ul>
    <details v-if="claims.length">
      <summary>可检验的来源说法（仍未证实）</summary>
      <ul><li v-for="(claim, index) in claims" :key="index">{{ claim.text }}</li></ul>
      <p v-if="typeof value.omitted_claim_count === 'number' && value.omitted_claim_count > 0">另有 {{ value.omitted_claim_count }} 条未在此预览，仍保留在计算依据中。</p>
    </details>
  </section>
</template>

<style scoped>
.attention-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
.attention-grid dt { color: var(--el-text-color-secondary); font-size: 13px; }
.attention-grid dd { margin: 6px 0; }
</style>
