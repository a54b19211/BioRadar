"""The mechanism stage over MechEvidence's version-bound contract, end to end against a stand-in service that
speaks the protocol with the radar's real endpoints and ledger: the evaluation, its reuse, and everything that
must stop a result from being adopted or a request from being sent."""

import hashlib

import pytest
from conftest import FakeFabric, record
from mech_support import (
    EVALUATOR,
    PROVIDER_USAGE,
    Fab,
    FakeMech,
    install,
    service_client,
)
from sqlalchemy import select

from bioradar import db
from bioradar.config import settings
from bioradar.material import of
from bioradar.mech import contract
from bioradar.research import analyses, lease
from bioradar.research.pipeline import Run
from bioradar.research.run import Withheld, report, submit
from bioradar.research.run.mech_v2 import dimension, scope_of

TARGET = {"statement": "A 通过 M 改变 E", "species": "mouse", "system": "S", "disease": ""}
TEXTS = {
    "M1": "A changed M in mouse S cells. Blocking M prevented the E response to A.",
    "M2": "M2 reports that A raised M.",
}


def mech_record(rid, text, **more):
    found = record(
        rid,
        f"{rid} 机制研究",
        text,
        kind="primary_research",
        version=hashlib.sha256(rid.encode()).hexdigest(),
        **more,
    )
    found["parser_version"] = "p1"
    return found


class Ready:
    """An analysis at the start of its mechanism stage: lease held, evidence read, stand-in service up."""

    def __init__(
        self,
        monkeypatch,
        *,
        texts=None,
        question="A 是否通过 M 改变 E？",
        target=TARGET,
        cutoff=None,
        fabric=None,
        mech=None,
        usage=PROVIDER_USAGE,
        bind=True,
    ):
        self.client = service_client(monkeypatch)
        Fab.readable = {"external_archive"}
        self.mech = mech or FakeMech(self.client, usage=usage)
        install(monkeypatch, self.mech)
        texts = TEXTS if texts is None else texts
        self.fabric = fabric or FakeFabric([mech_record(rid, text) for rid, text in texts.items()])
        row = analyses.create(
            question=question, direction="science", is_demo=True, cutoff=cutoff, enqueue=False
        )
        with db.session() as s:
            lease.take([s.get(db.Analysis, row.id)], "tok", db.now(), lease.ANALYSIS_LEASE)
        settings_ = settings().model_copy(
            update={"mechevidence_url": "http://mech.invalid", "mechevidence_contract": "v2"}
        )
        self.run = Run(analyses.get(row.id), settings_, fabric=self.fabric, lease_token="tok")
        self.run.p.update(facts=[{"ref": f"E{n}"} for n in range(1, len(texts) + 1)], mechanism=target)
        for n, doc in enumerate(self.fabric.docs, 1):
            material = of(doc)
            with db.session() as s:
                read_id = submit.sent(self.run, s, "read", "radar", f"E{n}", doc, material) if bind else None
                s.add(
                    db.Evidence(
                        analysis_id=row.id,
                        ref=f"E{n}",
                        source=doc["source"],
                        record_id=doc["record_id"],
                        version_id=doc["version_id"],
                        title=doc["title"],
                        material_kind="primary_research",
                        verified=True,
                        text=material.text,
                        meta={"material_read_id": read_id, "text_level": "abstract"},
                    )
                )

    def go(self):
        self.run.mechanism()
        return self.run.p["mechanism_result"]


def table(model, **where):
    with db.session() as s:
        query = select(model)
        for key, value in where.items():
            query = query.where(getattr(model, key) == value)
        return list(s.scalars(query))


# ---- the scope in three states ----


def test_an_unstated_scope_is_unknown_never_not_required():
    assert dimension("mouse") == {"state": "stated", "value": "mouse"}
    for nothing in ("", "  ", "unknown", "未知", None, 5):
        assert dimension(nothing) == {"state": "unknown"}
    assert dimension({"state": "not_required"}) == {"state": "not_required"}
    assert dimension({"state": "stated", "value": "human"}) == {"state": "stated", "value": "human"}
    assert dimension({"state": "stated", "value": ""}) == {
        "state": "unknown"
    }  # stated but empty is not stated
    assert dimension({"state": "unknown", "value": "mouse"}) == {"state": "unknown"}


def test_an_old_plan_with_three_empty_strings_asks_about_three_unknowns():
    assert scope_of({"statement": "x", "species": "", "system": "", "disease": ""}) == {
        name: {"state": "unknown"} for name in ("species", "system", "disease")
    }
    assert scope_of({"statement": "x"})["species"] == {"state": "unknown"}


# ---- the evaluation ----


def test_an_evaluation_is_sent_adopted_cached_and_accounted_for(monkeypatch):
    ready = Ready(monkeypatch)
    result = ready.go()
    assert result["status"] == "completed" and result["overall"] == "supported" and result["reused"] is False
    assert (
        result["contract_version"] == contract.CONTRACT_V2
        and result["evaluator_id"] == EVALUATOR["evaluator_id"]
    )
    assert [m["ref"] for m in result["materials"]] == ["E1", "E2"] and "llm_calls" not in result
    request = ready.mech.requests[0]
    assert request["expected_evaluator_id"] == EVALUATOR["evaluator_id"] and request["authorization_ref"]
    assert (
        request["scope"]["disease"] == {"state": "unknown"}
        and request["scope"]["species"]["value"] == "mouse"
    )
    [auth] = table(db.ModelAttemptAuthorization)
    assert (auth.analysis_id, auth.lease_token, auth.evaluation_key) == (
        ready.run.id,
        "tok",
        result["reuse_key"],
    )
    assert auth.sources == ["external_archive"] and auth.max_attempts == 2 * request["max_llm_calls"]
    # cached by its key, no text kept; this analysis used it
    [cached] = table(db.MechEvaluation)
    assert cached.reuse_key == result["reuse_key"] and all(
        text not in str(cached.materials) for text in TEXTS.values()
    )
    assert [(u.analysis_id, u.how) for u in table(db.MechEvaluationUse)] == [(ready.run.id, "computed")]
    # the model calls: the provider's usage, the raw answers, linked to the ledger
    calls = table(db.LlmCall)
    assert [
        (c.service, c.stage, c.requests, c.total_tokens, c.usage_known, c.authorization_ref) for c in calls
    ] == [
        ("mechevidence", "decompose", 1, 120, True, auth.ref),
        ("mechevidence", "review", 1, 120, True, auth.ref),
    ]
    outputs = table(db.LlmOutput)
    assert sorted(o.attempt_id for o in outputs) == sorted(a.attempt_id for a in table(db.ModelAttempt))
    assert all(
        o.delivered == "yes" and o.http_status == 200 and o.truncated is False and o.content for o in outputs
    )
    assert ready.run.calls_used == 2
    # the materials sent: sent, linked to the review call
    reads = [r for r in table(db.MaterialRead) if r.stage == "mechanism"]
    assert {r.status for r in reads} == {"sent"} and {r.llm_call_id for r in reads} == {calls[1].id}


def test_the_calculation_basis_names_the_evaluator_the_key_and_the_bindings(monkeypatch):
    ready = Ready(monkeypatch)
    ready.go()
    [item] = [r for r in report.analysis_results(ready.run) if r["method"] == "mechevidence"]
    basis = item["calculation_basis"]
    assert basis["evaluator_id"] == EVALUATOR["evaluator_id"] and basis["reused"] is False
    assert basis["reuse_key"] == ready.run.p["mechanism_result"]["reuse_key"] and len(basis["materials"]) == 2


def test_the_same_evaluation_again_is_reused_with_no_request_and_no_model_call(monkeypatch):
    first = Ready(monkeypatch)
    first.go()
    second = Ready(monkeypatch, mech=first.mech)
    result = second.go()
    assert result["reused"] is True and result["reuse_key"] == first.run.p["mechanism_result"]["reuse_key"]
    assert len(first.mech.requests) == 1  # the service was asked once, by the first analysis
    assert second.run.calls_used == 0 and table(db.LlmCall, analysis_id=second.run.id) == []
    reads = [r for r in table(db.MaterialRead, analysis_id=second.run.id) if r.stage == "mechanism"]
    assert {r.status for r in reads} == {"reused"}  # the text went to no service; the record says so
    assert sorted((u.analysis_id, u.how) for u in table(db.MechEvaluationUse)) == sorted(
        [(first.run.id, "computed"), (second.run.id, "reused")]
    )


@pytest.mark.parametrize(
    "change",
    [
        {"question": "B 是否通过 N 改变 F？"},  # another question (N4)
        {"target": dict(TARGET, species="human")},  # another scope (N3)
        {"target": dict(TARGET, species="")},  # a scope nobody stated
        {"target": dict(TARGET, statement="A 不通过 M 改变 E")},  # another proposition
    ],
    ids=["question", "species", "unstated_species", "mechanism"],
)
def test_a_changed_question_or_scope_is_never_a_cache_hit(monkeypatch, change):
    first = Ready(monkeypatch)
    first.go()
    other = Ready(monkeypatch, mech=first.mech, **change)
    assert other.go()["reused"] is False
    assert len(first.mech.requests) == 2 and len(table(db.MechEvaluation)) == 2


def test_another_title_or_order_or_text_is_another_evaluation(monkeypatch):
    first = Ready(monkeypatch)
    first.go()
    titled = FakeFabric([mech_record(rid, text) for rid, text in TEXTS.items()])
    for doc in titled.docs:
        doc["title"] += " (v2)"
    assert Ready(monkeypatch, mech=first.mech, fabric=titled).go()["reused"] is False  # the title is an input
    swapped = Ready(monkeypatch, mech=first.mech, texts=dict(reversed(TEXTS.items())))
    assert swapped.go()["reused"] is False  # the same texts in another order (phase 1: the order is an input)
    changed = Ready(monkeypatch, mech=first.mech, texts={**TEXTS, "M2": TEXTS["M2"] + " More."})
    assert changed.go()["reused"] is False
    assert len(first.mech.requests) == 4


def test_an_evaluator_that_changed_does_not_reuse_the_old_ones_result(monkeypatch):
    first = Ready(monkeypatch)
    first.go()
    upgraded = dict(EVALUATOR, rules_version="mechevidence.aggregate.v2")
    upgraded["evaluator_id"] = contract.evaluator_id_of(upgraded)
    other = Ready(monkeypatch, mech=FakeMech(first.client, evaluator=upgraded))
    result = other.go()
    assert result["reused"] is False and result["evaluator_id"] == upgraded["evaluator_id"]
    assert len(table(db.MechEvaluation)) == 2  # the old result stays as history


def test_a_cached_result_is_not_used_once_its_source_is_revoked(monkeypatch):
    first = Ready(monkeypatch)
    first.go()
    other = Ready(monkeypatch, mech=first.mech)
    other.fabric.revoked_sources = {"external_archive"}
    other.fabric.sources = lambda: __import__("fabric_receipts").sources_receipt([])
    with pytest.raises(Withheld) as stopped:
        other.run.mechanism()
    assert stopped.value.code == "source_access_withdrawn"
    assert len(first.mech.requests) == 1 and table(
        db.MechEvaluation
    )  # not asked again, the row is not deleted
    assert {r.status for r in table(db.MaterialRead, analysis_id=other.run.id) if r.stage == "mechanism"} == {
        "withheld"
    }


def test_a_replay_before_a_material_was_visible_does_not_reuse_a_later_evaluation(monkeypatch):
    first = Ready(monkeypatch)
    first.go()
    early = Ready(monkeypatch, mech=first.mech, cutoff="2026-01-01T00:00:00+00:00")
    result = early.go()  # the cached evaluation is not visible at that cutoff: evaluated again, not reused
    assert result["reused"] is False and len(first.mech.requests) == 2


def test_an_incomplete_evaluation_is_used_by_its_analysis_and_never_cached(monkeypatch):
    def budget_cut(receipt):
        receipt["complete"], receipt["not_complete_because"] = False, ["pending_budget"]
        receipt["material_notes"] = [{"material": "E1", "code": "pending_budget", "message": "budget"}]
        return receipt

    ready = Ready(monkeypatch)
    ready.mech.edit = budget_cut
    result = ready.go()
    assert result["status"] == "completed" and result["complete"] is False
    assert table(db.MechEvaluation) == [] and "mechevidence_incomplete" in [
        n["code"] for n in ready.run.p["notes"]
    ]
    again = Ready(
        monkeypatch, mech=ready.mech
    )  # the same request is evaluated again, not answered from a partial
    again.go()
    assert len(ready.mech.requests) == 2


# ---- what is not adopted ----


def not_adopted(ready, reason):
    result = ready.go()
    assert result == {"status": "failed", "reason": reason}
    [kept] = table(db.MechReceiptRejected)
    assert kept.reason == reason and kept.analysis_id == ready.run.id and kept.response
    assert table(db.MechEvaluation) == [] and table(db.MechEvaluationUse) == []  # never cached
    assert [r for r in report.analysis_results(ready.run) if r["method"] == "mechevidence"] == []  # no report
    return kept


def test_a_receipt_for_another_request_is_failed_kept_and_feeds_nothing(monkeypatch):
    ready = Ready(monkeypatch)
    ready.mech.edit = lambda receipt: dict(receipt, reuse_key="0" * 64)
    kept = not_adopted(ready, "receipt_mismatch")
    assert kept.detail["mismatch"][0]["field"] == "reuse_key"
    assert len(table(db.LlmCall)) == 2  # the requests were made and are accounted for all the same


def test_a_receipt_that_echoes_other_materials_or_another_evaluator_is_failed(monkeypatch):
    for damage, field in (
        (lambda r: r["materials"][0].update(binding_sha256="1" * 64), "materials"),
        (
            lambda r: r["evaluator"].update(rules_version="other"),
            "evaluator.evaluator_id(recomputed from its fields)",
        ),
    ):
        ready = Ready(monkeypatch)
        ready.mech.edit = lambda receipt, damage=damage: (damage(receipt), receipt)[1]
        result = ready.go()
        assert result["reason"] == "receipt_mismatch"
        assert field in [m["field"] for m in table(db.MechReceiptRejected)[-1].detail["mismatch"]]


def test_a_receipt_of_the_wrong_shape_is_invalid_and_its_cost_is_not_zero(monkeypatch):
    ready = Ready(monkeypatch)
    ready.mech.edit = lambda receipt: {"overall": "supported", "llm_calls": "none"}
    kept = not_adopted(ready, "receipt_invalid")
    assert kept.detail["invalid"]
    calls = table(
        db.LlmCall
    )  # the radar's ledger knows two requests were allowed; the receipt said nothing usable
    assert [(c.requests, c.usage_known, c.total_tokens, c.error) for c in calls] == [
        (1, False, 0, "no_receipt")
    ] * 2
    assert sorted(o.content_sha256 for o in table(db.LlmOutput)) == sorted(
        a.response_sha256 for a in table(db.ModelAttempt)
    )
    assert ready.run.calls_used == 2


def test_an_audit_that_is_not_complete_is_failed(monkeypatch):
    ready = Ready(monkeypatch)
    ready.mech.edit = lambda receipt: dict(receipt, audit_complete=False)
    not_adopted(ready, "audit_incomplete")


def test_a_receipt_the_radars_own_ledger_contradicts_is_failed_even_when_it_is_well_formed(monkeypatch):
    def another_decision(receipt):
        receipt["llm_calls"][0]["outputs"][0]["decision_id"] = "decision-the-radar-never-made"
        return receipt

    ready = Ready(monkeypatch)
    ready.mech.edit = another_decision
    kept = not_adopted(ready, "ledger_mismatch")
    assert "decision" in str(kept.detail["reconciliation"])


def test_an_outcome_the_service_reported_differently_to_the_radar_is_failed(monkeypatch):
    def other_answer(receipt):
        out = receipt["llm_calls"][1]["outputs"][0]
        out["content"] = '{"observations": [{"edited": true}]}'
        out["sha256"] = hashlib.sha256(out["content"].encode()).hexdigest()
        return receipt

    ready = Ready(monkeypatch)
    ready.mech.edit = other_answer
    kept = not_adopted(ready, "ledger_mismatch")
    assert "outcome" in str(kept.detail["reconciliation"])


def test_a_request_the_radar_allowed_and_the_receipt_does_not_archive_is_not_accounted_for(monkeypatch):
    ready = Ready(monkeypatch)
    ready.mech.extra_attempt = True  # a third permission, never reported: it may have been sent
    kept = not_adopted(ready, "ledger_mismatch")
    assert "does not archive it" in str(kept.detail["reconciliation"])
    calls = table(db.LlmCall)
    assert [c.error for c in calls].count("outcome_unknown") == 1  # counted as a request, usage not known
    assert ready.run.calls_used == 3


# ---- the request stops before it is sent ----


def test_a_material_that_names_no_fixed_version_is_never_sent(monkeypatch):
    ready = Ready(monkeypatch, bind=False)  # read before bindings existed
    assert ready.go() == {"status": "not_run", "reason": "no_bound_material"}
    assert ready.mech.requests == [] and table(db.ModelAttemptAuthorization) == []
    assert [n["code"] for n in ready.run.p["notes"]] == ["mechevidence_material_unbound"] * 2


def test_a_part_fabric_no_longer_gives_or_whose_text_changed_is_not_sent(monkeypatch):
    ready = Ready(monkeypatch)
    ready.fabric.revoked = {ready.fabric.docs[0]["version_id"]}
    ready.fabric.tampered = {ready.fabric.docs[1]["version_id"]}
    assert ready.go() == {"status": "not_run", "reason": "no_bound_material"}
    codes = [n["code"] for n in ready.run.p["notes"]]
    assert codes == ["mechevidence_material_unreadable", "mechevidence_material_changed"]
    assert ready.mech.requests == []


def test_one_material_that_cannot_be_sent_does_not_stop_the_others(monkeypatch):
    ready = Ready(monkeypatch)
    ready.fabric.revoked = {ready.fabric.docs[0]["version_id"]}
    result = ready.go()
    assert result["status"] == "completed" and [m["ref"] for m in result["materials"]] == ["E2"]
    assert [m["id"] for m in ready.mech.requests[0]["materials"]] == ["E2"]


def test_a_lost_lease_sends_nothing_and_writes_nothing(monkeypatch):
    ready = Ready(monkeypatch)
    with db.session() as s:
        lease.take([s.get(db.Analysis, ready.run.id)], "successor", db.now(), lease.ANALYSIS_LEASE)
    with pytest.raises(lease.Lost):
        ready.run.mechanism()
    assert ready.mech.requests == [] and table(db.ModelAttemptAuthorization) == []


# ---- the service's side goes wrong ----


def test_an_authorization_revoked_in_the_middle_stops_the_evaluation_and_accounts_for_what_was_sent(
    monkeypatch,
):
    ready = Ready(monkeypatch)
    ready.mech.between = lambda n: setattr(Fab, "readable", set()) if n == 1 else None
    result = ready.go()
    assert result == {"status": "failed", "reason": "authorization_blocked"}
    assert [(a.idx, a.status, a.reason_code) for a in table(db.ModelAttempt)] == [
        (1, "settled", None),
        (2, "refused", "source_access_withdrawn"),
    ]
    assert [(c.stage, c.requests) for c in table(db.LlmCall)] == [
        ("decompose", 1)
    ]  # the second was never sent
    assert table(db.MechEvaluation) == [] and ready.run.calls_used == 1


def test_a_service_that_fails_after_a_permission_leaves_the_request_counted_with_its_usage_unknown(
    monkeypatch,
):
    from bioradar.adapters.services import ServiceError

    ready = Ready(monkeypatch)
    ready.mech.evaluate_original = ready.mech.evaluate

    def dies(settings_, request):
        key = contract.reuse_key(request, ready.mech.evaluator)
        ready.mech.attempt(request, key, "decompose", 1, [], last=True)  # permission taken, then nothing
        raise ServiceError("mechevidence", "timeout")

    install(monkeypatch, ready.mech)
    monkeypatch.setattr("bioradar.adapters.services.mechevidence_v2", dies)
    assert ready.go() == {"status": "failed", "reason": "timeout"}
    [call] = table(db.LlmCall)
    assert (call.requests, call.usage_known, call.error) == (
        1,
        False,
        "outcome_unknown",
    )  # may have been sent
    assert [a.status for a in table(db.ModelAttempt)] == ["prepared"]


def test_usage_the_service_did_not_report_is_recorded_as_not_known_not_as_zero(monkeypatch):
    ready = Ready(monkeypatch, usage={})
    assert ready.go()["status"] == "completed"
    assert [(c.total_tokens, c.usage_known) for c in table(db.LlmCall)] == [(0, False), (0, False)]


# ---- the evaluator changes under the request ----


def test_an_evaluator_that_changes_before_the_request_is_asked_again_once(monkeypatch):
    upgraded = dict(EVALUATOR, rules_version="mechevidence.aggregate.v2")
    upgraded["evaluator_id"] = contract.evaluator_id_of(upgraded)
    ready = Ready(monkeypatch)
    ready.mech.swap_before_answering = upgraded
    result = ready.go()
    assert result["status"] == "completed" and result["evaluator_id"] == upgraded["evaluator_id"]
    first, second = ready.mech.requests
    assert first["expected_evaluator_id"] == EVALUATOR["evaluator_id"] != second["expected_evaluator_id"]
    assert (
        table(db.LlmCall)[0].authorization_ref != ""
    )  # the 409 made no model call: only the second request did
    assert ready.run.calls_used == 2 and len(table(db.ModelAttemptAuthorization)) == 2


def test_an_evaluator_that_keeps_changing_is_unstable(monkeypatch):
    ready = Ready(monkeypatch)
    flips = iter(
        dict(
            EVALUATOR,
            rules_version=f"v{n}",
            evaluator_id=contract.evaluator_id_of(dict(EVALUATOR, rules_version=f"v{n}")),
        )
        for n in range(10)
    )
    original = ready.mech.evaluate

    def always_another(settings_, request):
        ready.mech.evaluator = next(flips)
        return original(settings_, request)

    install(monkeypatch, ready.mech)
    monkeypatch.setattr("bioradar.adapters.services.mechevidence_v2", always_another)
    assert ready.go() == {"status": "failed", "reason": "evaluator_unstable"}
    assert table(db.LlmCall) == [] and table(db.MechEvaluation) == []
