// Exercise the actual component with deliberately late API responses.
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "..");
const ts = require(path.join(root, "apps/web/node_modules/typescript"));
const Vue = require(path.join(root, "apps/web/node_modules/vue"));
const source = fs.readFileSync(path.join(root, "apps/web/src/components/engine/MachineAnalysisPanel.vue"), "utf8");
const compiled = ts.transpileModule(source.slice(source.indexOf(">") + 1, source.indexOf("</script>")),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function summary(id, state = "completed") {
  return { id, state, result_summary: "Synthetic test analysis", based_on_revision: 1 };
}
function listing(items, next_cursor = null) {
  return { enabled: true, configured: true, items, next_cursor, unavailable_count: 0 };
}
function panel(request) {
  class TestError extends Error { constructor(status) { super("Synthetic denial"); this.status = status; } }
  const callbacks = [], events = [], downloads = [];
  const props = { investigation: { id: "I", revision: 3, status: "open" }, busy: false, loading: false };
  const context = {
    exports: {}, console, Blob, setTimeout: () => 1, clearTimeout() {},
    crypto: { randomUUID: () => "synthetic-key" },
    URL: { createObjectURL: () => { downloads.push("created"); return "blob:synthetic"; }, revokeObjectURL() {} },
    document: { createElement: () => ({ click: () => downloads.push("clicked") }) },
    defineProps: () => props, defineEmits: () => (...args) => events.push(args),
    require(name) {
      if (name === "vue") return { ...Vue, watch() {}, onUnmounted: fn => callbacks.push(fn) };
      if (name.endsWith("/api/engine")) return { engineRequest: request, EngineError: TestError };
      return {};
    },
  };
  vm.createContext(context);
  vm.runInContext(compiled + "\nglobalThis.probe={refresh,openAnalysis,loadMore,startAnalysis,readSource,closeSource,exportAnalysis,clearPrivate,fail,list,selected,source,sourceOpen,error,writing,loading};", context);
  return { ...context.probe, props, events, downloads, errorFor: status => new TestError(status),
    unmount: () => callbacks.forEach(fn => fn()) };
}
async function main() {
  const a = deferred(), b = deferred();
  const switching = panel(url => url.endsWith("/A") ? a.promise : b.promise);
  const first = switching.openAnalysis("A", true), second = switching.openAnalysis("B", true);
  b.resolve(summary("B")); await second; a.resolve(summary("A")); await first;
  assert.equal(switching.selected.value.id, "B");
  switching.unmount();

  const oldSource = deferred();
  const reading = panel(url => url.includes("/evidence/") ? oldSource.promise : Promise.resolve(summary("B")));
  reading.selected.value = summary("A");
  const read = reading.readSource("E1"); await reading.openAnalysis("B", true);
  oldSource.resolve({ normalized_text: "Synthetic private A" }); await read;
  assert.equal(reading.source.value, null); assert.equal(reading.sourceOpen.value, false);
  reading.unmount();

  const pending = deferred();
  const denied = panel(() => pending.promise);
  denied.list.value = listing([summary("private")]); denied.selected.value = summary("private");
  const privateRead = denied.openAnalysis("private");
  denied.fail(denied.errorFor(403));
  pending.resolve(summary("private")); await privateRead;
  assert.equal(denied.selected.value, null); assert.equal(denied.list.value, null);
  assert.equal(denied.events[0][0], "unauthorized"); denied.unmount();

  const empty = panel(() => Promise.resolve(listing([])));
  empty.list.value = listing([summary("removed")]); empty.selected.value = summary("removed");
  await empty.refresh();
  assert.equal(empty.selected.value, null); empty.unmount();

  const oldPage = deferred();
  const paginated = panel(url => url.includes("?cursor=") ? oldPage.promise
    : url.endsWith("/analyses") ? Promise.resolve(listing([summary("new")]))
    : Promise.resolve(summary("new")));
  paginated.list.value = listing([summary("old")], "old-page");
  const pageRead = paginated.loadMore(); await paginated.refresh();
  oldPage.resolve(listing([summary("stale")], "stale-next")); await pageRead;
  assert.deepEqual(Array.from(paginated.list.value.items, item => item.id), ["new"]);
  assert.equal(paginated.list.value.next_cursor, null); paginated.unmount();

  const calls = [];
  const history = panel(url => {
    calls.push(url);
    return Promise.resolve(url.endsWith("/analyses")
      ? listing([summary("latest"), summary("old")]) : summary(url.split("/").at(-1)));
  });
  history.list.value = listing([summary("latest"), summary("old")]);
  await history.openAnalysis("old", true); await history.refresh();
  assert.equal(history.selected.value.id, "old"); history.unmount();

  const writing = deferred();
  const gone = panel(() => writing.promise);
  gone.list.value = listing([]);
  const write = gone.startAnalysis(); gone.unmount();
  writing.resolve(summary("late")); await write;
  assert.equal(gone.list.value, null); assert.equal(gone.selected.value, null);
  assert.equal(gone.events.length, 0);

  const download = deferred();
  const leaving = panel(() => download.promise);
  leaving.selected.value = summary("A");
  const exportRead = leaving.exportAnalysis(); leaving.unmount();
  download.resolve({ text: "Synthetic old analysis" }); await exportRead;
  assert.equal(leaving.downloads.length, 0);

  const writes = [];
  const retries = panel((url, options) => {
    if (options?.method === "POST") { writes.push(options); return Promise.reject(new Error("Synthetic lost response")); }
    return Promise.resolve(listing([]));
  });
  retries.list.value = listing([]);
  await retries.startAnalysis(); await retries.startAnalysis();
  assert.equal(writes.length, 2);
  assert.equal(writes[0].headers["Idempotency-Key"], writes[1].headers["Idempotency-Key"]);
  assert.equal(writes[0].body, writes[1].body); retries.unmount();

  console.log(JSON.stringify({ status: "passed", checks: [
    "late_analysis_cannot_replace_selection", "old_source_cannot_cross_analysis",
    "denial_clears_content_and_discards_late_read", "empty_authorized_list_clears_cached_analysis",
    "old_pagination_cannot_overwrite_refreshed_list", "refresh_preserves_chosen_history",
    "late_create_after_unmount_cannot_repopulate", "late_export_after_unmount_cannot_download",
    "lost_create_response_reuses_original_idempotency_key",
  ] }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
