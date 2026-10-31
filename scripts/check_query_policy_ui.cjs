// Render the actual policy panel offline; no network, model or invented integration receipts.
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "..");
const deps = path.join(root, "apps/web/node_modules");
const ts = require(path.join(deps, "typescript"));
const Vue = require(path.join(deps, "vue"));
const compiler = require(path.join(deps, "@vue/compiler-sfc"));
const { renderToString } = require(path.join(deps, "@vue/server-renderer"));

function compileComponent(relative, imports = {}) {
  const source = fs.readFileSync(path.join(root, relative), "utf8");
  const { descriptor } = compiler.parse(source, { filename: relative });
  const script = compiler.compileScript(descriptor, { id: "query-policy-ui-check" });
  const template = compiler.compileTemplate({
    source: descriptor.template.content, filename: relative, id: "query-policy-ui-check",
    compilerOptions: { bindingMetadata: script.bindings },
  });
  assert.equal(template.errors.length, 0);
  const context = { exports: {}, require: name => name === "vue" ? Vue : imports[name] ?? {} };
  vm.createContext(context);
  const code = ts.transpileModule(script.content + "\n" + template.code, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInContext(code + "\nexports.default.render=exports.render;", context);
  return context.exports.default;
}

const apiCache = new Map();
function loadApi(relative) {
  const filename = path.resolve(root, relative);
  if (apiCache.has(filename)) return apiCache.get(filename);
  const context = { exports: {}, require(name) {
    if (name.startsWith(".")) return loadApi(path.relative(root, path.resolve(path.dirname(filename), name + ".ts")));
    throw new Error("Unexpected offline import: " + name);
  } };
  const code = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(code, context);
  apiCache.set(filename, context.exports);
  return context.exports;
}
const kddApi = loadApi("apps/web/src/api/kdd.ts");
const queryPolicyApi = loadApi("apps/web/src/api/queryPolicy.ts");
const kddSummary = compileComponent("apps/web/src/components/engine/KddRunSummary.vue", {
  "../../api/kdd": kddApi,
  "../../api/engine": { engineRequest() { throw new Error("Offline UI check must not make HTTP"); } },
});
const policyPanel = compileComponent("apps/web/src/components/engine/PolicyTracePanel.vue", {
  "../../api/kdd": kddApi, "../../api/queryPolicy": queryPolicyApi,
  "./KddRunSummary.vue": { default: kddSummary },
});
const progressPanel = compileComponent("apps/web/src/components/engine/ResearchProgress.vue", {
  "./PolicyTracePanel.vue": { default: policyPanel }, "../../api/queryPolicy": queryPolicyApi,
});
function render(policy, queries = []) {
  return renderToString(Vue.createSSRApp(policyPanel, { policy, queries }));
}

// Project the actual SSR tree through the shared print rule, including ancestor classes.
function printableText(html) {
  const { baseParse } = require(path.join(deps, "@vue/compiler-dom"));
  function text(node) {
    if (node.type === 2) return node.content;
    if (node.type === 1 && node.props.some(prop =>
      prop.type === 6 && prop.name === "class" &&
      prop.value?.content.split(/\s+/).includes("no-print")
    )) return "";
    return (node.children ?? []).map(text).join(" ");
  }
  return text(baseParse(html));
}

function historyPage(request) {
  const source = fs.readFileSync(path.join(root, "apps/web/src/pages/HistoryPage.vue"), "utf8");
  const code = ts.transpileModule(source.slice(source.indexOf(">") + 1, source.indexOf("</script>")), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  class EngineError extends Error { constructor(status, message) { super(message); this.status = status; } }
  const context = {
    exports: {}, console, Error, SyntaxError, crypto: { randomUUID: () => "offline-history-write" },
    require(name) {
      if (name === "vue") return { ...Vue, onMounted() {}, onUnmounted() {} };
      if (name.endsWith("/api/engine")) return { engineRequest: request, EngineError };
      return {};
    },
  };
  vm.createContext(context);
  vm.runInContext(code + "\nglobalThis.probe={selected,semanticPolicy,startSemantic,uncertainWrite};", context);
  return context.probe;
}

async function main() {
  const legacy = await render(null);
  assert.match(legacy, /旧版记录/);
  assert.doesNotMatch(legacy, /未训练启发式/);
  const action = {
    id: "offline-action", kind: "document_search",
    arguments: { provider: "fabric", source_id: "europe_pmc", terms: ["BCMA"] },
    covers: ["反证"], estimated_cost: { tool_calls: 1 },
  };
  const trace = {
    execution_version: "query-step-v1", requested: "query_planner", effective: "query_planner",
    version: "offline-control-version", selector: "heuristic", stop: "rule",
    training_status: "heuristic_untrained", status: "blocked", pending_action: action,
    evidence_time: { mode: "operational_replay", cutoff: "2018-01-01T00:00:00Z" },
    checks: [{ id: "反证", status: "unknown", evidence_ids: [] }],
    budget: {
      limits: { tool_calls: 6 }, usage: { tool_calls: null, known_tool_calls: 1, unknown_tool_calls: 1, reserved_tool_calls: 2, budgeted_tool_calls: 3, preparation_tool_calls: 1, tokens: null },
      remaining: { tool_calls: 3 },
    },
    decisions: [
      { step: 0, decision: "continue", action, reason: "selected_by_heuristic",
        state_summary: { observed_history: [] },
        receipt: { query_index: 1, status: "unconfirmed", evidence_ids: [], cost: { tool_calls: null, reserved_tool_calls: 2, tokens: null } } },
      { step: 1, decision: "insufficient", action: null, reason: "pending_execution_or_reconciliation",
        state_summary: { observed_history: [{ action_id: action.id, status: "unconfirmed", query_index: 1, evidence_ids: [] }] },
        receipt: null },
    ],
  };
  const html = await render(trace, [
    { status: "read", action_id: "other-action", returned: 99, new_versions: 99 },
    { status: "unconfirmed", action_id: action.id, returned: 0, new_versions: 0 },
  ]);
  assert.match(html, /未训练启发式/);
  assert.match(html, /offline-control-version/);
  assert.match(html, /先核对已发送动作的结果，再继续规划/);
  assert.match(html, /token 未知/);
  assert.doesNotMatch(html, /token 0/);
  assert.match(html, /预算另保守预留 2 次/);
  assert.match(html, /本动作费用尚未确认，预算保守预留 2 次/);
  assert.match(html, /未查完的覆盖项：1 \/ 1/);
  assert.match(html, /本动作可能覆盖：反证/);
  assert.doesNotMatch(html, /有已采用资料支撑覆盖/);
  assert.match(html, /本次决策已观察到/);
  assert.match(html, /查询记录 2/);
  assert.doesNotMatch(html, /返回 99 条/);
  assert.match(html, /operational_replay/);
  assert.match(html, /2018-01-01/);
  const printed = printableText(await renderToString(Vue.createSSRApp(progressPanel, {
    analysis: {
      state: "needs_review", calls: [], queries: [], providers: {},
      budget: { limits: {}, usage: {} }, query_policy: trace,
    },
  })));
  assert.match(printed, /未训练启发式/);
  assert.match(printed, /offline-control-version/);
  assert.match(printed, /先核对已发送动作的结果，再继续规划/);
  assert.match(printed, /预算另保守预留 2 次/);
  assert.match(printed, /未查完的覆盖项：1 \/ 1/);
  assert.match(printed, /反证/);
  assert.doesNotMatch(printed, /逐步动作、真实回执与下一决策/);
  assert.doesNotMatch(printed, /策略版本与原始轨迹/);
  assert.equal(printableText('<div class="no-print"><p>hidden ancestor</p></div><p>visible</p>').trim(), "visible");
  const impactAnalysis = {
    id: "latest-failed-analysis", state: "needs_review", calls: [], queries: [], providers: {}, budget: { limits: {}, usage: {} },
    query_policy: { evidence_time: { mode: "current", cutoff: "2026-02-03T00:00:00Z" } },
    result: { report: { status: "FAIL", final_text: "SYNTHETIC immutable later report" } },
    progress: { material_update_impact: { version: "dependency-aware-material-update-v1", parent_analysis_id: "prior-analysis",
      report_currentness: "prior_fixed_materials", report_as_of: "2026-01-02T00:00:00Z", checked_revision: 8, status: "impact_pending", decisions: [{ entry_id: "source-version-change", revision: 8,
        old_ref: { provider: "fixture", object_id: "source", adopted_version: "v1" },
        new_ref: { provider: "fixture", object_id: "source", adopted_version: "v2" },
        status: "impact_pending", reason: "changed_context_or_unmapped_dependency", affected_claim_ids: ["calculation"] }] } },
  };
  const impactBefore = JSON.stringify(impactAnalysis);
  const impactHtml = await renderToString(Vue.createSSRApp(progressPanel, { analysis: impactAnalysis }));
  assert.match(printableText(impactHtml), /有新版本等待判断/);
  assert.match(printableText(impactHtml), /下方已有报告仍绑定自身原固定材料/);
  assert.match(printableText(impactHtml), /本次影响比较依据的报告：prior-analysis/);
  assert.doesNotMatch(impactHtml, /涉及本版命题/);
  assert.match(impactHtml, /受影响命题／计算依据：calculation/);
  assert.match(printableText(impactHtml), /该报告资料截止/);
  assert.ok(printableText(impactHtml).includes(new Date("2026-01-02T00:00:00Z").toLocaleString("zh-CN", { hour12: false })));
  assert.match(impactHtml, /fixture \/ source \/ v1/);
  assert.match(impactHtml, /fixture \/ source \/ v2/);
  assert.equal(JSON.stringify(impactAnalysis), impactBefore, "Impact projection never edits the prior report/state/hash inputs");
  const equivalentAnalysis = JSON.parse(impactBefore);
  equivalentAnalysis.progress.material_update_impact.status = "equivalent_inputs";
  const equivalentHtml = await renderToString(Vue.createSSRApp(progressPanel, { analysis: equivalentAnalysis }));
  assert.match(equivalentHtml, /系统只确认下述依据报告的本次已比较输入等同/);
  assert.match(equivalentHtml, /这不代表下方其他版本已经完成比较或重新研判/);
  const missingBasis = JSON.parse(impactBefore);
  delete missingBasis.progress.material_update_impact.parent_analysis_id;
  delete missingBasis.progress.material_update_impact.report_as_of;
  const missingBasisHtml = await renderToString(Vue.createSSRApp(progressPanel, { analysis: missingBasis }));
  assert.match(printableText(missingBasisHtml), /未记录，未推测对应报告/);
  assert.match(printableText(missingBasisHtml), /该报告资料截止：未提供，未补成当前时点/);
  const styles = fs.readFileSync(path.join(root, "apps/web/src/style.css"), "utf8");
  assert.match(styles, /@media print[\s\S]*?\.no-print[\s\S]*?display:\s*none\s*!important/);
  trace.effective = "baseline";
  const fallback = await render(trace);
  assert.match(fallback, /请求策略：逐步查询策略/);
  assert.match(fallback, /本版实际策略：原有顺序基线/);
  const dataset = {
    schema_version: "bioradar-analysis-dataset-v1", direction: "research", input_count: 3,
    qualified_count: 2, unit_contract: "study_x_conditions_or_explicit_exploratory_document",
    units: [], excluded_records: [], unknown_reasons: ["independent_study_or_conditions_unresolved_document_exploration_only"],
    denominator: { kind: "qualified_adopted_units", population_total: null, population_rate_permitted: false },
    dataset_hash: "offline-dataset-version",
  };
  const refs = [{ id: "dataset:offline", version: "offline-dataset-version" }];
  const capability = { id: "offline-prepare", kind: "dataset_prepare",
    arguments: { capability_id: "radar.dataset.prepare", scope: { direction: "research" } },
  };
  const v2 = { ...trace, schema_version: "query-planner.external.v2", direction: "research", direction_basis: "explicit",
    effective: "query_planner", requested: "query_planner", status: "insufficient", pending_action: null,
    artifacts: [{ ...refs[0], kind: "analysis_dataset", qualified_count: 2 }],
    artifact_results: { "dataset:offline": dataset }, execution: [],
    checks: [{ id: "kdd_dataset", status: "passed", artifact_refs: refs, reason: "只证明加工已执行" }],
    decisions: [{ step: 0, decision: "continue", action: capability, receipt: {
      status: "read", query_index: 0, new_artifact_refs: refs, cost: { tool_calls: 1, tokens: 0, model_calls: 0 },
    } }, { step: 1, decision: "insufficient", reason: "no_legal_action", state_summary: {
      observed_history: [{ action_id: capability.id, status: "read", new_artifact_refs: refs }],
    } }],
  };
  const kddHtml = await render(v2, [{ action_id: capability.id, kind: capability.kind, status: "read", returned: 1 }]);
  assert.match(kddHtml, /加工固定资料/);
  assert.match(kddHtml, /本动作实际产物/);
  assert.match(kddHtml, /加工后的分析数据/);
  assert.match(kddHtml, /已有固定产物支持本项执行检查/);
  assert.match(kddHtml.replace(/<!--[\s\S]*?-->/g, ""), /offline-prepare（已取得回执，/);
  const kddPrinted = printableText(kddHtml);
  assert.match(kddPrinted, /合格分析单位\s*2/);
  assert.match(kddPrinted, /执行检查完成不表示模式有科研价值/);
  assert.match(kddPrinted, /独立研究或条件未确认/);
  assert.match(kddPrinted, /总体数量：未提供/);
  assert.match(kddPrinted, /未训练启发式/);
  assert.match(kddPrinted, /本地数据加工和计算各计一次工具动作、零模型调用/);

  const laterArtifact = { id: "professional:offline-later", version: "offline-later-version",
    kind: "professional_assessment", provider: "mech_evidence", status: "completed" };
  const retainedBasis = {
    call_key: "read_plan:before-later-results", call_id: "offline-read-call", reading_evidence_hash: "original-reading-hash",
    status: "unfinished_reading_draft", qualification: "SYNTHETIC original reading qualification",
    coverage_version: "retained-reading-coverage-v1", cannot_use_as_current_conclusion: true,
    call_index: 2, after_query_index: 0, after_decision_step: 1,
    input_evidence: [{ evidence_key: "original-input", source_id: "europe_pmc",
      record_ref: { provider: "fabric", object_id: "original-document", adopted_version: "original-version" },
      text_sha256: "original-window-hash", selection_hash: "original-selection-hash", start: 0, end: 16 }],
    input_artifact_refs: refs,
    not_incorporated: {
      evidence: [{ evidence_key: "later-input", source_id: "europe_pmc",
        record_ref: { provider: "fabric", object_id: "later-document", adopted_version: "later-fixed-version" },
        text_sha256: "later-window-hash", selection_hash: "later-selection-hash", start: 16, end: 32 }],
      artifacts: [laterArtifact],
    },
  };
  const retainedAnalysis = {
    state: "needs_review", calls: [], queries: [], budget: { limits: {}, usage: {} },
    result: { report: null, conditional_answer: { answer: "SYNTHETIC exact earlier reading answer" } },
    query_policy: { ...v2, synthesis_stop_reason: "analysis_token_budget", synthesis_reading_basis: retainedBasis,
      artifacts: [...v2.artifacts, laterArtifact],
      artifact_results: { ...v2.artifact_results, [laterArtifact.id]: { status: "completed", overall: { verdict: "insufficient" } } },
    },
  };
  const retainedBefore = JSON.stringify(retainedAnalysis);
  const retainedHtml = await renderToString(Vue.createSSRApp(progressPanel, { analysis: retainedAnalysis }));
  const retainedPrinted = printableText(retainedHtml);
  assert.match(retainedPrinted, /此前阅读草稿（综合未完成）/);
  assert.match(retainedPrinted, /尚未纳入后续结果，不能作为当前结论/);
  assert.match(retainedHtml, /read_plan:before-later-results/);
  assert.match(retainedHtml, /original-document \/ original-version/);
  assert.match(retainedHtml, /later-document \/ later-fixed-version/);
  assert.match(retainedPrinted, /原样状态：completed/);
  const artifactAnchor = queryPolicyApi.queryPolicyArtifactAnchor(laterArtifact);
  assert.ok(retainedHtml.includes('href="#' + artifactAnchor + '"'));
  assert.ok(retainedHtml.includes('id="' + artifactAnchor + '"'));
  assert.match(retainedHtml, /阅读这份固定专业产物记录/);
  assert.match(retainedHtml, /insufficient/);
  assert.equal(JSON.stringify(retainedAnalysis), retainedBefore, "Showing the old draft must not change its answer, refs or professional results");
  const mismatchedArtifact = JSON.parse(retainedBefore);
  mismatchedArtifact.query_policy.synthesis_reading_basis.not_incorporated.artifacts[0].version = "unmatched-version";
  const mismatchedHtml = await renderToString(Vue.createSSRApp(progressPanel, { analysis: mismatchedArtifact }));
  assert.doesNotMatch(mismatchedHtml, /查看这份专业产物记录/);
  assert.match(mismatchedHtml, /未找到可展开的对应专业产物记录，未猜测入口/);

  const historyWrites = [];
  const history = historyPage(async (url, options) => {
    historyWrites.push({ url, options });
    throw new Error("Offline unknown history acceptance response");
  });
  history.selected.value = { id: "frozen-study", cutoff: "2018-01-01T00:00:00Z" };
  await history.startSemantic();
  await history.startSemantic();
  assert.equal(historyWrites.length, 1);
  assert.equal(historyWrites[0].url, "/history/studies/frozen-study/analyses");
  assert.deepEqual(JSON.parse(historyWrites[0].options.body), { query_policy: "query_planner" });
  assert.equal(history.uncertainWrite.value, "offline-history-write");
  console.log(JSON.stringify({ status: "passed", checks: [
    "legacy_analysis_remains_readable", "actual_policy_version_and_untrained_status_visible",
    "unknown_cost_remains_unknown", "possible_coverage_does_not_mean_completed",
    "global_query_receipt_identity_visible", "next_decision_observed_receipts_visible",
    "actual_as_of_input_boundary_visible", "effective_fallback_policy_visible",
    "printed_report_keeps_actual_policy_unknown_budget_and_unchecked_scope",
    "history_uses_server_frozen_scope_and_never_retries_unknown_write",
    "v2_capability_receipt_artifact_and_next_observation_visible",
    "v2_print_keeps_real_processing_scope_and_scientific_limits",
    "impact_comparison_names_its_parent_report_without_relabeling_the_displayed_later_report",
    "retained_reading_input_boundary_later_results_and_exact_artifact_record_links_remain_separate",
  ], qualification: "Offline UI controls only." }));
}

module.exports = main;
module.exports.printableText = printableText;
module.exports.compileComponent = compileComponent;
if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
