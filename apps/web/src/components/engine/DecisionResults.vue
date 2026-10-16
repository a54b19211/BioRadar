<script setup lang="ts">
import { computed, onUnmounted, ref } from "vue";
import { ElButton, ElDrawer, ElOption, ElSelect, ElTag } from "element-plus";
import {
  engineRequest,
  type DecisionRunView,
  type DecisionInput,
  type NormalizedSnapshot,
} from "../../api/engine";
import { decisionExplanation } from "../../api/decisionLabels";
type Basis = DecisionInput["scenarios"][number]["basis"];
type EvidenceLink = NonNullable<Basis["evidence"]>[number];
const props = defineProps<{ run: DecisionRunView }>();
const input = computed(() => props.run.decision_input!);
const result = computed(() => props.run.result!);
const scenarioId = ref(
  props.run.decision_input?.scenarios[0]?.scenario_id ?? "",
);
const basisLimit = ref(40);
const sourceOpen = ref(false);
const sourceLoading = ref(false);
const sourceError = ref("");
const sourceRecord = ref<NormalizedSnapshot["records"][number] | null>(null);
const sourceLink = ref<EvidenceLink | null>(null);
const sourceIndicator = computed(() => {
  const matched = sourceLink.value?.field_path?.match(
    /^\/regional_indicators\/(0|[1-9]\d*)\/value$/,
  );
  return matched
    ? sourceRecord.value?.regional_indicators?.[Number(matched[1])]
    : undefined;
});
let sourceVersion = 0;
const gateLabels = {
  pass: "满足已声明门槛",
  fail: "不满足门槛",
  uncertain: "区间跨越门槛",
  unknown: "输入未知",
};
const basisLabels = {
  observed: "观察数据（提交者声明）",
  declared_assumption: "署名假设",
  supplied_model_result: "外部模型估计",
};
const relationLabels = {
  a_dominates: "A 在声明指标上保证优于 B",
  b_dominates: "B 在声明指标上保证优于 A",
  tied: "点值相同",
  incomparable: "存在取舍或区间重叠",
  unknown: "缺少可比较输入",
};
const questionLabels = {
  hard_gate: "门槛需要核实",
  missing_comparison: "比较缺少输入",
  overlapping_comparison: "区间重叠待核实",
  governance: "研究声明待补",
  source: "来源需要核实",
};
const optionName = (id: string) =>
  input.value.options.find((item) => item.option_id === id)?.label ?? id;
const metricName = (id: string) =>
  input.value.metrics.find((item) => item.metric_id === id)?.label ?? id;
const constraintName = (id: string) =>
  input.value.constraints?.find((item) => item.constraint_id === id)?.label ??
  id;
const optionGate = (id: string) =>
  result.value.feasibility?.find((item) => item.option_id === id);
const gateSummary = computed(() => ({
  passed: (result.value.feasibility ?? [])
    .filter((item) => item.status === "pass" && item.checks?.length)
    .map((item) => optionName(item.option_id)),
  failed: (result.value.feasibility ?? [])
    .filter((item) => item.status === "fail")
    .map((item) => optionName(item.option_id)),
  unresolved: (result.value.feasibility ?? [])
    .filter(
      (item) =>
        ["uncertain", "unknown"].includes(item.status) || !item.checks?.length,
    )
    .map((item) => optionName(item.option_id)),
}));
function interval(
  value: { lower: string | number; upper: string | number } | null | undefined,
) {
  if (!value) return "未知";
  return String(value.lower) === String(value.upper)
    ? String(value.lower)
    : `${value.lower}～${value.upper}`;
}
const cells = computed(
  () =>
    new Map(
      (input.value.outcomes ?? []).map((cell) => [
        JSON.stringify([cell.option_id, cell.scenario_id, cell.metric_id]),
        cell,
      ]),
    ),
);
const cell = (option: string, metric: string) =>
  cells.value.get(JSON.stringify([option, scenarioId.value, metric]));
const delta = (option: string, metric: string) =>
  result.value.baseline_differences?.find(
    (item) =>
      item.option_id === option &&
      item.metric_id === metric &&
      item.scenario_id === scenarioId.value,
  );
const selectedScenario = computed(() =>
  input.value.scenarios.find((item) => item.scenario_id === scenarioId.value),
);
const pareto = computed(() =>
  (result.value.pareto_comparisons ?? []).filter(
    (item) => item.scenario_id == null,
  ),
);
const basisRows = computed(() => {
  const rows: { label: string; basis: Basis }[] = [];
  if (input.value.brief.why_now)
    rows.push({ label: "为何现在", basis: input.value.brief.why_now.basis });
  for (const scenario of input.value.scenarios)
    if (scenario.scenario_id === scenarioId.value)
      rows.push({ label: `情景：${scenario.label}`, basis: scenario.basis });
  for (const outcome of input.value.outcomes ?? [])
    if (outcome.scenario_id === scenarioId.value)
      rows.push({
        label: `${optionName(outcome.option_id)} · ${metricName(outcome.metric_id)}`,
        basis: outcome.basis,
      });
  for (const constraint of input.value.constraints ?? [])
    rows.push({ label: `门槛：${constraint.label}`, basis: constraint.basis });
  for (const option of input.value.options) {
    if (option.implementation_owner)
      rows.push({
        label: `${option.label} · 实施责任`,
        basis: option.implementation_owner.basis,
      });
    for (const [label, statements] of [
      ["实施步骤", option.implementation_steps],
      ["受益群体", option.beneficiaries],
      ["受损群体", option.adversely_affected],
      ["不可逆承诺", option.irreversible_commitments],
    ] as const)
      for (const statement of statements ?? [])
        rows.push({
          label: `${option.label} · ${label}`,
          basis: statement.basis,
        });
  }
  const plan = input.value.brief.review_plan;
  if (plan) {
    rows.push({ label: "复查计划", basis: plan.basis });
    for (const statement of [
      ...(plan.trigger_indicators ?? []),
      ...(plan.change_conditions ?? []),
    ])
      rows.push({ label: statement.text, basis: statement.basis });
  }
  for (const [role, disclosure] of Object.entries(
    input.value.brief.interest_declarations ?? {},
  ))
    if (disclosure.statement)
      rows.push({
        label: `${role} · 利益声明`,
        basis: disclosure.statement.basis,
      });
  return rows;
});
const roleLabels: Record<string, string> = {
  commissioner: "委托方",
  funder: "资助方",
  author: "作者",
  reviewer: "审阅者",
};
const interestLabels: Record<string, string> = {
  unknown: "尚未声明",
  declared_none: "提交者声明无相关利益",
  declared_interest: "已声明相关利益",
};
function explainGap(value: string) {
  try {
    const parts = JSON.parse(value);
    if (!Array.isArray(parts)) return decisionExplanation(value);
    const scenarioName = (id: string) =>
      input.value.scenarios.find((item) => item.scenario_id === id)?.label ??
      id;
    if (parts[0] === "hard_gate")
      return `${optionName(parts[1])} / ${scenarioName(parts[2])}：${constraintName(parts[3])}仍待核实。`;
    if (parts[0] === "missing_outcome")
      return `${optionName(parts[1])} / ${scenarioName(parts[2])}：缺少${metricName(parts[3])}，未填零。`;
    if (parts[0] === "interest_declaration_unknown")
      return `${roleLabels[parts[1]] ?? parts[1]}的利益声明尚未提供。`;
    if (parts[0] === "option_statement_unknown") {
      const fields: Record<string, string> = {
        implementation_owner: "实施责任",
        implementation_steps: "实施步骤",
        beneficiaries: "受益群体",
        adversely_affected: "可能受损群体",
        irreversible_commitments: "不可逆承诺",
      };
      return `${optionName(parts[1])}：尚未说明${fields[parts[2]] ?? parts[2]}。`;
    }
  } catch {
    /* Unknown diagnostics remain visible, never become a success message. */
  }
  return decisionExplanation(value);
}
const disclosure = (role: string) =>
  Object.entries(input.value.brief.interest_declarations ?? {}).find(
    ([key]) => key === role,
  )?.[1];
async function showSource(link: EvidenceLink) {
  const version = ++sourceVersion;
  sourceLink.value = link;
  sourceOpen.value = true;
  sourceRecord.value = null;
  sourceError.value = "";
  sourceLoading.value = true;
  try {
    const binding = props.run.source_snapshots.find(
      (item) => item.snapshot_hash === link.snapshot_hash,
    );
    if (!binding) throw new Error("当前固定运行未绑定此来源，不能展示正文。");
    const adopted = await engineRequest<{ snapshot: NormalizedSnapshot }>(
      `/snapshots/${encodeURIComponent(binding.id)}`,
    );
    if (version !== sourceVersion) return;
    const keys = [
      "provider",
      "object_type",
      "object_id",
      "adopted_version",
      "version_source",
      "content_hash",
      "authorization_ref",
    ] as const;
    const record = adopted.snapshot.records.find((item) =>
      keys.every((key) => item.record_ref[key] === link.record_ref[key]),
    );
    if (!record) throw new Error("完整版本引用未与采用资料匹配。");
    sourceRecord.value = record;
  } catch (value) {
    if (version === sourceVersion)
      sourceError.value =
        value instanceof Error ? value.message : "来源读取失败";
  } finally {
    if (version === sourceVersion) sourceLoading.value = false;
  }
}
function closeSource() {
  ++sourceVersion;
  sourceOpen.value = false;
  sourceRecord.value = null;
  sourceLink.value = null;
}
onUnmounted(() => {
  ++sourceVersion;
});
</script>

<template>
  <p class="notice">
    当前委托的决策准备草案。数值依据、模型适用性和利益声明仍需独立审阅；没有自动形成专业建议或批准行动。
  </p>
  <section class="engine-panel decision-brief">
    <h2>需要作出的决策</h2>
    <p class="decision-question">{{ input.brief.decision_question }}</p>
    <dl class="metric-list">
      <dt>为何现在</dt>
      <dd>
        {{ input.brief.why_now?.text ?? "尚未说明变化或决策窗口，需补充" }}
      </dd>
      <dt>职责与可采取行动</dt>
      <dd>{{ input.brief.mandate }}</dd>
      <dt>适用范围</dt>
      <dd>{{ input.brief.scope_description }}</dd>
      <dt>现状基线</dt>
      <dd>{{ optionName(input.brief.baseline_option_id) }}</dd>
      <dt>决策截止</dt>
      <dd>{{ input.brief.decision_deadline }}</dd>
      <dt>结果观察期间</dt>
      <dd>{{ input.brief.horizon_start }} 至 {{ input.brief.horizon_end }}</dd>
      <dt>证据截止</dt>
      <dd>{{ input.brief.as_of }}</dd>
      <dt>本次采用</dt>
      <dd>
        {{ run.accepted_at }} · {{ run.submitted_by }}（系统记录的提交者）
      </dd>
    </dl>
    <p class="muted">
      这是当前分析。当前填写的假设和模型估计，不表示历史当时已经知道。
    </p>
  </section>
  <section class="engine-panel decision-summary">
    <h2>本次比较能支持到哪一步</h2>
    <div class="decision-takeaways">
      <div>
        <strong>满足已声明门槛</strong>
        <p>{{ gateSummary.passed.join("、") || "尚无可确认的方案" }}</p>
      </div>
      <div>
        <strong>不满足已声明门槛</strong>
        <p>{{ gateSummary.failed.join("、") || "尚无明确不满足的方案" }}</p>
      </div>
      <div>
        <strong>还不能确认</strong>
        <p>
          {{ gateSummary.unresolved.join("、") || "当前声明门槛中暂无未决项" }}
        </p>
      </div>
    </div>
    <h3>当前优先核实</h3>
    <ol class="evidence-list">
      <li
        v-for="item in result.investigation_questions?.slice(0, 3)"
        :key="item.question_id"
      >
        {{ item.question }}
      </li>
    </ol>
    <p class="muted">
      门槛检查覆盖本次明确提供的条件。后面的方案取舍仍需结合指标目标、未覆盖条件和专业审阅。
    </p>
  </section>
  <section class="engine-panel">
    <div class="section-heading">
      <h2>方案与硬约束</h2>
      <span class="muted">跨全部声明情景检查</span>
    </div>
    <article
      v-for="option in input.options"
      :key="option.option_id"
      class="decision-option"
    >
      <div class="section-heading">
        <h3>
          {{ option.label }}
          <small v-if="option.option_id === input.brief.baseline_option_id"
            >现状基线</small
          >
        </h3>
        <ElTag
          :type="
            optionGate(option.option_id)?.checks?.length &&
            optionGate(option.option_id)?.status === 'pass'
              ? 'success'
              : 'warning'
          "
        >
          {{
            optionGate(option.option_id)?.checks?.length
              ? gateLabels[optionGate(option.option_id)?.status ?? "unknown"]
              : "未声明硬约束"
          }}
        </ElTag>
      </div>
      <p>{{ option.description }}</p>
      <details>
        <summary>检查项、受影响群体与实施条件</summary>
        <div class="decision-table-wrap">
          <table class="decision-table">
            <thead>
              <tr>
                <th>情景 / 门槛</th>
                <th>输入区间</th>
                <th>阈值</th>
                <th>检查</th>
                <th>缺口</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="check in result.feasibility?.find(
                  (item) => item.option_id === option.option_id,
                )?.checks ?? []"
                :key="JSON.stringify([check.scenario_id, check.constraint_id])"
              >
                <td>
                  {{
                    input.scenarios.find(
                      (s) => s.scenario_id === check.scenario_id,
                    )?.label
                  }}
                  / {{ constraintName(check.constraint_id) }}
                </td>
                <td>{{ interval(check.value) }}</td>
                <td>{{ interval(check.threshold) }}</td>
                <td>{{ gateLabels[check.status] }}</td>
                <td>{{ interval(check.shortfall) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <dl class="metric-list">
          <dt>实施责任</dt>
          <dd>
            {{
              option.implementation_owner?.text ?? "未提供，需核实授权与责任"
            }}
          </dd>
          <dt>受益群体</dt>
          <dd>
            {{
              option.beneficiaries?.map((item) => item.text).join("；") ||
              "未提供，不能推定无人受益"
            }}
          </dd>
          <dt>可能受损群体</dt>
          <dd>
            {{
              option.adversely_affected?.map((item) => item.text).join("；") ||
              "未提供，不能推定无人受损"
            }}
          </dd>
          <dt>不可逆承诺</dt>
          <dd>
            {{
              option.irreversible_commitments
                ?.map((item) => item.text)
                .join("；") || "未提供，需核实"
            }}
          </dd>
          <dt>实施步骤</dt>
          <dd>
            {{
              option.implementation_steps
                ?.map((item) => item.text)
                .join("；") || "未提供，需核实"
            }}
          </dd>
        </dl>
      </details>
    </article>
  </section>
  <section class="engine-panel">
    <div class="section-heading">
      <h2>同口径的结果与现状比较</h2>
      <ElSelect
        v-model="scenarioId"
        aria-label="比较情景"
        class="decision-scenario"
        ><ElOption
          v-for="scenario in input.scenarios"
          :key="scenario.scenario_id"
          :value="scenario.scenario_id"
          :label="scenario.label"
      /></ElSelect>
    </div>
    <p class="muted">
      {{
        selectedScenario?.description
      }}。情景用于比较条件，不表示发生概率。差值按“方案减现状”显示。
    </p>
    <div class="decision-table-wrap">
      <table class="decision-table">
        <thead>
          <tr>
            <th>方案</th>
            <th v-for="metric in input.metrics" :key="metric.metric_id">
              {{ metric.label
              }}<small
                >{{ metric.unit }} ·
                {{
                  metric.direction === "maximize" ? "目标为增加" : "目标为减少"
                }}</small
              >
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="option in input.options" :key="option.option_id">
            <th>{{ option.label }}</th>
            <td v-for="metric in input.metrics" :key="metric.metric_id">
              {{ interval(cell(option.option_id, metric.metric_id)?.value) }}
              <small
                v-if="cell(option.option_id, metric.metric_id)?.unknown_reason"
                >{{
                  cell(option.option_id, metric.metric_id)?.unknown_reason
                }}</small
              >
              <small v-if="delta(option.option_id, metric.metric_id)"
                >较现状差异：{{
                  interval(
                    delta(option.option_id, metric.metric_id)?.difference,
                  )
                }}</small
              >
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <details>
      <summary>指标口径与适用范围</summary>
      <dl
        v-for="metric in input.metrics"
        :key="metric.metric_id"
        class="metric-list"
      >
        <dt>{{ metric.label }}</dt>
        <dd>{{ metric.definition }} · {{ metric.comparability_basis }}</dd>
        <dt>范围与期间</dt>
        <dd>
          {{ metric.population }} / {{ metric.geography }} /
          {{ metric.jurisdiction }} · {{ metric.period_start }} 至
          {{ metric.period_end }}
        </dd>
        <dt>价格基期</dt>
        <dd>{{ metric.price_base ?? "未提供或不适用；不能自行换算" }}</dd>
        <dt>统计分母</dt>
        <dd>{{ metric.denominator ?? "未提供，需按指标定义判断可比性" }}</dd>
      </dl>
    </details>
    <h3>跨声明情景的多目标取舍</h3>
    <p class="muted">
      只有在所选指标与区间上都能保证不差，且至少一项更好，才显示区间优势。它不代替预算和实施资格检查。
    </p>
    <p>
      本次条件性比较集合：{{
        result.conditional_comparison_set?.map(optionName).join("、") ||
        "尚未形成"
      }}。
    </p>
    <ul class="evidence-list">
      <li v-for="(reasons, id) in result.excluded_from_comparison" :key="id">
        {{ optionName(String(id)) }} 未纳入本次比较：{{
          reasons.map(decisionExplanation).join("；")
        }}。原方案保留，不能据此称它没有价值。
      </li>
    </ul>
    <ul class="evidence-list">
      <li
        v-for="comparison in pareto"
        :key="JSON.stringify([comparison.option_a, comparison.option_b])"
      >
        A：{{ optionName(comparison.option_a) }}；B：{{
          optionName(comparison.option_b)
        }}
        — {{ relationLabels[comparison.status] }}
      </li>
    </ul>
    <p v-if="!pareto.length" class="muted">
      没有形成跨情景比较，请检查备选方案和输入。
    </p>
  </section>
  <section class="engine-panel">
    <h2>不同情景下，选择的代价</h2>
    <p v-if="result.regret.status === 'not_requested'" class="muted">
      未指定同单位的后悔指标，本次保留多目标比较。
    </p>
    <template v-else>
      <p class="muted">
        “后悔”表示在给定情景下，相对可比方案损失了多少该指标。不是后悔概率或收益预测。
      </p>
      <p>
        比较指标：{{ metricName(result.regret.metric_id ?? "") }}（{{
          result.regret.unit ?? "未知单位"
        }}）
      </p>
      <p>
        条件性可比集合：{{
          result.regret.conditional_comparison_set
            ?.map(optionName)
            .join("、") || "尚未形成"
        }}
      </p>
      <div class="decision-table-wrap">
        <table class="decision-table">
          <thead>
            <tr>
              <th>方案</th>
              <th>声明情景内的最坏后悔区间</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(value, id) in result.regret.worst_case_bounds"
              :key="id"
            >
              <td>{{ optionName(String(id)) }}</td>
              <td>{{ interval(value) }} {{ result.regret.unit }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="result.regret.least_upper_bound_options?.length">
        当前集合中，上界最小的方案：{{
          result.regret.least_upper_bound_options.map(optionName).join("、")
        }}。这不代表全体方案最优。
      </p>
      <p class="muted">
        {{
          result.regret.interpretation === "exact_on_declared_scenarios"
            ? "采用声明场景中的点值计算。"
            : "区间为相关性未知时的保守包络，不保证所有端点能同时发生。"
        }}
      </p>
      <ul class="evidence-list">
        <li v-for="id in result.regret.excluded_options" :key="id">
          {{ optionName(id) }}：{{
            result.regret.exclusion_reasons?.[id]
              ?.map(decisionExplanation)
              .join("；") || "未进入当前可比集合，请核实输入"
          }}
        </li>
      </ul>
      <p
        v-for="reason in result.regret.limitations"
        :key="reason"
        class="muted"
      >
        {{ decisionExplanation(reason) }}
      </p>
    </template>
  </section>
  <section class="engine-panel">
    <h2>先核实哪些问题</h2>
    <p class="muted">
      按阻断门槛、比较缺口等类型整理，尚未自动发起调查，也未估计补证的货币收益。
    </p>
    <ol class="decision-questions">
      <li
        v-for="question in result.investigation_questions"
        :key="question.question_id"
      >
        <ElTag size="small">{{ questionLabels[question.category] }}</ElTag>
        <p>{{ question.question }}</p>
        <small
          >{{ question.option_ids?.map(optionName).join("、") }}
          {{
            question.metric_id ? " · " + metricName(question.metric_id) : ""
          }}</small
        >
      </li>
    </ol>
    <p v-if="result.questions_omitted" class="notice">
      展示达到本次问题预算，另有
      {{ result.questions_omitted }} 项未列出，不能据此称核查完整。
    </p>
    <details>
      <summary>其余输入缺口与计算范围</summary>
      <ul class="evidence-list">
        <li v-for="gap in result.data_gaps" :key="gap">
          {{ explainGap(gap) }}
        </li>
        <li v-for="limit in result.limitations" :key="limit">
          {{ decisionExplanation(limit) }}
        </li>
      </ul>
    </details>
  </section>
  <section class="engine-panel">
    <h2>依据、估计与署名假设</h2>
    <p class="muted">
      以下是提交者对输入性质的声明。原文匹配只证明引用对应采用资料，不自动证明数字、预测或因果成立。
    </p>
    <details
      v-for="(row, index) in basisRows.slice(0, basisLimit)"
      :key="index"
      class="decision-basis"
    >
      <summary>{{ row.label }} · {{ basisLabels[row.basis.kind] }}</summary>
      <p>{{ row.basis.rationale }}</p>
      <p class="muted">
        声明作者：{{ row.basis.claimed_author }} · 声明时间：{{
          row.basis.authored_at
        }}（未经身份或专业审批认证）
      </p>
      <p v-if="row.basis.model_description">
        模型说明：{{ row.basis.model_description }}
      </p>
      <div
        v-for="(link, i) in row.basis.evidence ?? []"
        :key="i"
        class="decision-citation"
      >
        <blockquote v-if="link.quote">{{ link.quote }}</blockquote>
        <p v-if="link.field_path" class="muted">
          采用字段：{{ link.field_path }}
        </p>
        <ElButton text type="primary" @click="showSource(link)"
          >查看固定来源 {{ i + 1 }}</ElButton
        >
      </div>
      <p v-if="!row.basis.evidence?.length" class="muted">
        此声明没有绑定来源，不能作为已验证事实。
      </p>
    </details>
    <ElButton
      v-if="basisRows.length > basisLimit"
      text
      @click="basisLimit += 40"
      >显示更多依据</ElButton
    >
  </section>
  <section class="engine-panel">
    <h2>利益声明与复查安排</h2>
    <dl class="metric-list">
      <template v-for="(label, role) in roleLabels" :key="role"
        ><dt>{{ label }}</dt>
        <dd>
          {{ interestLabels[disclosure(role)?.status ?? "unknown"] }}
          <p v-if="disclosure(role)?.statement">
            {{ disclosure(role)?.statement?.text }}
          </p>
        </dd></template
      >
    </dl>
    <p class="muted">
      声明无相关利益不等于已经独立核查；用途和委托身份不会扩大资料访问权限。
    </p>
    <template v-if="input.brief.review_plan"
      ><p>计划复查：{{ input.brief.review_plan.review_at ?? "日期未定" }}</p>
      <h3>应再次观察的指标</h3>
      <ul class="evidence-list">
        <li
          v-for="(item, index) in input.brief.review_plan.trigger_indicators"
          :key="index"
        >
          {{ item.text }}
        </li>
      </ul>
      <h3>可能改变结论的条件</h3>
      <ul class="evidence-list">
        <li
          v-for="(item, index) in input.brief.review_plan.change_conditions"
          :key="index"
        >
          {{ item.text }}
        </li>
      </ul>
      <p class="muted">
        复查安排目前仅作为声明保存，尚未建立自动监测或通知。
      </p></template
    >
    <p v-else class="muted">
      尚未提供复查计划。需明确观察什么、何时复查，以及哪些条件会改变判断。
    </p>
  </section>
  <ElDrawer
    :model-value="sourceOpen"
    title="决策输入的固定来源"
    size="min(820px,96vw)"
    @close="closeSource"
  >
    <p v-if="sourceLoading">正在读取授权资料…</p>
    <p v-if="sourceError" class="error-panel">{{ sourceError }}</p>
    <template v-if="sourceRecord">
      <p
        v-if="
          sourceRecord.record_flags?.some((flag) =>
            ['retracted', 'deleted'].includes(flag),
          )
        "
        class="error-panel"
      >
        此固定来源带有撤回或删除标记，不能作为已观察事实使用。引用它的假设或模型依据需要重新核实。
      </p>
      <p class="muted">
        {{ sourceRecord.source_instance }} / {{ sourceRecord.source_id }} ·
        {{ sourceRecord.record_ref.object_id }}
      </p>
      <blockquote v-if="sourceLink?.quote">{{ sourceLink.quote }}</blockquote>
      <dl class="metric-list">
        <dt>来源状态标记</dt>
        <dd>
          {{
            sourceRecord.record_flags?.join("、") ||
            "未提供状态标记；不等于已证明有效"
          }}
        </dd>
        <dt>采用版本</dt>
        <dd>{{ sourceRecord.record_ref.adopted_version }}</dd>
        <dt>本次观察</dt>
        <dd>{{ sourceRecord.observed_at }}</dd>
        <dt>上游可见时间</dt>
        <dd>{{ sourceRecord.available_at ?? "未提供" }}</dd>
        <dt>来源日期</dt>
        <dd>
          {{ sourceRecord.published_time_raw ?? "未知" }} ·
          {{
            sourceRecord.publication_time_verified
              ? "公开时间含义已确认"
              : "含义未确认，不能当发布时间"
          }}
        </dd>
      </dl>
      <section
        v-if="sourceIndicator"
        class="source-reading decision-typed-source"
      >
        <h3>实际采用的结构化字段</h3>
        <p class="muted">
          以下数值来自所引用版本的字段；字段匹配不代表专业质量已核验。
        </p>
        <dl class="metric-list">
          <dt>指标与值</dt>
          <dd>
            {{ sourceIndicator.indicator }}：{{ sourceIndicator.value }}
            {{ sourceIndicator.unit }}
          </dd>
          <dt>分母</dt>
          <dd>{{ sourceIndicator.denominator ?? "未提供，不补零" }}</dd>
          <dt>期间</dt>
          <dd>{{ sourceIndicator.period }}</dd>
          <dt>人群</dt>
          <dd>{{ sourceIndicator.population_scope }}</dd>
          <dt>地域 / 法域</dt>
          <dd>
            {{ sourceIndicator.region }} / {{ sourceIndicator.jurisdiction }}
          </dd>
          <dt>字段可见时间</dt>
          <dd>
            {{
              sourceIndicator.provenance.available_at ??
              "未知，不能证明历史可见"
            }}
          </dd>
          <dt>字段来源版本</dt>
          <dd>{{ sourceIndicator.provenance.provider_ref.adopted_version }}</dd>
        </dl>
      </section>
      <h3>固定采用文本</h3>
      <pre class="source-text">{{
        sourceRecord.normalized_text ?? "此条采用资料未提供可读取的正文。"
      }}</pre>
    </template>
  </ElDrawer>
</template>
