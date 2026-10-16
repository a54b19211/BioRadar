// Presentation of explicit decision diagnostics; no changes to calculation semantics.
const labels: Record<string, string> = {
  only_one_comparable_option_no_between_option_choice_established:
    "只有一个方案进入当前可比集合。自身后悔为零不表示低风险，也没有完成方案间优劣判断。",
  all_scenario_feasibility_fail: "至少一个声明情景不满足硬约束",
  all_scenario_feasibility_uncertain: "至少一个情景的输入区间跨越门槛",
  all_scenario_feasibility_unknown: "至少一个情景缺少门槛所需输入",
  all_declared_bounds_violate_threshold: "整个声明区间均未达门槛",
  bounds_cross_threshold_no_joint_dependence_model:
    "区间跨越门槛，尚无联合关系模型消除不确定性",
  missing_outcome_or_threshold: "缺少结果值或门槛值",
  only_explicit_all_scenario_gate_pass_options_compared_not_global_optimum:
    "仅比较在全部声明情景通过已列门槛的选项，不是全局最优判断。",
  scenario_set_is_declared_not_exhaustive_and_has_no_probabilities:
    "情景由提交者声明，未覆盖全部未来，也没有发生概率。",
  interval_endpoints_are_outer_bounds_not_asserted_jointly_attainable:
    "区间是保守包络，不表示端点可以同时发生。",
  missing_competitor_outcome_blocks_entire_regret_comparison:
    "有可比方案缺少目标结果，本次不能计算完整后悔比较。",
  current_appraisal_only_not_historical_decision_replay:
    "本次为当前委托分析，不能当作历史决策回放。",
  claimed_authorship_and_interest_declarations_are_not_verified_expert_approval:
    "署名和利益声明未经独立身份及专业审批核验。",
  calculation_is_conditional_on_declared_options_metrics_scopes_assumptions_and_constraints:
    "计算只在本次声明的方案、指标、范围、假设和约束下成立。",
  no_default_weights_no_probabilities_no_policy_causality_or_investment_return_inferred:
    "没有默认综合权重、发生概率、政策因果或投资收益推断。",
  no_automatic_portfolio_addition_shared_cost_or_synergy_model:
    "未将单项自动相加为组合方案，也未估计共享成本与协同。",
  differences_and_interval_comparisons_use_conservative_bounds_not_joint_attainability:
    "差值与区间比较采用保守界限，不保证端点联合可实现。",
  review_and_implementation_statements_are_plans_not_executed_tasks:
    "复查与实施内容是声明的计划，尚未执行。",
  assumptions_and_model_results_are_current_declarations_not_historical_knowledge:
    "假设和模型结果在本次采用，不能声称历史当时已知。",
  no_hard_constraints_declared_no_authority_or_capacity_inferred:
    "未声明硬约束，不能据此推断拥有实施权限或能力。",
  why_now_not_declared: "尚未说明为何当前需要作出这项决策。",
  review_time_triggers_or_change_conditions_unknown:
    "复查日期、触发指标或改变结论的条件尚不完整。",
  unknown_source_availability_uses_current_frozen_export_observation_only:
    "来源首次可见时间未知，目前仅依据本次固定采用记录。",
  typed_facet_availability_unknown_current_export_only:
    "结构化字段的首次可见时间未知，不能证明历史可见。",
  no_constraints_declared: "没有声明硬约束，不能据此认定实施条件已经完整。",
  constraints_not_declared: "没有声明硬约束，不能据此认定实施条件已经完整。",
  current_appraisal_not_historical_replay:
    "当前委托分析不代表历史当时已经得出此判断。",
  scenarios_are_not_probabilities: "声明情景不代表其发生概率。",
  interval_bounds_do_not_assume_joint_attainability:
    "区间端点不保证能够同时发生。",
  fixture_not_real_intelligence: "本项为测试夹具，不是真实情报。",
  fixture_not_real_advice: "本项为测试夹具，不是真实建议。",
  hard_constraint_failed: "至少一项声明的硬约束不满足。",
  hard_constraint_uncertain: "输入区间跨越门槛，尚不能确认满足硬约束。",
  hard_constraint_unknown: "缺少硬约束所需输入。",
};
export function decisionExplanation(value: string): string {
  if (value.startsWith("declared_input_cites_inactive_source:"))
    return "有假设或模型依据引用了撤回或删除的来源，需要重新核实，不能作为观察事实。";
  if (value.startsWith("policy_budget_exceeded:")) {
    const names: Record<string, string> = {
      options: "方案数量",
      scenarios: "情景数量",
      metrics: "指标数量",
      constraints: "约束数量",
    };
    const key = value.split(":")[1];
    return `${names[key] ?? key}超过本次策略的计算限额，未进行比较。`;
  }
  return labels[value] ?? value;
}
