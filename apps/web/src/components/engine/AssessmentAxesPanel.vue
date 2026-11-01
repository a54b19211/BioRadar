<script setup lang="ts">
import { computed } from "vue";
import { object } from "../../api/humanReview";
const props = defineProps<{ axes: unknown }>();
const value = computed(() => object(props.axes));
const sources = computed(() => rows(value.value.source_reliability));
const information = computed(() => rows(value.value.information_credibility));
const heat = computed(() => object(value.value.heat));
function rows(value: unknown) { return Array.isArray(value) ? value.map(object) : []; }
const sourcesLabels: Record<string, string> = { A: "完全可靠", B: "通常可靠", C: "较为可靠", D: "通常不可靠", E: "不可靠", F: "无依据评定" };
const informationLabels: Record<string, string> = { "1": "已有独立来源确认", "2": "很可能真实，尚未确认", "3": "可能真实，尚未确认", "4": "可疑", "5": "不太可能真实", "6": "无依据评定" };
const status: Record<string, string> = { not_assessed: "未评定", machine_assessed: "机器评定，待人核对", human_reviewed: "已记录人工评定" };
const evidence: Record<string, string> = { unverified: "原始说法未证实", unknown: "未知", independent_sources: "已有独立来源支持记录", institution_confirmed: "已有机构来源核实记录", comparable_numeric: "已有可比较数值" };
function ref(value: unknown) { const r = object(value); return [r.provider, r.object_id, r.adopted_version].filter(Boolean).join(" / ") || "未记录固定原件"; }
</script>
<template>
  <section v-if="Object.keys(value).length" class="assessment-axes notice" aria-label="来源可靠性信息可信度证据等级与热度">
    <h4>来源与信息分开评定</h4>
    <dl class="axes-grid">
      <div><dt>来源可靠性（A–F）</dt><dd><p v-if="!sources.length">F · 未评定，无充分依据</p><p v-for="(item,index) in sources" :key="index">{{ item.grade }} · {{ sourcesLabels[String(item.grade)] || '该标签未支持' }}；{{ status[String(item.assessment_status)] || '评定状态未记录' }}<br>{{ item.publisher_scope }}</p></dd></div>
      <div><dt>具体信息可信度（1–6）</dt><dd><p v-if="!information.length">6 · 未评定，无充分依据</p><p v-for="(item,index) in information" :key="index">{{ item.grade }} · {{ informationLabels[String(item.grade)] || '该标签未支持' }}；{{ status[String(item.assessment_status)] || '评定状态未记录' }}<br>{{ item.claim_id }}：{{ item.claim_text }}</p></dd></div>
      <div><dt>证据等级</dt><dd>{{ evidence[String(value.evidence_level)] || value.evidence_level || '未记录' }}</dd></div>
      <div><dt>曝光热度</dt><dd>{{ typeof heat.score === 'number' ? heat.score : '未记录' }}</dd></div>
    </dl>
    <p class="muted">四项独立记录，不合成信用分。F／6 表示未能评定，不表示虚假，也不是 50% 概率；热度、转载数、机构名和图谱置信度均不能代替这两项评级。采用 Admiralty v5 的经典 A–F 子集。</p>
    <details v-if="sources.length || information.length"><summary>评级的固定原件、依据与人工记录</summary>
      <article v-for="(item,index) in [...sources,...information]" :key="index"><p>{{ ref(item.record_ref) }}</p><p>{{ item.assessment_basis || item.reason_code }}</p><p>{{ item.assessed_by || '评定者未记录' }} · {{ item.assessed_at || '评定时间未记录' }} · {{ item.native_review_entry_id || '无人工评定记录' }}</p></article>
    </details>
  </section>
</template>
<style scoped>.axes-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px; } dd { margin:6px 0; } dt { font-weight:600; } article { overflow-wrap:anywhere; }</style>
