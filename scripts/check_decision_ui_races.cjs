// Offline synthetic probes of the current decision Vue scripts; no DOM/server claim.
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const root = path.resolve(__dirname, "..");
const ts = require(path.join(root, "apps/web/node_modules/typescript"));
const Vue = require(path.join(root, "apps/web/node_modules/vue"));
const files = ["apps/web/src/pages/DecisionPage.vue", "apps/web/src/components/engine/DecisionResults.vue", "apps/web/src/api/decisionLabels.ts"];
const digest = value => createHash("sha256").update(value).digest("hex");
function script(relative) {
  const text = fs.readFileSync(path.join(root, relative), "utf8");
  return ts.transpileModule(text.slice(text.indexOf(">") + 1, text.indexOf("</script>")), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
}
function defer() {
  let resolve, reject;
  return { promise: new Promise((yes, no) => { resolve = yes; reject = no; }), resolve, reject };
}
function page(request) {
  const context = {
    exports: {}, console, setTimeout: () => 1, clearTimeout() {},
    require(name) {
      if (name === "vue") return { ref: value => ({ value }), computed: getter => ({ get value() { return getter(); } }), onMounted() {}, onUnmounted() {} };
      if (name.endsWith("/api/engine")) return { engineRequest: request, post: request, EngineError: class extends Error {} };
      return {};
    },
  };
  vm.createContext(context);
  vm.runInContext(script(files[0]) + "\nglobalThis.probe={refresh,openRun,logout,importDraft,calculate,selectedRun,runs,authenticated,snapshots,snapshotIds,draft,loggingOut,loading,busy,error};", context);
  return context.probe;
}
const fullRef = id => ({ provider: "fixture", object_type: "document", object_id: id, adopted_version: "v1", version_source: "provider", content_hash: "fixture-hash", authorization_ref: "fixture-grant" });

async function main() {
  const old = defer(), fresh = defer();
  const switching = page(url => url === "/decision-runs/B" ? old.promise : fresh.promise);
  switching.selectedRun.value = { id: "A", revision: 1 };
  const history = { id: "C", revision: 1, state: "queued" };
  switching.runs.value = [history];
  const openingB = switching.openRun("B");
  assert.equal(switching.selectedRun.value, null);
  const openingC = switching.openRun("C");
  fresh.resolve({ id: "C", revision: 3, state: "completed" });
  await openingC;
  old.resolve({ id: "B", revision: 2, state: "completed" });
  await openingB;
  assert.equal(switching.selectedRun.value.id, "C");
  assert.equal(history.state, "completed");

  const first = defer(), second = defer();
  let sequence = 0;
  const overlapping = page(() => ++sequence === 1 ? first.promise : second.promise);
  const staleRead = overlapping.openRun("D"), latestRead = overlapping.openRun("D");
  second.resolve({ id: "D", revision: 4, state: "completed" });
  await latestRead;
  first.resolve({ id: "D", revision: 3, state: "running" });
  await staleRead;
  assert.equal(overlapping.selectedRun.value.state, "completed");

  const session = defer(), deletion = defer();
  let calls = 0;
  const leaving = page((url, options) => { calls++; assert.equal(url, "/session"); return options?.method === "DELETE" ? deletion.promise : session.promise; });
  const refreshing = leaving.refresh(), logout = leaving.logout();
  await leaving.refresh();
  assert.equal(calls, 2);
  assert.equal(leaving.loggingOut.value, true);
  deletion.resolve(null);
  await logout;
  session.resolve({ enabled: true, authenticated: true });
  await refreshing;
  assert.equal(leaving.authenticated.value, false);
  assert.equal(leaving.snapshots.value.length, 0);

  const reading = defer();
  const importPage = page((url, options) => { assert.equal(url, "/session"); assert.equal(options.method, "DELETE"); return Promise.resolve(null); });
  const target = { files: [{ size: 12, text: () => reading.promise }], value: "fixture.json" };
  const importing = importPage.importDraft({ target });
  await importPage.logout();
  reading.resolve("{}");
  await importing;
  assert.equal(importPage.draft.value, null);
  assert.equal(target.value, "");

  const requests = [];
  const props = Vue.reactive({ run: { source_snapshots: [{ id: "source-A", snapshot_hash: "hash-A" }, { id: "source-B", snapshot_hash: "hash-B" }] } });
  const unmounted = [];
  const sourceContext = {
    exports: {}, console, defineProps: () => props,
    require(name) {
      if (name === "vue") return { ...Vue, onUnmounted: callback => unmounted.push(callback) };
      if (name.endsWith("/api/engine")) return { engineRequest: url => { const pending = defer(); requests.push({ url, pending }); return pending.promise; } };
      if (name.endsWith("decisionLabels")) return { decisionExplanation: value => value };
      return {};
    },
  };
  vm.createContext(sourceContext);
  vm.runInContext(script(files[1]) + "\nglobalThis.probe={showSource,closeSource,sourceOpen,sourceRecord,sourceIndicator,sourceError};", sourceContext);
  const detail = sourceContext.probe;
  const record = { record_ref: fullRef("B"), normalized_text: "SYNTHETIC prose", regional_indicators: [{ indicator: "SYNTHETIC metric", value: 7, unit: "CNY", period: "2026", population_scope: "TEST", region: "TEST", jurisdiction: "TEST" }] };
  const linkA = { snapshot_hash: "hash-A", record_ref: fullRef("A"), field_path: "/regional_indicators/0/value" };
  const linkB = { snapshot_hash: "hash-B", record_ref: fullRef("B"), field_path: "/regional_indicators/0/value" };
  const sourceA = detail.showSource(linkA), sourceB = detail.showSource(linkB);
  requests[1].pending.resolve({ snapshot: { records: [record] } });
  await sourceB;
  requests[0].pending.resolve({ snapshot: { records: [{ record_ref: fullRef("A") }] } });
  await sourceA;
  assert.equal(detail.sourceRecord.value.record_ref.object_id, "B");
  assert.equal(detail.sourceIndicator.value.value, 7);
  const closingRead = detail.showSource(linkB);
  detail.closeSource();
  requests[2].pending.resolve({ snapshot: { records: [record] } });
  await closingRead;
  assert.equal(detail.sourceOpen.value, false);
  assert.equal(detail.sourceRecord.value, null);
  const mismatched = detail.showSource({ ...linkB, record_ref: { ...fullRef("B"), authorization_ref: "wrong-grant" } });
  requests[3].pending.resolve({ snapshot: { records: [record] } });
  await mismatched;
  assert.equal(detail.sourceRecord.value, null);
  assert.ok(detail.sourceError.value);
  const disposingRead = detail.showSource(linkB);
  unmounted.forEach(callback => callback());
  requests[4].pending.resolve({ snapshot: { records: [record] } });
  await disposingRead;
  assert.equal(detail.sourceRecord.value, null);

  // A login can reveal the composer while refresh is still awaiting sources.
  // A fixed request must never be auto-matched against that empty source page.
  const sourcePage = defer();
  const sourceRequestStarted = defer();
  const posted = [];
  const importRequest = {
    input: {
      schema_version: "bioradar.decision.input.v1",
      brief: { title: "SYNTHETIC request", decision_question: "SYNTHETIC choice", audience: "central" },
      evidence_snapshot_hashes: ["known-hash"], outcomes: [], constraints: [],
    },
    policy: { policy_version: "bioradar.decision.policy.v1" },
  };
  const correctSource = { id: "known-source", snapshot_hash: "known-hash" };
  const delayedSources = page((url, body) => {
    if (url === "/session") return Promise.resolve({ enabled: true, authenticated: true });
    if (url === "/snapshots?limit=100") {
      sourceRequestStarted.resolve();
      return sourcePage.promise;
    }
    if (url === "/decision-runs" && body === undefined) return Promise.resolve({ items: [] });
    if (url === "/decision-runs" && body !== undefined) {
      posted.push(body);
      return Promise.resolve({ id: "new-run" });
    }
    if (url === "/decision-runs/new-run") return Promise.resolve({ id: "new-run", revision: 1, state: "completed" });
    throw new Error(`Unexpected probe request: ${url}`);
  });
  let fileReads = 0;
  const requestFile = { size: 123, text: async () => { fileReads++; return JSON.stringify(importRequest); } };
  const pendingSources = delayedSources.refresh();
  await sourceRequestStarted.promise;
  assert.equal(delayedSources.authenticated.value, true);
  assert.equal(delayedSources.loading.value, true);
  const blockedTarget = { files: [requestFile], value: "synthetic.json" };
  await delayedSources.importDraft({ target: blockedTarget });
  assert.equal(fileReads, 0);
  assert.equal(blockedTarget.value, "");
  assert.equal(delayedSources.draft.value, null);
  assert.match(delayedSources.error.value, /加载/);
  // Even a previously valid draft cannot submit during a refresh.
  delayedSources.draft.value = importRequest;
  delayedSources.snapshots.value = [correctSource];
  delayedSources.snapshotIds.value = [correctSource.id];
  await delayedSources.calculate();
  assert.equal(posted.length, 0);
  delayedSources.draft.value = null;
  delayedSources.snapshots.value = [];
  delayedSources.snapshotIds.value = [];
  sourcePage.resolve({ items: [correctSource] });
  await pendingSources;
  assert.equal(delayedSources.loading.value, false);
  const readyTarget = { files: [requestFile], value: "synthetic.json" };
  await delayedSources.importDraft({ target: readyTarget });
  assert.equal(fileReads, 1);
  assert.equal(delayedSources.draft.value.input.brief.title, "SYNTHETIC request");
  assert.equal(Array.from(delayedSources.snapshotIds.value).join(), correctSource.id);
  const wrongSource = { id: "wrong-source", snapshot_hash: "wrong-version-hash" };
  delayedSources.snapshots.value = [correctSource, wrongSource];
  for (const choice of [[], [wrongSource.id], ["not-loaded"], [correctSource.id, wrongSource.id]]) {
    delayedSources.snapshotIds.value = choice;
    await delayedSources.calculate();
    assert.equal(posted.length, 0);
    assert.match(delayedSources.error.value, /所选资料与任务声明的来源不一致/);
  }
  // A refresh updates the available list without overwriting manual selection.
  delayedSources.snapshotIds.value = [wrongSource.id];
  await delayedSources.refresh();
  assert.equal(Array.from(delayedSources.snapshotIds.value).join(), wrongSource.id);
  await delayedSources.calculate();
  assert.equal(posted.length, 0);
  delayedSources.snapshotIds.value = [correctSource.id];
  await delayedSources.calculate();
  assert.equal(posted.length, 1);
  assert.equal(Array.from(posted[0].snapshot_ids).join(), correctSource.id);

  console.log(JSON.stringify({
    probe_format: "bioradar.decision-ui-race-probe.v1", captured_at: new Date().toISOString(),
    synthetic_only: true, network: "disabled", repository: "read-only",
    scope: "Current Vue scripts with controlled Promise delays; no DOM/browser/server claim",
    checks: {
      different_run_stale_response_dropped_and_history_synced: "passed",
      same_run_older_revision_cannot_overwrite_completed: "passed",
      logout_blocks_new_refresh_and_drops_old_success: "passed",
      logout_drops_pending_local_file_import: "passed",
      source_order_close_unmount_and_fullref_scope: "passed",
      actual_typed_source_value_available: "passed",
      source_refresh_blocks_import_then_matches_and_submit_requires_exact_hash_set: "passed",
    },
    source_sha256: Object.fromEntries(files.map(file => [file, digest(fs.readFileSync(path.join(root, file)))])),
    node_version: process.version, typescript_version: ts.version, vue_version: Vue.version,
  }, null, 2));
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
