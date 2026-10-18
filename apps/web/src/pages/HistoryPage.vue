<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { ElButton, ElInput } from "element-plus";
import EngineLogin from "../components/engine/EngineLogin.vue";
import { engineRequest, EngineError } from "../api/engine";
import type { components } from "../api/generated/engine";

type Study = components["schemas"]["HistoryStudyView"];
type Summary = Pick<Study, "id" | "title" | "is_demo" | "state" | "cutoff">;
const authenticated = ref(false);
const studies = ref<Summary[]>([]);
const selected = ref<Study | null>(null);
const catalog = ref<unknown>(null);
const error = ref("");
const busy = ref(false);
const uncertainWrite = ref("");
const evaluationCutoff = ref("2019-01-01T00:00:00Z");
const original = ref<unknown>(null);
const requestText = ref(JSON.stringify({
  title: "历史资料研究", question: "预先声明队列在12个月内是否公开获批事件？",
  source_id: "external_archive", is_demo: true,
  mode: "public_history_reconstruction", cutoff: "2018-01-01T00:00:00Z",
  target: "regulatory_approval", horizon_months: 12,
  run_id: "填写当时已知清单的归档run_id",
  training_runs: [{cutoff: "2016-01-01T00:00:00Z", run_id: "填写训练清单归档run_id"}],
  followup_queries: 2, review_budget: 10, minimum_training_units: 3,
  max_records: 5000, max_bytes: 8000000, max_seconds: 90
}, null, 2));
interface FactRow {
  source_record_id: string; version_id: string; title: string; source_url?: string | null;
  boundary: string; missing_fields: string[];
  fields: Record<string, {value: unknown; locator: unknown}>;
  source_field_excerpt?: {text: string; truncated: boolean} | null;
}
const factRows = computed(() => (selected.value?.fact_summary?.rows ?? []) as FactRow[]);
const fieldLabels: Record<string, string> = {
  trial_id: "登记号", status: "登记/项目状态", phase: "阶段", why_stopped: "登记停止原因",
  conditions: "登记疾病", interventions: "登记干预", enrollment: "入组人数",
  start_date: "登记开始日期", start_date_type: "开始日期类型", completion_date: "登记完成日期",
  application_numbers: "申请号（保留多值）", letter_type: "原信函类型", letter_date: "签字日期",
  file_name: "信函文件标识", company_name: "原公司字段", approval_status: "原监管状态",
  pmid: "PMID", pmcid: "PMCID", doi: "DOI", publication_types: "文献类型",
  correction_links: "原更正/撤稿关联", publication_date: "原发表日期", date_of_revision: "来源修订日期",
  license: "来源许可字段", is_open_access: "开放获取字段", program_id: "已声明项目ID",
  lineage_id: "来源谱系ID（审核资格见限制）", indication: "适应证口径", jurisdiction: "地区/法域口径",
  mapping_available_at: "映射声称可见日期",
};
function valueText(value: unknown) {
  return typeof value === "string" ? value : JSON.stringify(value);
}
function stateLabel(state: string) {
  return ({unsupported: "调查已保存，预测资料不足", partial: "部分可用，查看排除与未知",
    predicted: "固定预测已保存", collecting: "正在整理或执行结局待核", failed: "本次读取未完成"} as Record<string,string>)[state] ?? state;
}
let generation = 0;
let selectionVersion = 0;
let disposed = false;
function fail(value: unknown) {
  error.value = value instanceof Error ? value.message : "请求未完成";
  if (value instanceof EngineError && [403, 503].includes(value.status)) {
    ++selectionVersion; selected.value = null; original.value = null;
  }
  if (value instanceof EngineError && value.status === 401) {
    ++generation; ++selectionVersion;
    authenticated.value = false; studies.value = []; selected.value = null;
    original.value = null; catalog.value = null;
  }
}
async function refresh() {
  const token = ++generation;
  error.value = "";
  try {
    const session = await engineRequest<{authenticated: boolean}>("/session");
    if (disposed || token !== generation) return;
    authenticated.value = session.authenticated;
    if (!session.authenticated) return;
    const list = await engineRequest<{items: Summary[]}>("/history/studies");
    if (disposed || token !== generation) return;
    studies.value = list.items;
  } catch (e) { if (!disposed && token === generation) fail(e); }
}
async function open(id: string) {
  const token = ++selectionVersion;
  const session = generation;
  error.value = ""; original.value = null;
  try {
    const result = await engineRequest<Study>("/history/studies/"+id);
    if (!disposed && token === selectionVersion && session === generation) selected.value = result;
  } catch (e) { if (!disposed && token === selectionVersion && session === generation) fail(e); }
}
async function sources() {
  const token = generation;
  try {
    const result = await engineRequest<unknown>("/history/sources");
    if (!disposed && token === generation) catalog.value = result;
  } catch (e) { if (!disposed && token === generation) fail(e); }
}
async function write(action: "create" | "score") {
  if (busy.value || uncertainWrite.value) return;
  const token = generation;
  busy.value = true; error.value = "";
  const key = crypto.randomUUID();
  const path = action === "create" ? "/history/studies"
    : "/history/studies/"+selected.value?.id+"/evaluations";
  try {
    const body = action === "create" ? JSON.parse(requestText.value) : {cutoff: evaluationCutoff.value};
    const result = await engineRequest<Study | {study_id: string}>(path, {
      method: "POST", body: JSON.stringify(body), headers: {"Idempotency-Key": key}
    });
    if (disposed || token !== generation) return;
    const id = "study_id" in result ? result.study_id : result.id;
    await refresh(); await open(id);
  } catch (e) {
    if (!disposed && token === generation) {
      if (!(e instanceof SyntaxError) && !(e instanceof EngineError && [401,403,409,422].includes(e.status))) {
        uncertainWrite.value = key;
      }
      fail(e);
    }
  } finally { if (!disposed) busy.value = false; }
}
async function source(version: string, evaluationId?: string) {
  const id = selected.value?.id;
  if (!id) return;
  const token = selectionVersion, session = generation;
  try {
    const suffix = evaluationId ? "?evaluation_id="+encodeURIComponent(evaluationId) : "";
    const result = await engineRequest<unknown>("/history/studies/"+id+"/evidence/"+encodeURIComponent(version)+suffix);
    if (!disposed && token === selectionVersion && session === generation) original.value = result;
  } catch (e) { if (!disposed && token === selectionVersion && session === generation) fail(e); }
}
async function exportStudy() {
  const id = selected.value?.id;
  if (!id) return;
  const token = generation;
  const selection = selectionVersion;
  try {
    const response = await fetch("/api/engine/history/studies/"+id+"/export", {credentials: "same-origin", cache: "no-store"});
    if (!response.ok) throw new EngineError(response.status, "导出未完成");
    const blob = await response.blob();
    if (disposed || token !== generation || selection !== selectionVersion || selected.value?.id !== id) return;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a"); link.href = url; link.download = "historical-research-"+id+".md"; link.click();
    URL.revokeObjectURL(url);
  } catch (e) { if (!disposed && token === generation && selection === selectionVersion && selected.value?.id === id) fail(e); }
}
onMounted(refresh);
onUnmounted(() => {disposed = true; ++generation; ++selectionVersion;});
</script>

<template>
  <section class="history-page">
    <div class="eyebrow">HISTORICAL RESEARCH</div>
    <h1>历史资料与固定预测</h1>
    <p>先声明队列、资料截止、事件和观察窗；保存预测后，另用后续结局评分。事件率不是疗效、药物成功率或投资收益。</p>
    <EngineLogin v-if="!authenticated" @authenticated="refresh" />
    <template v-else>
      <p v-if="error" class="error-inline" role="alert">{{ error }}</p>
      <p v-if="uncertainWrite" role="alert">写入结局待核对（请求键 {{ uncertainWrite }}）。请先刷新列表核对；本页不自动重发。</p>
      <div class="actions">
        <ElButton @click="refresh">刷新已保存研究</ElButton>
        <ElButton @click="sources">查看来源目录与覆盖边界</ElButton>
      </div>
      <details v-if="catalog"><summary>目录不代表已接通所有领域预测</summary><pre>{{ JSON.stringify(catalog, null, 2) }}</pre></details>
      <div class="history-grid">
        <section class="engine-panel">
          <h2>新建研究</h2>
          <p>真实当前资料与历史演示分开；历史完整性依赖当时已知清单的 run_id 和可核公开证明。缺映射或标签会显示资格不足。</p>
          <label for="history-request">请求 JSON（来源、范围和预算均显式）</label>
          <ElInput id="history-request" v-model="requestText" type="textarea" :rows="19" :disabled="busy" />
          <ElButton type="primary" :loading="busy" :disabled="busy || !!uncertainWrite" @click="write('create')">冻结资料并建立研究</ElButton>
          <h2>已保存记录</h2>
          <p v-if="!studies.length">尚无记录；这不代表没有相关研究。</p>
          <ul class="study-list"><li v-for="study in studies" :key="study.id">
            <button :disabled="busy" @click="open(study.id)">{{ study.is_demo ? '【合成演示】' : '【真实资料】' }}{{ study.title }} · {{ stateLabel(study.state) }}</button>
          </li></ul>
        </section>
        <section v-if="selected" class="engine-panel">
          <h2>{{ selected.title }}</h2>
          <p>{{ selected.is_demo ? '显式合成资料，不是医学效果验证' : '真实采用资料' }} · {{ stateLabel(selected.state) }}</p>
          <p>模式 {{ selected.request.mode }}；资料截止 {{ selected.cutoff }}；窗口 {{ selected.request.horizon_months }} 个月。</p>
          <p class="hash">固定预测 {{ selected.prediction_hash || '尚未形成，查看缺口' }}</p>
          <p>WeKnora 未接通；专业评价未审阅。现代模型历史实验不等于当年已部署回放。</p>
          <div class="table-scroll"><table>
            <thead><tr><th>单位</th><th>历史事件率/资格</th><th>训练谱系</th><th>边界</th><th>采用原件</th></tr></thead>
            <tbody><tr v-for="p in selected.predictions" :key="p.unit.unit_kind+':'+p.unit.unit_id">
              <td>{{ p.unit.label }}<small>{{ p.unit.unit_kind }} · {{ p.unit.unit_id }}</small></td>
              <td>{{ p.probability == null ? '资格不足' : p.probability.toFixed(4) }}</td>
              <td>{{ p.training_events }}/{{ p.training_units }}</td>
              <td>{{ p.reasons.join('；') || p.baseline }}</td>
              <td><button @click="source(p.unit.evidence_ref.adopted_version)">读取冻结原件</button></td>
            </tr></tbody>
          </table></div>
          <p v-if="!selected.predictions.length">未产生合资格项目预测；阅读查询记录和未知，不能当作零风险。</p>
          <RouterLink v-if="selected.investigation_id" :to="'/investigations?id='+selected.investigation_id">查看普通调查材料与待核问题</RouterLink>
          <h3>独立后续评价</h3>
          <label for="evaluation-cutoff">仅读取该截止已可见结局</label>
          <ElInput id="evaluation-cutoff" v-model="evaluationCutoff" :disabled="busy" />
          <ElButton :disabled="busy || !selected.prediction_hash || !!uncertainWrite" @click="write('score')">追加评价版本</ElButton>
          <p>窗口未成熟保留删失；缺结局不补失败；Brier 仅对应已明确结局子集，须同时看覆盖率。</p>
          <details v-for="(evaluation, index) in selected.evaluations" :key="index">
            <summary>评价版本 {{ index + 1 }}</summary><pre>{{ JSON.stringify(evaluation, null, 2) }}</pre>
          </details>
          <details><summary>查询与真实追加追查</summary><pre>{{ JSON.stringify(selected.queries, null, 2) }}</pre></details>
          <details><summary>训练范围与采用版本</summary><pre>{{ JSON.stringify(selected.training, null, 2) }}</pre><pre>{{ JSON.stringify(selected.sources, null, 2) }}</pre></details>
          <section v-if="selected.fact_summary">
            <h3>实际资料整理（程序 context，未作专业判断）</h3>
            <article v-for="fact in factRows" :key="fact.version_id" class="fact-card">
              <h4>{{ fact.title }}</h4>
              <p>{{ fact.source_record_id }} · {{ fact.boundary }}</p>
              <dl><template v-for="(field, name) in fact.fields" :key="name">
                <dt>{{ fieldLabels[name] || name }}</dt><dd>{{ valueText(field.value) }}</dd>
              </template></dl>
              <p v-if="fact.source_field_excerpt">原字段节选：{{ fact.source_field_excerpt.text }}
                <span v-if="fact.source_field_excerpt.truncated">（已截断，可读完整原件）</span>
              </p>
              <p v-else>当前采用字段未提供可展示摘要/信函正文；这不表示原站没有全文。</p>
              <button @click="source(fact.version_id)">读取冻结原件与定位</button>
              <small class="hash">版本 {{ fact.version_id }}</small>
              <details><summary>字段定位与缺失详情</summary><pre>{{ JSON.stringify(fact, null, 2) }}</pre></details>
            </article>
            <details><summary>整理范围与技术详情</summary><pre>{{ JSON.stringify(selected.fact_summary, null, 2) }}</pre></details>
          </section>
          <h3>待核与限制</h3>
          <ul><li v-for="q in selected.next_questions" :key="q">{{ q }}</li></ul>
          <ul><li v-for="gap in selected.limitations" :key="gap">{{ gap }}</li></ul>
          <ElButton @click="exportStudy">导出可读研究记录</ElButton>
          <details v-if="original" open><summary>冻结原件、来源与公开证明</summary><pre>{{ JSON.stringify(original, null, 2) }}</pre></details>
        </section>
      </div>
    </template>
  </section>
</template>

<style scoped>
.history-page {max-width: 1600px; min-width: 0; margin: auto; overflow-wrap: anywhere;}
.history-grid {display: grid; grid-template-columns: minmax(280px, 0.8fr) minmax(0, 1.5fr); gap: 24px; margin-top: 24px;}
.engine-panel {padding: 24px; min-width: 0;}
.actions {display: flex; flex-wrap: wrap; gap: 10px;}
pre {white-space: pre-wrap; overflow-wrap: anywhere; max-height: 480px; overflow: auto; font-size: 12px;}
.hash {overflow-wrap: anywhere; font-size: 12px;}
table {border-collapse: collapse; min-width: 620px; width: 100%; font-size: 13px;}
th,td {text-align: left; vertical-align: top; padding: 10px; border-bottom: 1px solid #ddd;}
small {display: block; opacity: .7;}
.table-scroll {overflow-x: auto;}
button {cursor: pointer;}
.fact-card {padding: 16px 0; border-top: 1px solid #ddd;}
dl {display: grid; grid-template-columns: minmax(100px, 1fr) minmax(0, 3fr); gap: 8px;}
dt {font-weight: 600;}
dd {margin: 0; overflow-wrap: anywhere;}
.study-list {padding-left: 20px;}
.study-list button {background: transparent; border: 0; text-align: left; padding: 8px 0; color: #264e7b;}
@media (max-width: 1000px) {.history-grid {grid-template-columns: minmax(0, 1fr);}}
</style>
