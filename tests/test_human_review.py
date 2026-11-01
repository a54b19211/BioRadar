"""SYNTHETIC fixed-object human reviews; never a real signal or model call."""

import copy
import hashlib

import pytest
from bioradar.research.investigations.models import InvestigationEntryCreate
from pydantic import ValidationError
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import service as service  # noqa: F401
from test_engine_persistence import settings as settings  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_investigation_analysis import runtime as runtime  # noqa: F401


def _review_body():
    return {
        "expected_revision": 2,
        "kind": "judgment",
        "text": "SYNTHETIC needs a human check",
        "assessment": "unable_to_determine",
        "basis_entry_ids": [],
        "review": {
            "decision": "needs_verification",
            "scope": "SYNTHETIC selected claim only",
            "target": {
                "type": "claim",
                "analysis_id": "synthetic-analysis",
                "claim_id": "c1",
                "claim_sha256": "a" * 64,
            },
        },
    }


def test_fixed_review_request_and_legacy_judgment_are_distinct():
    body = _review_body()
    parsed = InvestigationEntryCreate.model_validate(body)
    assert parsed.model_dump(mode="json")["review"] == body["review"]
    legacy = {key: value for key, value in body.items() if key != "review"}
    assert "review" not in InvestigationEntryCreate.model_validate(legacy).model_dump(mode="json")
    for target in (
        {"type": "report", "analysis_id": "a", "text_sha256": "bad"},
        {"type": "candidate", "run_id": "r", "result_hash": "b" * 64},
    ):
        invalid = copy.deepcopy(body)
        invalid["review"]["target"] = target
        with pytest.raises(ValidationError):
            InvestigationEntryCreate.model_validate(invalid)
    for field in ("assessed_by", "assessed_at", "assessment_status"):
        invalid = copy.deepcopy(body)
        invalid["review"][field] = "human_reviewed"
        with pytest.raises(ValidationError):
            InvestigationEntryCreate.model_validate(invalid)


def test_delivery_scopes_manual_record_and_preserves_the_exact_failed_body():
    from bioradar.delivery.research_report import (
        build_report_delivery,
        human_review_context,
        render_report_markdown,
    )
    from bioradar.research.mining.models import canonical_hash
    from test_delivery_report import _view

    view = _view()
    claim = {"id": "c1", "text": "SYNTHETIC bounded assertion", "conditions": ["SYNTHETIC condition"]}
    view["result"]["claims"] = [claim]
    body = view["result"]["report"]["final_text"]
    entry = {
        "id": "review-1",
        "investigation_id": view["investigation_id"],
        "revision": 3,
        "created_by": "synthetic-human",
        "created_at": "2026-10-31T10:00:00Z",
        "payload": {
            "kind": "judgment",
            "text": "SYNTHETIC needs verification",
            "conditions": "SYNTHETIC only",
            "basis_entry_ids": [],
            "review": {
                **_review_body()["review"],
                "target": {
                    "type": "claim",
                    "analysis_id": view["id"],
                    "claim_id": "c1",
                    "claim_sha256": canonical_hash(claim),
                },
                "basis_record_refs": [],
            },
        },
    }
    old = copy.deepcopy((view, entry))
    context = human_review_context(view, [entry])
    assert context["status"] == "recorded"
    assert context["reviews"][0]["reviewer_actor"] == "synthetic-human"
    delivery = build_report_delivery(view, human_review=context)
    markdown = render_report_markdown(view, human_review=context)
    start, end = (delivery["body_range"][key] for key in ("start", "end"))
    assert markdown[start:end] == body
    assert hashlib.sha256(markdown[start:end].encode()).hexdigest() == view["result"]["report"]["text_sha256"]
    assert delivery["verification_status"] == "FAIL"
    from scripts.report_delivery_assertions import expected_delivery_markdown

    native_view = {**view, "human_review_context": context}
    assert expected_delivery_markdown(native_view, delivery, body) == markdown
    forged_delivery = copy.deepcopy(delivery)
    forged_delivery["human_review_context"]["reviews"][0]["target"]["claim_sha256"] = "f" * 64
    with pytest.raises(AssertionError):
        expected_delivery_markdown(native_view, forged_delivery, body)
    assert markdown == delivery["human_review_prefix"] + body + delivery["source_index_markdown"]
    assert (view, entry) == old
    view["result"]["claims"][0]["conditions"].append("SYNTHETIC later condition")
    stale = human_review_context(view, [entry])
    assert stale["status"] == "not_reviewed"
    assert stale["reviews"][0]["binding_status"] == "target_changed"
    other = copy.deepcopy(old[0])
    other["id"] = "new-analysis"
    assert human_review_context(other, [entry])["reviews"][0]["binding_status"] == "different_target"
    legacy = copy.deepcopy(entry)
    legacy["payload"].pop("review")
    assert human_review_context(old[0], [legacy])["status"] == "not_reviewed"
    assert "human_review_context" not in build_report_delivery(old[0])


@pytest.mark.ENGINE
def test_human_review_http_binds_fixed_candidate_and_never_signs_as_automation(
    service, settings, snapshot, database
):
    from bioradar.api.app import create_app
    from bioradar.api.errors import DomainError
    from bioradar.research.investigations.models import InvestigationAutomation
    from fastapi.testclient import TestClient
    from test_engine_persistence import CREDENTIAL
    from test_investigations import append, candidate_origin, create_case, material

    actor, adopted, origin = candidate_origin(service, settings, snapshot, database)
    runtime, _, _, view = create_case(service, settings, origin=origin)
    view = append(runtime, actor, view, "material", "basis", **material(adopted, snapshot))
    material_id = view["entries"][0]["id"]
    body = {
        "expected_revision": view["revision"],
        "kind": "judgment",
        "text": "SYNTHETIC human review",
        "assessment": "provisional",
        "basis_entry_ids": [material_id],
        "review": {
            "decision": "accepted_within_scope",
            "scope": "SYNTHETIC fixed candidate only",
            "target": {"type": "candidate", **origin},
        },
    }
    app = create_app(settings)
    headers = {"Authorization": "Bearer " + CREDENTIAL, "Idempotency-Key": "fixed-human"}
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        app.state.database = database
        url = f"/api/engine/investigations/{view['id']}/entries"
        assert client.post(url, json=body).status_code == 401
        saved = client.post(url, headers=headers, json=body)
        assert saved.status_code == 201, saved.text
        data = saved.json()
        entry = data["entries"][-1]
        assert entry["created_by"] == actor.actor_id and entry["created_at"]
        assert entry["payload"]["review"]["basis_record_refs"] == [
            snapshot.records[0].record_ref.model_dump(mode="json")
        ]
        assert data["professional_status"] == "not_reviewed"
        assert client.post(url, headers=headers, json=body).json() == data
        assert client.get(f"/api/engine/investigations/{view['id']}", headers=headers).json() == data
        forged = copy.deepcopy(body)
        forged["review"]["assessed_by"] = "fake-expert"
        assert (
            client.post(url, headers={**headers, "Idempotency-Key": "forged"}, json=forged).status_code == 422
        )
        wrong = copy.deepcopy(body)
        wrong["expected_revision"] = data["revision"]
        wrong["review"]["target"]["result_hash"] = "f" * 64
        assert (
            client.post(url, headers={**headers, "Idempotency-Key": "wrong"}, json=wrong).status_code == 422
        )
        missing = copy.deepcopy(body)
        missing["expected_revision"] = data["revision"]
        missing["basis_entry_ids"] = []
        missing["assessment"] = "unable_to_determine"
        failure = client.post(url, headers={**headers, "Idempotency-Key": "missing"}, json=missing)
        assert failure.status_code == 422 and failure.json()["error"]["code"] == "human_review_basis_required"
        assert runtime.get(actor, view["id"]).revision == data["revision"]
        app.state.database = None
    with pytest.raises(DomainError) as automated:
        runtime.append(
            actor,
            view["id"],
            InvestigationEntryCreate.model_validate(body),
            "auto",
            automation=InvestigationAutomation(source="monitor", batch_id="batch", lead_key="a" * 64),
        )
    assert automated.value.code == "automation_judgment_forbidden"


@pytest.mark.ENGINE
def test_native_rating_stamps_actual_actor_time_and_rejects_unadopted_basis(
    service, settings, snapshot, database
):
    from bioradar.api.errors import DomainError
    from test_investigations import append, candidate_origin, create_case, material

    actor, adopted, origin = candidate_origin(service, settings, snapshot, database)
    runtime, _, _, view = create_case(service, settings, origin=origin)
    view = append(runtime, actor, view, "material", "basis", **material(adopted, snapshot))
    ref = snapshot.records[0].record_ref.model_dump(mode="json")
    body = {
        "expected_revision": view["revision"],
        "kind": "judgment",
        "text": "SYNTHETIC scoped source rating",
        "assessment": "provisional",
        "basis_entry_ids": [view["entries"][0]["id"]],
        "review": {
            "decision": "needs_verification",
            "scope": "SYNTHETIC publisher only",
            "target": {"type": "candidate", **origin},
        },
        "assessment_axes": {
            "source_reliability": [
                {
                    "grade": "C",
                    "record_ref": ref,
                    "publisher_scope": "SYNTHETIC publisher history only",
                    "assessment_basis_refs": [ref],
                    "assessment_basis": "SYNTHETIC documented limitations",
                }
            ]
        },
    }
    first = runtime.append(actor, view["id"], InvestigationEntryCreate.model_validate(body), "rating")[1]
    entry = first["entries"][-1]
    axes = entry["payload"]["assessment_axes"]
    rated = axes["source_reliability"][0]
    assert rated["grade"] == "C" and rated["assessment_status"] == "human_reviewed"
    assert rated["assessed_by"] == entry["created_by"] == actor.actor_id
    assert rated["assessed_at"] == entry["created_at"]
    assert rated["native_review_entry_id"] == entry["id"] and rated["review_revision"] == entry["revision"]
    assert axes["heat"] is None and axes["evidence_level"] is None
    assert (
        runtime.append(actor, view["id"], InvestigationEntryCreate.model_validate(body), "rating")[1] == first
    )
    fake = copy.deepcopy(body)
    fake["assessment_axes"]["source_reliability"][0]["assessed_by"] = "fake-expert"
    with pytest.raises(ValidationError):
        InvestigationEntryCreate.model_validate(fake)
    bad = copy.deepcopy(body)
    bad["expected_revision"] = first["revision"]
    bad["assessment_axes"]["source_reliability"][0]["assessment_basis_refs"][0]["adopted_version"] = "bad"
    with pytest.raises(DomainError) as failure:
        runtime.append(actor, view["id"], InvestigationEntryCreate.model_validate(bad), "bad-rating")
    assert failure.value.code == "assessment_review_invalid"
    assert runtime.get(actor, view["id"]).revision == first["revision"]
    revised = copy.deepcopy(body)
    revised["expected_revision"] = first["revision"]
    revised["assessment_axes"]["source_reliability"][0]["grade"] = "D"
    second = runtime.append(
        actor, view["id"], InvestigationEntryCreate.model_validate(revised), "revised-rating"
    )[1]
    updated = second["entries"][-1]["payload"]["assessment_axes"]["source_reliability"][0]
    assert updated["grade"] == "D" and updated["previous_assessment_ref"] == entry["id"]
    assert second["entries"][-2] == entry


@pytest.mark.ENGINE
def test_report_and_claim_review_reject_cross_investigation_and_changed_hash(runtime, database):
    from uuid import uuid4

    from bioradar.api.errors import DomainError
    from bioradar.delivery.research_report import human_review_context
    from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
    from bioradar.research.investigations.service import InvestigationService
    from bioradar.research.mining.models import canonical_hash
    from sqlalchemy.orm import Session
    from test_investigations import create_case

    make, actor, view, _, _, settings, adopted = runtime
    service = InvestigationService(make().engine)
    _, _, _, other = create_case(make().engine, settings, key="other")
    claim = {"id": "c1", "text": "SYNTHETIC bounded claim", "conditions": ["SYNTHETIC original condition"]}
    body = "SYNTHETIC exact report🧬"
    result = {"claims": [claim], "report": {"final_text": body, "status": "FAIL"}}
    identifiers = [str(uuid4()), str(uuid4())]
    with Session(database) as session, session.begin():
        for n, inv in enumerate([view, other]):
            session.add(
                InvestigationAnalysisRecord(
                    id=identifiers[n],
                    workspace_id=actor.workspace_id,
                    investigation_id=inv["id"],
                    input_hash=canonical_hash(["SYNTHETIC", n]),
                    state="needs_review",
                    trigger="manual",
                    is_demo=True,
                    based_on_revision=inv["revision"],
                    fixed_input={"snapshot_ids": [adopted["id"]]},
                    progress={},
                    result=result,
                    dispatch_pending=False,
                    rq_job_id="SYNTHETIC-only",
                )
            )

    def request(target, revision=view["revision"]):
        return InvestigationEntryCreate.model_validate(
            {
                "expected_revision": revision,
                "kind": "judgment",
                "text": "SYNTHETIC human check",
                "assessment": "provisional",
                "basis_entry_ids": [view["entries"][0]["id"]],
                "review": {
                    "decision": "needs_verification",
                    "scope": "SYNTHETIC current object only",
                    "target": target,
                },
            }
        )

    target = {
        "type": "report",
        "analysis_id": identifiers[0],
        "text_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    for changed, code in [
        ({**target, "analysis_id": identifiers[1]}, "human_review_target_mismatch"),
        ({**target, "text_sha256": "f" * 64}, "human_review_target_mismatch"),
    ]:
        with pytest.raises(DomainError) as failure:
            service.append(actor, view["id"], request(changed), code + changed["analysis_id"])
        assert failure.value.code == code
    saved = service.append(actor, view["id"], request(target), "report-review")[1]
    target = {
        "type": "claim",
        "analysis_id": identifiers[0],
        "claim_id": "c1",
        "claim_sha256": canonical_hash(claim),
    }
    axes = {
        "information_credibility": [
            {
                "grade": "3",
                "record_ref": view["entries"][0]["payload"]["support"]["record_ref"],
                "claim_id": "c1",
                "claim_text": claim["text"],
                "assessment_basis_refs": [view["entries"][0]["payload"]["support"]["record_ref"]],
                "assessment_basis": "SYNTHETIC limited basis",
            }
        ]
    }
    body_request = request(target, saved["revision"]).model_dump(mode="json")
    body_request["assessment_axes"] = axes
    saved = service.append(
        actor, view["id"], InvestigationEntryCreate.model_validate(body_request), "claim-review"
    )[1]
    assert (
        saved["entries"][-1]["payload"]["assessment_axes"]["information_credibility"][0]["claim_id"] == "c1"
    )
    with Session(database) as session:
        original = copy.deepcopy(session.get(InvestigationAnalysisRecord, identifiers[0]).result)
    projection = {
        "id": identifiers[0],
        "investigation_id": view["id"],
        "result": original,
        "input_hash": canonical_hash(["SYNTHETIC", 0]),
    }
    assert human_review_context(projection, saved["entries"])["status"] == "recorded"
    projection["result"]["report"]["final_text"] += " SYNTHETIC changed"
    projection["result"]["claims"][0]["conditions"].append("SYNTHETIC changed")
    assert human_review_context(projection, saved["entries"])["status"] == "not_reviewed"


@pytest.mark.ENGINE
def test_native_analysis_http_review_json_markdown_keep_the_exact_failed_report(runtime, database, snapshot):
    from datetime import UTC, datetime

    from bioradar.api.app import create_app
    from bioradar.research.investigations.analysis_models import AnalysisResult
    from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
    from bioradar.research.investigations.service import InvestigationService
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from test_engine_persistence import CREDENTIAL
    from test_investigation_analysis import start

    from scripts.report_delivery_assertions import expected_delivery_markdown

    make, actor, view, source, model, settings, _ = runtime
    accepted = start(runtime, query_policy="baseline", auto_retrieve=False)
    from bioradar.adapters.biocheck import build_packet
    from bioradar.research.mining.models import canonical_hash

    material = snapshot.records[0]
    quote = material.normalized_text
    body = f"# SYNTHETIC report\n\n{quote} [E1]\n\nSYNTHETIC unsupported further assertion🧬.\n"
    packet = build_packet(
        "SYNTHETIC local delivery check",
        [{"key": "E1", "text": quote, "record_ref": material.record_ref.model_dump(mode="json")}],
    )
    verification = {
        "draft": body,
        "status": "FAIL",
        "units": [
            {
                "unit_id": "U1",
                "verdict": "supported",
                "issues": [],
                "claims": [
                    {
                        "kind": "fact",
                        "span": quote,
                        "verdict": "supported",
                        "evidence": [{"evidence_id": "E1", "segment_id": "S1"}],
                    }
                ],
            },
            {
                "unit_id": "U2",
                "verdict": "unsupported",
                "issues": ["unsupported"],
                "claims": [
                    {
                        "kind": "fact",
                        "span": "SYNTHETIC unsupported further assertion🧬.",
                        "verdict": "unsupported",
                    }
                ],
            },
        ],
        "verification_errors": [],
    }
    audit = {
        "provider": "biocheck",
        "provider_version": "SYNTHETIC stored fixture, no professional execution",
        "status": "PASS",
        "original_status": "FAIL",
        "targets": [["U1", 0]],
        "text_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "review_sha256": canonical_hash(verification),
        "evidence_map": copy.deepcopy(packet["evidence_map"]),
        "evidence_versions": {
            "E1": {
                "version": material.record_ref.adopted_version,
                "selected_text_sha256": hashlib.sha256(quote.encode()).hexdigest(),
            }
        },
        "checks": [
            {
                "unit_id": "U1",
                "claim_index": 0,
                "verdict": "supported",
                "evidence": [
                    {
                        "evidence_id": "E1",
                        "segment_id": "S1",
                        "quote": quote,
                        "version": material.record_ref.adopted_version,
                    }
                ],
            }
        ],
        "skipped": [],
        "verification_errors": [],
        "scope": "SYNTHETIC selected support only; PASS does not pass the full report.",
        "error_code": None,
    }
    result = AnalysisResult(
        claims=[],
        explanations=[],
        gaps=[],
        conditional_answer={"answer": "SYNTHETIC bounded answer", "conditions": [], "unknowns": []},
        update={
            "stance": "initial",
            "changed_claims": [],
            "reasons": ["SYNTHETIC test fixture"],
            "decisive_evidence_keys": [],
        },
        review={"status": "needs_review", "claims": [], "overall_limitations": []},
        limitations=[],
        report={
            "final_text": body,
            "text_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "status": "FAIL",
            "provider": "SYNTHETIC fixture, no model",
            "verification": verification,
            "support_audit": audit,
            "evidence_map": copy.deepcopy(packet["evidence_map"]),
        },
    )
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        row.state = "needs_review"
        row.completed_at = datetime.now(UTC)
        row.result = result.model_dump(mode="json")
        original_fixed = copy.deepcopy(row.fixed_input)
        original_result = copy.deepcopy(row.result)
        original_progress = copy.deepcopy(row.progress)
    app = create_app(settings)
    headers = {"Authorization": "Bearer " + CREDENTIAL}
    root = f"/api/engine/investigations/{view['id']}/analyses/{accepted['id']}"
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        app.state.database = database
        before = client.get(root, headers=headers)
        assert before.status_code == 200, before.text
        assert before.json()["human_review_context"]["status"] == "not_reviewed"
        current = InvestigationService(make().engine).get(actor, view["id"])
        request = {
            "kind": "judgment",
            "expected_revision": current.revision,
            "text": "SYNTHETIC human still requests verification",
            "assessment": "provisional",
            "basis_entry_ids": [view["entries"][0]["id"]],
            "review": {
                "decision": "needs_verification",
                "scope": "SYNTHETIC exact report only",
                "target": {
                    "type": "report",
                    "analysis_id": accepted["id"],
                    "text_sha256": hashlib.sha256(body.encode()).hexdigest(),
                },
            },
        }
        saved = client.post(
            f"/api/engine/investigations/{view['id']}/entries",
            headers={**headers, "Idempotency-Key": "http-report-human"},
            json=request,
        )
        assert saved.status_code == 201, saved.text
        detail = client.get(root, headers=headers).json()
        exported = client.get(root + "/export", headers=headers).json()
        markdown = client.get(root + "/export", headers=headers, params={"format": "markdown"})
        assert markdown.status_code == 200
        context = detail["human_review_context"]
        assert context["status"] == "recorded" and context["reviews"][0]["decision"] == "needs_verification"
        assert (
            context
            == exported["analysis"]["human_review_context"]
            == exported["delivery"]["human_review_context"]
        )
        assert detail["result"]["report"]["final_text"] == body
        for native in (before.json(), detail, exported["analysis"]):
            report = native["result"]["report"]
            assert report["final_text"] == body and report["status"] == "FAIL"
            assert report["verification"] == verification
            assert report["support_audit"] == audit
            assert report["support_audit"]["review_sha256"] == canonical_hash(report["verification"])
            assert report["support_audit"]["text_sha256"] == report["text_sha256"]
        assert exported["delivery"]["verification_status"] == "FAIL"
        assert exported["delivery"]["support_audit_status"] == "PASS"
        assert "全文核验：FAIL" in markdown.text
        assert "所选支持片段复核：PASS" in markdown.text
        assert "PASS不代表整稿通过" in markdown.text
        assert markdown.text == expected_delivery_markdown(exported["analysis"], exported["delivery"], body)
        span = exported["delivery"]["body_range"]
        assert markdown.text[span["start"] : span["end"]] == body
        assert (
            hashlib.sha256(markdown.text[span["start"] : span["end"]].encode()).hexdigest()
            == audit["text_sha256"]
        )
        assert "全文核验：" not in markdown.text[span["start"] : span["end"]]
        app.state.database = None
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert (
            row.fixed_input == original_fixed
            and row.result == original_result
            and row.progress == original_progress
        )
        assert row.result["report"]["verification"] == verification
        assert row.result["report"]["support_audit"] == audit
        assert row.result["report"]["status"] == "FAIL"
    assert model.requests == []


def test_axes_export_keeps_four_fields_separate_outside_the_exact_body():
    from bioradar.delivery.research_report import build_report_delivery, render_report_markdown
    from test_delivery_report import _view

    from scripts.report_delivery_assertions import expected_delivery_markdown

    view = _view()
    view["result"]["assessment_axes"] = {
        "source_reliability": [],
        "information_credibility": [],
        "evidence_level": "unverified",
        "heat": {"score": 999},
    }
    original = copy.deepcopy(view)
    metadata = build_report_delivery(view)
    markdown = render_report_markdown(view)
    body = view["result"]["report"]["final_text"]
    assert markdown == expected_delivery_markdown(view, metadata, body)
    assert "F · 未评定" in metadata["assessment_axes_prefix"]
    assert "6 · 未评定" in metadata["assessment_axes_prefix"]
    assert "曝光热度：999" in metadata["assessment_axes_prefix"]
    span = metadata["body_range"]
    assert markdown[span["start"] : span["end"]] == body
    assert view == original and metadata["verification_status"] == "FAIL"
