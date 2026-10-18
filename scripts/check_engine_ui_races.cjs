// Offline probes against the current Vue scripts. No HTTP, credentials, or DB writes.
// Run from the repository in the existing Node 22 image, with a read-only mount:
// docker run --rm --network none -v "$PWD:/app:ro" -w /app node:22-bookworm-slim \
//   node /app/scripts/check_engine_ui_races.cjs

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const root = path.resolve(__dirname, "..");
const ts = require(path.join(root, "apps/web/node_modules/typescript"));
const Vue = require(path.join(root, "apps/web/node_modules/vue"));
const files = [
  "apps/web/src/pages/RadarPage.vue",
  "apps/web/src/components/engine/CandidateDetail.vue",
  "apps/web/src/api/engineLabels.ts",
  "apps/web/src/pages/InvestigationsPage.vue",
  "apps/web/src/components/engine/SnapshotComparison.vue",
  "apps/web/src/components/engine/InvestigationEntryForm.vue",
  "apps/web/src/components/engine/InvestigationRecord.vue",
  "apps/web/src/components/engine/GraphInvestigationForm.vue",
];
function digest(value) {
  return createHash("sha256").update(value).digest("hex");
}
function source(relative) {
  const value = fs.readFileSync(path.join(root, relative), "utf8");
  return ts.transpileModule(
    value.slice(value.indexOf(">") + 1, value.indexOf("</script>")),
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText;
}
function defer() {
  let resolve;
  let reject;
  return {
    promise: new Promise((yes, no) => {
      resolve = yes;
      reject = no;
    }),
    resolve,
    reject,
  };
}
function page(request) {
  const context = {
    exports: {},
    require(name) {
      if (name === "vue") {
        return {
          ref: (value) => ({ value }),
          computed: (getter) => ({
            get value() {
              return getter();
            },
          }),
          onMounted() {},
          onUnmounted() {},
        };
      }
      if (name.endsWith("/api/engine")) {
        return {
          engineRequest: request,
          post: request,
          EngineError: class extends Error {},
        };
      }
      if (name.endsWith("engineLabels")) return { explainCode: (x) => x };
      return {};
    },
    setTimeout: () => 1,
    clearTimeout() {},
    console,
  };
  vm.createContext(context);
  vm.runInContext(
    source(files[0]) +
      "\nglobalThis.probe={refresh,logout,openRun,rememberFeedback," +
      "selectedRun,selectedCandidate,authenticated,snapshots,runs,loggingOut};",
    context,
  );
  return context.probe;
}
function investigationPage(request) {
  const route = Vue.reactive({ query: {} });
  const unmounted = [];
  const printed = [];
  const context = {
    exports: {},
    console,
    window: { print: () => printed.push("printed") },
    require(name) {
      if (name === "vue")
        return {
          ...Vue,
          onMounted() {},
          onUnmounted: (callback) => unmounted.push(callback),
        };
      if (name === "vue-router")
        return {
          useRoute: () => route,
          useRouter: () => ({
            replace: async (value) => {
              route.query = value.query;
            },
          }),
        };
      if (name.endsWith("/api/engine"))
        return {
          engineRequest: request,
          post: request,
          EngineError: class extends Error {},
        };
      return {};
    },
  };
  vm.createContext(context);
  vm.runInContext(
    source(files[3]) +
      "\nglobalThis.probe={refresh,openInvestigation,logout,readSupport,closeSource,printRecord,appendEntry,selected,authenticated,sourceRecord,sourceOpen,message,error};",
    context,
  );
  return {
    ...context.probe,
    unmount: () => unmounted.forEach((callback) => callback()),
    printed,
  };
}
function entryForm(request, investigation, save) {
  const props = Vue.reactive({
    investigation,
    save,
    busy: false,
    loading: false,
    snapshots: [],
    sourceCursor: null,
  });
  const unmounted = [];
  const context = {
    exports: {},
    console,
    defineProps: () => props,
    defineEmits: () => () => {},
    require(name) {
      if (name === "vue")
        return { ...Vue, onUnmounted: (callback) => unmounted.push(callback) };
      if (name.endsWith("/api/engine")) return { engineRequest: request };
      return {};
    },
  };
  vm.createContext(context);
  vm.runInContext(
    source(files[5]) +
      "\nglobalThis.probe={addJudgment,addNote,materialSnapshotId,materialSnapshot,judgmentText,conditions,note};",
    context,
  );
  return {
    ...context.probe,
    unmount: () => unmounted.forEach((callback) => callback()),
  };
}

function graphForm(request, investigation) {
  const props = Vue.reactive({ investigation, loading: false });
  const emitted = [];
  const context = {
    exports: {}, console,
    defineProps: () => props,
    defineEmits: () => (...args) => emitted.push(args),
    require(name) {
      if (name === "vue") return { ...Vue, onMounted() {}, onUnmounted() {} };
      if (name.endsWith("/api/engine")) return {
        engineRequest: request, post: request, EngineError: class extends Error {},
      };
      return {};
    },
  };
  vm.createContext(context);
  vm.runInContext(
    source(files[7]) +
      "\nglobalThis.probe={result,selectedCandidate,packages,packageKey,appendCurrent,seedId,submit,running,error};",
    context,
  );
  return { ...context.probe, props, emitted };
}
async function graphChecks() {
  const writes = [];
  const page = graphForm(async (_url, body) => {
    writes.push(body);
    return { completion_status: "bounded_context_saved", investigation: investigation(body.investigation_id, 8) };
  }, investigation("A", 4));
  page.packages.value = [{
    source_id: "reviewed_graph", source_record_id: "package", version_id: "a".repeat(64),
    is_demo: true, title: "SYNTHETIC", namespace: "synthetic", graph_kind: "reviewed",
    scope_description: "SYNTHETIC", selection_truncated: false,
  }];
  page.packageKey.value = JSON.stringify(["package", "a".repeat(64)]);
  await Vue.nextTick();
  page.result.value = { completion_status: "awaiting_seed_selection",
    investigation: investigation("A", 4), seed_resolutions: [] };
  page.selectedCandidate.value = "synthetic|old";
  page.props.investigation = investigation("B", 7);
  await Vue.nextTick();
  assert.equal(page.result.value, null);
  assert.equal(page.selectedCandidate.value, "");
  page.appendCurrent.value = true;
  page.seedId.value = "synthetic|explicit";
  await page.submit();
  assert.equal(writes[0].investigation_id, "B");
  assert.equal(writes[0].expected_revision, 7);

  const delayed = defer();
  const pending = graphForm(() => delayed.promise, investigation("A", 4));
  pending.packages.value = page.packages.value;
  pending.packageKey.value = page.packageKey.value;
  pending.seedId.value = "synthetic|explicit";
  pending.appendCurrent.value = true;
  await Vue.nextTick();
  const saving = pending.submit();
  pending.props.investigation = investigation("C", 2);
  await Vue.nextTick();
  delayed.resolve({ completion_status: "bounded_context_saved", investigation: investigation("A", 5) });
  await saving;
  assert.equal(pending.emitted.filter((event) => event[0] === "saved").length, 0);
  assert.equal(pending.result.value, null);
}

function readEntry(page, entry) {
  return page.readSupport(
    entry.payload.snapshot_id,
    entry.payload.snapshot_hash,
    entry.payload.support,
  );
}
function refFor(id) {
  return {
    provider: "fixture",
    object_type: "document",
    object_id: id,
    adopted_version: "v1",
    version_source: "provider",
    content_hash: "synthetic-hash",
    authorization_ref: "synthetic-grant",
  };
}
function materialEntry(id, sourceId, stance) {
  return {
    id,
    kind: "material",
    revision: 2,
    created_by: "SYNTHETIC actor",
    created_at: "2026-10-14T00:00:00Z",
    payload: {
      kind: "material",
      text: "SYNTHETIC material",
      stance,
      snapshot_id: sourceId,
      snapshot_hash: "hash-" + sourceId,
      support: { record_ref: refFor(sourceId), quote: "SYNTHETIC text" },
    },
  };
}
function investigation(id, revision = 1, entries = []) {
  return {
    id,
    title: "SYNTHETIC " + id,
    question: "SYNTHETIC question",
    status: "open",
    is_demo: true,
    revision,
    entries,
    questions: [],
    origin: null,
    latest_judgment_entry_id: null,
    updated_at: "2026-10-14T00:00:00Z",
    needs_review: false,
  };
}
async function investigationChecks() {
  const oldRead = defer(),
    freshRead = defer(),
    oldPackage = defer();
  const switched = investigationPage((url) => {
    if (url === "/investigations/A") return oldRead.promise;
    if (url === "/investigations/B") return freshRead.promise;
    if (url === "/snapshots/old-source") return oldPackage.promise;
    throw new Error("Unexpected synthetic URL");
  });
  const openingA = switched.openInvestigation("A");
  const formerForm = entryForm(
    () => oldPackage.promise,
    investigation("A"),
    async () => false,
  );
  formerForm.materialSnapshotId.value = "old-source";
  const openingB = switched.openInvestigation("B");
  formerForm.unmount();
  freshRead.resolve(investigation("B", 3));
  await openingB;
  oldRead.resolve(investigation("A", 2));
  oldPackage.resolve({
    snapshot: { records: [{ record_ref: refFor("old") }] },
  });
  await openingA;
  await Vue.nextTick();
  assert.equal(switched.selected.value.id, "B");
  assert.equal(formerForm.materialSnapshot.value, null);
  switched.unmount();

  const reads = [];
  const reading = investigationPage((url) => {
    if (url === "/investigations/B") return Promise.resolve(investigation("B"));
    const pending = defer();
    reads.push(pending);
    return pending.promise;
  });
  await reading.openInvestigation("B");
  const sourceA = readEntry(
    reading,
    materialEntry("material-A", "A", "support"),
  );
  const sourceB = readEntry(
    reading,
    materialEntry("material-B", "B", "oppose"),
  );
  reads[1].resolve({
    snapshot_hash: "hash-B",
    snapshot: {
      records: [{ record_ref: refFor("B"), normalized_text: "SYNTHETIC B" }],
    },
  });
  await sourceB;
  reads[0].resolve({
    snapshot_hash: "hash-A",
    snapshot: {
      records: [{ record_ref: refFor("A"), normalized_text: "SYNTHETIC A" }],
    },
  });
  await sourceA;
  assert.equal(reading.sourceRecord.value.normalized_text, "SYNTHETIC B");
  const closing = readEntry(
    reading,
    materialEntry("material-B", "B", "oppose"),
  );
  reading.closeSource();
  reads[2].resolve({
    snapshot_hash: "hash-B",
    snapshot: { records: [{ record_ref: refFor("B") }] },
  });
  await closing;
  assert.equal(reading.sourceRecord.value, null);
  const wrongRef = readEntry(
    reading,
    materialEntry("material-B", "B", "oppose"),
  );
  reads[3].resolve({
    snapshot_hash: "hash-B",
    snapshot: {
      records: [
        { record_ref: { ...refFor("B"), authorization_ref: "wrong-grant" } },
      ],
    },
  });
  await wrongRef;
  assert.equal(reading.sourceRecord.value, null);
  reading.unmount();

  const support = materialEntry("support-entry", "support", "support");
  const opposing = materialEntry("oppose-entry", "opposing", "oppose");
  const priorJudgment = {
    id: "first-judgment",
    kind: "judgment",
    payload: {
      kind: "judgment",
      text: "SYNTHETIC old judgment",
      basis_entry_ids: [support.id],
    },
  };
  const entries = [support, priorJudgment, opposing];
  const priorHash = digest(JSON.stringify(priorJudgment));
  const submitted = [];
  const judge = investigationPage((url, body) => {
    if (url === "/investigations/J")
      return Promise.resolve({
        ...investigation("J", 4, entries),
        latest_judgment_entry_id: priorJudgment.id,
        needs_review: true,
      });
    assert.equal(url, "/investigations/J/entries");
    submitted.push(body);
    return Promise.resolve({
      ...investigation("J", 5, [
        ...entries,
        { id: "new-judgment", kind: "judgment", payload: body },
      ]),
      latest_judgment_entry_id: "new-judgment",
    });
  });
  await judge.openInvestigation("J");
  const judgingForm = entryForm(
    () => {
      throw new Error("No source read expected");
    },
    judge.selected.value,
    (body) => judge.appendEntry(body),
  );
  judgingForm.judgmentText.value = "SYNTHETIC unable to determine";
  judgingForm.conditions.value = "SYNTHETIC check both findings";
  await judgingForm.addJudgment();
  assert.equal(submitted.length, 1);
  assert.equal(
    Array.from(submitted[0].basis_entry_ids).join(),
    "support-entry,oppose-entry",
  );
  assert.equal(submitted[0].expected_revision, 4);
  assert.equal(submitted[0].assessment, "unable_to_determine");
  assert.equal(judge.selected.value.entries.length, 4);
  assert.equal(digest(JSON.stringify(priorJudgment)), priorHash);
  judgingForm.unmount();
  judge.unmount();

  const pendingWrite = defer(),
    deletion = defer();
  const leaving = investigationPage((url, options) => {
    if (url === "/investigations/L") return Promise.resolve(investigation("L"));
    if (url === "/investigations/L/entries") return pendingWrite.promise;
    assert.equal(url, "/session");
    assert.equal(options.method, "DELETE");
    return deletion.promise;
  });
  await leaving.openInvestigation("L");
  const leavingForm = entryForm(
    () => {
      throw new Error("No source read expected");
    },
    leaving.selected.value,
    (body) => leaving.appendEntry(body),
  );
  leavingForm.note.value = "SYNTHETIC note";
  const saving = leavingForm.addNote(),
    logout = leaving.logout();
  leavingForm.unmount();
  deletion.resolve(null);
  await logout;
  pendingWrite.resolve(investigation("L", 2));
  await saving;
  assert.equal(leaving.selected.value, null);
  assert.equal(leaving.message.value, "");
  leaving.unmount();
  const printRead = defer();
  let readsForPrint = 0;
  const printer = investigationPage((url) =>
    url === "/session"
      ? Promise.resolve(null)
      : ++readsForPrint === 1
        ? Promise.resolve(investigation("P"))
        : printRead.promise,
  );
  await printer.openInvestigation("P");
  const printing = printer.printRecord();
  await printer.logout();
  printRead.resolve(investigation("P", 2));
  await printing;
  assert.equal(printer.printed.length, 0);
  printer.unmount();

  // Returning to the same ID must not revive requests from the earlier visit.
  const delayedSave = defer();
  let saveReads = 0;
  const revisiting = investigationPage((url) => {
    if (url === "/investigations/A")
      return Promise.resolve(investigation("A", ++saveReads === 1 ? 1 : 3));
    if (url === "/investigations/B") return Promise.resolve(investigation("B"));
    assert.equal(url, "/investigations/A/entries");
    return delayedSave.promise;
  });
  await revisiting.openInvestigation("A");
  const oldSave = revisiting.appendEntry({
    expected_revision: 1,
    kind: "note",
    text: "SYNTHETIC earlier visit",
  });
  await revisiting.openInvestigation("B");
  await revisiting.openInvestigation("A");
  delayedSave.resolve(investigation("A", 2));
  await oldSave;
  assert.equal(revisiting.selected.value.revision, 3);
  revisiting.unmount();

  for (const outcome of ["success", "failure"]) {
    const delayedPrint = defer();
    let printReads = 0;
    const returning = investigationPage((url) => {
      if (url === "/investigations/B")
        return Promise.resolve(investigation("B"));
      assert.equal(url, "/investigations/A");
      if (++printReads === 2) return delayedPrint.promise;
      return Promise.resolve(investigation("A", printReads === 1 ? 1 : 3));
    });
    await returning.openInvestigation("A");
    const oldPrint = returning.printRecord();
    await returning.openInvestigation("B");
    await returning.openInvestigation("A");
    if (outcome === "success") delayedPrint.resolve(investigation("A", 2));
    else delayedPrint.reject(new Error("SYNTHETIC canceled print read failed"));
    await oldPrint;
    assert.equal(returning.selected.value.revision, 3);
    assert.equal(returning.printed.length, 0);
    assert.equal(returning.error.value, "");
    returning.unmount();
  }

  const comparisons = [],
    comparisonUnmount = [];
  const comparisonProps = Vue.reactive({
    snapshots: ["A", "B", "C"].map((id) => ({
      id,
      snapshot_hash: "hash-" + id,
      source_instance: "fixture",
      is_demo: true,
    })),
  });
  const comparisonContext = {
    exports: {},
    console,
    defineProps: () => comparisonProps,
    require(name) {
      if (name === "vue")
        return {
          ...Vue,
          onUnmounted: (callback) => comparisonUnmount.push(callback),
        };
      if (name.endsWith("/api/engine"))
        return {
          engineRequest: () => {
            const pending = defer();
            comparisons.push(pending);
            return pending.promise;
          },
        };
      return {};
    },
  };
  vm.createContext(comparisonContext);
  vm.runInContext(
    source(files[4]) + "\nglobalThis.probe={beforeId,afterId,compare,result};",
    comparisonContext,
  );
  const comparison = comparisonContext.probe;
  comparison.beforeId.value = "A";
  comparison.afterId.value = "B";
  const olderComparison = comparison.compare();
  comparison.afterId.value = "C";
  const newerComparison = comparison.compare();
  comparisons[1].resolve({ before_hash: "hash-A", after_hash: "hash-C" });
  await newerComparison;
  comparisons[0].resolve({ before_hash: "hash-A", after_hash: "hash-B" });
  await olderComparison;
  assert.equal(comparison.result.value.after_hash, "hash-C");
  const unmountedRead = comparison.compare();
  comparisonUnmount.forEach((callback) => callback());
  comparisons[2].resolve({
    before_hash: "hash-A",
    after_hash: "hash-C",
    marker: "late",
  });
  await unmountedRead;
  assert.equal(comparison.result.value, null);
}

async function main() {
  const pendingRun = defer();
  const candidateA = { candidate_key: "same-group", title: "SYNTHETIC A" };
  const current = page((url) => {
    assert.equal(url, "/runs/B");
    return pendingRun.promise;
  });
  current.selectedRun.value = { id: "A", state: "completed", feedback: [] };
  const history = { id: "B", state: "running", revision: 1, attempt: 1 };
  current.runs.value = [history];
  const opening = current.openRun("B");
  assert.equal(current.selectedRun.value, null);
  // Simulate an old click queued before the new run was adopted.
  current.selectedCandidate.value = candidateA;
  pendingRun.resolve({
    id: "B",
    state: "completed",
    revision: 3,
    attempt: 2,
    completed_at: "2026-10-14T00:00:00Z",
    candidate_count: 1,
    feedback: [],
    result: { result_hash: "SYNTHETIC hash-B", candidates: [] },
  });
  await opening;
  assert.equal(current.selectedCandidate.value, null);
  assert.equal(history.state, "completed");
  assert.equal(history.revision, 3);
  assert.equal(history.attempt, 2);
  assert.equal(history.candidate_count, 1);

  const before = digest(JSON.stringify(current.selectedRun.value.result));
  current.rememberFeedback({ id: "saved-A", run_id: "A" });
  assert.equal(current.selectedRun.value.feedback.length, 0);
  current.rememberFeedback({ id: "saved-B", run_id: "B" });
  current.rememberFeedback({ id: "saved-B", run_id: "B" });
  assert.equal(current.selectedRun.value.feedback.length, 1);
  assert.equal(
    digest(JSON.stringify(current.selectedRun.value.result)),
    before,
  );

  const session = defer();
  const deletion = defer();
  let calls = 0;
  const leavingPage = page((url, options) => {
    calls++;
    assert.equal(url, "/session");
    return options?.method === "DELETE" ? deletion.promise : session.promise;
  });
  const refreshing = leavingPage.refresh();
  const leaving = leavingPage.logout();
  await leavingPage.refresh();
  assert.equal(calls, 2); // Pending refresh plus DELETE; no new request during logout.
  assert.equal(leavingPage.authenticated.value, false);
  assert.equal(leavingPage.loggingOut.value, true);
  deletion.resolve(null);
  await leaving;
  session.resolve({ enabled: true, authenticated: true });
  await refreshing;
  assert.equal(leavingPage.authenticated.value, false);
  assert.equal(leavingPage.snapshots.value.length, 0);
  assert.equal(leavingPage.runs.value.length, 0);

  const props = Vue.reactive({
    candidate: null,
    runId: "A",
    resultHash: "hash-A",
    snapshotId: "snapshot-A",
    feedback: [],
  });
  const oldSnapshot = defer();
  const newSnapshot = defer();
  const oldFeedback = defer();
  const emitted = [];
  const created = defer();
  const creationBodies = [];
  const detailContext = {
    exports: {},
    defineProps: () => props,
    defineEmits:
      () =>
      (...args) =>
        emitted.push(args),
    require(name) {
      if (name === "vue") return Vue;
      if (name.endsWith("/api/engine")) {
        return {
          engineRequest: (url) =>
            url === "/snapshots/snapshot-A"
              ? oldSnapshot.promise
              : newSnapshot.promise,
          post: (url, body) => {
            if (url === "/investigations") {
              creationBodies.push(body);
              return created.promise;
            }
            return oldFeedback.promise;
          },
        };
      }
      if (name.endsWith("engineLabels")) return { explainCode: (x) => x };
      return {};
    },
    console,
  };
  vm.createContext(detailContext);
  vm.runInContext(
    source(files[1]) +
      "\nglobalThis.probe={feedback,saveInvestigation,investigationId,snapshot,savedFeedback,message};",
    detailContext,
  );
  props.candidate = { candidate_key: "same-group", supports: [] };
  await Vue.nextTick();
  const saving = detailContext.probe.feedback("needs_check");
  // Keep the same candidate object, but change all immutable run context.
  props.runId = "B";
  props.snapshotId = "snapshot-B";
  props.resultHash = "hash-B";
  await Vue.nextTick();
  newSnapshot.resolve({ snapshot: { snapshot_id: "snapshot-B", records: [] } });
  await Promise.resolve();
  oldSnapshot.resolve({ snapshot: { snapshot_id: "snapshot-A", records: [] } });
  oldFeedback.resolve({
    id: "saved-A",
    run_id: "A",
    candidate_key: "same-group",
  });
  await saving;
  await Promise.resolve();
  assert.equal(detailContext.probe.snapshot.value.snapshot_id, "snapshot-B");
  assert.equal(detailContext.probe.savedFeedback.value.length, 0);
  assert.equal(detailContext.probe.message.value, "");
  assert.equal(emitted.length, 1); // Parent handles run scoping; old response is still real.
  assert.equal(emitted[0][0], "feedbackSaved");
  assert.equal(emitted[0][1].run_id, "A");

  props.candidate = {
    candidate_key: "same-group",
    title: "SYNTHETIC B",
    next_questions: ["SYNTHETIC question"],
    supports: [],
  };
  await Vue.nextTick();
  const creating = detailContext.probe.saveInvestigation();
  assert.equal(creationBodies[0].origin.run_id, "B");
  assert.equal(creationBodies[0].origin.result_hash, "hash-B");
  props.runId = "C";
  props.resultHash = "hash-C";
  await Vue.nextTick();
  created.resolve({ id: "investigation-B" });
  await creating;
  assert.equal(detailContext.probe.investigationId.value, "");
  await investigationChecks();
  await graphChecks();

  console.log(
    JSON.stringify(
      {
        probe_format: "bioradar.ui-race-probe.v1",
        captured_at: new Date().toISOString(),
        synthetic_only: true,
        network: "disabled",
        repository: "read-only",
        scope:
          "Current Vue scripts with controlled Promise delays; no DOM/browser/server claim",
        checks: {
          run_change_clears_old_candidate_and_updates_history: "passed",
          logout_drops_stale_refresh_and_blocks_new_refresh: "passed",
          feedback_parent_run_scope_dedup_and_result_unchanged: "passed",
          detail_context_switch_discards_old_snapshot_and_feedback: "passed",
          candidate_investigation_creation_binds_original_run_and_drops_stale_link:
            "passed",
          investigation_switch_and_return_preserve_newer_revision_and_material_context:
            "passed",
          investigation_source_order_close_and_fullref_match: "passed",
          judgment_includes_opposing_material_and_preserves_old_revision:
            "passed",
          logout_and_revisit_discard_stale_print_success_and_failure: "passed",
          comparison_switch_and_unmount_discard_old_response: "passed",
          graph_seed_pending_switch_and_late_save_are_bound_to_current_investigation: "passed",
        },
        source_sha256: Object.fromEntries(
          files.map((file) => [
            file,
            digest(fs.readFileSync(path.join(root, file))),
          ]),
        ),
        node_version: process.version,
        typescript_version: ts.version,
        vue_version: Vue.version,
      },
      null,
      2,
    ),
  );
}
main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
