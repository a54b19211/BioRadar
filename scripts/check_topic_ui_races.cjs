// Delayed-response regressions for the real topic page scripts; no network or DB.
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "..");
const ts = require(path.join(root, "apps/web/node_modules/typescript"));
const Vue = require(path.join(root, "apps/web/node_modules/vue"));
function script(file) {
  const text = fs.readFileSync(path.join(root, file), "utf8");
  return ts.transpileModule(text.slice(text.indexOf(">") + 1, text.indexOf("</script>")),
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function topic(id, revision = 1) {
  return { id, control_revision: revision, scope_revision: 1, is_demo: true,
    definition: { title: "教学 " + id, question: "教学问题", domain: "research",
      source_instance: "synthetic", source_ids: ["external_archive"], profiles: ["text_exposure"] },
    sources: [], data_gaps: [], observation_count: 0 };
}
function page(request) {
  class TestEngineError extends Error {
    constructor(status) { super("Synthetic HTTP failure"); this.status = status; }
  }
  const callbacks = [];
  const context = { exports: {}, console, setTimeout: () => 1, clearTimeout() {},
    crypto: { randomUUID: () => "synthetic-request" },
    require(name) {
      if (name === "vue") return { ...Vue, onMounted() {}, onUnmounted: fn => callbacks.push(fn) };
      if (name === "vue-router") return { useRoute: () => ({ query: {} }), useRouter: () => ({ replace: async () => {} }) };
      if (name.endsWith("/api/engine")) return { engineRequest: request, post: request, EngineError: TestEngineError };
      return {};
    },
  };
  vm.createContext(context);
  vm.runInContext(script("apps/web/src/pages/TopicsPage.vue") +
    "\nglobalThis.probe={refresh,openTopic,startForm,save,logout,selected,topics,evaluations,evaluation,editTarget,form,error,authenticated,loading,fail,busy};", context);
  return { ...context.probe, httpError: status => new TestEngineError(status), unmount: () => callbacks.forEach(fn => fn()) };
}
async function main() {
  const oldList = deferred(), calls = [];
  const editing = page(async (url, options) => {
    calls.push([url, options]);
    if (url === "/session") return { enabled: true, authenticated: true };
    if (url === "/topics?limit=50") return oldList.promise;
    if (url === "/topics/templates") return { items: [] };
    if (url === "/topics/A" && options?.method === "PUT") return topic("A", 5);
    if (url === "/topics/A") return topic("A", 5);
    if (url === "/topics/A/evaluations?limit=20") return { items: [] };
    throw new Error("Unexpected request " + url);
  });
  editing.selected.value = topic("A", 4);
  const refresh = editing.refresh();
  await Promise.resolve(); await Promise.resolve();
  editing.startForm("edit");
  oldList.resolve({ items: [topic("B")], unavailable_count: 0 });
  await refresh;
  assert.equal(editing.editTarget.value.id, "A");
  assert.equal(editing.selected.value.id, "A");
  await editing.save({ is_demo: true, definition: topic("A").definition });
  const update = calls.find(([url, options]) => url === "/topics/A" && options?.method === "PUT");
  assert.equal(JSON.parse(update[1].body).expected_revision, 4);
  assert.equal(calls.filter(([url]) => url === "/topics").length, 0);

  editing.startForm("edit"); editing.editTarget.value = null;
  const previous = calls.length;
  await editing.save({ is_demo: true, definition: topic("A").definition });
  assert.equal(calls.length, previous);
  assert.match(editing.error.value, /失效/);
  editing.unmount();

  const oldTopic = deferred(), newTopic = deferred();
  const switching = page(url => {
    if (url.endsWith("/evaluations?limit=20")) return Promise.resolve({ items: [] });
    if (url === "/topics/A") return oldTopic.promise;
    if (url === "/topics/B") return newTopic.promise;
    throw new Error("Unexpected request");
  });
  const openA = switching.openTopic("A"), openB = switching.openTopic("B");
  newTopic.resolve(topic("B")); await openB;
  oldTopic.resolve(topic("A")); await openA;
  assert.equal(switching.selected.value.id, "B");
  switching.unmount();

  const late = deferred();
  const leaving = page(url => {
    if (url === "/session") return Promise.resolve(null);
    if (url.endsWith("/evaluations?limit=20")) return Promise.resolve({ items: [] });
    return late.promise;
  });
  const opening = leaving.openTopic("A");
  await leaving.logout();
  late.resolve(topic("A")); await opening;
  assert.equal(leaving.selected.value, null);
  assert.equal(leaving.authenticated.value, false);
  leaving.unmount();

  const forbiddenRead = deferred();
  const denied = page(url => url.endsWith("/evaluations?limit=20")
    ? Promise.resolve({ items: [] }) : forbiddenRead.promise);
  denied.authenticated.value = true;
  denied.topics.value = [topic("private")];
  denied.evaluation.value = { draft: "SYNTHETIC restricted draft" };
  const deniedPending = denied.openTopic("private");
  denied.fail(denied.httpError(403));
  forbiddenRead.resolve(topic("private")); await deniedPending;
  assert.equal(denied.authenticated.value, true);
  assert.equal(denied.selected.value, null);
  assert.equal(denied.evaluation.value, null);
  assert.equal(denied.topics.value.length, 0);
  denied.unmount();

  const pending = deferred(), callbacks = [], downloads = [];
  const context = { exports: {}, console, Blob, setTimeout: () => 1,
    URL: { createObjectURL: () => { downloads.push("created"); return "blob:test"; }, revokeObjectURL() {} },
    document: { createElement: () => ({ click: () => downloads.push("clicked") }) },
    defineProps: () => ({ topicId: "A", isDemo: true, evaluation: { id: "e1", as_of: "2026-01-01T00:00:00Z" } }),
    defineEmits: () => () => {},
    require(name) {
      if (name === "vue") return { ...Vue, onUnmounted: fn => callbacks.push(fn) };
      if (name.endsWith("/api/engine")) return { engineRequest: () => pending.promise };
      return {};
    },
  };
  vm.createContext(context);
  vm.runInContext(script("apps/web/src/components/engine/TopicEvaluationDetail.vue") + "\nglobalThis.probe={download};", context);
  const exportPending = context.probe.download();
  callbacks.forEach(fn => fn());
  pending.resolve({ draft: "SYNTHETIC old topic" }); await exportPending;
  assert.equal(downloads.length, 0);
  console.log(JSON.stringify({ checks: [
    "refresh_does_not_replace_open_edit_target",
    "missing_edit_target_never_creates_another_topic",
    "late_topic_read_cannot_replace_current_selection",
    "logout_drops_pending_private_content",
    "unmounted_export_cannot_download_old_topic",
    "permission_denial_removes_cached_draft_and_rejects_late_topic_read"
  ], status: "passed" }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
