// Replay saved, real BioCheck audit results through the actual Vue report renderer.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { renderReport } = require("./check_research_ui.cjs");

async function main() {
  const upstream = process.env.BIORADAR_TEST_BIOCHECK_DIR ||
    path.resolve(__dirname, "../../BioCheck");
  const saved = JSON.parse(fs.readFileSync(path.join(upstream, "evaluation/selected_support_results.json"), "utf8"));
  const original = JSON.parse(fs.readFileSync(path.join(upstream, "evaluation/selected_support_case.json"), "utf8"));
  const results = saved.cases;
  const escaped = text => text.replace(/[&<>"']/g, value => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[value]));
  const expectedDocument = '<div class="report-document">' + escaped(original.draft) + "</div>";
  const before = JSON.stringify(saved);
  const review = results.find(item => item.name === "original_selection").audit;
  const control = results.find(item => item.name === "manual_s2_control").audit;
  assert.equal(review.status, "REVIEW");
  assert.equal(control.status, "PASS");
  assert.equal(review.checks[0].verdict, "not_supported");
  assert.equal(review.checks[1].verdict, "supported");

  for (const audit of [review, control]) {
    const html = await renderReport(saved.original_status, "passed", [], {
      final_text: original.draft, support_audit: audit,
    });
    assert.ok(html.includes(expectedDocument), "Delivered report text must remain exactly unchanged");
    assert.match(html, /BioCheck 原核验通过/);
    assert.match(html, /所选支持片段复核/);
    assert.ok(html.includes(escaped(audit.scope)));
    assert.ok(html.includes(escaped(audit.checks[0].reason)));
    assert.ok(html.includes(escaped(audit.checks[1].reason)));
    assert.match(html, /q78a8fc419026 · S13/);
    assert.doesNotMatch(html, /本版已通过所配置服务的核验/);
    if (audit.status === "REVIEW") {
      assert.match(html, /所选支持片段待核/);
      assert.match(html, /所选片段不支持/);
    } else {
      assert.match(html, /所列片段支持复核通过/);
      assert.match(html, /qef6ac51e879c · S2/);
    }
  }

  const notRun = await renderReport("PASS", "passed", [], {
    support_audit: { status: "NOT_RUN", not_run_reason: "本轮预算不足，未执行片段复核。" },
  });
  assert.match(notRun, /所选片段复核未执行/);
  assert.match(notRun, /本轮预算不足，未执行片段复核。/);
  assert.doesNotMatch(notRun, /所列片段支持复核通过/);
  const old = await renderReport("PASS");
  assert.match(old, /本版未保存所选片段复核结果/);
  const malformed = await renderReport("PASS", "passed", [], {
    support_audit: {
      status: "REVIEW", not_run_reason: "已收到片段复核响应，但格式无效。",
      verification_errors: [{ reason: "S999不存在" }],
    },
  });
  assert.match(malformed, /定位或回执仍有问题/);
  assert.match(malformed, /已收到片段复核响应，但格式无效。/);
  assert.match(malformed, /S999不存在/);
  assert.doesNotMatch(malformed, /所列片段支持复核通过/);
  assert.equal(JSON.stringify(saved), before);
  console.log(JSON.stringify({ status: "passed", checks: [
    "saved_real_wrong_segment_review_keeps_original_pass_and_exact_text",
    "saved_real_multi_segment_support_remains_supported",
    "manual_s2_control_pass_is_limited_to_selected_claims",
    "not_run_and_old_records_do_not_claim_full_report_support",
    "invalid_audit_receipt_stays_visible_and_unapproved",
  ] }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
