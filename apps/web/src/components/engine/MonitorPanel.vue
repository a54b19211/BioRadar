<script setup lang="ts">
import AssessmentAxesPanel from "./AssessmentAxesPanel.vue";
import { onMounted, onUnmounted, ref } from "vue";
import { RouterLink } from "vue-router";
import { ElButton, ElTag } from "element-plus";
import { EngineError, engineRequest, post } from "../../api/engine";
import type { components } from "../../api/generated/engine";

type Status = components["schemas"]["MonitorStatus"];
type SelectionSummary = {
  new_records: number; evaluated: number; selected: number; no_candidate: number; unknown: number;
  scope?: string; limitations?: string[];
  methods?: { method_id: string; execution_status: string; processed_manifest_versions: number;
    excluded_manifest_versions: number; truncated_count: number }[];
  record_outcomes?: { source_record_id: string; version_id: string; outcome: string; reason: string }[];
};
type BatchList = Omit<components["schemas"]["MonitorBatchList"], "items"> & {
  items: (components["schemas"]["MonitorBatchView"] & { selection_summary?: SelectionSummary | null })[];
};
type LeadList = Omit<components["schemas"]["MonitorLeadList"], "items"> & {
  items: (components["schemas"]["MonitorLeadView"] & { importance_reason?: string | null; urgency_reason?: string | null })[];
};
const emit = defineEmits<{ openRun: [id: string]; unauthorized: [error: unknown] }>();
const status = ref<Status | null>(null);
const batches = ref<BatchList | null>(null);
const leads = ref<LeadList | null>(null);
const loading = ref(false);
const changing = ref(false);
const error = ref("");
let generation = 0;
let disposed = false;
let timer: ReturnType<typeof setTimeout> | undefined;
const processLabels: Record<string, string> = {
  disabled: "配置未启用", paused: "已暂停", starting: "等待后台启动",
  running: "正在运行", stale: "后台心跳中断", not_connected: "数据服务未接通",
};
const outcomeLabels: Record<string, string> = { selected: "支持本轮入选线索", no_candidate: "本轮已处理未选中", unknown: "尚未完成评价" };
const methodStates: Record<string, string> = {
  success: "成功执行", partial: "部分完成", failed: "执行失败", unsupported: "输入不支持",
};
const unknownReasons: Record<string, string> = {
  not_in_frozen_computation_window: "未进入本次计算窗口",
  method_receipts_not_available: "尚无实际方法回执",
  record_excluded_by_method_preparation: "被方法的输入资格检查排除",
  failed_unsupported_partial_or_missing_method_scope: "仍有方法失败、不支持、部分完成或缺少回执",
  no_record_level_processing_receipt: "缺少能确认该版本已处理的回执",
  actual_selected_lead_supports_this_manifest_version: "本批版本支持了实际入选线索",
  processed_by_successful_methods_but_not_selected_this_run: "本轮方法成功处理后未选中",
};
const stateLabels: Record<string, string> = {
  idle: "等待新资料", accepted: "已接收", queued: "等待计算", investigating: "正在查证",
  completed: "本批完成", blocked: "需要处理", pending: "等待处理", investigated: "已完成本轮查证",
  deferred: "暂缓查证", paused: "已暂停", unavailable: "来源暂不可读", running: "正在处理",
  resource_blocked: "等待存储空间恢复", not_started: "尚未启动", stale: "心跳中断",
};
function selectionRecords(summary: unknown) {
  if (!summary || typeof summary !== "object") return [];
  const records = (summary as Record<string, unknown>).record_outcomes;
  if (!Array.isArray(records)) return [];
  return records.flatMap((value: unknown) => {
    if (!value || typeof value !== "object") return [];
    const record = value as Record<string, unknown>;
    if (!["selected", "no_candidate", "unknown"].includes(String(record.outcome)) || typeof record.source_record_id !== "string"
      || typeof record.version_id !== "string" || typeof record.reason !== "string") return [];
    return [{ source_record_id: record.source_record_id, version_id: record.version_id, reason: record.reason, outcome: String(record.outcome) }];
  });
}
function time(value?: string | null) {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "尚无记录";
}
function current(version: number) { return !disposed && generation === version; }
function fail(value: unknown) {
  error.value = value instanceof Error ? value.message : "暂时无法读取后台状态。";
  if (value instanceof EngineError && (value.status === 401 || value.status === 403)) {
    status.value = null;
    batches.value = null;
    leads.value = null;
    emit("unauthorized", value);
  }
}
function schedule() {
  clearTimeout(timer);
  if (!disposed) timer = setTimeout(() => void refresh(), 10000);
}
async function refresh() {
  if (loading.value || changing.value || disposed) return;
  clearTimeout(timer);
  const version = ++generation;
  loading.value = true;
  try {
    const [state, recentBatches, recentLeads] = await Promise.all([
      engineRequest<Status>("/monitor/status"),
      engineRequest<BatchList>("/monitor/batches?limit=8"),
      engineRequest<LeadList>("/monitor/leads?limit=8"),
    ]);
    if (!current(version)) return;
    status.value = state;
    batches.value = recentBatches;
    leads.value = recentLeads;
    error.value = "";
  } catch (value) {
    if (current(version)) {
      status.value = null;
      batches.value = null;
      leads.value = null;
      fail(value);
    }
  } finally {
    if (current(version)) { loading.value = false; schedule(); }
  }
}
async function setPaused(paused: boolean) {
  const state = status.value;
  if (!state || changing.value || loading.value || disposed) return;
  clearTimeout(timer);
  const version = ++generation;
  changing.value = true;
  try {
    await post("/monitor/control", { paused, expected_revision: state.control_revision });
    if (!current(version)) return;
    status.value = null;
    error.value = "";
  } catch (value) {
    if (!current(version)) return;
    status.value = null;
    fail(value);
    error.value += " 操作未自动重发，请读取当前状态后再处理。";
  } finally {
    if (current(version)) {
      changing.value = false;
      await refresh();
    }
  }
}
onMounted(() => void refresh());
onUnmounted(() => { disposed = true; ++generation; clearTimeout(timer); });
</script>

<template>
  <section class="engine-panel monitor-panel" aria-labelledby="monitor-heading">
    <div class="section-heading monitor-heading">
      <div>
        <h2 id="monitor-heading">后台自动运行</h2>
        <p class="muted">新资料发布后自动比较、计算和查证。关闭网页不会停止后台。</p>
      </div>
      <div class="monitor-actions">
        <ElButton :loading="loading" :disabled="changing" @click="refresh">刷新后台状态</ElButton>
        <ElButton v-if="status?.enabled" :loading="changing" :disabled="loading"
          @click="setPaused(!status.paused)">{{ status.paused ? "恢复自动处理" : "暂停自动处理" }}</ElButton>
      </div>
    </div>
    <p v-if="error" class="error-panel" role="alert">{{ error }}</p>
    <template v-if="status">
      <p class="monitor-summary">
        <ElTag :type="status.process_status === 'running' ? 'success' : 'warning'">
          {{ processLabels[status.process_status] ?? "状态待核对" }}
        </ElTag>
        <span>上次心跳：{{ time(status.last_heartbeat) }}</span>
        <span>当前可见来源：{{ status.sources.length }}</span>
      </p>
      <p v-if="status.process_status === 'disabled'" class="notice">
        后台尚未启用。手动分析仍可使用；恢复按钮不会替代服务器启动配置。
      </p>
      <section v-if="status.publication_sources?.length">
        <h3>上游资料发布</h3>
        <div class="monitor-sources">
          <article v-for="publisher in status.publication_sources" :key="publisher.source_id">
            <strong>{{ publisher.source_id }} · {{ stateLabels[publisher.publisher_state] ?? "状态待核对" }}</strong>
            <p class="muted">上次心跳：{{ time(publisher.last_heartbeat) }}</p>
            <template v-if="publisher.counts && publisher.publisher_state !== 'unavailable'">
            <p class="muted">已发布 {{ publisher.counts?.published ?? 0 }} 条；等待发布 {{ publisher.counts?.pending ?? 0 }} 条；正在处理 {{ publisher.counts?.processing ?? 0 }} 条。</p>
            <p v-if="(publisher.counts?.blocked ?? 0) + (publisher.counts?.failed ?? 0)" class="notice">
              {{ publisher.counts?.blocked ?? 0 }} 条需要上游处理，{{ publisher.counts?.failed ?? 0 }} 条等待重试。
              这些资料未计入已发布数量，其余资料继续处理。
            </p>
            </template>
            <p v-if="publisher.disk_paused" class="notice">存储空间不足，发布已暂停；待处理资料仍保留。</p>
            <p v-if="publisher.error_code" class="error-panel">{{ publisher.error_code }}</p>
          </article>
        </div>
      </section>
      <h3>雷达处理进度</h3>
      <div class="monitor-sources">
        <article v-for="source in status.sources" :key="source.source_id + ':' + source.is_demo">
          <strong>{{ source.source_id }}</strong>
          <ElTag v-if="source.is_demo" type="warning" size="small">教学数据</ElTag>
          <p>{{ stateLabels[source.state] ?? "处理中" }} · 上次完成：{{ time(source.last_success_at) }}</p>
          <p class="muted">已确认位置 {{ source.acked_after }}<template v-if="source.scan_max_seq !== null">
            / 本轮读取至 {{ source.scan_max_seq }}</template>；计算窗口保留 {{ source.retained_records }} 条。</p>
          <p v-if="source.error_code" class="error-panel">本来源需要处理：{{ source.error_code }}</p>
        </article>
      </div>
      <p v-if="!status.sources.length" class="muted">尚未发现可监测来源。程序不会用空数据冒充计算完成。</p>
      <p class="muted">读取位置用于断点继续，不是全站覆盖量；每次计算和查证都有预算，未处理的工作保留在后台。暂停后不再接新批次，已经受理的计算可能继续完成。</p>
    </template>
    <div class="monitor-results">
      <section>
        <h3>最近的自动线索</h3>
        <article v-for="lead in leads?.items ?? []" :key="lead.id" class="monitor-item">
          <strong>{{ lead.title }}</strong>
          <p>
            <ElTag v-if="lead.new_to_radar" size="small">本轮首次发现的线索</ElTag>
            <ElTag v-if="lead.needs_investigation" size="small">本轮规则选中查证</ElTag>
            <ElTag v-if="lead.urgency === 'immediate'" size="small" type="danger">需要及时核实</ElTag>
            <span>{{ stateLabels[lead.state] ?? "状态待核对" }}</span>
          </p>
          <p class="muted">本轮涉及 {{ lead.changed_support_versions }} 个支持资料版本。线索仍需研究人员核对。</p>
          <p v-if="lead.importance_reason"><strong>为何值得查：</strong>{{ lead.importance_reason }}</p>
          <p v-if="lead.urgency_reason"><strong>紧急度依据：</strong>{{ lead.urgency_reason }}</p>
          <p class="muted">优先级与紧急度只安排研究核实；未证实高风险仍待人工核实，不是警报或批准。</p>
          <AssessmentAxesPanel :axes="lead.assessment_axes" />
          <p v-if="lead.data_gaps.length" class="muted">仍缺：{{ lead.data_gaps.join("；") }}</p>
          <div class="monitor-actions">
            <RouterLink v-if="lead.investigation_id" :to="{ path: '/investigations', query: { id: lead.investigation_id } }">查看调查、实际策略与原文</RouterLink>
            <ElButton text @click="emit('openRun', lead.run_id)">查看计算依据</ElButton>
          </div>
        </article>
        <p v-if="leads && !leads.items.length" class="muted">暂无自动线索。没有值得继续查的变化时，后台可以保持安静。</p>
      </section>
      <section>
        <h3>最近处理的资料</h3>
        <article v-for="batch in batches?.items ?? []" :key="batch.id" class="monitor-item">
          <strong>{{ batch.source_id }} · {{ stateLabels[batch.state] ?? "状态待核对" }}</strong>
          <p>{{ batch.changed_versions }} 个变更版本；本次计算采用 {{ batch.window_records }} 条。{{ time(batch.updated_at) }}</p>
          <p v-if="batch.selection_summary" class="muted">
            新到雷达 {{ batch.selection_summary.new_records }} 条；
            进入本轮计算窗口 {{ batch.selection_summary.evaluated }} 条，
            选中 {{ batch.selection_summary.selected }} 条，
            未选中 {{ batch.selection_summary.no_candidate }} 条，
            尚未完成评价 {{ batch.selection_summary.unknown }} 条。
          </p>
          <p v-if="batch.selection_summary" class="muted">未选中仅说明本轮已完成方法的选择范围；新到资料、值得查证与紧急程度分别判断。</p>
          <details v-if="batch.selection_summary && (batch.selection_summary.methods?.length || batch.selection_summary.unknown)">
            <summary>本轮方法与逐条评价范围</summary>
            <p class="muted">进入窗口仅表示参与本轮计算；每种方法的实际处理情况分别保留。</p>
            <ul>
              <li v-for="method in batch.selection_summary.methods" :key="method.method_id">
                {{ method.method_id }}：{{ methodStates[method.execution_status] ?? method.execution_status }}；
                有明确处理凭据的本批版本 {{ method.processed_manifest_versions }} 个，
                排除 {{ method.excluded_manifest_versions }} 个，截断诊断 {{ method.truncated_count }} 项。
              </li>
            </ul>
            <ul>
              <li v-for="record in selectionRecords(batch.selection_summary)" :key="record.source_record_id + record.version_id">
                {{ record.source_record_id }}：{{ outcomeLabels[record.outcome] }}；{{ unknownReasons[record.reason] ?? record.reason }}
              </li>
            </ul>
          </details>
          <ul v-if="batch.selection_summary?.limitations?.length" class="muted"><li v-for="item in batch.selection_summary.limitations" :key="item">{{ item }}</li></ul>
          <p v-if="batch.error_code" class="error-panel">{{ batch.error_code }}</p>
          <ElButton v-if="batch.run_id" text @click="emit('openRun', batch.run_id)">查看这次计算</ElButton>
        </article>
        <p v-if="batches && !batches.items.length" class="muted">还没有收到可处理的新版本。</p>
      </section>
    </div>
    <p v-if="batches?.unavailable_count || leads?.unavailable_count" class="notice">部分来源当前不可读取，其记录未显示。</p>
    <p v-if="batches?.next_cursor || leads?.next_cursor" class="muted">这里只显示最近一页，完整计算和调查记录仍在原列表中保留。</p>
    <details v-if="status">
      <summary>查看本轮处理预算与范围</summary>
      <dl><template v-for="(value, key) in status.limits" :key="key"><dt>{{ key === "investigations_per_batch" ? "每批自动调查上限" : key }}</dt><dd>{{ value }}</dd></template></dl>
      <ul><li v-for="item in status.limitations" :key="item">{{ item }}</li></ul>
    </details>
  </section>
</template>

<style scoped>
.monitor-panel { min-width: 0; overflow-wrap: anywhere; }
.monitor-heading, .monitor-actions, .monitor-summary { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
.monitor-summary { margin: 16px 0; }
.monitor-sources, .monitor-results { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.monitor-sources article, .monitor-item { min-width: 0; border-bottom: 1px solid var(--line, #e2e8f0); padding: 12px 0; }
.monitor-item p { display: flex; flex-wrap: wrap; gap: 6px; }
.monitor-actions a { color: var(--el-color-primary); }
dl { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px 12px; }
dd { margin: 0; }
@media (max-width: 760px) {
  .monitor-sources, .monitor-results { grid-template-columns: minmax(0, 1fr); }
}
</style>
