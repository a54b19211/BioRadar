"""Chain-1 r2 (Codex, point 6): the two defects that may not be waived, shown by fault injection, no model -
each test asserts the behaviour wanted and is a strict xfail until the next implementation batch fixes it;
CHAIN1_DEFECT_OUTPUT (optional) receives what actually happened, as evidence.
1. no publication gate: a report whose check did not run is still finished as a deliverable (no 待核 state);
2. no mid-run revocation check of the analysis' own sources: evidence of a source revoked after it was read
   still goes out to BioCheck."""

import json
import os

import pytest
from conftest import FakeFabric
from test_pipeline import RECORDS, fake_model, first_policy

from bioradar.adapters import llm, services
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
        return {"sources": [{"source_id": s} for s in readable]}


def run_with(monkeypatch, biocheck, fabric=None):
    sent = []
    monkeypatch.setattr(llm, "chat_json", fake_model(sent))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", biocheck)
    row = analyses.create(
        question="XQ-17 的PFS结果现在应怎样判断？", direction="pipeline", is_demo=True, enqueue=False
    )
    return Run(row, settings(), fabric=fabric or FakeFabric(RECORDS)).run()


@pytest.mark.xfail(
    strict=True,
    reason="defect (Codex chain-1 r2): no publication gate - a report whose "
    "check did not run is finished like any other; to be fixed in the next implementation batch",
)
def test_a_report_whose_check_did_not_run_is_held_as_pending_not_delivered(monkeypatch):
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
    # ... so nothing is delivered as a finished report: it is held as 待核, explicitly not publishable
    assert result.get("publishable") is False and result.get("hold") == "pending_verification"


@pytest.mark.xfail(
    strict=True,
    reason="defect (Codex chain-1 r2): the analysis does not check its own "
    "sources again during the run; to be fixed in the next implementation batch",
)
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
