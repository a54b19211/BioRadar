// The API keeps stable codes; readers see their practical meaning.
const gaps: Record<string, string> = {
  adopted_records_not_complete_domain_denominator:
    "只统计已采用资料，尚无全领域的完整比较基数。",
  evaluation_feature_admission_limited:
    "词组数量达到本次计算上限，部分文字线索尚未计算。",
  evaluation_record_budget_incomplete:
    "本次文字计算未覆盖全部合格材料，不能据此作完整趋势判断。",
  metric_basis_or_period_not_comparable:
    "部分数值的单位、期间或统计口径不一致，未合并比较。",
  metric_denominator_unknown_not_inferred:
    "缺少可靠分母，不能据此计算发生率或人群风险。",
  metric_point_computation_limited:
    "数值序列超过本次比较范围，更多期间仍保留在原始记录中。",
  missing_exposure_bucket_not_zero:
    "中间有周期缺少资料，未把它补成零，也未当作连续变化。",
  no_eligible_observations_not_evidence_of_absence:
    "尚无满足条件的观察资料，不能推导事件没有发生。",
  record_feature_admission_limited:
    "部分长材料只提取了有限词组，相关文字线索可能遗漏。",
  registered_trial_fields_not_supplied: "尚未收到合格的试验登记字段。",
  source_unavailable_not_no_change: "部分来源暂不可读，不能据此判断没有变化。",
  topic_seed_or_catchup_incomplete:
    "仍在补齐或追赶资料，本次观察范围尚不完整。",
  typed_metrics_not_supplied: "尚未收到有明确单位、期间和对象的合格数值。",
  undated_or_unverified_publication_time:
    "部分材料缺少可靠公开时间，未强行归入时间周期。",
  unresolved_study_independence: "部分材料是否来自独立研究尚未确认。",
};
export function topicGapLabel(value: string) {
  return gaps[value] ?? value;
}
