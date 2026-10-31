<script setup lang="ts">
import { computed } from "vue";
import PolicyTracePanel from "./PolicyTracePanel.vue";
import { queryPolicyArtifactAnchor, type QueryPolicyArtifactRef } from "../../api/queryPolicy";
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
const materialImpact = computed(() => props.analysis.progress?.material_update_impact);
const currentPolicy = computed(() => props.analysis.query_policy ?? props.analysis.result?.query_policy);
const readingBasis = computed(() => props.analysis.query_policy?.synthesis_reading_basis
  ?? props.analysis.result?.query_policy?.synthesis_reading_basis);
const synthesisStop = computed(() => props.analysis.query_policy?.synthesis_stop_reason
  ?? props.analysis.result?.query_policy?.synthesis_stop_reason);
const artifactKinds: Record<string, string> = {
  source_documents: "固定来源资料", analysis_dataset: "加工后的分析数据",
  pattern_result: "实际模式计算", professional_assessment: "专业评价结果",
};
function retainedArtifactLink(ref: QueryPolicyArtifactRef) {
  const policy = currentPolicy.value;
  const artifact = policy?.artifacts?.find(item => item.id === ref.id && item.version === ref.version
    && item.kind === "professional_assessment");
  return artifact && policy?.artifact_results?.[artifact.id] != null
    ? "#" + queryPolicyArtifactAnchor(ref) : undefined;
}
const reportAsOf = computed(() => materialImpact.value?.report_as_of);
const impactStates: Record<string, string> = {
  equivalent_inputs: "已比较的分析输入等同",
  affects_dependency: "新版本影响既有依据，需更新分析",
  impact_pending: "有新版本等待判断",
};
const impactReasons: Record<string, string> = {
  fixed_version_mapping_incomplete: "新固定版本的身份映射不完整",
  previous_fixed_version_unavailable: "此前固定版本暂不可用",
  complete_source_inputs_unavailable: "完整授权内容暂不可用",
  actual_dependency_source_inputs_changed: "实际支持、反证、限制或计算依据所用的来源输入已变化",
  complete_fixed_inputs_equal: "本次完整固定输入等同",
  changed_context_or_unmapped_dependency: "背景资料有变化，或尚不能确定其与旧判断的依赖关系",
};
function impactRef(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "未提供固定版本身份";
  const ref = value as Record<string, unknown>;
  return [ref.provider || ref.source_id, ref.object_id || ref.source_record_id, ref.adopted_version || ref.version_id]
    .filter((part): part is string => typeof part === "string" && part.length > 0).join(" / ") || "未提供固定版本身份";
}

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
    <section v-if="materialImpact" class="material-update-impact notice" role="status" aria-label="材料新版本影响判断">
      <strong>{{ impactStates[materialImpact.status] || "有新版本等待判断" }}</strong>
      <p v-if="materialImpact.status === 'equivalent_inputs'">系统只确认下述依据报告的本次已比较输入等同；这不代表下方其他版本已经完成比较或重新研判。原报告及核验状态保持原记录。</p>
      <p v-else-if="materialImpact.status === 'affects_dependency'">已发现影响下述依据报告的输入变化，复查结果另行保存；下方已有报告仍绑定自身原固定材料，尚未据此更新。</p>
      <p v-else>有新版本等待判断；下方已有报告仍绑定自身原固定材料，尚未据此更新。可沿本页已有追问或补查入口提出复核。</p>
      <p>本次影响比较依据的报告：{{ materialImpact.parent_analysis_id || "未记录，未推测对应报告" }}；该报告资料截止：{{ reportAsOf ? time(reportAsOf) : "未提供，未补成当前时点" }}。本次影响检查记录的修订号：{{ materialImpact.checked_revision }}。</p>
      <details v-if="materialImpact.decisions?.length" class="no-print">
        <summary>查看版本变化与影响依据（{{ materialImpact.decisions.length }} 条）</summary>
        <ul><li v-for="decision in materialImpact.decisions" :key="decision.entry_id + ':' + decision.revision">
          {{ impactStates[decision.status] || "影响尚待判断" }} · 修订 {{ decision.revision }} · {{ impactReasons[decision.reason] || decision.reason }}
          <p>原版本：{{ decision.old_refs?.length ? decision.old_refs.map(impactRef).join('；') : impactRef(decision.old_ref) }}；新版本：{{ impactRef(decision.new_ref) }}。</p>
          <span v-if="decision.affected_claim_ids?.length">受影响命题／计算依据：{{ decision.affected_claim_ids.join('、') }}。</span>
        </li></ul>
      </details>
    </section>
    <section v-if="readingBasis" class="retained-reading notice" role="status" aria-label="此前阅读草稿的输入范围">
      <strong>此前阅读草稿（综合未完成）</strong>
      <p>尚未纳入后续结果，不能作为当前结论。下方文字只保留原阅读输入下的草稿，不表示后续材料或专业产物已经被综合、核验或解决原有缺口。</p>
      <p>原阅读调用：{{ readingBasis.call_key }}；
        调用记录：{{ readingBasis.call_index != null ? '第 ' + (readingBasis.call_index + 1) + ' 条' : '旧记录未提供' }}。
        原阅读后的查询边界：{{ readingBasis.after_query_index != null ? '第 ' + (readingBasis.after_query_index + 1) + ' 条记录' : '未记录' }}；
        策略步号：{{ readingBasis.after_decision_step ?? '未记录' }}。
      </p>
      <p v-if="readingBasis.input_evidence">原阅读输入包含 {{ readingBasis.input_evidence.length }} 个选定片段、
        {{ readingBasis.input_artifact_refs?.length ?? '数量未记录的' }} 个固定产物；这是输入范围，不是事实覆盖或专业核验范围。</p>
      <p v-else>旧记录未保存原阅读输入清单，不能据此推断已纳入后续材料。</p>
      <p v-if="synthesisStop">综合停止原因：{{ waitingReasons[synthesisStop] || synthesisStop }}。</p>
      <p v-if="readingBasis.qualification" class="muted">{{ readingBasis.qualification }}</p>
      <template v-if="readingBasis.not_incorporated">
        <p>后续取得、尚未纳入此草稿：{{ readingBasis.not_incorporated.evidence.length }} 个选定片段、
          {{ readingBasis.not_incorporated.artifacts.length }} 个固定产物。</p>
        <ul v-if="readingBasis.not_incorporated.artifacts.length">
          <li v-for="artifact in readingBasis.not_incorporated.artifacts" :key="artifact.id + ':' + artifact.version">
            {{ artifactKinds[artifact.kind] || artifact.kind }} · {{ artifact.provider || '提供方未记录' }}；
            原样状态：{{ artifact.status ?? '未记录实际状态' }}；
            {{ artifact.id }} / {{ artifact.version }}。
            <a v-if="retainedArtifactLink(artifact)" class="no-print" :href="retainedArtifactLink(artifact)">查看这份专业产物记录</a>
            <span v-else class="muted">未找到可展开的对应专业产物记录，未猜测入口。</span>
          </li>
        </ul>
        <details v-if="readingBasis.not_incorporated.evidence.length" class="no-print">
          <summary>查看尚未纳入的后续片段</summary>
          <ul><li v-for="item in readingBasis.not_incorporated.evidence" :key="item.evidence_key">
            {{ item.evidence_key }} · {{ item.source_id || '来源未记录' }} · {{ impactRef(item.record_ref) }}；
            选定窗口 {{ item.start ?? '未记录' }}–{{ item.end ?? '未记录' }}（Unicode 字符）。
          </li></ul>
        </details>
      </template>
      <details class="no-print">
        <summary>原阅读输入与回执身份</summary>
        <p>调用 ID：{{ readingBasis.call_id }}；原阅读输入哈希：{{ readingBasis.reading_evidence_hash }}。</p>
        <ul><li v-for="item in readingBasis.input_evidence" :key="item.evidence_key">
          {{ item.evidence_key }} · {{ item.source_id || '来源未记录' }} · {{ impactRef(item.record_ref) }}；
          选定窗口 {{ item.start ?? '未记录' }}–{{ item.end ?? '未记录' }}（Unicode 字符）；
          片段 SHA-256 {{ item.text_sha256 || '未记录' }}。
        </li></ul>
        <ul><li v-for="artifact in readingBasis.input_artifact_refs" :key="artifact.id + ':' + artifact.version">
          {{ artifact.id }} / {{ artifact.version }}
        </li></ul>
      </details>
    </section>
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
