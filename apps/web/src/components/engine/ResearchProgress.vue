<script setup lang="ts">
import { computed } from "vue";
import PolicyTracePanel from "./PolicyTracePanel.vue";
import type { ResearchAnalysisView, ResearchBudgetView, ResearchProvider } from "../../api/research";

const props = defineProps<{ analysis: ResearchAnalysisView }>();
const stages: Record<string, string> = {
  queued: "等待执行", preparing: "固定本轮输入",
  question_plan: "理解问题并规划查询", initial_retrieval: "取得起始材料",
  read_plan: "解读材料并规划补查", digging: "补查关键缺口",
  read_plan_repair: "修复结构化阅读格式", narrative_extract: "从固定原文选择数值字段",
  synthesize: "综合证据形成分析", check: "核对陈述与引文",
  revise: "修订分析", recheck: "复核修订结果", compare: "比较前后判断",
  mechanism_evaluation: "评价机制证据", final_verification: "核验最终报告",
  biocheck_plan: "规划报告内容", biocheck_write: "生成报告正文",
  biocheck_check: "核验报告正文", biocheck_revise: "修订报告正文",
  biocheck_recheck: "复核报告正文", biocheck_support_audit: "复核所选支持片段",
  mech_decomposition: "拆分机制命题",
  mech_local_review: "核对局部机制证据", mech_mechanism_review: "评价机制证据链",
  completed: "调查完成", needs_review: "报告待核", failed: "本次未完成", blocked: "等待处理",
};
const callStates: Record<string, string> = {
  prepared: "准备调用", sent: "正在调用", received: "已收到结果",
  failed: "调用失败", unconfirmed: "结果尚未确认",
};
const providers: Record<string, string> = {
  biocheck: "BioCheck 报告生成与核验",
  mech_evidence: "MechEvidence 机制证据评价", weknora: "WeKnora 证据检索",
};
const waitingReasons: Record<string, string> = {
  analysis_manual_budget: "等待本小时研究额度恢复",
  analysis_provider_changed: "恢复时共享方法已更新，原回执保留，可另起研究",
  analysis_paused: "后台已暂停，恢复后可继续",
  analysis_model_budget: "本轮调用次数已用完，可调整预算开展新版研究",
  analysis_token_budget: "本轮 token 预算不足，可调整预算开展新版研究",
  analysis_time_budget: "本轮时间预算已用完，已保留过程记录",
  model_call_outcome_unconfirmed: "上次调用结果尚未确认，需先核对回执",
};
const budgetPools: Record<string, string> = { manual: "手动研究", monitor: "无对象监测", topic: "专题跟踪", material_update: "资料自动更新" };
const budgetWait = computed(() => props.analysis.progress?.budget_wait);
const fetchWait = computed(() => props.analysis.progress?.waiting_fetch);
function time(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}
const currentStage = computed(() => {
  const view = props.analysis;
  if (view.state === "queued" && budgetWait.value) return "等待预算恢复";
  if (view.state === "queued" && fetchWait.value) return "等待公开资料采集";
  const stage = view.progress?.stage || [...view.calls].reverse().find(call => call.status === "sent")?.stage || view.state;
  return stages[stage] || stage;
});
const limits = computed(() => props.analysis.budget?.limits ?? {});
const usage = computed<ResearchBudgetView["usage"]>(() => {
  const view = props.analysis;
  if (view.budget?.usage) return view.budget.usage;
  const sent = view.calls.filter(call => call.status !== "prepared");
  const known = sent.map(call => call.receipt?.usage?.total_tokens);
  const values = known.filter((value): value is number => typeof value === "number");
  const total = values.reduce((sum, value) => sum + value, 0);
  const unknown = known.length - values.length;
  return {
    calls: sent.length,
    queries: view.queries.filter(query => query.status !== "prepared").length,
    total_tokens: unknown ? null : total, known_total_tokens: total, unknown_usage_calls: unknown,
  };
});
function number(value: number | null | undefined) {
  return typeof value === "number" ? value.toLocaleString() : "尚无回执";
}
function providerState(value: ResearchProvider) {
  const results: Record<string, string> = {
    PASS: "原报告核验通过", REVIEW: "原报告待核", FAIL: "原报告未通过核验",
    completed: "本版已执行", not_requested: "本版未请求", needs_input: "本版仍缺输入",
    unavailable: "本版暂不可用", failed: "本版调用未完成",
    skipped_budget: "本轮预算保留给最终报告，未执行此项",
  };
  if (typeof value.result_status === "string") return results[value.result_status] || value.result_status;
  const states: Record<string, string> = {
    ready: "配置就绪", configured: "已配置", unavailable: "暂不可用",
    not_configured: "未配置", disabled: "未启用", failed: "调用失败",
  };
  const state = value.status || value.state;
  return state ? states[state] || state : value.configured ? "已配置" : "状态待确认";
}
</script>

<template>
  <div class="research-progress" aria-live="polite">
    <div class="research-current-stage">
      <strong>{{ currentStage }}</strong>
      <span v-if="analysis.progress?.waiting_reason && !['blocked', 'failed'].includes(analysis.state)">
        {{ waitingReasons[analysis.progress.waiting_reason] || "等待相关条件恢复，具体原因见执行记录。" }}
      </span>
    </div>
    <p v-if="fetchWait" class="fetch-wait notice" role="status">
      公开来源补采任务已排队，正在等待实际采集回执；下次检查：<strong>{{ time(fetchWait.next_poll_at) }}</strong>。
      已有资料与回执保留，这段排队不表示采集失败。
    </p>
    <p v-if="budgetWait" class="budget-wait notice" role="status">
      {{ budgetPools[budgetWait.pool] || budgetWait.pool }}额度暂不可用，任务在队列中等待。
      预计恢复时点：<strong>{{ time(budgetWait.next_budget_at) }}</strong>。
      已有回执与材料保留；这段等待不表示调查失败，也不计作执行时间。
    </p>
    <p v-else-if="analysis.progress?.budget_pool" class="muted">本轮预算池：{{ budgetPools[analysis.progress.budget_pool] || analysis.progress.budget_pool }}。</p>
    <div class="research-usage">
      <span>调用 {{ number(usage.calls) }}<template v-if="limits.max_calls != null"> / {{ number(limits.max_calls) }}</template> 次</span>
      <span>检索 {{ number(usage.queries) }}<template v-if="limits.max_queries != null"> / {{ number(limits.max_queries) }}</template> 次</span>
      <span>已知 token {{ number(usage.known_total_tokens ?? usage.total_tokens) }}<template v-if="limits.max_tokens != null"> / {{ number(limits.max_tokens) }}</template></span>
      <span v-if="usage.elapsed_seconds != null">已执行 {{ Math.round(usage.elapsed_seconds) }} 秒<template v-if="limits.max_seconds != null"> / {{ limits.max_seconds }} 秒</template></span>
      <span>费用：{{ usage.monetary_cost == null ? "未配置费率" : usage.monetary_cost }}</span>
    </div>
    <p v-if="usage.estimated_tokens" class="muted">
      预算占用 {{ number(usage.budgeted_tokens) }} token，其中 {{ number(usage.estimated_tokens) }} 为缺少回执时的保守预留。
    </p>
    <p v-if="usage.unknown_usage_calls" class="muted">{{ usage.unknown_usage_calls }} 次调用尚无 token 用量回执，累计量暂不完整。</p>
    <p v-if="['queued', 'running'].includes(analysis.state)" class="muted" role="status">任务在后台继续执行；刷新或离开页面后，可从这项调查重新打开。</p>
    <p v-if="['blocked', 'failed'].includes(analysis.state) && !analysis.result?.report?.final_text" class="notice">
      本版尚未保存最终报告正文，不能视为报告核验已完成。
      <template v-if="analysis.progress?.waiting_reason">阻断原因：{{ waitingReasons[analysis.progress.waiting_reason] || analysis.progress.waiting_reason }}。</template>
    </p>
    <ol v-if="analysis.calls.length" class="research-stage-list no-print">
      <li v-for="call in analysis.calls" :key="String(call.call_id || call.stage)">
        <strong>{{ stages[call.stage] || call.stage }}</strong>
        <span>{{ callStates[call.status] || call.status }}</span>
        <span v-if="call.receipt?.usage?.total_tokens != null">{{ number(call.receipt.usage.total_tokens) }} token</span>
        <span v-if="call.receipt?.elapsed_ms != null">{{ (call.receipt.elapsed_ms / 1000).toFixed(1) }} 秒</span>
      </li>
    </ol>
    <PolicyTracePanel :policy="analysis.query_policy ?? analysis.result?.query_policy" :queries="analysis.queries" />
    <details v-if="analysis.providers && Object.keys(analysis.providers).length" class="no-print">
      <summary>本版共享服务状态</summary>
      <ul><li v-for="(value, name) in analysis.providers" :key="name">
        {{ providers[name] || name }}：{{ providerState(value) }}
        <span v-if="value.message || value.reason"> · {{ value.message || value.reason }}</span>
      </li></ul>
    </details>
  </div>
</template>

<style scoped>
.research-progress { margin: 18px 0; padding: 16px; border: 1px solid var(--el-border-color); border-radius: 10px; background: var(--el-fill-color-light); }
.research-current-stage { display: flex; flex-wrap: wrap; gap: 12px; }
.research-usage { display: flex; flex-wrap: wrap; gap: 8px 22px; margin: 10px 0; font-size: 13px; }
.research-stage-list { padding-left: 22px; }
.research-stage-list li { padding: 5px 0; }
.research-stage-list li span { margin-left: 12px; font-size: 13px; color: var(--el-text-color-secondary); }
</style>
