// SYNTHETIC offline ConnectionsPage controls: configuration is not a live receipt.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const deps = path.join(root, "apps/web/node_modules");
const ts = require(path.join(deps, "typescript"));
const Vue = require(path.join(deps, "vue"));
const { renderToString } = require(path.join(deps, "@vue/server-renderer"));
const loader = require("./check_query_policy_ui.cjs");
const source = fs.readFileSync(path.join(root, "apps/web/src/pages/ConnectionsPage.vue"), "utf8");
const code = ts.transpileModule(source.slice(source.indexOf(">") + 1, source.indexOf("</script>")), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const provider = { id: "knowledge", name: "WeKnora 知识检索", state: "not_connected", owner: "SYNTHETIC", note: "雷达执行器负责查询路由。" };
const system = { providers: [provider], dependencies: {} };
function probe(authenticated = true, rejectOptions = false) {
  const paths = [];
  const context = { exports: {}, AbortController, Error, require(name) {
    if (name === "vue") return { ...Vue, onMounted() {}, onUnmounted() {} };
    if (name.endsWith("/api/system")) return { getSystemStatus: async () => system };
    if (name.endsWith("/api/engine")) return { engineRequest: async request => {
      paths.push(request);
      if (request === "/session") return { authenticated };
      if (rejectOptions) throw new Error("SYNTHETIC scoped-options-unavailable");
      return { enabled: true, configured: true, retrieval_sources: [], limits: {}, providers: { weknora: { available: request.endsWith("true"), connection_checked: false } } };
    } };
    return {};
  } };
  vm.runInNewContext(code + "\nglobalThis.probe={refresh,providerState,research,isDemo:typeof isDemo==='undefined'?null:isDemo};", context);
  return { ...context.probe, paths };
}
async function main() {
  const page = probe();
  page.research.value = { providers: { weknora: { available: true } } };
  assert.match(page.providerState(provider), /配置可用/);
  assert.doesNotMatch(page.providerState(provider), /已连接/);
  page.research.value.providers.weknora.available = false;
  assert.match(page.providerState(provider), /当前范围不可用/);
  await page.refresh();
  assert.equal(page.paths.at(-1), "/investigations/research-options?is_demo=false");
  page.isDemo.value = true;
  await page.refresh();
  assert.equal(page.paths.at(-1), "/investigations/research-options?is_demo=true");
  page.research.value = null;
  assert.equal(page.providerState(provider), "当前范围状态待确认");
  const failed = probe(true, true); await failed.refresh();
  assert.equal(failed.providerState({ ...provider, state: "configured" }), "当前范围状态待确认");
  const anonymous = probe(false); await anonymous.refresh();
  assert.deepEqual(anonymous.paths, ["/session"]);
  const element = tag => Vue.defineComponent({ setup(_, { slots }) { return () => Vue.h(tag, slots.default?.()); } });
  const component = loader.compileComponent("apps/web/src/pages/ConnectionsPage.vue", {
    "../api/system": { getSystemStatus: async () => system },
    "../api/engine": { engineRequest: async () => { throw new Error("SSR cannot send HTTP"); } },
    "element-plus": { ElButton: element("button"), ElTag: element("span"), ElSwitch: element("label") },
  });
  const setup = component.setup;
  component.setup = (props, context) => {
    const state = setup(props, context); state.data.value = system;
    state.authenticated.value = true; state.research.value = { enabled: true, configured: true, retrieval_sources: [], providers: { weknora: { available: true } } };
    return state;
  };
  const app = Vue.createSSRApp(component); app.component("RouterLink", element("a"));
  const html = await renderToString(app);
  assert.match(html, /配置可用/); assert.match(html, /不代表.*实连/); assert.match(html, /真实资料/); assert.match(html, /演示资料/);
  assert.doesNotMatch(html, /knowledge_base_id|source_scope|SYNTHETIC-private/);
  console.log(JSON.stringify({ status: "passed", checks: ["available-only-config-status-visible", "unavailable-does-not-fall-back-to-stale-registry", "real-demo-scope-request-switch", "anonymous-does-not-request-scoped-options", "actual-connections-SSR-configuration-not-live-connection"] }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
