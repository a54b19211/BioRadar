import type { Candidate } from "./engine";
import { explainCode } from "./engineLabels";

export type KddDirection = "research" | "pipeline" | "business" | "public_health";
export type KddObject = Record<string, unknown>;
export const kddDirections: Record<KddDirection, string> = {
  research: "科研与技术", pipeline: "管线与竞争",
  business: "商业与产业", public_health: "公共卫生",
};
const methodNames: Record<string, string> = {
  research_tfidf_topics: "科研主题：TF-IDF 与 NMF 基线",
  research_embedding_clusters: "科研主题：固定版本向量聚类",
  research_evidence_balance: "科研证据：独立支持与反证",
  conditional_pipeline_landscape: "管线：同条件项目与登记格局",
  business_metric_change: "商业：同口径期间指标变化",
  public_health_metric_change: "公卫：同口径地区指标变化",
  public_health_notifications: "公卫：真实通报模式（非病例率）",
  public_health_source_claims: "公卫：待核来源说法与保留材料",
  pipeline_graph_hints: "管线：固定图背景假说与补证",
};
export function methodLabel(id: string, known?: Record<string, string>): string {
  return known?.[id] ?? methodNames[id] ?? id;
}
export const defaultValuePolicy = {
  minimum_pattern_support: 2, relative_change_threshold: 0.1,
  absolute_change_threshold: 0, novelty_distance_threshold: 0.45,
  allow_singleton_exploration: true,
};

export function object(value: unknown): KddObject | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as KddObject : null;
}
export function objects(value: unknown): KddObject[] {
  return Array.isArray(value) ? value.flatMap(item => {
    const row = object(item); return row ? [row] : [];
  }) : [];
}
export function count(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}
export function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}
export function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}
const labels: Record<string, string> = {
  multi_domain_discovery: "多方向发现",
  direction_specific_analysis_units_not_documents_or_population: "各方向独立的分析单位条目，不是文档数或总体数",
  direction_specific_no_pooled_population: "各方向独立分母，不建立跨方向合并总体",
  research: "科研与技术", pipeline: "管线与竞争", business: "商业与产业", public_health: "公共卫生",
  selected: "已进入本轮审阅", value_excluded: "未满足本轮价值条件",
  exploratory_content_followup: "探索内容待补证",
  exploratory_content_followup_not_supported_pattern: "仅作探索性内容补证，尚无获支持的模式",
  descriptive_pattern: "描述性模式符合本轮条件",
  source_statement: "来源陈述材料", source_information_event: "来源信息事件（非疾病事件确认）",
  source_claim: "可检验的来源说法（待核）",
  unverified: "尚未证实", source_confirmed: "来源身份已核实，非说法成立",
  source_reported: "来源报告，非事实确认", unverified_source_claim: "来源说法尚未证实",
  deleted: "已删除", retracted: "已撤回", corrects: "更正", retracts: "撤回",
  correction: "更正", retraction: "撤回", supplement: "补充", repost: "转载",
  supplements: "补充", reposts: "转载", reported_deletion: "来源报告删除",
  official_denial: "来源发布辟谣／否认", moderation: "内容管理记录", access_failure: "来源访问失败",
  source_material_retention_not_cases_confirmed_events_or_independent_studies: "仅保留来源材料，不代表病例、确认事件或独立研究",
  attributed_material_not_confirmed_health_event_or_numeric_observation: "归属于来源的材料，非已确认公卫事件或数值观察",
  attributed_information_context_not_proof_of_health_claim_or_risk: "来源信息环境记录，不证明健康说法或风险",
  no_checkable_claim_or_retained_source_material: "本轮没有可检验说法或合格保留材料",
  capacity_deferred: "审阅容量暂缓", redundancy_deferred: "相同依据暂缓",
  undetermined: "尚未确定", low: "低", medium: "中", high: "高",
  routine: "常规", soon: "近期核查", immediate: "及时核查",
  ready: "可计算", exploratory_only: "仅作探索", unsupported: "输入不支持",
  success: "已执行", partial: "部分完成", failed: "执行失败",
  known: "已知", unknown: "未知", incomplete: "不完整", complete: "完整",
  descriptive: "描述性结果", hypothesis: "待验证假设",
  not_assessed: "未评价", not_available: "尚未取得", not_applicable: "不适用",
  not_population_denominator: "不是总体分母",
  qualified_adopted_units: "合格采用分析单位", adopted_snapshot: "本次采用快照",
  tfidf_nmf_control: "TF-IDF 与 NMF 基线", embedding: "固定版本向量",
  one_per_analysis_unit: "每个分析单位等权", baseline_only: "仅用基准期拟合",
  stock_exploration_not_historical_prediction: "库存探索，不是历史预测",
  patterns_are_not_expert_approved_or_statistically_significant: "模式仍待专业评价，未证明统计显著性",
  complete_same_model_version_embedding_units_required: "需同模型、版本与维度的完整单位向量",
  topic_content_and_independent_replication_require_review: "主题内容和独立复现仍需核对",
  novelty_is_not_scientific_importance: "新颖性不代表科学重要性",
  context_not_available_at_cutoff: "条件资料在截止前尚不可知",
  study_independence_mapping_not_available_at_cutoff: "截止前缺少可用的独立研究映射",
  independent_study_or_conditions_unresolved_document_exploration_only: "独立研究或条件未确认，仅按文档探索",
  competitive_conditions_incomplete_inventory_only: "竞争条件不完整，仅列当前库存",
  notice_publication_time_unknown_no_period_pattern: "通报公开时间未知，不能计算分期模式",
  notification_event_only_population_and_incidence_unknown: "仅有通报事件，人口分母和真实发病情况未知",
  metric_not_available_at_cutoff: "该指标在截止前尚不可知",
  metric_period_not_complete_by_cutoff: "截止时该指标期间尚未结束",
  metric_source_field_binding_required: "数值尚未绑定原件字段",
  metric_forecast_or_unverified_not_reported: "预测或未核实数值，不能当作已报告值",
  business_reporting_scope_required: "缺少商业指标的报告口径",
  monetary_currency_required: "货币指标缺少币种",
  public_health_population_and_region_required: "公卫指标缺少人群或地区",
  public_health_rate_denominator_required: "计算公卫比率需要合格分母",
  metric_denominator_unit_required: "分母缺少单位",
  metric_numeric_calculation_limit: "超过本次数值计算范围，待进一步处理",
  metric_subject_source_qualified_not_cross_source_confirmed: "对象仅在本来源内确认，跨来源身份尚未确认",
  text_feature_budget_exhausted: "文字特征预算已用完，部分资料未进入特征计算",
  study_members_span_time_windows_not_independent_new_studies: "同一研究的资料跨时窗，不能当作多项新研究",
  conflicting_metric_values_not_averaged: "指标数值冲突，保留冲突而不取平均",
  authorized_text_units_required: "需要可读取的授权文本单位",
  no_usable_text_features: "尚无可用文字特征",
  semantic_cluster_content_and_value_unreviewed: "向量聚类的内容和价值尚待核对",
  embedding_distance_does_not_establish_usefulness: "向量距离不能证明线索有用",
  explicit_shared_claim_or_intervention_scope_required: "需要明确共同命题或干预范围才能比较证据",
  project_or_native_registration_identity_required: "需要项目身份或原生登记身份",
  registration_is_not_drug_program_or_failure_reason: "登记不等于完整药物项目，也不能替代失败原因",
  qualified_source_bound_metric_periods_required: "需要原件绑定且期间可比的指标",
  at_least_two_comparable_periods_required: "至少需要两个可比期间",
  source_population_and_observation_coverage_require_review: "来源总体与观察覆盖仍需核对",
  native_notice_publication_units_required: "需要有原生身份与公开时间的通报",
  notification_sampling_coverage_unknown: "通报选择与采样覆盖未知",
  case_counts_population_and_outbreak_onset_not_supplied: "未提供完整病例数、人口分母或疫情起点",
  study_condition: "研究与条件", exploratory_document: "探索性文档",
  company_metric_period: "企业指标期次", region_indicator_period_population: "地区指标期次与人群",


  descriptive_cosine_distance: "余弦距离（描述性）", not_established: "尚未建立",
  study_x_conditions_or_explicit_exploratory_document: "独立研究 × 条件；未确认独立性时保留为探索性文档单位",
  project_or_registration_x_indication_x_conditions_x_observed_time: "项目或登记 × 适应证 × 条件 × 观察时间",
  source_qualified_company_x_metric_x_comparable_period: "有来源资格的企业 × 指标 × 可比期间",
  region_x_indicator_x_period_x_population: "地区 × 指标 × 期次 × 人群",
  conditions_or_periods_not_comparable: "条件或期间不可比",
  conflicting_source_values_require_resolution: "来源数值冲突，需先核对",
  single_unit_novelty_for_followup_not_independent_replication: "单个单位的新颖线索，仅供补查，不能算独立复现",
  below_frozen_minimum_pattern_support: "支持单位数不足本轮预设阈值",
  effect_unknown: "变化幅度尚不明确",
  no_directional_change_or_overlapping_value_intervals: "未确认方向变化，或数值区间重叠",
  below_frozen_absolute_change_threshold: "未达到预设绝对变化阈值",
  below_frozen_relative_change_threshold: "未达到预设相对变化阈值",
  relative_change_undefined_requires_explicit_absolute_threshold: "相对变化无法定义，需明确绝对变化阈值",
  comparable_directional_change_meets_frozen_thresholds: "可比条件下的方向变化达到本轮预设阈值",
  below_frozen_novelty_threshold: "未达到本轮预设新颖性阈值",
  descriptive_novelty_requires_content_review: "描述性新颖线索，仍需核对内容价值",
  data_quality_gap_not_business_pattern: "数据质量缺口，未作为业务模式入选",
  unchanged_inventory_retained_as_negative_control: "库存未变，保留为无变化对照",
  bounded_pattern_for_expert_value_review: "限定范围内的模式，待专业评价其价值",
  descriptive_attention_rule_not_validated_business_value_or_scientific_approval: "描述性关注规则，尚未验证业务价值或科学结论",
  frozen_value_policy_not_met: "未满足本轮固定价值条件",
  value_eligible_and_within_review_capacity: "符合本轮价值条件，且在审阅容量内",
  method_candidate_capacity: "本方法的候选容量已满", same_support_review_capacity: "相同依据已占用本轮额度",
  total_review_capacity: "本轮总审阅容量已满",
  scope_limited: "范围受限", missing_denominator: "缺少可比分母",
  document: "文档", independent_study: "独立研究", program: "项目",
  company_period: "企业与期间", region_metric_period: "地区、指标与期次",
};
const fieldLabels: Record<string, string> = {
  label: "名称", description: "口径", qualification: "适用资格", status: "状态",
  reason: "依据", reasons: "依据", scope: "范围", scope_description: "采用范围",
  unit: "单位", analysis_unit: "分析单位", key_fields: "主键字段", dimensions: "比较条件",
  count: "数量", unit_count: "单位数", record_count: "记录数", support_count: "支持单位数",
  input_count: "输入数", qualified_count: "合格数", excluded_count: "排除数",
  retained_source_version_count: "保留资料版本数", retained_source_unit_count: "保留材料单位数",
  source_statement_count: "来源陈述材料数", source_information_event_count: "来源信息事件数",
  quantitative_qualified_count: "数值合格单位数", quantitative_exclusions: "未进入数值计算的条目",
  material_type: "材料类型", statement_status: "说法状态", source_state: "来源状态",
  attribution: "来源归属", claimed_origin: "来源声称的出处", verification_status: "来源核实状态",
  source_independence: "来源独立性", retention_qualification: "材料保留资格",
  retention_builds_candidates: "材料保留是否生成候选", source_intelligence_version: "来源材料契约版本",
  population_known: "总体是否已知", complete: "是否完整", is_complete: "是否完整",
  known: "是否已知", source_total: "来源报告总数", total_reported: "来源报告总数",
  numerator: "分子", denominator: "分母", value: "值", metric: "指标",
  baseline: "比较基准", current: "本期", absolute_change: "绝对变化",
  relative_change: "相对变化", relative_change_threshold: "相对变化阈值",
  magnitude: "变化幅度", direction: "方向", estimate: "估计值",
  lower: "下界", upper: "上界", confidence_interval: "区间", method: "方法",
  distance: "距离", threshold: "阈值", source: "来源", limitations: "限制",
  currency: "币种", period: "期间", population: "人群", region: "地区",
  metric_id: "指标", subject_id: "对象", indication: "适应证", condition: "条件",
  available_at: "当时可知时间", event_time: "事件时间", as_of: "截止时间",
  mode: "时间模式", time_mode: "时间模式", value_score: "本轮规则评分",
  importance: "重要性", urgency: "紧急度", feature_source: "特征来源",
  representation: "数据表示", embedding_model: "向量模型", vector_model: "向量模型",
  embedding_dimensions: "向量维度", cluster_count: "聚类数", document_count: "文档数",
  member_count: "成员数", total_units: "分析单位数", eligible_units: "适用单位数",
  sampling: "抽样方式", selected_count: "入选数", omitted_records: "未入窗记录数",
  kind: "口径", population_total: "总体数量", population_rate_permitted: "是否可推算总体比率",
  adopted_scope_complete: "采用范围是否完整", record_budget_omitted: "记录预算未纳入数",
  source_population_coverage: "来源总体覆盖", sources: "来源范围",
  statistical_significance: "统计显著性", data_gaps: "数据缺口",
  relative_minimum: "最小相对变化", analysis_unit_count: "分析单位数",
  record_ref: "固定原件", provider: "提供方", object_type: "对象类型", object_id: "原件编号",
  adopted_version: "采用版本", version_source: "版本来源", content_hash: "内容哈希",
  authorization_ref: "许可引用", source_instance: "来源实例", source_id: "来源",
  observed_at: "本次观察时间", text_hash: "采用文本哈希", normalization_version: "规范化版本",
  project_ref: "项目身份", known_at: "可知时间", identity_basis: "身份依据",
  value_lower: "数值下界", value_upper: "数值上界", denominator_unit: "分母单位",
  metric_version: "指标口径版本", period_start: "期间开始", period_end: "期间结束",
  currency_scope: "币种口径", consolidation_scope: "合并范围",
  analysis_units: "分析单位数", independent_study_count: "已确认独立研究数",
  terms: "主题词", model_hash: "拟合模型版本", vocabulary_hash: "词表版本",
  fit_unit_ids: "拟合采用单位", fit_scope: "特征拟合范围", solver_warnings: "算法运行提示",
  spaces: "向量空间", rejected_unit_ids: "未满足向量资格的单位",
  model_version: "模型版本", dimension: "维度", clusters: "聚类",
  unit_weights: "单位权重",
  minimum_pattern_support: "最少支持分析单位", absolute_change_threshold: "绝对变化阈值",
  novelty_distance_threshold: "新颖距离阈值", allow_singleton_exploration: "允许单单位新颖线索进入补查",
  review_capacity: "本轮审阅容量", method_candidate_capacity: "每方法候选容量",
  processed_unit_ids: "实际参与单位", model_spaces: "分别计算的向量空间",
  model_signature: "提供方、模型版本与维度", noise_unit_ids: "未形成聚类的单位",
  algorithm_input_deferred_unit_ids: "算法容量未纳入单位",
  tfidf_control_method: "同数据文字基线", assessment_inventory: "逐研究与条件的评价清单",
  study_ref: "研究身份", mapping_versions: "身份映射版本", context_provenance: "条件原件依据",



};
export function kddLabel(value: string): string {
  return labels[value] ?? explainCode(value);
}
export function fieldLabel(value: string): string {
  return fieldLabels[value] ?? value.replaceAll("_", " ");
}
export function readable(value: unknown): string {
  if (value === null || value === undefined) return "未提供";
  if (typeof value === "string") return kddLabel(value);
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.length ? value.map(readable).join("；") : "本轮无记录";
  const row = object(value);
  return row ? Object.entries(row).map(([key, item]) => fieldLabel(key) + "：" + fieldValue(key, item)).join("；") : "未提供";
}
function fieldValue(key: string, value: unknown): string {
  if (key.endsWith("_unit_ids") && Array.isArray(value) && value.every(item => typeof item === "string")) {
    return value.length + " 个单位" + (value.length ? "；预览：" + value.slice(0, 4).map(id => id.slice(0, 12)).join("、")
      + (value.length > 4 ? "；完整清单在固定结果中" : "") : "");
  }
  return readable(value);
}
export function facts(value: unknown): { key: string; label: string; value: string }[] {
  const row = object(value);
  return row ? Object.entries(row).map(([key, item]) => ({ key, label: fieldLabel(key), value: fieldValue(key, item) })) : [];
}
export function retainedSourceRecord(snapshot: unknown, unit: KddObject): KddObject | null {
  const body = object(object(unit.features)?.body_ref);
  const expected = object(body?.record_ref);
  const refFields = ["provider", "object_type", "object_id", "adopted_version", "version_source", "content_hash", "authorization_ref"];
  if (!expected || refFields.some(key => !text(expected[key])) || !text(body?.text_hash) || !text(body?.normalization_version)) return null;
  return objects(object(snapshot)?.records).find(record => {
    const actual = object(record.record_ref);
    return actual && refFields.every(key => actual[key] === expected[key])
      && record.normalized_text_hash === body?.text_hash
      && record.normalization_version === body?.normalization_version;
  }) ?? null;
}
export function preparation(result: unknown): KddObject | null {
  return object(object(result)?.data_preparation);
}
export function preparationDirections(result: unknown): { direction: string; data: KddObject }[] {
  const directions = object(preparation(result)?.direction_datasets);
  if (!directions) return [];
  return Object.entries(directions).flatMap(([direction, value]) => {
    const data = object(value);
    return data ? [{ direction, data }] : [];
  });
}
export function evaluations(result: unknown): KddObject[] {
  return objects(object(result)?.pattern_evaluations);
}
export function decisions(result: unknown): KddObject[] {
  return objects(object(result)?.selection_decisions);
}
export function candidateEvaluation(result: unknown, key?: string): KddObject | null {
  return evaluations(result).find(row => row.candidate_key === key) ?? null;
}
export function candidateDecision(result: unknown, key?: string): KddObject | null {
  return decisions(result).find(row => row.candidate_key === key) ?? null;
}
export function allCandidates(result: {
  candidates?: Candidate[]; method_results?: { candidates?: Candidate[] }[];
} | null | undefined): Candidate[] {
  const candidates = new Map<string, Candidate>();
  for (const row of result?.method_results ?? [])
    for (const candidate of row.candidates ?? []) candidates.set(candidate.candidate_key, candidate);
  for (const candidate of result?.candidates ?? []) candidates.set(candidate.candidate_key, candidate);
  return [...candidates.values()];
}
export function selectionCounts(result: unknown): Record<string, number> | null {
  const row = object(result);
  if (!row || !preparation(result) || !Array.isArray(row.selection_decisions)) return null;
  const counts: Record<string, number> = { selected: 0, value_excluded: 0, capacity_deferred: 0, redundancy_deferred: 0 };
  for (const item of decisions(result)) {
    const decision = text(item.decision);
    if (Object.hasOwn(counts, decision)) counts[decision] += 1;
  }
  return counts;
}
export function makeKddPolicy(value: unknown, direction: KddDirection | "legacy" | "discovery") {
  const policy = object(value);
  if (!policy) throw new Error("分析参数必须是对象。");
  if (direction === "legacy") {
    if ("kdd_direction" in policy || "value_policy" in policy || "discovery_directions" in policy)
      throw new Error("原有方法不接受 KDD 方向和价值条件，请在上方选择对应方向。");
    return policy;
  }
  if (direction === "discovery") {
    if ("kdd_direction" in policy) throw new Error("全方向发现不接受单一 kdd_direction，请移除旧方向字段。");
    return { ...policy, discovery_directions: Object.keys(kddDirections),
      value_policy: { ...defaultValuePolicy, ...(object(policy.value_policy) ?? {}) } };
  }
  if ("discovery_directions" in policy) throw new Error("单方向计算不接受全方向参数，请改选全方向发现。");
  return { ...policy, kdd_direction: direction,
    value_policy: { ...defaultValuePolicy, ...(object(policy.value_policy) ?? {}) } };
}


export function representationScope(result: unknown) {
  const methods = objects(object(result)?.method_results);
  const tfidf = methods.find(row => row.method_id === "research_tfidf_topics");
  const embedding = methods.find(row => row.method_id === "research_embedding_clusters");
  if (!tfidf && !embedding) return null;
  const tf = object(tfidf?.statistics);
  const em = object(embedding?.statistics);
  const textIds = Array.isArray(tf?.processed_unit_ids) ? new Set(strings(tf.processed_unit_ids)) : null;
  const spaces = Array.isArray(em?.model_spaces) ? objects(em.model_spaces) : null;
  const vectorIds = spaces && spaces.every(space => Array.isArray(space.processed_unit_ids))
    ? new Set(spaces.flatMap(space => strings(space.processed_unit_ids))) : null;
  return {
    textCount: textIds?.size ?? null, vectorCount: vectorIds?.size ?? null,
    fitCount: Array.isArray(tf?.fit_unit_ids) ? strings(tf.fit_unit_ids).length : null,
    sharedCount: textIds && vectorIds ? [...textIds].filter(id => vectorIds.has(id)).length : null,
    sameUnits: textIds && vectorIds ? textIds.size === vectorIds.size && [...textIds].every(id => vectorIds.has(id)) : null,
  };
}
