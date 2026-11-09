"""The two chain-1 defects that may not be waived, shown by fault injection, no model (fixed: see
docs/decisions/0005-publication-gate-and-own-source-revocation.md); CHAIN1_DEFECT_OUTPUT (optional) receives
what actually happened, as evidence.
1. verification mark: a report whose check did not run is marked 待核 (pending_verification) and is delivered
   with that mark, never as a finished report;
2. own-source revocation: evidence of a source revoked after it was read never goes out again."""

import json
import os

from conftest import FakeFabric
from fabric_receipts import sources_receipt
from sqlalchemy import select
from test_access import fabric_sources  # noqa: F401 - the fixture a test here takes
from test_pipeline import RECORDS, fake_model, first_policy

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.adapters.fabric import FabricError
from bioradar.config import settings
from bioradar.research import analyses
from bioradar.research.pipeline import Failed, Run


def evidence(case, data):
    out = os.environ.get("CHAIN1_DEFECT_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps({"case": case, "evidence": data}, ensure_ascii=False, default=str) + "\n")


class RevocableFabric(FakeFabric):
    """FakeFabric with Fabric's readable-source list; `revoked` sources drop out of it."""

    def __init__(self, records):
        super().__init__(records)
        self.revoked_sources = set()

    def sources(self):
        readable = {d["source"] for d in self.docs} - self.revoked_sources
        return sources_receipt(readable)


def run_with(monkeypatch, biocheck, fabric=None):
    sent = []
    monkeypatch.setattr(llm, "chat_json", fake_model(sent))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", biocheck)
    row = analyses.create(
        question="XQ-17 的PFS结果现在应怎样判断？", direction="pipeline", is_demo=True, enqueue=False
    )
    return Run(row, settings(), fabric=fabric or FakeFabric(RECORDS)).run()


def test_a_report_whose_check_did_not_run_is_marked_pending(monkeypatch):
    def biocheck(s, operation, body):
        if operation == "generate":
            return {"draft": "同一试验更正后PFS的HR为0.69[E1]。", "llm_calls": []}
        raise services.ServiceError("biocheck", "timeout")

    result = run_with(monkeypatch, biocheck)
    evidence(
        "no_publication_gate",
        {
            "report_status": result.get("report_status"),
            "report_reason": (result.get("report") or {}).get("reason"),
            "draft_in_result": bool(result.get("draft")),
            "assessment_delivered": bool(result.get("assessment")),
            "result_keys": sorted(result),
        },
    )
    assert result["report_status"] == "NOT_RUN"  # the check did not run ...
    # ... so it is not delivered as a finished report: it is marked 待核, explicitly not publishable
    assert result.get("publishable") is False and result.get("hold") == "pending_verification"


def test_evidence_of_a_source_revoked_during_the_run_never_goes_out_again(monkeypatch):
    fabric = RevocableFabric(RECORDS)
    sent_to_biocheck = []

    def biocheck(s, operation, body):
        sent_to_biocheck.append((operation, [(e["id"], e.get("source")) for e in body.get("evidence", [])]))
        call = [{"stage": operation, "model": "fake", "usage": {}}]
        if operation == "generate":
            return {"draft": "同一试验更正后PFS的HR为0.69[E1]。", "llm_calls": call}
        return {"status": "PASS", "claims": [], "coverage": {"verdict": "complete"}, "llm_calls": call}

    original = Run.compute

    def revoke_then_compute(self):  # the source is revoked after investigate read it, before the report
        fabric.revoked_sources.add("external_archive")
        return original(self)

    monkeypatch.setattr(Run, "compute", revoke_then_compute)
    failed = None
    try:
        run_with(monkeypatch, biocheck, fabric)
    except Failed as exc:
        failed = exc.code
    evidence(
        "no_midrun_revocation_check",
        {
            "revoked": "external_archive",
            "failed": failed,
            "sent_to_biocheck_after_revocation": sent_to_biocheck,
        },
    )
    leaked = [ids for _op, ids in sent_to_biocheck if ids]
    assert not leaked  # nothing read from the revoked source is sent out after the revocation


# ---- around the two fixes -------------------------------------------------------------------------------


def passing(sent):
    def biocheck(s, operation, body):
        sent.append((operation, [e.get("source") for e in body.get("evidence", [])]))
        call = [{"stage": operation, "model": "fake", "usage": {}}]
        if operation == "generate":
            return {"draft": "同一试验更正后PFS的HR为0.69[E1]。", "llm_calls": call}
        return {"status": "PASS", "claims": [], "coverage": {"verdict": "complete"}, "llm_calls": call}

    return biocheck


def test_a_passed_report_is_publishable(monkeypatch):
    result = run_with(monkeypatch, passing([]))
    assert result["report_status"] == "PASS"
    assert (result["publishable"], result["hold"], result["hold_reason"]) == (True, None, None)


def test_a_pending_report_shows_its_draft_and_the_mark_in_every_exit(monkeypatch, fabric_sources):  # noqa: F811
    from test_access import admin_client

    from bioradar import app

    def biocheck(s, operation, body):
        if operation == "generate":
            return {"draft": "未核验的稿件句子[E1]。", "llm_calls": []}
        raise services.ServiceError("biocheck", "timeout")

    result = run_with(monkeypatch, biocheck)
    assert result["hold_reason"] == "biocheck_timeout" and result["assessment"]
    with db.session() as s:  # as analyses.execute stores it
        row = s.scalars(select(db.Analysis)).one()
        row.state, row.result, analysis_id = "completed", result, row.id
    fabric_sources["value"] = ["external_archive"]  # every source readable
    app._readable.update(at=None, sources=None)
    admin = admin_client()
    detail = admin.get(f"/api/analyses/{analysis_id}").json()["result"]
    mark = (detail["report_status"], detail["publishable"], detail["hold"], detail["hold_reason"])
    assert mark == ("NOT_RUN", False, "pending_verification", "biocheck_timeout")
    assert detail["draft"] == "未核验的稿件句子[E1]。" and detail["assessment"] == result["assessment"]
    assert [v["draft"] for v in detail["review"]["versions"]] == ["未核验的稿件句子[E1]。"]
    listed = admin.get("/api/analyses").json()[0]["result"]
    assert listed["assessment"] == result["assessment"] and listed["hold"] == "pending_verification"
    export = admin.get(f"/api/analyses/{analysis_id}/export").text
    assert "未核验的稿件句子" in export and "报告待核：核验状态 NOT_RUN，biocheck_timeout" in export
    fabric_sources["value"] = []  # the source is revoked: the permission still withholds the words
    app._readable.update(at=None, sources=None)
    detail = admin.get(f"/api/analyses/{analysis_id}/export").text
    assert "未核验的稿件句子" not in detail and "报告待核：核验状态 NOT_RUN" in detail
    withheld = admin.get(f"/api/analyses/{analysis_id}").json()["result"]
    assert withheld["draft"] == "" and withheld["withheld"] and withheld["hold"] == "pending_verification"
    with db.session() as s:  # nothing stored is changed
        assert s.get(db.Analysis, analysis_id).result["draft"] == "未核验的稿件句子[E1]。"


def test_the_export_of_a_pending_report_lists_the_sentences_its_check_did_not_support(fabric_sources):  # noqa: F811
    from test_access import admin_client

    claims = [
        {"index": 1, "text": "第一句有来源", "verdict": "supported", "citations": ["E1"]},
        {"index": 2, "text": "第二句无来源", "verdict": "insufficient", "citations": [], "reason": "no_cite"},
    ]
    draft = "第一句有来源[E1]。第二句无来源。"
    stored = {"report_status": "REVIEW", "draft": draft, "review": {"claims": claims}}
    row = analyses.create(question="XQ-17 的PFS怎样？", direction="pipeline", is_demo=True, enqueue=False)
    with db.session() as s:
        analysis = s.get(db.Analysis, row.id)
        analysis.state, analysis.result = "completed", stored
    export = admin_client().get(f"/api/analyses/{row.id}/export").text
    assert "报告待核：核验状态 REVIEW，biocheck_review" in export and "第二句无来源[E1]" not in export
    assert export.endswith("- 第 2 句（insufficient）第二句无来源 no_cite")
    assert "第 1 句" not in export  # a sentence the check supported is not listed


def test_a_resumed_run_checks_its_own_sources_again_before_anything_goes_out(monkeypatch):
    """Revoked between two workers: the resumed run (from the saved stage) withholds the report request,
    records the prepared material as withheld, spends no call and sends nothing."""
    fabric = RevocableFabric(RECORDS)
    sent = []
    monkeypatch.setattr(llm, "chat_json", fake_model([]))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", passing(sent))
    row = analyses.create(
        question="XQ-17 的PFS结果现在应怎样判断？", direction="pipeline", is_demo=True, enqueue=False
    )
    first = Run(row, settings(), fabric=fabric)
    for stage, after in (("plan", "investigate"), ("investigate", "compute"), ("compute", "mechanism")):
        getattr(first, stage)()
        first.save(after)
    first.mechanism()
    first.save("report")  # the worker stops here
    fabric.revoked_sources.add("external_archive")
    resumed = Run(analyses.get(row.id), settings(), fabric=fabric)
    calls = resumed.calls_used
    try:
        resumed.run()
    except Failed as exc:
        code = exc.code
    assert code == "source_access_withdrawn" and sent == [] and resumed.calls_used == calls
    with db.session() as s:
        statuses = {(r.stage, r.status) for r in s.scalars(select(db.MaterialRead))}
        stored = s.get(db.Analysis, row.id)
    assert {st for stage, st in statuses if stage.startswith("report")} == {"withheld"}
    assert ("read", "sent") in statuses  # what went out before the revocation keeps its record
    assert stored.stage == "report"  # the saved state is what the last save wrote


def test_when_fabric_cannot_confirm_the_sources_nothing_is_sent(monkeypatch):
    class Unlisted(FakeFabric):
        def sources(self):
            raise FabricError("fabric_http_503", "down")

    sent = []
    try:
        run_with(monkeypatch, passing(sent), Unlisted(RECORDS))
    except Failed as exc:
        code = exc.code
    assert code == "source_access_unconfirmed" and sent == []
