// Presentation only; algorithm semantics and schema remain server-owned.
const descriptions: Record<string, string> = {
  stock_exploration: "从现有资料中提取的探索线索，尚未构成近期变化。",
  topic_requires_expert_naming:
    "需要审阅者核实这些材料是否构成同一主题，并为主题命名。",
  same_segment_cooccurrence:
    "这些词语在同一局部叙述中共同出现，专业关系仍需核实。",
  open_vocabulary_stock_and_incremental_discovery:
    "从已采用文本发现词组，不要求事先列出关注词。",
  first_observation_in_adopted_current_window:
    "在本次比较窗口中首次观察到，不能据此认定全领域首次出现。",
  observed_exposure_increase:
    "采用语料中的出现比例增加，仍需检查覆盖与独立性。",
  combination_exploration: "对象组合值得继续核查，机制支持尚未确认。",
  r2_threshold_met: "本次采用范围满足预设组合筛选条件，仍需专业核实。",
  not_a_mechanism_confirmation: "实体共同出现不等于机制得到证实。",
  initial_observation_not_state_progress:
    "首次记录到该状态，尚不能认定发生了进展。",
  source_revision_not_new_effective_event:
    "来源修订了原断言，不一定发生新的事实变化。",
  conflicting_assertions: "相同条件下存在相互冲突的来源断言。",
  asserted_state_change_requires_verification:
    "前后断言的值发生变化，需要核查是否为真实迁移。",
  fixed_project_denominator: "按已确认项目去重并固定统计分母。",
  quantity_is_not_value_or_clinical_success:
    "项目数量不能直接代表研究价值或临床成功。",
  reviewed_evidence_stage_not_registered_trial_phase:
    "比较专业评价的证据阶段，注册分期不能替代结果证据。",
  negative_cue_requires_conditions_and_negation_review:
    "出现负面词句，需要核查否定范围、对象和适用条件。",
  exact_version_dependency_match: "变化命中了既有主张引用的具体证据版本。",
  source_change_is_not_automatic_judgment_reversal:
    "证据有变化，不代表旧判断必须立即推翻。",
  previously_unreferenced_counterevidence_recall:
    "找到可能涉及旧主张、但过去未引用的反面材料。",
  direct_source_event: "来源直接记录了这一事件，仍需检查对象与适用范围。",
  typed_directional_exposure_hypothesis:
    "沿有来源、方向明确的关系得到待核实的影响路径。",
  reachability_is_not_causality_or_risk_probability:
    "存在关系路径不等于因果成立，也不是风险概率。",
  standardized_descriptive_difference_not_policy_causation:
    "按声明口径比较区域差异，未作政策因果归因。",
  study_independence_unresolved: "研究独立性尚未确认。",
  source_independence_unresolved: "来源之间是否独立尚未确认。",
  coverage_not_complete: "采用范围不完整，不能外推为全源结果。",
  publication_time_unknown: "缺少可确认的公开时间。",
  publication_time_semantics_unverified: "来源日期字段的公开时间含义尚未确认。",
  counts_and_absence_are_limited_to_the_adopted_scope:
    "计数与未发现结论仅适用于本次采用范围。",
  comparability_context_missing: "缺少判断材料是否可比较的研究条件。",
  record_budget_exhausted: "已达到记录处理限额，结果仅覆盖实际处理部分。",
  future_effective_assertion_is_a_plan:
    "这是一项未来计划，不是已经发生的状态变化。",
  authorized_inline_text_required: "需要可采用的叙述文本。",
  inventory_is_not_a_competition_or_investment_recommendation:
    "项目清单尚不能直接支持竞争或投资建议。",
};
export function explainCode(value: string): string {
  return descriptions[value] ?? value;
}
