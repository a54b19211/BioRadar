<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { engineRequest } from "../../api/engine";
import {
  object, objects, count, text, strings, preparation, selectionCounts,
  facts, readable, kddLabel, methodLabel, representationScope, preparationDirections, retainedSourceRecord,
} from "../../api/kdd";

const props = defineProps<{ result: unknown; policy?: unknown; methodLabels?: Record<string, string>; snapshotId?: string }>();
const data = computed(() => preparation(props.result));
const directions = computed(() => preparationDirections(props.result));
const isMulti = computed(() => directions.value.length > 0);
const units = computed(() => objects(data.value?.units));
const excluded = computed(() => objects(data.value?.excluded_records));
const quantitativeExcluded = computed(() => objects(data.value?.quantitative_exclusions));
const methods = computed(() => objects(object(props.result)?.method_results));
const selections = computed(() => selectionCounts(props.result));
const selectionSummary = computed(() => object(data.value?.selection_summary));
const methodCount = computed(() => methods.value.filter(row => ["success", "partial"].includes(text(row.execution_status))).length);
const unknown = computed(() => strings(data.value?.unknown_reasons));
const coverage = computed(() => facts(data.value?.coverage));
const denominator = computed(() => readable(data.value?.denominator));
const unitContract = computed(() => readable(data.value?.unit_contract));
const limitations = computed(() => strings(object(props.result)?.limitations));
const valueRules = computed(() => facts(object(props.policy)?.value_policy));
const comparison = computed(() => representationScope(props.result));
const retainedMethods = computed(() => methods.value.filter(method => retainedUnits(method).length > 0));
const sourceText = ref("");
const sourceError = ref("");
const sourceBusy = ref(false);
let sourceRequest = 0;
watch(() => [props.result, props.snapshotId], () => {
  sourceRequest++; sourceText.value = ""; sourceError.value = ""; sourceBusy.value = false;
});
function retainedUnits(method: Record<string, unknown>) {
  return objects(object(method.statistics)?.retained_source_units);
}
function methodFacts(method: Record<string, unknown>) {
  return facts(Object.fromEntries(Object.entries(object(method.statistics) ?? {}).filter(([key]) => key !== "retained_source_units")));
}
async function openSource(unit: Record<string, unknown>) {
  if (!props.snapshotId) return;
  const request = ++sourceRequest;
  const snapshotId = props.snapshotId;
  const result = props.result;
  sourceText.value = ""; sourceError.value = ""; sourceBusy.value = true;
  try {
    const data = await engineRequest<{ id: string; snapshot: unknown }>(`/snapshots/${encodeURIComponent(snapshotId)}`);
    if (request !== sourceRequest || props.snapshotId !== snapshotId || props.result !== result) return;
    if (data.id !== snapshotId) throw new Error("固定资料快照身份不匹配，未展示其他快照内容。");
    const record = retainedSourceRecord(data.snapshot, unit);
    if (!record) throw new Error("固定原件的完整版本或采用文本哈希不匹配，未展示其他版本。");
    if (record.text_status !== "inline_authorized" || !text(record.normalized_text)) throw new Error("该固定版本未提供可读的授权文本；来源状态与身份仍保留。");
    sourceText.value = text(record.normalized_text);
  } catch (error) {
    if (request === sourceRequest && props.snapshotId === snapshotId && props.result === result) sourceError.value = error instanceof Error ? error.message : "读取固定材料失败";
  } finally {
    if (request === sourceRequest && props.snapshotId === snapshotId && props.result === result) sourceBusy.value = false;
  }
}
function members(unit: Record<string, unknown>) {
  const rows = objects(unit.members);
  if (!rows.length) return "未提供成员记录";
  return rows.map(member => {
    const ref = object(member.record_ref);
    return (text(member.source_id) || "来源未记录") + " / " + (text(ref?.object_id) || "身份未记录")
      + "（版本 " + (text(ref?.adopted_version).slice(0, 12) || "未提供") + "）";
  }).join("；");
}
function unitName(unit: Record<string, unknown>) {
  return text(unit.unit_id) || text(unit.id) || "未提供单位身份";
}
function reason(row: Record<string, unknown>) {
  const ref = object(row.record_ref);
  const source = text(ref?.object_id);
  const section = text(row.section_id);
  return (source ? source + (section ? "（" + section + "）" : "") + "：" : "")
    + readable(row.reason ?? row.reasons ?? row);
}
function download() {
  const url = URL.createObjectURL(new Blob([JSON.stringify(props.result, null, 2)], { type: "application/json;charset=utf-8" }));
  const link = document.createElement("a"); link.href = url;
  link.download = "模式计算-" + (text(data.value?.dataset_hash).slice(0, 12) || "已保存结果") + ".json";
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
</script>
<template>
  <section class="kdd-summary" aria-label="从数据加工到模式评价">
    <div class="kdd-heading">
      <h3>数据、计算与模式评价</h3>
      <span v-if="data">{{ kddLabel(text(data.direction)) }}</span>
      <button class="text-link no-print" @click="download">下载本次固定计算结果</button>
    </div>
    <ol class="kdd-flow" aria-label="研究路径">
      <li>数据加工</li><li>适用计算</li><li>模式依据与价值</li><li>补证与专业核验</li><li>报告与版本更新</li>
    </ol>
    <template v-if="data">
      <div class="kdd-facts">
        <div><span>输入资料</span><strong>{{ count(data.input_count) ?? "未记录" }}</strong></div>
        <div><span>{{ isMulti ? "各方向单位条目" : "合格分析单位" }}</span><strong>{{ count(data.qualified_count) ?? "未记录" }}</strong></div>
        <div><span>{{ isMulti ? "各方向排除条目" : "排除记录" }}</span><strong>{{ Array.isArray(data.excluded_records) ? excluded.length : "未记录" }}</strong></div>
        <div v-if="count(data.quantitative_qualified_count) !== null"><span>数值合格单位</span><strong>{{ count(data.quantitative_qualified_count) }}</strong></div>
        <div v-if="count(data.retained_source_version_count) !== null"><span>保留资料版本</span><strong>{{ count(data.retained_source_version_count) }}</strong></div>
        <div><span>实际执行方法</span><strong>{{ methodCount }} / {{ methods.length }}</strong></div>
      </div>
      <p v-if="isMulti" class="notice">同一批输入分别进入各方向的资格判断。各方向的单位身份、分母与排除口径独立，不能相加当作文档数、独立研究数或一个共同总体；所有方向共用本轮审阅容量。</p>
      <div v-if="isMulti" class="direction-datasets">
        <article v-for="entry in directions" :key="entry.direction" class="direction-dataset">
          <h4>{{ kddLabel(entry.direction) }} · 真实单位资格</h4>
          <p>收到原批次输入 {{ count(entry.data.input_count) ?? "未记录" }} 条；该方向合格分析单位 {{ count(entry.data.qualified_count) ?? "未记录" }}；排除条目 {{ Array.isArray(entry.data.excluded_records) ? entry.data.excluded_records.length : "未记录" }}。</p>
          <p><strong>分析单位：</strong>{{ readable(entry.data.unit_contract) }}</p>
          <p><strong>该方向分母：</strong>{{ readable(entry.data.denominator) }}</p>
          <p v-if="count(entry.data.retained_source_version_count) !== null">保留资料版本 {{ count(entry.data.retained_source_version_count) }}；数值合格单位 {{ count(entry.data.quantitative_qualified_count) ?? "未记录" }}。材料保留与数值资格分别判断。</p>
          <details v-if="objects(entry.data.quantitative_exclusions).length"><summary>未进入该方向数值计算的条目</summary><ul><li v-for="(item, index) in objects(entry.data.quantitative_exclusions)" :key="index">{{ reason(item) }}</li></ul><p>未进入数值计算不等于整份资料被丢弃；符合条件的来源材料仍保留。</p></details>
          <ul v-if="strings(entry.data.unknown_reasons).length" class="notice"><li v-for="item in strings(entry.data.unknown_reasons)" :key="item">{{ kddLabel(item) }}</li></ul>
          <details>
            <summary>该方向的单位成员、排除与覆盖</summary>
            <dl class="kdd-definition"><template v-for="item in facts(entry.data.coverage)" :key="item.key"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></template></dl>
            <ul><li v-for="(unit, index) in objects(entry.data.units).slice(0, 10)" :key="unitName(unit) + index">{{ unitName(unit) }} · {{ readable(unit.dimensions) }} · {{ members(unit) }}</li></ul>
            <p v-if="objects(entry.data.units).length > 10" class="muted">单位预览 10 / {{ objects(entry.data.units).length }}；全部固定成员可下载。</p>
            <ul><li v-for="(item, index) in objects(entry.data.excluded_records)" :key="index">未进入该方向计算 · {{ reason(item) }}</li></ul>
            <p>该方向数据哈希：{{ text(entry.data.dataset_hash) || "未记录" }}</p>
          </details>
        </article>
      </div>
      <p><strong>分析单位：</strong>{{ unitContract }}</p>
      <p><strong>分母及适用范围：</strong>{{ denominator }}</p>
      <dl v-if="coverage.length" class="kdd-definition">
        <template v-for="item in coverage" :key="item.key"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></template>
      </dl>
      <ul v-if="unknown.length" class="notice"><li v-for="item in unknown" :key="item">{{ kddLabel(item) }}</li></ul>
      <details v-if="!isMulti && units.length">
        <summary>查看分析单位、比较条件和原件成员（预览 {{ Math.min(units.length, 10) }} / {{ units.length }}）</summary>
        <table class="kdd-table"><thead><tr><th>稳定单位</th><th>比较条件</th><th>固定原件成员</th></tr></thead><tbody>
          <tr v-for="(unit, index) in units.slice(0, 10)" :key="unitName(unit) + index">
            <td :title="unitName(unit)">{{ unitName(unit).slice(0, 16) }}<small v-if="unit.unit_kind"> · {{ kddLabel(text(unit.unit_kind)) }}</small></td><td>{{ readable(unit.dimensions) }}</td><td>{{ members(unit) }}</td>
          </tr>
        </tbody></table>
        <p class="muted">完整成员与特征版本保存在本次固定结果中，可由上方下载。预览数量不改变实际计算范围。</p>
      </details>
      <details v-if="!isMulti && quantitativeExcluded.length"><summary>未进入数值计算的条目</summary>
        <ul><li v-for="(item, index) in quantitativeExcluded" :key="index">{{ reason(item) }}</li></ul>
        <p>未进入数值计算不等于整份资料被丢弃；符合条件的来源材料仍保留。</p>
      </details>
      <details v-if="!isMulti && excluded.length"><summary>哪些资料没有进入分析</summary>
        <ul><li v-for="(item, index) in excluded" :key="index">{{ reason(item) }}</li></ul>
      </details>
    </template>
    <p v-else class="muted">这份旧结果未保存 KDD 数据加工口径。以下保留当时方法、依据和入选候选，未补造分母或排除数量。</p>
    <dl v-if="data && valueRules.length" class="kdd-definition">
      <template v-for="rule in valueRules" :key="rule.key"><dt>{{ rule.label }}</dt><dd>{{ rule.value }}</dd></template>
    </dl>
    <div v-if="selections" class="kdd-selection">
      <strong>{{ isMulti ? "跨方向共享容量筛选" : "本轮筛选" }}</strong>
      <span>入选 {{ selections.selected }}</span><span>价值条件未通过 {{ selections.value_excluded }}</span>
      <span>容量暂缓 {{ selections.capacity_deferred }}</span><span>相同依据暂缓 {{ selections.redundancy_deferred }}</span>
      <span v-if="count(selectionSummary?.exploratory_followup_eligible_count) !== null">仅探索补证合格 {{ count(selectionSummary?.exploratory_followup_eligible_count) }}</span>
      <span v-if="count(selectionSummary?.descriptive_pattern_eligible_count) !== null">描述性模式合格 {{ count(selectionSummary?.descriptive_pattern_eligible_count) }}</span>
      <p>容量与同依据暂缓表示本轮未排入审阅，不代表没有研究价值；可在下方查看全部算法候选。</p>
    </div>
    <p v-else-if="data && !methods.length" class="muted">本轮尚未保存模式筛选结果；已保存的数据加工范围见上。</p>
    <p v-else class="muted">旧结果未保存逐项筛选理由；不能从未进入审阅队列推断为无价值。</p>
    <table v-if="methods.length" class="kdd-table">
      <thead><tr><th>方法</th><th>实际状态</th><th>生成候选</th><th>范围与截断</th></tr></thead>
      <tbody><tr v-for="method in methods" :key="text(method.method_id)">
        <td>{{ methodLabel(text(method.method_id), methodLabels) }}
          <details v-if="object(method.statistics) && Object.keys(object(method.statistics) ?? {}).length"><summary>计算口径与数据表示</summary>
            <dl class="kdd-definition"><template v-for="item in methodFacts(method)" :key="item.key"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></template></dl>
          </details>
        </td>
        <td>{{ kddLabel(text(method.execution_status)) }} · {{ kddLabel(text(method.readiness)) }}</td>
        <td>{{ Array.isArray(method.candidates) ? method.candidates.length : "未记录" }}</td>
        <td>排除 {{ Array.isArray(method.excluded_records) ? method.excluded_records.length : "未记录" }}；截断 {{ count(method.truncated_count) ?? "未记录" }}</td>
      </tr></tbody>
    </table>
    <section v-for="method in retainedMethods" :key="'retained-' + text(method.method_id)" class="retained-materials notice" aria-label="保留的非数值来源材料">
      <h4>保留的非数值来源材料 · {{ methodLabel(text(method.method_id), methodLabels) }}</h4>
      <p>保留 {{ retainedUnits(method).length }} 个材料单位。材料可读与数值资格、候选入选分别记录；这些单位不额外生成候选，不占用模式审阅容量，也不代表病例、确认事件、独立研究或风险解除。</p>
      <details v-for="unit in retainedUnits(method)" :key="unitName(unit)">
        <summary>{{ kddLabel(text(unit.unit_kind)) }} · {{ members(unit) }}</summary>
        <p>材料类型：{{ readable(object(unit.dimensions)?.material_type) }}；说法状态：{{ readable(object(unit.dimensions)?.statement_status) }}；来源状态：{{ readable(object(unit.dimensions)?.source_state) }}。</p>
        <p>资料时间窗：{{ readable(object(unit.features)?.window) }}；{{ readable(object(unit.features)?.qualification) }}。</p>
        <p v-if="object(object(unit.features)?.source_context)">来源归属：{{ readable(object(unit.features)?.source_context) }}</p>
        <p v-if="object(unit.dimensions)?.evidence_status">真实性状态：{{ readable(object(unit.dimensions)?.evidence_status) }}。</p>
        <blockquote v-if="text(object(object(unit.features)?.excerpt)?.quote)">{{ text(object(object(unit.features)?.excerpt)?.quote) }}</blockquote>
        <p v-if="unit.unit_kind === 'source_statement' && object(unit.features)?.excerpt === null" class="muted">本版未提供可展示的授权原文窗口；未补写原文或推定全文长度。</p>
        <p v-if="count(object(unit.features)?.omitted_text_character_count)" class="muted">本原文窗口之外另有 {{ count(object(unit.features)?.omitted_text_character_count) }} 个字符；可按固定版本读取，不将窗口视作全文复核。</p>
        <ul v-if="objects(object(unit.features)?.claims).length"><li v-for="(claim, index) in objects(object(unit.features)?.claims)" :key="index">来源原句（待核）：{{ claim.text }}</li></ul>
        <p v-if="count(object(unit.features)?.omitted_claim_count)" class="muted">另有 {{ count(object(unit.features)?.omitted_claim_count) }} 条来源说法未在此预览。</p>
        <template v-if="object(object(unit.features)?.event)">
          <p>信息事件类型：{{ readable(object(object(unit.features)?.event)?.event_type || object(object(unit.features)?.event)?.relation_type) }}。</p>
          <p v-if="text(object(object(unit.features)?.event)?.description)">{{ text(object(object(unit.features)?.event)?.description) }}</p>
          <p v-if="object(object(unit.features)?.event)?.target_ref || object(object(unit.features)?.event)?.target">关联原版本：{{ readable(object(object(unit.features)?.event)?.target_ref || object(object(unit.features)?.event)?.target) }}</p>
          <p v-else-if="object(object(unit.features)?.event)?.target_source_id || object(object(unit.features)?.event)?.target_source_record_id">关联来源：{{ text(object(object(unit.features)?.event)?.target_source_id) || "来源未提供" }} / {{ text(object(object(unit.features)?.event)?.target_source_record_id) || "原件编号未提供" }}；目标版本：{{ text(object(object(unit.features)?.event)?.target_version_id) || "未提供，未补猜" }}。</p>
          <p>定位状态：{{ readable(object(unit.features)?.basis_status) }}；时间状态：{{ readable(object(unit.features)?.temporal_status) }}。</p>
        </template>
        <ul v-if="objects(object(unit.features)?.information_event_gaps).length"><li v-for="(gap, index) in objects(object(unit.features)?.information_event_gaps)" :key="index">尚未采用的信息事件：{{ readable(gap.reason) }}</li></ul>
        <p class="muted">固定原件与文本依据：{{ readable(object(unit.features)?.body_ref) }}</p>
        <button v-if="snapshotId" type="button" class="text-link no-print" :disabled="sourceBusy" @click="openSource(unit)">查看该版本固定采用文本</button>
        <p v-else class="muted">原件入口见本版调查的固定材料列表；此处不将未提供的原文或时间补成事实。</p>
      </details>
    </section>
    <p v-if="sourceError" class="error-inline">{{ sourceError }}</p>
    <section v-if="sourceText" class="retained-source-text"><h4>固定采用文本（资料原件，未据此宣称全文已核验）</h4><pre>{{ sourceText }}</pre></section>
    <section v-if="comparison" class="kdd-comparison">
      <h4>向量与 TF-IDF 的实际比较范围</h4>
      <p>TF-IDF 实际参与 {{ comparison.textCount ?? "未记录" }} 个单位，向量方法实际参与 {{ comparison.vectorCount ?? "未记录" }} 个单位；共同 {{ comparison.sharedCount ?? "未记录" }} 个。TF-IDF 拟合采用 {{ comparison.fitCount ?? "未记录" }} 个单位，拟合时点和版本见方法口径。</p>
      <p v-if="comparison.sameUnits === true && (comparison.textCount ?? 0) > 0">双方处理同一组分析单位，方法效果仍需独立评价。</p>
      <p v-else-if="comparison.sameUnits === true" class="muted">双方没有实际参与单位，本次不构成效果对照。</p>
      <p v-else-if="comparison.sameUnits === false" class="notice">双方参与单位不同，结果不能直接解读为同条件效果提升。</p>
      <p v-else class="muted">缺少双方实际处理单位清单，尚不能确认同条件对照。</p>
    </section>
    <p class="muted">缺少向量时只报告实际执行的基线；不同模型空间分别计算。聚类或排序分数不表示统计显著性。</p>
    <p>计算结果和价值筛选完成后，可从候选进入调查继续补证，按问题调用适用的专业方法，再阅读报告与新版。后三步的实际进度、失败和未查问题在对应调查保存。</p>
    <ul v-if="limitations.length" class="muted"><li v-for="item in limitations" :key="item">{{ kddLabel(item) }}</li></ul>
    <details v-if="data" class="no-print"><summary>固定数据与方法版本</summary>
      <p>数据集：{{ text(data.dataset_hash) || "未提供" }}</p>
      <p>该哈希核对数据加工内容；价值筛选与审阅容量另存，不混入同数据比较。</p>
      <dl class="kdd-definition"><template v-for="item in facts(data.versions)" :key="item.key"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></template></dl>
    </details>
  </section>
</template>
<style scoped>
.retained-materials details { margin: 10px 0; }.retained-source-text pre { white-space: pre-wrap; overflow-wrap: anywhere; }
.direction-dataset { border-top: 1px solid var(--line, #dce5e9); padding: 12px 0; }
.kdd-summary { min-width: 0; border: 1px solid var(--line, #dce5e9); border-radius: 10px; padding: 16px; margin: 16px 0; overflow-wrap: anywhere; }
.kdd-heading, .kdd-selection { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 16px; }
.kdd-heading h3 { margin: 0; }.kdd-heading button { margin-left: auto; }
.kdd-flow { display: flex; flex-wrap: wrap; gap: 8px; padding: 0; list-style: none; color: var(--el-color-primary); }
.kdd-flow li + li::before { content: "→"; margin-right: 8px; }
.kdd-facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.kdd-facts span, .kdd-facts strong { display: block; }.kdd-facts strong { font-size: 1.25rem; }
.kdd-definition { display: grid; grid-template-columns: minmax(100px, 1fr) minmax(0, 3fr); gap: 6px 12px; }
.kdd-definition dd { margin: 0; }.kdd-selection p { flex-basis: 100%; margin: 0; }
.kdd-table { width: 100%; table-layout: fixed; border-collapse: collapse; font-size: .9rem; margin: 12px 0; }
.kdd-table th, .kdd-table td { text-align: left; vertical-align: top; padding: 8px; border-bottom: 1px solid var(--line, #dce5e9); overflow-wrap: anywhere; }
@media (max-width: 640px) { .kdd-facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }.kdd-definition { grid-template-columns: 1fr; }.kdd-flow { font-size: .85rem; } }
</style>
