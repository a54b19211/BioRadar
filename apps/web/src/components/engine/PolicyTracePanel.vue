<script setup lang="ts">
import { computed } from "vue";
import type {
  PolicyQueryReceipt,
  QueryPolicyDecision,
  QueryPolicyTrace,
} from "../../api/queryPolicy";

const props = defineProps<{
  policy?: QueryPolicyTrace | null;
  queries?: PolicyQueryReceipt[];
}>();
const decisions = computed(() => props.policy?.decisions ?? []);
const checks = computed(() => props.policy?.checks ?? []);
const unsearched = computed(() => checks.value.filter((check) => check.status !== "passed"));
const latest = computed(() => decisions.value.at(-1));
const dimensions = computed(() => [...new Set([
  ...Object.keys(props.policy?.budget?.limits ?? {}),
  ...Object.keys(props.policy?.budget?.usage ?? {}).filter((key) => !["preparation_tool_calls", "known_tool_calls", "unknown_tool_calls", "reserved_tool_calls", "budgeted_tool_calls"].includes(key)),
  ...Object.keys(props.policy?.budget?.remaining ?? {}),
])]);
const policyNames: Record<string, string> = {
  baseline: "原有顺序基线",
  query_planner: "逐步查询策略",
};
const trainingNames: Record<string, string> = {
  heuristic_untrained: "未训练启发式",
  trained_in_memory: "进程内训练策略",
  llm_rules_baseline: "模型规划与固定规则基线",
};
const decisionNames: Record<string, string> = {
  continue: "选择下一动作", complete: "检索覆盖检查已满足",
  insufficient: "本轮停止，覆盖仍不足",
};
const statusNames: Record<string, string> = {
  running: "正在执行", complete: "本轮检索已结束", insufficient: "本轮检索覆盖不足",
  blocked: "等待核对执行结果", prepared: "已准备，尚未发送",
  sent: "已发送，等待回执", unconfirmed: "执行结果未确认", unknown: "执行结果未知",
  read: "已读取", empty: "未命中", completed: "已完成", failed: "执行失败",
  unavailable: "来源暂不可用", not_sent: "未发送",
};
const dimensionNames: Record<string, string> = {
  tool_calls: "查询动作", tokens: "token", relation_access: "关系读取",
};
const reasonNames: Record<string, string> = {
  selected_by_heuristic: "启发式选择本次动作",
  selected_by_baseline: "按基线顺序选择本次动作",
  legacy_sequence: "按原有查询顺序执行",
  retrieval_checks_satisfied: "已取得所声明覆盖项的原件",
  budget_exhausted_or_unknown_cost: "预算不足或候选费用尚不明确",
  sufficient: "所声明的检索覆盖检查已满足",
  budget_exhausted: "可用查询预算已用完",
  no_legal_action: "当前没有可执行的新动作",
  no_legal_actions: "当前没有可执行的新动作",
  max_steps: "已达到本轮动作上限",
  no_progress: "连续查询没有取得新资料",
  pending_execution_or_reconciliation: "先核对已发送动作的结果，再继续规划",
};
function number(value: number | null | undefined) {
  return typeof value === "number" ? value.toLocaleString() : value === null ? "未知" : "未记录";
}
function name(value?: string) {
  return value ? policyNames[value] ?? value : "未记录";
}
function reason(value?: string) {
  return value ? reasonNames[value] ?? value : "尚无停止原因";
}
function status(value: string) {
  return statusNames[value] ?? value;
}
function actualQuery(decision: QueryPolicyDecision) {
  const index = decision.receipt?.query_index;
  return typeof index === "number" ? props.queries?.[index] : undefined;
}
function coverLabel(id: string) {
  return checks.value.find((check) => check.id === id)?.question || id;
}
function measuredCosts(decision: QueryPolicyDecision) {
  return Object.entries(decision.receipt?.cost ?? {}).filter(([key]) => key !== "reserved_tool_calls");
}
function actionText(decision: QueryPolicyDecision) {
  const action = decision.action;
  if (!action) return "本次未选择执行动作";
  const args = action.arguments;
  const query = args.terms?.join(" + ") || args.query;
  return [args.provider, args.source_id, query || action.kind].filter(Boolean).join(" · ");
}
</script>

<template>
  <section class="policy-trace" aria-label="本版查询策略">
    <template v-if="policy">
      <h3>本版查询策略</h3>
      <p class="policy-summary">
        <strong>{{ name(policy.effective) }}</strong>
        <span>{{ trainingNames[policy.training_status ?? ""] ?? policy.training_status ?? "训练状态未记录" }}</span>
        <span>{{ status(policy.status) }}</span>
        <span v-if="policy.version">版本 {{ policy.version }}</span>
      </p>
      <p v-if="policy.requested !== policy.effective" class="notice">
        请求策略：{{ name(policy.requested) }}；本版实际策略：{{ name(policy.effective) }}。
      </p>
      <p class="muted">策略用于安排查询和检查检索覆盖；专业判断与报告核验见本版研究结果。</p>
      <p v-if="policy.status !== 'running' || latest?.decision !== 'continue'">
        <strong>停止或等待原因：</strong>{{ reason(policy.reason || latest?.reason) }}
      </p>
      <p v-if="policy.pending_action" class="notice">
        待核对动作 {{ policy.pending_action.id }}。核对原回执后再继续规划。
      </p>
      <p v-if="policy.evidence_time" class="muted">原件读取模式 {{ policy.evidence_time.mode }}；固定资料截止 {{ policy.evidence_time.cutoff }}。</p>
      <div v-if="dimensions.length" class="policy-budget">
        <strong>查询策略预算</strong>
        <span v-for="dimension in dimensions" :key="dimension">
          {{ dimensionNames[dimension] ?? dimension }}：已用 {{ number(policy.budget?.usage?.[dimension]) }} /
          上限 {{ number(policy.budget?.limits?.[dimension]) }}，剩余 {{ number(policy.budget?.remaining?.[dimension]) }}
        </span>
      </div>
      <p v-if="policy.budget?.usage?.preparation_tool_calls != null" class="muted">固定原文准备读取：{{ number(policy.budget.usage.preparation_tool_calls) }} 次，已计入查询策略实际用量。</p>
      <p v-if="policy.budget?.usage?.unknown_tool_calls" class="notice">{{ number(policy.budget.usage.unknown_tool_calls) }} 个动作的实际费用尚未确认；已确认 {{ number(policy.budget.usage.known_tool_calls) }} 次，预算另保守预留 {{ number(policy.budget.usage.reserved_tool_calls) }} 次，合计占用 {{ number(policy.budget.usage.budgeted_tool_calls) }} 次。</p>
      <template v-if="checks.length">
        <p><strong>未查完的覆盖项：{{ unsearched.length }} / {{ checks.length }}</strong></p>
        <ul class="policy-checks">
          <li v-for="check in checks" :key="check.id">
            <strong>{{ check.question || check.id }}</strong>：
            {{ check.status === "passed" ? "有已采用资料支撑覆盖" : check.status === "failed" ? "覆盖检查未满足" : "仍待查证" }}
            <span v-if="check.reason"> · {{ check.reason }}</span>
            <small v-if="check.evidence_ids?.length">采用资料：{{ check.evidence_ids.join("、") }}</small>
          </li>
        </ul>
      </template>
      <p v-else class="muted">本版尚未保存覆盖检查项。</p>
      <details :open="policy.status === 'blocked'" class="no-print">
        <summary>逐步动作、真实回执与下一决策（{{ decisions.length }} 次决策）</summary>
        <ol class="policy-decisions">
          <li v-for="(decision, index) in decisions" :key="index">
            <p><strong>决策 {{ index + 1 }} · {{ decisionNames[decision.decision] ?? decision.decision }}</strong></p>
            <p>{{ actionText(decision) }}</p>
            <p v-if="decision.action?.covers?.length" class="muted">
              本动作可能覆盖：{{ decision.action.covers.map(coverLabel).join("、") }}；完成情况以实际回执与覆盖检查为准。
            </p>
            <template v-if="decision.receipt">
              <p>
                <strong>实际回执：</strong>{{ status(decision.receipt.status) }}
                <template v-if="decision.receipt.error_code"> · {{ decision.receipt.error_code }}</template>
                <template v-if="decision.receipt.query_index != null"> · 查询记录 {{ decision.receipt.query_index + 1 }}</template>
              </p>
              <p v-if="actualQuery(decision)" class="muted">
                关联查询 {{ actualQuery(decision)?.action_id || decision.action?.id }}：
                {{ status(actualQuery(decision)!.status) }}，返回 {{ number(actualQuery(decision)?.returned) }} 条，
                新增版本 {{ number(actualQuery(decision)?.new_versions) }} 条。
              </p>
              <p v-if="decision.receipt.cost && Object.keys(decision.receipt.cost).length" class="muted">
                实际用量：
                <span v-for="entry in measuredCosts(decision)" :key="entry[0]">
                  {{ dimensionNames[entry[0]] ?? entry[0] }} {{ number(entry[1]) }}；
                </span>
              </p>
              <p v-if="decision.receipt.cost?.reserved_tool_calls" class="notice">本动作费用尚未确认，预算保守预留 {{ number(decision.receipt.cost.reserved_tool_calls) }} 次；原动作与回执保留。</p>
              <p v-if="decision.receipt.evidence_ids?.length" class="muted">
                本动作取得：{{ decision.receipt.evidence_ids.join("、") }}
              </p>
            </template>
            <p v-else-if="decision.action" class="muted">尚未保存本动作的执行回执。</p>
            <p><strong>决策理由：</strong>{{ reason(decision.reason) }}</p>
            <p v-if="decision.state_summary?.observed_history?.length" class="muted">
              本次决策已观察到：
              <span v-for="(observed, observedIndex) in decision.state_summary.observed_history" :key="observedIndex">
                {{ observed.action_id }}（{{ status(observed.status) }}）；
              </span>
            </p>
            <p v-else-if="index === 0" class="muted">首个决策，尚无本轮动作回执。</p>
            <p v-else class="muted">本次决策未保存已观察回执摘要。</p>
            <p v-if="decision.state_summary?.warnings?.length" class="notice">
              {{ decision.state_summary.warnings.join("；") }}
            </p>
          </li>
        </ol>
      </details>
      <details class="no-print">
        <summary>策略版本与原始轨迹</summary>
        <p>版本 {{ policy.version || "未记录" }}；选择器 {{ policy.selector || "未记录" }}；停止规则 {{ policy.stop || "未记录" }}。</p>
        <p v-if="policy.source_sha256">源码标识 {{ policy.source_sha256 }}</p>
        <pre>{{ JSON.stringify(policy, null, 2) }}</pre>
      </details>
    </template>
    <p v-else class="muted">旧版记录：本版保留原有查询与模型回执，尚无逐步策略轨迹。</p>
  </section>
</template>

<style scoped>
.policy-trace { margin-top: 18px; border-top: 1px solid var(--el-border-color); padding-top: 4px; overflow-wrap: anywhere; }
.policy-summary, .policy-budget { display: flex; flex-wrap: wrap; gap: 8px 20px; }
.policy-budget { margin: 12px 0; font-size: 13px; }
.policy-checks, .policy-decisions { padding-left: 22px; }
.policy-decisions > li { padding: 8px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.policy-decisions p { margin: 6px 0; }
small { display: block; color: var(--el-text-color-secondary); }
details { margin-top: 14px; }
summary { cursor: pointer; }
pre { white-space: pre-wrap; max-height: 360px; overflow: auto; font-size: 12px; }
</style>
