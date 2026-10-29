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
    "\nglobalThis.probe={question,selectedSources,budget,customBudget,loadedOptions,submit,loadOptions};", context);
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
    "\nglobalThis.probe={startAnalysis,refresh,list,selected};", context);
  return { ...context.probe, props, events };
}
async function renderReport(status, genericState = "passed", calls = [], reportFields = {}) {
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
  };
  return renderToString(Vue.createSSRApp(context.exports.default, {
    analysis: { state: "completed", result, calls, evidence: [] },
  }));
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
  question.budget.value = { max_calls: 4 };
  question.submit();
  assert.equal(question.events[0][1].question, "Follow this question");
  assert.deepEqual(Array.from(question.events[0][1].source_ids), ["clinicaltrials"]);
  assert.equal(question.events[0][1].budget.max_calls, 4);
  question.props.initialQuestion = "Background refresh";
  assert.equal(question.question.value, "  Follow this question  ");
  question.unmount();

  const lostWrites = [];
  const lost = page(async (url, options) => {
    lostWrites.push(options);
    throw new Error("Synthetic lost acceptance response");
  });
  const submission = { question: "Synthetic question", source_ids: [] };
  await lost.createInvestigation(submission);
  await lost.createInvestigation(submission);
  assert.equal(lostWrites.length, 2);
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
  follow.list.value = { enabled: true, configured: true, items: [{ id: "parent", state: "completed", result_summary: "Old" }] };
  follow.selected.value = { id: "parent", question: "Earlier question", result: { report: { final_text: "Old report" } }, search_scope: ["clinicaltrials"] };
  await follow.startAnalysis(undefined, { question: "New follow-up", source_ids: ["europe_pmc"], budget: { max_calls: 4 } });
  assert.equal(followWrites[0].question, "New follow-up");
  assert.equal(followWrites[0].parent_analysis_id, "parent");
  assert.equal(followWrites[0].expected_revision, 7);
  assert.equal(followWrites[0].auto_retrieve, true);
  assert.deepEqual(followWrites[0].source_ids, ["europe_pmc"]);
  assert.equal(followWrites[0].budget.max_calls, 4);

  const retryWrites = [];
  let acceptRetry, retryAccepted = false;
  const pendingRetry = new Promise(resolve => { acceptRetry = resolve; });
  const retainedDraft = {
    id: "held", state: "needs_review", question: "Same research question",
    parent_analysis_id: "original-parent", search_scope: ["europe_pmc"],
    budget: { limits: { max_calls: 10, max_tokens: 50000, max_seconds: 600, max_queries: 3 } },
    result: { report: { final_text: "Preserved failed draft", status: "FAIL" } },
    calls: [], queries: [],
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
  assert.ok(retryWrites[0].headers["Idempotency-Key"]);
  retryAccepted = true; acceptRetry({ id: "new-attempt" }); await retryWork;
  assert.equal(retry.selected.value.id, "new-attempt");
  assert.equal(retainedDraft.result.report.final_text, "Preserved failed draft");
  assert.equal(retainedDraft.result.report.status, "FAIL");

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
  ] }));
}
module.exports = { renderReport };
if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
