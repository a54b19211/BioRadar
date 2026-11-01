// Offline checks of the actual KDD Vue views. These are UI controls, not scientific results.
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "..");
const deps = path.join(root, "apps/web/node_modules");
const ts = require(path.join(deps, "typescript"));
const Vue = require(path.join(deps, "vue"));
const { renderToString } = require(path.join(deps, "@vue/server-renderer"));
const { compileComponent, printableText } = require("./check_query_policy_ui.cjs");
const cache = new Map();
function load(relative) {
  const filename = path.resolve(root, relative);
  if (cache.has(filename)) return cache.get(filename);
  const context = { exports: {}, require(name) {
    if (name.startsWith(".")) return load(path.relative(root, path.resolve(path.dirname(filename), name + ".ts")));
    throw new Error("Unexpected offline import: " + name);
  } };
  const code = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(code, context);
  cache.set(filename, context.exports);
  return context.exports;
}
const kdd = load("apps/web/src/api/kdd.ts");
const Summary = compileComponent("apps/web/src/components/engine/KddRunSummary.vue", { "../../api/kdd": kdd, "../../api/engine": { engineRequest() { throw new Error("UI check must not make HTTP"); } } });
const slot = { setup(_props, { slots }) { return () => Vue.h("section", slots.default?.()); } };
const Detail = compileComponent("apps/web/src/components/engine/CandidateDetail.vue", {
  "../../api/kdd": kdd,
  "./SourceAttentionPanel.vue": { default: compileComponent("apps/web/src/components/engine/SourceAttentionPanel.vue", { "../../api/kdd": kdd }) },
  "../../api/engineLabels": load("apps/web/src/api/engineLabels.ts"),
  "../../api/engine": { engineRequest() { throw new Error("UI check must not make HTTP"); } },
  "element-plus": { ElDrawer: slot, ElTag: slot, ElButton: slot, ElInput: slot },
});
async function render(component, props) {
  const app = Vue.createSSRApp(component, props);
  app.component("RouterLink", slot);
  return renderToString(app);
}
function candidate(key) {
  return { candidate_key: key, method_id: "text_topics", method_version: "offline-ui-control",
    group_key: key, kind: "hypothesis", title: "Explicit offline display control " + key,
    reasons: ["bounded_pattern_for_expert_value_review"], supports: [], metrics: {},
    score: 0.3, importance: "undetermined", urgency: "routine", data_gaps: [],
    next_questions: ["核对原文和反证"], next_data_requirements: ["原始研究"] };
}
function result(direction = "research") {
  const candidates = ["chosen", "excluded", "capacity", "same-support"].map(candidate);
  const states = ["selected", "value_excluded", "capacity_deferred", "redundancy_deferred"];
  return {
    compute_version: "bioradar-mining-compute-v0.3",
    data_preparation: {
      direction, unit_contract: {
        research: "study_x_conditions_or_explicit_exploratory_document",
        pipeline: "project_or_registration_x_indication_x_conditions_x_observed_time",
        business: "source_qualified_company_x_metric_x_comparable_period",
        public_health: "region_x_indicator_x_period_x_population",
      }[direction],
      input_count: 3, qualified_count: 2, units: [{ unit_id: "u1", dimensions: { currency: "USD" },
        members: [{ record_ref: { object_id: "offline-original", adopted_version: "v1" } }] }, { unit_id: "u2" }],
      excluded_records: [{ record_ref: { object_id: "missing-unit" }, reason: "missing_denominator" }],
      unknown_reasons: ["missing_denominator"],
      denominator: { kind: "qualified_adopted_units", count: 2, population_total: null, population_rate_permitted: false },
      coverage: { scope: "adopted_snapshot", source_population_coverage: "unknown", record_budget_omitted: 1 },
      versions: { dataset: "offline-ui-control" }, dataset_hash: "a".repeat(64),
    },
    candidates: [candidates[0]],
    method_results: [{ method_id: "text_topics", execution_status: "success", readiness: "exploratory_only",
      candidates, statistics: { representation: "TF-IDF", embedding_model: null }, excluded_records: [], truncated_count: 1 }],
    pattern_evaluations: candidates.map((c, i) => ({ candidate_key: c.candidate_key, eligible: i !== 1,
      value_score: i === 1 ? null : 0.3, value_reasons: ["descriptive_novelty_requires_content_review"],
      novelty: { kind: "descriptive_cosine_distance", distance: 0.6 }, effect: null,
      uncertainty: { statistical_significance: "not_established", data_gaps: ["缺少原始实验"] },
      importance: "undetermined", urgency: "routine", required_evidence: ["原始研究"] })),
    selection_decisions: candidates.map((c, i) => ({ candidate_key: c.candidate_key, decision: states[i],
      reasons: [["value_eligible_and_within_review_capacity"], ["frozen_value_policy_not_met"],
        ["total_review_capacity"], ["same_support_review_capacity"]][i] })),
    limitations: ["descriptive_attention_rule_not_validated_business_value_or_scientific_approval"],
  };
}
async function main() {
  const fixture = result();
  const frozen = JSON.stringify(fixture);
  assert.equal(kdd.allCandidates(fixture).length, 4, "All generated candidates remain inspectable");
  assert.equal(kdd.selectionCounts(fixture).capacity_deferred, 1);
  const legacy = { candidates: fixture.candidates, method_results: fixture.method_results,
    data_preparation: null, pattern_evaluations: [], selection_decisions: [] };
  assert.equal(kdd.selectionCounts(legacy), null, "Default empty fields must not fabricate legacy zero counts");
  const old = await render(Summary, { result: legacy });
  assert.match(old, /旧结果未保存 KDD 数据加工口径/);
  assert.match(old, /未保存逐项筛选理由/);
  assert.doesNotMatch(old, /容量暂缓 0/);
  const unitLabels = ["独立研究 × 条件", "项目或登记 × 适应证", "有来源资格的企业 × 指标", "地区 × 指标 × 期次 × 人群"];
  for (const [i, direction] of ["research", "pipeline", "business", "public_health"].entries()) {
    const html = await render(Summary, { result: result(direction), policy: { value_policy: kdd.defaultValuePolicy } });
    assert.ok(html.includes(unitLabels[i]), "Each direction preserves its actual analysis unit");
    assert.match(html, /合格分析单位/);
    assert.match(printableText(html), /最少支持分析单位\s+2/);
    assert.match(html, /总体数量：未提供/);
    assert.match(html, /是否可推算总体比率：否/);
    assert.match(html, /容量暂缓 1/);
    assert.match(html, /价值条件未通过 1/);
    assert.match(html, /截断 1/);
    assert.match(html, /TF-IDF/);
    assert.match(printableText(html), /向量模型\s+未提供/);
    assert.match(printableText(html), /容量暂缓 1/);
    assert.match(printableText(html), /总体数量：未提供/);
  }
  const compared = result();
  compared.method_results[0].method_id = "research_tfidf_topics";
  compared.method_results[0].statistics.processed_unit_ids = ["u1", "u2"];
  compared.method_results[0].statistics.fit_unit_ids = ["u1"];
  compared.method_results.push({ method_id: "research_embedding_clusters", execution_status: "success",
    readiness: "exploratory_only", candidates: [], excluded_records: [], truncated_count: 0,
    statistics: { model_spaces: [{ model_signature: ["offline", "v1", 2], processed_unit_ids: ["u1"] }] } });
  assert.match(await render(Summary, { result: compared }), /双方参与单位不同/);
  compared.method_results[1].statistics.model_spaces[0].processed_unit_ids = ["u1", "u2"];
  assert.match(await render(Summary, { result: compared }), /双方处理同一组分析单位/);
  delete compared.method_results[0].statistics.processed_unit_ids;
  assert.match(await render(Summary, { result: compared }), /尚不能确认同条件对照/);
  for (const key of ["excluded", "capacity", "same-support"]) {
    const html = await render(Detail, { candidate: kdd.allCandidates(fixture).find(c => c.candidate_key === key),
      patternEvaluation: kdd.candidateEvaluation(fixture, key),
      selectionDecision: kdd.candidateDecision(fixture, key), investigationAllowed: false,
      runId: "offline-run", resultHash: "f".repeat(64), snapshotId: "offline-snapshot" });
    assert.match(html, /模式价值与本轮筛选/);
    assert.match(html, /统计显著性：尚未建立/);
    assert.match(html, /容量暂缓不等于价值条件未通过/);
    assert.match(html, /未入选不代表没有价值/);
    assert.doesNotMatch(html, /class="investigation-create"/, "Unselected candidates must not offer a rejected write");
  }
  const exploratory = await render(Detail, { candidate: fixture.candidates[0],
    patternEvaluation: { ...fixture.pattern_evaluations[0], value_score: null,
      eligibility_kind: "exploratory_content_followup",
      qualification: "exploratory_content_followup_not_supported_pattern" },
    selectionDecision: fixture.selection_decisions[0], investigationAllowed: true,
    runId: "offline-run", resultHash: "f".repeat(64), snapshotId: "offline-snapshot" });
  assert.match(exploratory, /仅进入探索性内容补证，未确认有价值模式/);
  assert.match(exploratory, /本轮未赋数值评分/);
  assert.match(exploratory, /尚无获支持的模式/);
  const original = { as_of: "2026-10-30T00:00:00Z", method_ids: ["text_topics"] };
  assert.equal(kdd.makeKddPolicy(original, "legacy"), original, "Legacy request remains unchanged");
  const policy = kdd.makeKddPolicy(original, "business");
  assert.equal(policy.kdd_direction, "business");
  assert.equal(policy.value_policy.relative_change_threshold, 0.1);
  assert.equal("kdd_direction" in original, false);
  const discovery = kdd.makeKddPolicy(original, "discovery");
  assert.deepEqual(Array.from(discovery.discovery_directions), ["research", "pipeline", "business", "public_health"]);
  assert.equal("kdd_direction" in discovery, false);
  assert.equal("discovery_directions" in original, false);
  assert.throws(() => kdd.makeKddPolicy({ kdd_direction: "research" }, "discovery"));
  assert.throws(() => kdd.makeKddPolicy({ discovery_directions: ["research"] }, "legacy"));
  assert.throws(() => kdd.makeKddPolicy({ kdd_direction: "research" }, "legacy"));
  assert.equal(JSON.stringify(fixture), frozen, "Rendering does not rewrite saved results");
  const multi = result();
  const manifests = Object.fromEntries(["research", "pipeline", "business", "public_health"].map(direction => [direction, result(direction).data_preparation]));
  multi.data_preparation = { ...multi.data_preparation, direction: "multi_domain_discovery", direction_datasets: manifests,
    input_count: 3, qualified_count: 8, qualified_count_unit: "direction_specific_analysis_units_not_documents_or_population",
    denominator: { kind: "direction_specific_no_pooled_population", count: null, population_total: null } };
  const multiHtml = await render(Summary, { result: multi });
  assert.match(multiHtml, /多方向发现/);
  assert.match(multiHtml, /各方向单位条目/);
  assert.match(multiHtml, /不能相加当作文档数、独立研究数或一个共同总体/);
  assert.match(multiHtml, /所有方向共用本轮审阅容量/);
  assert.match(multiHtml, /跨方向共享容量筛选/);
  assert.equal((multiHtml.match(/该方向合格分析单位 2/g) || []).length, 4);
  assert.equal((multiHtml.match(/该方向分母/g) || []).length, 4);
  const multiPrint = printableText(multiHtml);
  assert.match(multiPrint, /不能相加当作文档数/);
  assert.equal((multiPrint.match(/该方向合格分析单位 2/g) || []).length, 4);
  const materialRef = { provider: "fixture", object_type: "document", object_id: "medical-advice",
    adopted_version: "fixed-v1", version_source: "provider", content_hash: "a".repeat(64), authorization_ref: "fixture-grant" };
  const statement = { unit_id: "source-unit", unit_kind: "source_statement",
    dimensions: { material_type: "medical_advisory", statement_status: "source_reported", source_state: [], evidence_status: "unverified" },
    independent_study: false, members: [{ source_id: "fixture-source", record_ref: materialRef }],
    features: { window: "baseline", qualification: "attributed_material_not_confirmed_health_event_or_numeric_observation",
      body_ref: { record_ref: materialRef, text_hash: "b".repeat(64), normalization_version: "fixture-v1" },
      excerpt: { quote: "SYNTHETIC 医疗提醒：就诊请携带既往用药清单。", start: 0, end: "SYNTHETIC 医疗提醒：就诊请携带既往用药清单。".length,
        offset_basis: "fixed_normalized_unicode", text_hash: "b".repeat(64) }, omitted_text_character_count: 17,
      claims: [], source_context: { attribution: "named_institution", claimed_origin: "SYNTHETIC 医疗机构", verification_status: "unverified" } } };
  const information = { ...statement, unit_id: "source-correction", unit_kind: "source_information_event",
    dimensions: { ...statement.dimensions, source_state: ["deleted"] },
    features: { window: "baseline", body_ref: statement.features.body_ref,
      qualification: "attributed_information_context_not_proof_of_health_claim_or_risk",
      event: { event_type: "correction", target_ref: materialRef, description: "SYNTHETIC 更正此前说明。" },
      basis_status: "unavailable", temporal_status: "event_time_unknown" } };
  const retained = result("public_health");
  retained.data_preparation = { ...retained.data_preparation, excluded_records: [], retained_source_version_count: 1,
    quantitative_qualified_count: 0, quantitative_exclusions: [{ record_ref: materialRef, reason: "typed_metric_observations_not_supplied" }] };
  retained.method_results = [{ method_id: "public_health_source_claims", method_version: "future-retention-control",
    execution_status: "success", readiness: "exploratory_only", candidates: [], excluded_records: [], truncated_count: 0,
    statistics: { retained_source_units: [statement, information], retained_source_version_count: 1, retention_builds_candidates: false } }];
  retained.candidates = []; retained.pattern_evaluations = []; retained.selection_decisions = [];
  const materialBefore = JSON.stringify(retained);
  const retainedHtml = await render(Summary, { result: retained, snapshotId: "fixed-snapshot" });
  assert.match(retainedHtml, /保留的非数值来源材料/);
  assert.match(retainedHtml, /来源信息事件（非疾病事件确认）/);
  assert.match(retainedHtml, /来源陈述材料/);
  assert.match(retainedHtml, /未进入数值计算不等于整份资料被丢弃/);
  assert.match(retainedHtml, /SYNTHETIC 医疗提醒：就诊请携带既往用药清单。/);
  assert.match(retainedHtml, /窗口之外另有 17 个字符/);
  assert.match(retainedHtml, /查看该版本固定采用文本/);
  assert.match(retainedHtml, /关联原版本/);
  assert.doesNotMatch(retainedHtml, /class="investigation-create"|retained_source_units/);
  assert.match(printableText(retainedHtml), /不额外生成候选/);
  assert.match(printableText(retainedHtml), /未进入数值计算的条目/);
  assert.equal(JSON.stringify(retained), materialBefore);
  const adoptedRecord = { record_ref: materialRef, normalized_text_hash: "b".repeat(64), normalization_version: "fixture-v1",
    text_status: "inline_authorized", normalized_text: "SYNTHETIC fixed adopted source" };
  assert.equal(kdd.retainedSourceRecord({ records: [adoptedRecord] }, statement), adoptedRecord);
  for (const [field, value] of [["adopted_version", "different-version"], ["authorization_ref", "different-grant"], ["content_hash", "different-hash"]]) {
    assert.equal(kdd.retainedSourceRecord({ records: [{ ...adoptedRecord, record_ref: { ...materialRef, [field]: value } }] }, statement), null);
  }
  assert.equal(kdd.retainedSourceRecord({ records: [{ ...adoptedRecord, normalized_text_hash: "changed-text" }] }, statement), null);
  assert.equal(kdd.retainedSourceRecord({ records: [{ ...adoptedRecord, normalization_version: "changed-normalization" }] }, statement), null);
  assert.equal(kdd.retainedSourceRecord({ records: [adoptedRecord] }, { ...statement, features: {} }), null);

  console.log("KDD UI controls passed: four units, real scope, full candidates, capacity/value distinction, legacy gaps, printable summary; no HTTP.");
}
if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
module.exports = { main };
