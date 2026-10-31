// Exercise the real question form, API client and rendered report without network access.
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

function compile(code) {
  return ts.transpileModule(code, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
}
function script(relative) {
  const source = fs.readFileSync(path.join(root, relative), "utf8");
  return compile(source.slice(source.indexOf(">") + 1, source.indexOf("</script>")));
}
class EngineError extends Error {
  constructor(status, message, code, details) {
    super(message); Object.assign(this, { status, code, details });
  }
}
function form(request) {
  const events = [], cleanups = [];
  const props = { initialQuestion: "Original question", disabled: false };
  const context = {
    exports: {}, console, Error,
    defineProps: () => props, defineEmits: () => (...args) => events.push(args),
    require(name) {
      if (name === "vue") return { ...Vue, onMounted() {}, onUnmounted: fn => cleanups.push(fn) };
      if (name.endsWith("/api/engine")) return { engineRequest: request, EngineError };
      return {};
    },
  };
  vm.createContext(context);
  vm.runInContext(script("apps/web/src/components/engine/ResearchQuestionForm.vue") +
    "\nglobalThis.probe={question,selectedSources,queryPolicy,budget,customBudget,loadedOptions,submit,loadOptions};", context);
  return { ...context.probe, props, events, unmount: () => cleanups.forEach(fn => fn()) };
}
function investigation(id = "I") {
  return {
    id, title: "Synthetic research", question: "Synthetic question", status: "open",
    revision: 1, entries: [], questions: [], material_count: 0, judgment_count: 0,
    needs_review: false, is_demo: false,
  };
}
function page(request) {
  const route = Vue.reactive({ path: "/investigations", query: {} });
  const context = {
    exports: {}, console, Error,
    crypto: { randomUUID: () => "synthetic-stable-intent" },
    require(name) {
      if (name === "vue") return { ...Vue, onMounted() {}, onUnmounted() {} };
      if (name === "vue-router") return {
        useRoute: () => route, useRouter: () => ({ replace: async value => Object.assign(route, value) }),
      };
      if (name.endsWith("/api/engine")) return { engineRequest: request, post: request, EngineError };
      return {};
    },
  };
  vm.createContext(context);
  vm.runInContext(script("apps/web/src/pages/InvestigationsPage.vue") +
    "\nglobalThis.probe={createInvestigation,selected,error,message,showCreate};", context);
  return { ...context.probe, route };
}
function analysisPanel(request) {
  const events = [];
  const props = { investigation: { ...investigation("I"), revision: 7 }, busy: false, loading: false };
  const context = {
    exports: {}, console, Error,
    crypto: { randomUUID: () => "synthetic-follow-up" }, setTimeout: () => 1, clearTimeout() {},
    defineProps: () => props, defineEmits: () => (...args) => events.push(args),
    require(name) {
      if (name === "vue") return { ...Vue, watch() {}, onUnmounted() {} };
      if (name.endsWith("/api/engine")) return { engineRequest: request, EngineError };
      return {};
    },
  };
  vm.createContext(context);
  vm.runInContext(script("apps/web/src/components/engine/MachineAnalysisPanel.vue") +
    "\nglobalThis.probe={startAnalysis,refresh,list,selected,queryProvider,queryTerms,queryState};", context);
  return { ...context.probe, props, events };
}
async function renderReport(status, genericState = "passed", calls = [], reportFields = {}, resultFields = {}, analysisFields = {}, humanDecision = null) {
  const relative = "apps/web/src/components/engine/MachineAnalysisResult.vue";
  const source = fs.readFileSync(path.join(root, relative), "utf8");
  const { descriptor } = compiler.parse(source, { filename: relative });
  const compiledScript = compiler.compileScript(descriptor, { id: "research-report-test" });
  const template = compiler.compileTemplate({
    source: descriptor.template.content, filename: relative, id: "research-report-test",
    compilerOptions: { bindingMetadata: compiledScript.bindings },
  });
  assert.equal(template.errors.length, 0);
  const element = tag => Vue.defineComponent({
    inheritAttrs: false,
    setup(_, { slots, attrs }) {
      return () => Vue.h(tag, { "data-type": attrs.type }, slots.default?.());
    },
  });
  const context = {
    exports: {}, console, Error,
    require(name) {
      if (name === "vue") return Vue;
      if (name === "element-plus") return { ElButton: element("button"), ElTag: element("span") };
      if (name === "./ReportSourceIndex.vue") {
        const helper = { exports: {} };
        vm.runInNewContext(compile(fs.readFileSync(path.join(root, "apps/web/src/api/reportDelivery.ts"), "utf8")), helper);
        return { default: require("./check_query_policy_ui.cjs").compileComponent(
          "apps/web/src/components/engine/ReportSourceIndex.vue", { "../../api/reportDelivery": helper.exports },
        ) };
      }
      return {};
    },
  };
  vm.createContext(context);
  vm.runInContext(compile(compiledScript.content + "\n" + template.code) +
    "\nexports.default.render=exports.render;", context);
  const result = {
    report: { final_text: "Exact final report text", status, provider: "Synthetic provider", ...reportFields },
    conditional_answer: { answer: "Earlier structured answer", conditions: [], unknowns: [] },
    claims: [], explanations: [], gaps: [], limitations: [],
    review: { status: genericState, claims: [], overall_limitations: [] },
    update: { stance: "initial", reasons: [], changed_claims: [] },
    ...resultFields,
  };
  return renderToString(Vue.createSSRApp(context.exports.default, {
    analysis: { state: "completed", result, calls, evidence: [], ...analysisFields }, humanDecision,
  }));
}
async function renderPanelScope(scope) {
  const { compileComponent } = require("./check_query_policy_ui.cjs");
  const empty = Vue.defineComponent({ setup: () => () => null });
  const panel = compileComponent("apps/web/src/components/engine/MachineAnalysisPanel.vue", {
    "../../api/engine": {
      engineRequest: () => new Promise(() => {}),
      EngineError,
    },
    "element-plus": { ElButton: empty, ElDrawer: empty, ElTag: empty },
    "./MachineAnalysisResult.vue": { default: empty },
    "./ResearchQuestionForm.vue": { default: empty },
    "./ResearchProgress.vue": { default: empty },
  });
  const originalSetup = panel.setup;
  panel.setup = (props, context) => {
    const state = originalSetup(props, context);
    state.selected.value = {
      id: "offline-scope-control", state: "needs_review", question: "Offline scope control",
      is_demo: false, evidence_count: 5, queries: [], limitations: [], search_scope: [],
      calls: [], budget: {}, version: "offline", prompt_version: "offline",
      progress: scope ? { material_scope: scope } : {},
    };
    return state;
  };
  const app = Vue.createSSRApp(panel, {
    investigation: investigation(), busy: false, loading: false, readOnly: true,
  });
  app.component("RouterLink", empty);
  return renderToString(app);
}

async function main() {
  const apiContext = {
    exports: {}, Headers, console,
    fetch: async () => new Response(JSON.stringify({
      error: { message: "Synthetic rejected research", code: "research_failed", details: { investigation_id: "retained" } },
    }), { status: 503, headers: { "content-type": "application/json" } }),
    require: () => ({}),
  };
  vm.createContext(apiContext);
  vm.runInContext(compile(fs.readFileSync(path.join(root, "apps/web/src/api/engine.ts"), "utf8")), apiContext);
  await assert.rejects(apiContext.exports.engineRequest("/investigations/ask"), error =>
    error.code === "research_failed" && error.details.investigation_id === "retained");
  apiContext.fetch = async () => new Response("# Exact Markdown\n\nReport text", { status: 200 });
  assert.equal(await apiContext.exports.engineRequest("/export?format=markdown", {}, "text"), "# Exact Markdown\n\nReport text");

  const question = form(async () => ({
    enabled: true, configured: true, retrieval_sources: ["clinicaltrials"], limits: { max_calls: 8 },
  }));
  await question.loadOptions();
  question.question.value = "  Follow this question  ";
  question.selectedSources.value = ["clinicaltrials"];
  question.customBudget.value = true;
  question.queryPolicy.value = "query_planner";
  question.budget.value = { max_calls: 4 };
  question.submit();
  assert.equal(question.events[0][1].question, "Follow this question");
  assert.deepEqual(Array.from(question.events[0][1].source_ids), ["clinicaltrials"]);
  assert.equal(question.events[0][1].budget.max_calls, 4);
  assert.equal(question.events[0][1].query_policy, "query_planner");
  question.props.initialQuestion = "Background refresh";
  assert.equal(question.question.value, "  Follow this question  ");
  question.unmount();

  const lostWrites = [];
  const lost = page(async (url, options) => {
    lostWrites.push(options);
    throw new Error("Synthetic lost acceptance response");
  });
  const submission = { question: "Synthetic question", source_ids: [], query_policy: "query_planner" };
  await lost.createInvestigation(submission);
  await lost.createInvestigation(submission);
  assert.equal(lostWrites.length, 2);
  assert.equal(JSON.parse(lostWrites[0].body).query_policy, "query_planner");
  assert.equal(lostWrites[0].headers["Idempotency-Key"], lostWrites[1].headers["Idempotency-Key"]);

  const retained = page(async url => {
    if (url.endsWith("/ask")) throw new EngineError(503, "Synthetic retrieval failure", "failed", { investigation_id: "retained" });
    if (url === "/session") return { enabled: true, authenticated: true };
    if (url === "/investigations") return { items: [investigation("retained")] };
    if (url.startsWith("/snapshots")) return { items: [] };
    return investigation("retained");
  });
  await retained.createInvestigation(submission);
  assert.equal(retained.selected.value.id, "retained");
  assert.equal(retained.error.value, "Synthetic retrieval failure");
  assert.equal(retained.showCreate.value, false);

  const followWrites = [];
  const follow = analysisPanel(async (url, options) => {
    if (options?.method === "POST") { followWrites.push(JSON.parse(options.body)); return { id: "child" }; }
    if (url.endsWith("/analyses")) return { enabled: true, configured: true, items: [{ id: "child", state: "queued" }] };
    return { id: "child", state: "queued", calls: [], queries: [] };
  });
  const fixedRead = { kind: "document_read", provider: "fabric", phase: "followup", status: "read", plan: {
    source_id: "who_don", records: [{ source_record_id: "offline-fixed-original", version_id: "frozen", window_start: 6000 }],
  } };
  assert.equal(follow.queryProvider(fixedRead), "Fabric 固定原件点读");
  assert.match(follow.queryTerms(fixedRead), /offline-fixed-original.*固定版本 frozen.*Unicode窗口起点 6000/);
  assert.doesNotMatch(follow.queryTerms(fixedRead), /图谱/);
  for (const [kind, label] of [["dataset_prepare", "分析数据整理"], ["analysis_run", "实际计算"], ["professional_assess", "专业评价"], ["source_fetch", "公开来源补采"]]) {
    assert.ok(follow.queryProvider({ ...fixedRead, kind }).includes(label));
  }
  assert.match(follow.queryState({ ...fixedRead, kind: "analysis_run" }), /动作已返回/);
  follow.list.value = { enabled: true, configured: true, items: [{ id: "parent", state: "completed", result_summary: "Old" }] };
  follow.selected.value = { id: "parent", question: "Earlier question", result: { report: { final_text: "Old report" } }, search_scope: ["clinicaltrials"], query_policy: { requested: "query_planner" } };
  await follow.startAnalysis(undefined, { question: "New follow-up", source_ids: ["europe_pmc"], budget: { max_calls: 4 } });
  assert.equal(followWrites[0].question, "New follow-up");
  assert.equal(followWrites[0].parent_analysis_id, "parent");
  assert.equal(followWrites[0].expected_revision, 7);
  assert.equal(followWrites[0].auto_retrieve, true);
  assert.deepEqual(followWrites[0].source_ids, ["europe_pmc"]);
  assert.equal(followWrites[0].budget.max_calls, 4);
  assert.equal(followWrites[0].query_policy, "query_planner");

  const retryWrites = [];
  let acceptRetry, retryAccepted = false;
  const pendingRetry = new Promise(resolve => { acceptRetry = resolve; });
  const retainedDraft = {
    id: "held", state: "needs_review", question: "Same research question",
    parent_analysis_id: "original-parent", search_scope: ["europe_pmc"],
    budget: { limits: { max_calls: 10, max_tokens: 50000, max_seconds: 600, max_queries: 3 } },
    result: { report: { final_text: "Preserved failed draft", status: "FAIL" } },
    calls: [], queries: [], query_policy: { requested: "baseline" },
  };
  const retry = analysisPanel(async (url, options) => {
    if (options?.method === "POST") {
      retryWrites.push({ body: JSON.parse(options.body), headers: options.headers });
      return pendingRetry;
    }
    if (url.endsWith("/analyses")) return {
      enabled: true, configured: true,
      items: [{ id: retryAccepted ? "new-attempt" : "held", state: retryAccepted ? "queued" : "needs_review" }],
    };
    return retryAccepted ? { id: "new-attempt", state: "queued", calls: [], queries: [] } : retainedDraft;
  });
  retry.list.value = { enabled: true, configured: true, items: [{ id: "held", state: "needs_review" }] };
  retry.selected.value = retainedDraft;
  await retry.refresh();
  assert.equal(retryWrites.length, 0, "Refreshing a held report must not retry automatically");
  const retryWork = retry.startAnalysis("held");
  await retry.startAnalysis("held");
  assert.equal(retryWrites.length, 1, "Repeated clicks while submitting must not create another attempt");
  assert.equal(retryWrites[0].body.retry_of_analysis_id, "held");
  assert.equal(retryWrites[0].body.parent_analysis_id, "original-parent");
  assert.equal(retryWrites[0].body.question, retainedDraft.question);
  assert.deepEqual(retryWrites[0].body.source_ids, retainedDraft.search_scope);
  assert.deepEqual(retryWrites[0].body.budget, retainedDraft.budget.limits);
  assert.equal(retryWrites[0].body.expected_revision, 7);
  assert.equal(retryWrites[0].body.query_policy, "baseline");
  assert.ok(retryWrites[0].headers["Idempotency-Key"]);
  retryAccepted = true; acceptRetry({ id: "new-attempt" }); await retryWork;
  assert.equal(retry.selected.value.id, "new-attempt");
  assert.equal(retainedDraft.result.report.final_text, "Preserved failed draft");
  assert.equal(retainedDraft.result.report.status, "FAIL");

  const scope = {
    fixed_version_count: 8, read_version_count: 5,
    read_scope: "selected_passages_not_full_documents",
    unread_versions: ["unread-a", "unread-b", "unread-c"].map((id, i) => ({
      source_id: "who_don", source_record_id: id, version_id: String(i).repeat(64),
    })),
    source_failures: [
      { provider: "fabric", source_id: "who_don", status: "unavailable", error_code: "source_unavailable" },
      { provider: "weknora", source_id: null, status: "failed", error_code: "retrieval_failed" },
    ],
    qualification: "固定计算与选定原文片段分别计数。",
  };
  scope.unread_versions[0].query_hit = true;
  scope.unread_versions[0].adoption_stop_reason = "analysis_evidence_budget";
  const scopeHtml = await renderPanelScope(scope);
  assert.match(scopeHtml, /固定资料 8 个版本；实际读取其中 5 个版本的选定片段/);
  assert.match(scopeHtml, /未进入本轮原文片段：3 个版本；补查未完成：2 次/);
  assert.match(scopeHtml, /不是全文阅读量、独立研究数或全领域覆盖率/);
  assert.match(scopeHtml, /未读材料与失败补查不代表没有相关证据/);
  assert.match(scopeHtml, /成功补查命中但未读：1 个固定版本/);
  assert.match(scopeHtml, /本轮原文片段容量已满，这些命中未进入已读证据/);
  assert.match(scopeHtml, /unread-c/);
  assert.match(scopeHtml, /source_unavailable/);
  assert.match(scopeHtml, /retrieval_failed/);
  const scopePrint = require("./check_query_policy_ui.cjs").printableText(scopeHtml);
  assert.match(scopePrint, /固定资料 8 个版本；实际读取其中 5 个版本的选定片段/);
  assert.match(scopePrint, /未进入本轮原文片段：3 个版本；补查未完成：2 次/);
  assert.match(scopePrint, /成功补查命中但未读：1 个固定版本/);
  assert.doesNotMatch(await renderPanelScope(null), /本轮资料范围（系统记录）/,
    "Legacy analyses must not acquire fabricated reading counts");

  const failedVerification = await renderReport("failed", "needs_review", [], {
    verification: null, error_code: "method_input_invalid",
    verification_error: "SYNTHETIC 核验服务未能返回合格结果，正文保留。",
    support_audit: { status: "NOT_RUN", not_run_reason: "verification_unavailable" },
  });
  assert.match(failedVerification, /报告核验未完成/);
  assert.match(failedVerification, /核验未完成原因：/);
  assert.match(failedVerification, /SYNTHETIC 核验服务未能返回合格结果，正文保留/);
  assert.match(failedVerification, /method_input_invalid/);
  assert.match(failedVerification, /所选片段复核未执行/);
  assert.match(failedVerification, /Exact final report text/);
  assert.doesNotMatch(failedVerification, /BioCheck 原核验通过/);
  assert.match(require("./check_query_policy_ui.cjs").printableText(failedVerification),
    /SYNTHETIC 核验服务未能返回合格结果，正文保留/);

  const generationFailed = await renderReport("failed", "needs_review", [], {
    generation_error: "SYNTHETIC 专业生成拒绝，保留结构稿。",
    error_code: "method_input_invalid", verification: null,
  });
  assert.match(generationFailed, /报告生成未完成/);
  assert.match(generationFailed, /此前已保存的结构化分析稿，专业生成与核验未完成/);
  assert.match(generationFailed, /SYNTHETIC 专业生成拒绝，保留结构稿/);
  assert.doesNotMatch(generationFailed, /核验未完成原因：/);
  assert.doesNotMatch(generationFailed, /BioCheck 原核验通过/);

  const review = await renderReport("REVIEW");
  assert.match(review, /Exact final report text/);
  assert.match(review, /报告待核/);
  assert.doesNotMatch(review, /BioCheck 原核验通过/);
  assert.match(review, /报告生成前的结构化分析与引文记录/);
  const passed = await renderReport("PASS");
  assert.match(passed, /BioCheck 原核验通过/);
  assert.match(passed, /Exact final report text/);
  assert.match(passed, /上方核验状态对应上方报告正文/);

  const notRun = await renderReport("PASS", "needs_review");
  assert.match(notRun, /前期结构化分析未单独通用核验/);
  assert.doesNotMatch(notRun, /通用复核仍有待核问题/);
  assert.doesNotMatch(notRun, /同一配置模型的另一次检查/);
  const reviewed = await renderReport("REVIEW", "needs_review", [{ stage: "check", status: "received" }]);
  assert.match(reviewed, /通用复核仍有待核问题/);

  const gap = {
    unit_id: "U2", claim_index: 1, kind: "material_absence",
    span: "SYNTHETIC statement awaiting source evidence",
    reason: { item: "SYNTHETIC missing experiment", material: "E1" },
    action: "assess_need_for_retrieval",
  };
  const feedback = {
    text_sha256: "current-body", evidence_gaps: [gap], repair_units: [],
    review_required: [{ reason: "SYNTHETIC source receipt needs reconciliation" }],
  };
  const roundFields = {
    text_sha256: "current-body", quality_feedback: feedback,
    support_audit: { status: "PASS", checks: [] },
    rounds: [
      { index: 0, kind: "generation", status: "checked", quality_feedback: {
        text_sha256: "previous-body", evidence_gaps: [{ reason: "SYNTHETIC earlier round issue" }],
      }, unsearched_gaps: [] },
      { index: 1, kind: "generation", status: "checked", quality_feedback: feedback,
        feedback_stop_reason: "quality_feedback_round_limit", unsearched_gaps: [gap] },
    ],
  };
  const feedbackReport = await renderReport("FAIL", "passed", [], roundFields, {
    gaps: [{ id: "old-gap", question: "SYNTHETIC earlier structured gap", why_it_matters: "Earlier planning input" }],
  });
  assert.match(feedbackReport, /核验后反馈与未补齐问题/);
  assert.match(feedbackReport, /反馈对应当前保存正文/);
  assert.match(feedbackReport, /待补内容：SYNTHETIC missing experiment/);
  assert.match(feedbackReport, /材料：E1/);
  assert.match(feedbackReport, /SYNTHETIC source receipt needs reconciliation/);
  assert.match(feedbackReport, /仍未补齐：1 项/);
  assert.match(feedbackReport, /已达到核验后补查轮次上限/);
  assert.match(feedbackReport, /报告生成前的结构化分析缺口/);
  assert.match(feedbackReport, /报告核验未通过/);
  assert.match(feedbackReport, /所列片段支持复核通过/);
  assert.doesNotMatch(feedbackReport, /BioCheck 原核验通过/);
  const printedFeedback = require("./check_query_policy_ui.cjs").printableText(feedbackReport);
  assert.match(printedFeedback, /核验后反馈与未补齐问题/);
  assert.match(printedFeedback, /SYNTHETIC missing experiment/);
  assert.match(printedFeedback, /仍未补齐：1 项/);
  assert.match(printedFeedback, /已达到核验后补查轮次上限/);
  assert.doesNotMatch(printedFeedback, /核验后完整反馈回执/);
  const roundOnly = await renderReport("REVIEW", "passed", [], {
    ...roundFields, quality_feedback: undefined,
    rounds: [{ index: 0, kind: "generation", status: "checked", quality_feedback: feedback,
      unsearched_gaps: [gap], feedback_stop_reason: "budget_or_review_required" }],
  });
  assert.match(roundOnly, /反馈对应当前保存正文/);
  assert.match(roundOnly, /预算条件不足或仍有需核对事项/);
  const earlierFeedback = await renderReport("REVIEW", "passed", [], {
    text_sha256: "current-body", quality_feedback: { ...feedback, text_sha256: "previous-body" },
  });
  assert.match(earlierFeedback, /这份反馈对应此前稿件/);
  assert.doesNotMatch(earlierFeedback, /反馈对应当前保存正文/);
  assert.match(review, /本版未保存核验后补查与修订反馈/);
  const noReport = await renderReport("PASS", "needs_review", [], {}, { report: null });
  assert.doesNotMatch(noReport, /核验后反馈与未补齐问题/);
  assert.doesNotMatch(noReport, /BioCheck 原核验通过/);
  const retainedReading = await renderReport("PASS", "needs_review", [], {}, {
    report: null,
    query_policy: { synthesis_reading_basis: { call_key: "read_plan:original", call_id: "original-call",
      reading_evidence_hash: "original-input-hash", status: "unfinished_reading_draft",
      qualification: "SYNTHETIC original reading basis" } },
  }, { state: "needs_review" });
  assert.match(retainedReading, /此前阅读草稿（综合未完成）/);
  assert.match(retainedReading, /尚未纳入后续结果，不能作为当前结论/);
  assert.match(retainedReading, /Earlier structured answer/);
  assert.doesNotMatch(retainedReading, /对研究问题的回答/);
  assert.doesNotMatch(retainedReading, /BioCheck 原核验通过/);

  const citedBody = "Original body [qreal] [record:S1] [qmissing] <not-html>";
  const fixedRef = { source_id: "who_don", source_record_id: "notice", adopted_version: "frozen-version" };
  const citedHtml = await renderReport("FAIL", "passed", [], {
    final_text: citedBody, text_sha256: "saved-body-hash", evidence_map: { S1: { key: "qreal", record_ref: fixedRef } },
    support_audit: { status: "PASS", checks: [] },
  }, {
    warning_assessment: { decision: "verify_now", reason: "SYNTHETIC worthwhile verification", supporting_claim_ids: [], opposing_claim_ids: [], critical_unknowns: ["SYNTHETIC source independence unknown"], next_actions: [] },
    competing_hypotheses: [{ id: "H1", explanation: "SYNTHETIC competing explanation", supporting_claim_ids: [], opposing_claim_ids: [], discriminating_evidence_needed: "SYNTHETIC distinguishing observation", untested_conditions: ["SYNTHETIC untested condition"] }],
    reading_manifest: [{ source_id: "who_don", source_record_id: "notice", version_id: "v1", status: "unread", priority: "critical", reason: "counterevidence", covers: ["C1"], window_start: 321 }],
  }, {
    trigger: "history", query_policy: { evidence_time: { mode: "operational_replay", cutoff: "2020-01-01T00:00:00Z" } },
    evidence: [{ key: "qreal", source_id: "who_don", title: "SYNTHETIC original title", publication_time: "2019-12-31", record_ref: fixedRef }],
  });
  assert.match(citedHtml, /<div class="report-document">Original body \[qreal\] \[record:S1\] \[qmissing\] &lt;not-html&gt;<\/div>/);
  assert.match(citedHtml, /WHO 通报 · 2019-12-31 · SYNTHETIC original title/);
  assert.match(citedHtml, /qmissing.*无法绑定本版固定原文/);
  assert.match(citedHtml, /来源标签与下方阅读注释不属于原保存正文/);
  assert.match(citedHtml, /文字核验判断转述和引用是否准确/);
  assert.match(citedHtml, /不证明传言属实/);
  assert.match(citedHtml, /现代模型仍可能含训练后见知识/);
  assert.match(citedHtml, /立即核实/);
  assert.match(citedHtml, /不是真实性概率或潜在后果评分/);
  assert.match(citedHtml, /SYNTHETIC untested condition/);
  assert.match(citedHtml, /后续窗口未读，已保留此前片段/);
  assert.match(citedHtml, /窗口起点 321/);
  assert.match(citedHtml, /不代表覆盖已完成/);
  assert.match(citedHtml, /报告核验未通过/);
  assert.match(citedHtml, /所列片段支持复核通过/);
  assert.match(citedHtml, /尚未记录人工判断/);
  const conflictingVersion = await renderReport("FAIL", "passed", [], {
    final_text: "Unchanged body [record:S1]", evidence_map: { S1: { key: "qreal", version: "DIFFERENT-FIXED-VERSION" } },
  }, {}, {
    evidence: [{ key: "qreal", source_id: "who_don", title: "SYNTHETIC original title", publication_time: "2019-12-31", record_ref: fixedRef }],
  });
  assert.match(conflictingVersion, /S1.*无法绑定本版固定原文/);
  assert.doesNotMatch(conflictingVersion, /WHO 通报 · 2019-12-31 · SYNTHETIC original title/);
  const citedPrinted = require("./check_query_policy_ui.cjs").printableText(citedHtml);
  assert.match(citedPrinted, /系统补充/);
  assert.match(citedPrinted, /SYNTHETIC original title/);
  assert.match(citedPrinted, /不证明传言属实/);
  assert.doesNotMatch(citedPrinted, /这是添加系统来源标签的阅读视图/);

  const empty = Vue.defineComponent({ setup: () => () => null });
  const progressComponent = require("./check_query_policy_ui.cjs").compileComponent(
    "apps/web/src/components/engine/ResearchProgress.vue", { "./PolicyTracePanel.vue": { default: empty } },
  );
  const waitingHtml = await renderToString(Vue.createSSRApp(progressComponent, { analysis: {
    state: "queued", calls: [], queries: [], progress: { stage: "queued", budget_pool: "topic",
      budget_wait: { reason: "analysis_topic_budget", pool: "topic", next_budget_at: "2026-10-31T08:00:00Z", started_at: "2026-10-31T07:30:00Z" } },
  } }));
  assert.match(waitingHtml, /等待预算恢复/);
  assert.match(waitingHtml, /专题跟踪额度暂不可用/);
  assert.match(waitingHtml, /预计恢复时点/);
  assert.match(waitingHtml, /不表示调查失败/);
  assert.doesNotMatch(waitingHtml, /本次未完成/);
  const fetchingHtml = await renderToString(Vue.createSSRApp(progressComponent, { analysis: {
    state: "queued", calls: [], queries: [], progress: { waiting_fetch: {
      reason: "analysis_fetch_pending", action_id: "offline-fetch", next_poll_at: "2026-10-31T08:00:05Z",
    } },
  } }));
  assert.match(fetchingHtml, /等待公开资料采集/);
  assert.match(fetchingHtml, /下次检查/);
  assert.match(fetchingHtml, /不表示采集失败/);


  const engineLabelsHelper = { exports: {} };
  vm.runInNewContext(compile(fs.readFileSync(path.join(root, "apps/web/src/api/engineLabels.ts"), "utf8")), engineLabelsHelper);
  const kddHelper = { exports: {}, require(name) {
    if (name === "./engineLabels") return engineLabelsHelper.exports;
    throw new Error("Unexpected API helper import: " + name);
  } };
  vm.runInNewContext(compile(fs.readFileSync(path.join(root, "apps/web/src/api/kdd.ts"), "utf8")), kddHelper);
  const trackingComponent = require("./check_query_policy_ui.cjs").compileComponent(
    "apps/web/src/components/engine/TopicTrackingCard.vue", { "../../api/kdd": kddHelper.exports },
  );
  const trackingValue = {
    version: "source-evidence-heat-lifecycle-v1", lifecycle: "cooling", cadence_hours: 1,
    next_due_at: "2026-10-31T09:00:00Z", evidence: { level: "unverified", confirmed_source_groups: 0, independent_study_count: null, unknown_source_records: 2, basis_refs: [] },
    heat: { score: 3, new_source_groups: 1, confirmed_new_source_groups: 0, half_life_hours: 36 },
    correction: { status: "corrected", refs: [], counterevidence_refs: [] },
    risk: { status: "unresolved", critical_gaps: ["source_unavailable_not_evidence_of_resolution"] },
    model_update: { needed: false, reasons: [], status: "not_needed" },
    environment_signals: [{ kind: "deletion", record_ref: { provider: "offline", object_id: "notice", adopted_version: "fixed" } }],
  };
  const trackingHtml = await renderToString(Vue.createSSRApp(trackingComponent, { tracking: trackingValue }));
  for (const label of ["证据强度", "采用来源曝光热度", "更正／反证", "生命周期", "仍未解除", "降温", "尚不能完整确认", "不是人群热度或真实性概率", "不作为传言真伪或风险解除的证明"]) assert.ok(trackingHtml.includes(label), label);
  const trackingPrint = require("./check_query_policy_ui.cjs").printableText(trackingHtml);
  assert.match(trackingPrint, /仍未解除/);
  assert.match(trackingPrint, /已有更正／撤回材料/);
  assert.match(trackingPrint, /下次应检查/);
  assert.doesNotMatch(await renderToString(Vue.createSSRApp(trackingComponent, { tracking: null })), /证据强度/);
  const pendingJudgmentTracking = { ...trackingValue,
    negative_signals: [{ kind: "not_found", record_ref: { object_id: "notice-1" } }],
    hypothesis_comparison: { current_basis_unchanged: false, current_judgment: {
      least_refuted_hypothesis_id: "H1", reason: "SYNTHETIC prior comparison",
      next_discriminating_evidence: "SYNTHETIC next source check", qualification: "这是模型判断，未证实",
    } },
  };
  const pendingJudgmentHtml = await renderToString(Vue.createSSRApp(trackingComponent, { tracking: pendingJudgmentTracking }));
  assert.match(pendingJudgmentHtml, /热度可降、风险未排除/);
  assert.match(pendingJudgmentHtml, /旧依据下判断，等待新研判/);
  assert.match(pendingJudgmentHtml, /这是模型判断，未证实/);
  assert.match(pendingJudgmentHtml, /SYNTHETIC next source check/);

  const attentionComponent = require("./check_query_policy_ui.cjs").compileComponent(
    "apps/web/src/components/engine/SourceAttentionPanel.vue", { "../../api/kdd": kddHelper.exports },
  );
  const attentionHtml = await renderToString(Vue.createSSRApp(attentionComponent, { attention: {
    version: "offline", truth_confidence: { status: "unverified" }, potential_consequence: { level: "potentially_serious" },
    verification_priority: { importance: "high", urgency: "immediate" }, attention_reasons: [], checkable_claims: [],
  } }));
  assert.match(attentionHtml, /尚未证实/);
  assert.match(attentionHtml, /若说法成立，潜在后果可能严重/);
  assert.match(attentionHtml, /立即核实/);
  assert.match(attentionHtml, /事后不成立的说法，当时也可能值得核实/);

  const controls = [];
  const topicContext = {
    exports: {}, console, Error, setTimeout: () => 1, clearTimeout() {},
    require(name) {
      if (name === "vue") return { ...Vue, onMounted() {}, onUnmounted() {} };
      if (name === "vue-router") return { useRoute: () => ({ query: {} }), useRouter: () => ({ replace: async () => {} }) };
      if (name.endsWith("/api/engine")) return { EngineError, post: async (url, body) => {
        controls.push({ url, body }); return { ...topicContext.probe.selected.value, control_revision: 8 };
      } };
      return {};
    },
  };
  vm.createContext(topicContext);
  vm.runInContext(script("apps/web/src/pages/TopicsPage.vue") +
    "\nglobalThis.probe={togglePause,selected,lifecycleReason,error};", topicContext);
  topicContext.probe.selected.value = { id: "offline-topic", control_revision: 7, paused: false };
  await Vue.nextTick();
  await topicContext.probe.togglePause("closed");
  assert.equal(controls.length, 0);
  assert.match(topicContext.probe.error.value, /请先记录/);
  topicContext.probe.lifecycleReason.value = "SYNTHETIC explicit closure reason; risk remains unresolved";
  await topicContext.probe.togglePause("closed");
  assert.equal(controls.length, 1);
  assert.equal(controls[0].body.expected_revision, 7);
  assert.equal(controls[0].body.paused, true);
  assert.equal(controls[0].body.lifecycle, "closed");
  assert.equal(controls[0].body.reason, topicContext.probe.lifecycleReason.value);

  const formElement = Vue.defineComponent({ setup(_, { slots }) {
    return () => Vue.h("div", slots.default?.());
  } });
  const configForm = require("./check_query_policy_ui.cjs").compileComponent(
    "apps/web/src/components/engine/ResearchQuestionForm.vue", {
      "element-plus": Object.fromEntries(["ElButton", "ElCheckbox", "ElInput", "ElInputNumber",
        "ElOption", "ElSelect"].map(key => [key, formElement])),
      "../../api/engine": { engineRequest() { throw new Error("Offline options rendering must not request HTTP"); } },
    },
  );
  const optionsHtml = await renderToString(Vue.createSSRApp(configForm, { options: {
    enabled: true, configured: true, retrieval_sources: [],
    limits: { max_calls: 27, max_tokens: 777777, max_seconds: 3500, document_search: 13,
      document_read: 29, pages_per_chain: 4, monitor_investigations_per_batch: 7 },
  } }));
  assert.match(optionsHtml, /实际执行配置/);
  assert.match(optionsHtml, /每链最大页数/);
  assert.match(optionsHtml, /每批自动调查上限/);
  assert.match(optionsHtml, /777,777/);
  assert.match(optionsHtml, />7</);
  const originalConfigSetup = configForm.setup;
  let renderPolicy = "baseline";
  configForm.setup = (props, context) => {
    const state = originalConfigSetup(props, context);
    state.queryPolicy.value = renderPolicy;
    return state;
  };
  const scopedOptions = {
    enabled: true, configured: true, retrieval_sources: [],
    limits: { max_calls: 27, max_tokens: 777777, evidence_versions: 32 },
    limits_by_policy: {
      baseline: { evidence_versions: 11, evidence_characters: 41000, records_per_query: 4,
        document_search: 9, document_read: 0, pages_per_chain: 1 },
      query_planner: { evidence_versions: 33, evidence_characters: 101000, records_per_query: 9,
        document_search: 17, document_read: 31, pages_per_chain: 6 },
    },
  };
  const baselineOptionsHtml = await renderToString(Vue.createSSRApp(configForm, { options: scopedOptions }));
  assert.match(baselineOptionsHtml, /采用版本上限<\/dt><dd>11<\/dd>/);
  assert.match(baselineOptionsHtml, /原件读取动作上限<\/dt><dd>0<\/dd>/);
  assert.match(baselineOptionsHtml, /每链最大页数<\/dt><dd>1<\/dd>/);
  renderPolicy = "query_planner";
  const plannerOptionsHtml = await renderToString(Vue.createSSRApp(configForm, { options: scopedOptions }));
  assert.match(plannerOptionsHtml, /采用版本上限<\/dt><dd>33<\/dd>/);
  assert.match(plannerOptionsHtml, /原件读取动作上限<\/dt><dd>31<\/dd>/);
  assert.match(plannerOptionsHtml, /每链最大页数<\/dt><dd>6<\/dd>/);

  await require("./check_query_policy_ui.cjs")();
  const unboundAssessment = await renderReport("PASS", "passed", [], {}, {
    assessment_binding: { status: "unbound", scope: "current_result_claim_ids", can_use_bound_claim_refs: false,
      unbound_claim_ids: ["OLD"], issues: [], qualification: "Synthetic binding diagnostic" },
    warning_assessment: { decision: "watch", supporting_claim_ids: ["OLD"], opposing_claim_ids: [],
      critical_unknowns: [], next_actions: [], reason: "Synthetic original assessment" },
  });
  assert.match(unboundAssessment, /aria-label="结构化判断引用未绑定"/);
  assert.match(unboundAssessment, /不作为已绑定评分或自动行动依据/);
  assert.match(unboundAssessment, /Exact final report text/);
  assert.match(unboundAssessment, /OLD（本版未找到对应陈述）/);
  const hypothesisJudgmentHtml = await renderReport("REVIEW", "needs_review", [], {}, {
    competing_hypotheses: [{ id: "H1", explanation: "SYNTHETIC candidate explanation",
      supporting_claim_ids: [], opposing_claim_ids: [], discriminating_evidence_needed: "source test",
      untested_conditions: [] }],
    current_judgment: { least_refuted_hypothesis_id: "H1", reason: "SYNTHETIC fewest current refutations",
      next_discriminating_evidence: "SYNTHETIC next discriminating observation",
      qualification: "这是模型判断，未证实" },
  });
  assert.match(hypothesisJudgmentHtml, /当前反证最少的假说：H1/);
  assert.match(hypothesisJudgmentHtml, /这是模型判断，未证实/);
  assert.match(hypothesisJudgmentHtml, /SYNTHETIC next discriminating observation/);
  console.log(JSON.stringify({ status: "passed", checks: [
    "api_preserves_retained_investigation_receipt",
    "markdown_download_reads_exact_text",
    "question_form_keeps_draft_during_parent_refresh",
    "question_form_submits_selected_scope_and_budget",
    "lost_ask_response_reuses_acceptance_key",
    "failed_ask_reopens_retained_investigation",
    "follow_up_preserves_parent_and_uses_new_question_scope_and_budget",
    "needs_review_explicit_retry_keeps_question_parent_scope_budget_and_old_report",
    "held_report_refresh_does_not_retry_and_repeated_click_is_ignored",
    "review_report_is_not_presented_as_passed",
    "final_report_is_distinct_from_earlier_structured_analysis",
    "unexecuted_general_review_is_not_reported_as_a_failed_check",
    "post_verification_feedback_and_round_stop_reasons_are_visible_and_printable",
    "current_feedback_is_bound_to_the_saved_body_and_separate_from_old_structured_gaps",
    "support_audit_pass_never_upgrades_whole_report_failure",
    "actual_read_passage_scope_unread_and_failed_queries_are_visible_and_printable",
    "missing_report_does_not_fabricate_professional_feedback",
    "readable_source_index_is_separate_from_exact_saved_body_and_fixed_original",
    "text_audit_mechanism_human_and_replay_statuses_remain_separate",
    "warning_priority_does_not_assert_rumor_truth_or_consequences",
    "budget_wait_displays_pool_and_restore_time_without_failure",
    "topic_evidence_heat_correction_lifecycle_and_unresolved_risk_are_separate",
    "source_truth_conditional_consequence_and_verification_priority_are_separate",
    "manual_topic_close_requires_reason_and_preserves_revision_gate",
    "fixed_document_windows_computation_professional_and_source_fetch_labels_match_actual_actions",
    "legacy_unbound_assessment_status_preserves_exact_body_and_missing_id_label",
    "retained_reading_answer_remains_an_unfinished_old_draft_without_a_current_conclusion_label",
  ] }));
}
module.exports = { renderReport };
if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
