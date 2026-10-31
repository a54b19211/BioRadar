<script setup lang="ts">
import { computed } from "vue";
import { strings, text } from "../../api/kdd";
function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
const props = defineProps<{ tracking: unknown; title?: string; asOf?: string }>();
const value = computed(() => object(props.tracking));
const evidence = computed(() => object(value.value.evidence));
const heat = computed(() => object(value.value.heat));
const correction = computed(() => object(value.value.correction));
const risk = computed(() => object(value.value.risk));
const hypothesisComparison = computed(() => object(value.value.hypothesis_comparison));
const currentJudgment = computed(() => object(hypothesisComparison.value.current_judgment));
const negativeLabels: Record<string, string> = {
  not_found: "未发现：热度可降、风险未排除",
  excluded: "明确排除（仍需核对范围与依据）",
  reaction: "反应信号，不等于反证或风险解除",
};
const update = computed(() => object(value.value.model_update));
const lifecycle: Record<string, string> = { incipient: "初现", warming: "升温", high: "高关注", cooling: "降温", dormant: "休眠", closed: "已结束跟踪" };
const levels: Record<string, string> = { unverified: "原始说法未证实", independent_sources: "已有独立来源支持记录", institution_confirmed: "已有机构来源核实记录", comparable_numeric: "已有可比较数值", unknown: "证据强度未知" };
const corrections: Record<string, string> = { none: "未记录更正", corrected: "已有更正／撤回材料", counterevidence: "已有反证材料", unknown: "更正状态未知" };
const risks: Record<string, string> = { unresolved: "仍未解除", resolved: "已有解除记录", unknown: "尚不能判断" };
const reasons: Record<string, string> = {
  source_evidence_or_correction_state_changed: "证据或更正状态发生变化",
  new_independent_evidence_wakes_topic: "新增独立证据，需重新关注",
  pure_exposure_update_no_new_evidence_reason_for_model: "本轮只更新曝光情况，未据此要求重新生成模型报告",
  pathogen_transmission_and_control_not_resolved_by_adopted_evidence: "现有采用资料尚未解决病原、传播或控制方面的关键问题",
  source_unavailable_not_evidence_of_resolution: "来源暂不可读，不能据此判断风险解除",
};
function number(value: unknown) { return typeof value === "number" ? value.toLocaleString() : "未记录"; }
function date(value: unknown) {
  if (typeof value !== "string" || !value) return "未记录";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}
function rows(value: unknown) { return Array.isArray(value) ? value.map(object) : []; }
function ref(value: unknown) {
  const item = object(value);
  return [item.provider, item.object_id, item.adopted_version].filter(part => typeof part === "string").join(" / ") || "未记录固定身份";
}
const environmentLabels: Record<string, string> = { deletion: "删除／撤回迹象", denial_or_censorship: "辟谣／限制传播相关表述" };
</script>

<template>
  <section v-if="value.version" class="engine-panel topic-tracking" aria-label="证据热度与生命周期">
    <h3>{{ title || "证据、热度、更正与跟踪状态" }}</h3>
    <p v-if="asOf" class="muted">状态记录时点：{{ date(asOf) }}</p>
    <dl class="tracking-grid">
      <div><dt>证据强度</dt><dd>{{ levels[text(evidence.level)] || evidence.level || "未记录" }}</dd></div>
      <div><dt>{{ typeof heat.provisional_exposure_score === "number" ? "确认来源曝光热度" : "采用来源曝光热度" }}</dt><dd>{{ number(heat.score) }}</dd></div>
      <div><dt>更正／反证</dt><dd>{{ corrections[text(correction.status)] || correction.status || "未记录" }}</dd></div>
      <div><dt>生命周期</dt><dd>{{ lifecycle[text(value.lifecycle)] || value.lifecycle || "未记录" }}</dd></div>
    </dl>
    <p>已确认来源组 {{ number(evidence.confirmed_source_groups) }}；独立研究数 {{ evidence.independent_study_count == null ? "尚不能完整确认" : number(evidence.independent_study_count) }}；
      来源独立性未确认的记录 {{ number(evidence.unknown_source_records) }}。其中已确认独立研究 {{ number(evidence.confirmed_independent_study_count) }}。</p>
    <p>本轮新来源组 {{ number(heat.new_source_groups) }}，其中确认组 {{ number(heat.confirmed_new_source_groups) }}；热度衰减半衰期 {{ number(heat.half_life_hours) }} 小时。</p>
    <p v-if="typeof heat.provisional_exposure_score === 'number'" class="muted">未确认来源的暂记曝光 {{ number(heat.provisional_exposure_score) }}；本轮新增暂记来源组 {{ number(heat.new_provisional_groups) }}。这部分不计独立支持，也不因匿名新增帖子自动唤醒模型。</p>
    <p class="muted">热度只描述已采用来源的曝光及衰减，不是人群热度或真实性概率。多次转载不自动成为多份独立证据；机构来源核实记录也不替代对原始说法的事实判断。</p>
    <p :class="risk.status === 'unresolved' ? 'notice' : 'muted'"><strong>风险状态：{{ risks[text(risk.status)] || risk.status || "未记录" }}。</strong>降温、休眠或停止跟踪均不自动表示风险解除。</p>
    <ul v-if="strings(risk.critical_gaps).length"><li v-for="item in strings(risk.critical_gaps)" :key="item">{{ reasons[item] || item }}</li></ul>
    <p>检查间隔 {{ number(value.cadence_hours) }} 小时；下次应检查：{{ date(value.next_due_at) }}。
      <span v-if="value.automatic === true">由自动线索开始跟踪。</span></p>
    <p v-if="update.status === 'waiting_budget'" class="notice">模型更新等待预算恢复<template v-if="update.retry_at">，预计 {{ date(update.retry_at) }}</template>，已有证据与报告保留。</p>
    <p v-else>{{ update.needed === true ? "本轮有理由请求模型更新" : update.needed === false ? "本轮未要求重新生成模型报告" : "模型更新状态未记录" }}。专题资料检查与模型调用分别计量。</p>
    <ul v-if="strings(update.reasons).length"><li v-for="item in strings(update.reasons)" :key="item">{{ reasons[item] || item }}</li></ul>
    <div v-if="rows(value.environment_signals).length" class="notice">
      <strong>环境反应（不作为传言真伪或风险解除的证明）</strong>
      <ul><li v-for="(item, index) in rows(value.environment_signals)" :key="index">{{ environmentLabels[text(item.kind)] || item.kind }} · {{ ref(item.record_ref) }}</li></ul>
    </div>
    <div v-if="rows(value.negative_signals).length" class="notice">
      <strong>未发现、明确排除与反应信号</strong>
      <ul><li v-for="(item, index) in rows(value.negative_signals)" :key="index">
        {{ negativeLabels[text(item.kind)] || item.kind }} · {{ ref(item.record_ref) }}
      </li></ul>
    </div>
    <div v-if="Object.keys(currentJudgment).length" class="notice">
      <strong v-if="hypothesisComparison.current_basis_unchanged === false">旧依据下判断，等待新研判</strong>
      <strong v-else-if="hypothesisComparison.current_basis_unchanged === true">本版依据下的模型假说比较</strong>
      <strong v-else>判断依据是否仍一致未记录，不能视为当前判断</strong>
      <p>反证最少的假说：{{ currentJudgment.least_refuted_hypothesis_id }}。</p>
      <p>{{ currentJudgment.reason }}</p>
      <p>下一条区分证据：{{ currentJudgment.next_discriminating_evidence }}</p>
      <p>这是模型判断，未证实；不能据此自动解除风险。</p>
    </div>
    <details v-if="rows(evidence.basis_refs).length || rows(correction.refs).length || rows(correction.counterevidence_refs).length">
      <summary>固定证据、更正与反证身份</summary>
      <ul><li v-for="(item, index) in rows(evidence.basis_refs)" :key="'e' + index">证据 · {{ ref(item) }}</li>
        <li v-for="(item, index) in rows(correction.refs)" :key="'c' + index">更正／撤回 · {{ ref(item.target_ref || item.record_ref || item) }}</li>
        <li v-for="(item, index) in rows(correction.counterevidence_refs)" :key="'r' + index">反证 · {{ ref(item) }}</li></ul>
      <p class="muted">本版原件入口仍见观察记录中的固定原件列表。</p>
    </details>
  </section>
</template>

<style scoped>
.tracking-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; }
.tracking-grid dt { color: var(--el-text-color-secondary); font-size: 13px; }
.tracking-grid dd { margin: 6px 0; }
.topic-tracking li { margin: 7px 0; overflow-wrap: anywhere; }
</style>
