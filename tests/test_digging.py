"""Synthetic research/planning probes; no live source or PostgreSQL is used here."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import NAMESPACE_DNS, uuid5

import httpx
import pytest
from bioradar.adapters.data_fabric_research import DataFabricResearchReader
from bioradar.api.errors import DomainError
from bioradar.research.investigations import digging

pytestmark = pytest.mark.ENGINE


def document(
    label="first",
    *,
    name="BCMA-GPRC5D CAR-T cells",
    kind="BIOLOGICAL",
    status="RECRUITING",
    why_stopped=None,
    nct_id="NCT00000001",
    phases=None,
    start_date=None,
    last_update_date=None,
):
    """An explicitly synthetic projection with the production source shape."""
    interventions = [{"type": kind, "name": name}]
    protocol = {
        "identificationModule": {"nctId": nct_id, "briefTitle": "SYNTHETIC BCMA myeloma trial"},
        "conditionsModule": {"conditions": ["SYNTHETIC myeloma"]},
        "armsInterventionsModule": {"interventions": [{**interventions[0], "otherNames": ["test only"]}]},
        "designModule": {} if phases is None else {"phases": phases},
        "statusModule": {
            "overallStatus": status,
            "whyStopped": why_stopped,
            "startDateStruct": start_date,
            "lastUpdatePostDateStruct": last_update_date,
        },
    }
    text = json.dumps(protocol, ensure_ascii=False, indent=1)
    return {
        "schema_version": "datafabric.research.document.v1",
        "uuid": str(uuid5(NAMESPACE_DNS, "synthetic-digging-" + label)),
        "title": "SYNTHETIC: not a real BCMA myeloma trial",
        "source_site": "clinicaltrials",
        "source_url": None,
        "document_sha256": "a" * 64,
        "publish_date_raw": "unknown",
        "projection": {
            "version": "clinicaltrials.protocol.v1",
            "status": "available",
            "reason": None,
            "text": text,
            "locator": {
                "field": "raw_content",
                "start": 100,
                "end": 100 + len(text),
                "offset_unit": "unicode_codepoint",
            },
            "facts": {
                **{field: None for field in digging.FACT_PATHS},
                "nct_id": nct_id,
                "brief_title": "SYNTHETIC BCMA myeloma trial",
                "interventions": interventions,
                "phases": phases,
                "conditions": ["SYNTHETIC myeloma"],
                "overall_status": status,
                "why_stopped": why_stopped,
                "start_date": start_date,
                "last_update_post_date": last_update_date,
            },
            "field_paths": dict(digging.FACT_PATHS),
        },
    }


def test_followup_is_an_exact_field_substring_and_preserves_the_topic():
    source = document(name="测试用 BCMA-GPRC5D CAR-T cells")
    source.update(
        _snapshot_id="saved-snapshot", _material_entry_id="saved-material", _record_ref={"id": "frozen"}
    )
    followup = digging.select_followups(("BCMA", "myeloma"), [source])[0]
    assert followup.terms == ("BCMA", "myeloma", "GPRC5D")
    assert followup.field_value[followup.value_start : followup.value_end] == "GPRC5D"
    assert followup.field_path == "/protocolSection/armsInterventionsModule/interventions/0/name"
    assert followup.document_uuid == source["uuid"]
    assert followup.snapshot_id == "saved-snapshot" and followup.material_entry_id == "saved-material"
    assert followup.record_ref == {"id": "frozen"}
    assert followup.value_offset_unit == "unicode_codepoint"


def test_followup_order_is_deterministic_and_used_case_variants_are_not_repeated():
    first, second = document(), document("second", name="LCAR-BCX cells")
    inputs = [first, second]
    expected = digging.select_followups(("BCMA", "myeloma"), inputs)
    assert digging.select_followups(("BCMA", "myeloma"), inputs[::-1]) == expected
    assert {item.term for item in expected} == {"GPRC5D", "LCAR-BCX"}
    used = [tuple(term.lower() for term in expected[0].terms[::-1])]
    assert digging.select_followups(("BCMA", "myeloma"), inputs, used) == expected[1:]
    assert digging.select_followups(("BCMA", "myeloma"), inputs, max_followups=0) == []


@pytest.mark.parametrize("tamper", ["value", "path", "raw_source"])
def test_followup_and_summary_refuse_unbound_projected_facts(tamper):
    source = document()
    projection = source["projection"]
    if tamper == "value":
        projection["facts"]["interventions"][0]["name"] = "INVENTED-123"
    elif tamper == "path":
        projection["field_paths"]["interventions"] = "/protocolSection/conditionsModule/conditions"
    else:
        raw = json.loads(projection["text"])
        raw["statusModule"]["overallStatus"] = "TERMINATED"
        projection["text"] = json.dumps(raw)
    with pytest.raises(DomainError):
        digging.select_followups(("BCMA", "myeloma"), [source])
    with pytest.raises(DomainError):
        digging.summarize_trials([source])


def test_no_fixed_result_word_or_control_procedure_becomes_a_followup():
    sources = [
        document("placebo", name="Placebo"),
        document("generic", name="CAR-T cells"),
        document("device", name="DEVICE-123", kind="DEVICE"),
        document("risk-word", name="TERMINATED"),
    ]
    assert digging.select_followups(("BCMA", "myeloma"), sources) == []
    with pytest.raises(ValueError):
        digging.select_followups(("BCMA", "bcma"), sources)
    with pytest.raises(ValueError):
        digging.select_followups(("BCMA", "myeloma"), sources, max_followups=3)


def test_registry_status_result_unknown_and_literal_differences_stay_separate():
    stopped = document(status="TERMINATED", phases=["PHASE1"])
    completed = document("second", status="COMPLETED", phases=[])
    completed["hasResults"] = False  # Outside the permitted protocol projection.
    report = digging.summarize_trials([stopped, completed])
    assert report["registered_stoppage_record_count"] == 1
    assert report["distinct_reported_registration_ids"] == 1
    assert report["same_registration_documents"][0]["differing_fields"] == ["overall_status", "phases"]
    by_id = {row["document_uuid"]: row for row in report["rows"]}
    assert "stoppage_reason_not_supplied" in by_id[stopped["uuid"]]["gaps"]
    assert by_id[completed["uuid"]]["phases"] == []
    assert "missing_phases" not in by_id[completed["uuid"]]["gaps"]
    assert all(row["results_availability"] == "unknown_not_exposed" for row in report["rows"])
    assert all("professional_outcome_evaluation_not_connected" in row["gaps"] for row in report["rows"])
    assert {item["field"] for item in report["field_differences"]} == {"overall_status", "phases"}


def test_missing_or_unclassified_registry_status_is_not_false_non_stoppage():
    sources = [
        document("missing-status", status=None),
        document("unknown-status", status="UNKNOWN"),
        document("new-status", status="UNRECOGNIZED_SOURCE_STATUS"),
    ]
    report = digging.summarize_trials(sources)
    assert all(row["registered_stoppage_status"] is None for row in report["rows"])
    assert report["registered_stoppage_record_count"] == 0
    assert report["unclassified_stoppage_status_record_count"] == 3


def test_missing_phase_is_unknown_and_array_order_is_not_a_research_difference():
    first = document(phases=["PHASE1", "PHASE2"])
    second = document("second", phases=["PHASE2", "PHASE1"], nct_id="NCT00000002")
    assert digging.summarize_trials([first, second])["field_differences"] == []
    missing = digging.summarize_trials([document()])["rows"][0]
    assert missing["phases"] is None and "missing_phases" in missing["gaps"]
    second["projection"]["facts"]["nct_id"] = "NCT00000001"
    protocol = json.loads(second["projection"]["text"])
    protocol["identificationModule"]["nctId"] = "NCT00000001"
    second["projection"]["text"] = json.dumps(protocol, ensure_ascii=False, indent=1)
    second["projection"]["locator"]["end"] = 100 + len(second["projection"]["text"])
    assert (
        digging.summarize_trials([first, second])["same_registration_documents"][0]["differing_fields"] == []
    )


def test_dates_remain_bound_source_date_and_type_without_invented_timestamps():
    start = {"date": "2027-04", "type": "ESTIMATED"}
    update = {"date": "2026-10-01", "type": "ACTUAL"}
    source = document(start_date=start, last_update_date=update)
    row = digging.summarize_trials([source])["rows"][0]
    assert row["start_date"] == start and row["last_update_post_date"] == update
    assert row["study_first_post_date"] is None and row["completion_date"] is None
    assert row["dates_are_source_declarations_not_verified_event_times"] is True
    source["projection"]["facts"]["start_date"]["type"] = "ACTUAL"
    with pytest.raises(DomainError):
        digging.summarize_trials([source])


def storage(monkeypatch, *, fail_material_write=False, existing_revision=1):
    """Replace only PostgreSQL persistence; request validation remains the real DTO."""
    saved, create_keys = [], []
    state = {
        "id": str(uuid5(NAMESPACE_DNS, "synthetic-investigation")),
        "revision": existing_revision,
        "status": "open",
        "questions": [{"question_id": str(index)} for index in range(3)],
        "entries": [],
    }

    def create(actor, request, key):
        create_keys.append(key)
        assert request.is_demo is False
        return 201, {**copy.deepcopy(state), "revision": 1}

    def append(actor, identifier, request, key):
        payload = request.root.model_dump(mode="json")
        assert payload["expected_revision"] == state["revision"]
        saved.append(payload)
        if fail_material_write and payload["kind"] == "material":
            raise RuntimeError("synthetic uncertain PostgreSQL write outcome")
        state["revision"] += 1
        entry_id = str(uuid5(NAMESPACE_DNS, "entry-" + str(state["revision"])))
        state["entries"].append({"id": entry_id, "payload": payload})
        if payload["kind"] == "status":
            state["status"] = payload["status"]
        return 201, copy.deepcopy(state)

    runtime = SimpleNamespace(
        create=create,
        append=append,
        get=lambda *args: SimpleNamespace(
            revision=state["revision"],
            model_dump=lambda **kwargs: copy.deepcopy(state),
        ),
    )
    monkeypatch.setattr(digging, "InvestigationService", lambda engine: runtime)
    engine = SimpleNamespace(
        adopt_snapshot=Mock(
            side_effect=lambda *args: (
                201,
                {"id": str(uuid5(NAMESPACE_DNS, "snapshot-" + str(len(saved))))},
            )
        )
    )
    return engine, saved, state, create_keys


def reader(documents, searches, *, fail_document=None):
    """Exercise the actual adapter against a synthetic HTTP transport."""
    calls, search_calls = [], []
    by_id = {row["uuid"]: row for row in documents}

    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/search"):
            terms = request.url.params.get_list("terms")
            search_calls.append(terms)
            found = searches[len(search_calls) - 1]
            if isinstance(found, Exception):
                raise found
            return httpx.Response(
                200,
                json={
                    "schema_version": "datafabric.research.search.v1",
                    "source_site": "clinicaltrials",
                    "scope": "title_literal_and_current_db",
                    "terms": terms,
                    "limit": int(request.url.params["limit"]),
                    "has_more": True,
                    "observed_at": "2026-10-16T00:00:00Z",
                    "items": [
                        {"uuid": row["uuid"], "title": row["title"], "source_site": "clinicaltrials"}
                        for row in found
                    ],
                },
            )
        identifier = request.url.path.rsplit("/", 1)[-1]
        if identifier == fail_document:
            raise httpx.ConnectError("DO-NOT-EXPOSE provider credential")
        return httpx.Response(
            200,
            json={
                "schema_version": "datafabric.research.document.v1",
                **by_id[identifier],
            },
        )

    client = httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(handle))
    adapter = DataFabricResearchReader(
        client,
        source_instance="datafabric-test",
        authorization_ref="development:development:sources",
        allowed_source_sites={"clinicaltrials"},
    )
    return adapter, calls, search_calls


def test_bounded_pass_follows_source_names_saves_quotes_and_never_writes_judgment(monkeypatch):
    first = document()
    second = document("second", name="LCAR-BCX", status="TERMINATED", why_stopped="TEST: recruitment stopped")
    engine, saved, state, keys = storage(monkeypatch)
    adapter, calls, queries = reader([first, second], [[first], [second], []])
    report = digging.run_bounded_investigation(
        engine,
        object(),
        adapter,
        question="SYNTHETIC: compare registered fields",
        execution_id="fixed-execution",
    )
    assert keys == ["digging:fixed-execution"]
    assert report["execution_id"] == "fixed-execution"
    assert report["completion_status"] == "completed_with_gaps"
    assert report["followups_executed"] == 2
    assert queries == [["BCMA", "myeloma"], ["BCMA", "myeloma", "GPRC5D"], ["BCMA", "myeloma", "LCAR-BCX"]]
    assert report["queries"][-1]["status"] == "empty"
    assert all(row["has_more"] is True for row in report["queries"])
    trigger = report["queries"][1]["trigger"]
    assert trigger["field_value"][trigger["value_start"] : trigger["value_end"]] == "GPRC5D"
    assert trigger["material_entry_id"] in report["material_entry_ids"]
    assert trigger["record_ref"]["object_id"] == first["uuid"]
    assert len(calls) == 5 and engine.adopt_snapshot.call_count == 2
    materials = [entry for entry in saved if entry["kind"] == "material"]
    assert len(materials) == 2 and all(entry["stance"] == "context" for entry in materials)
    assert materials[0]["quote"] in first["projection"]["text"]
    assert materials[1]["quote"] in second["projection"]["text"]
    assert not any(entry["kind"] == "judgment" for entry in saved)
    assert state["status"] == "paused"
    assert saved[-1]["question_updates"][-1]["status"] == "blocked"
    assert all(row["snapshot_id"] for row in report["summary"]["rows"])
    adapter.client.close()


def test_read_failure_preserves_successful_material_and_is_not_an_empty_result(monkeypatch):
    first, broken = document(), document("broken")
    engine, saved, _, _ = storage(monkeypatch)
    adapter, _, queries = reader([first, broken], [[first, broken]], fail_document=broken["uuid"])
    report = digging.run_bounded_investigation(engine, object(), adapter, question="SYNTHETIC failure probe")
    assert report["completion_status"] == "partial"
    assert report["queries"][0]["status"] == "partial"
    assert report["queries"][0]["error_code"] == "provider_unavailable"
    assert len(queries) == 1 and engine.adopt_snapshot.call_count == 1
    assert len([entry for entry in saved if entry["kind"] == "material"]) == 1
    assert "DO-NOT-EXPOSE" not in json.dumps(saved)
    assert "DO-NOT-EXPOSE" not in json.dumps(report)
    adapter.client.close()


def test_unknown_postgresql_write_outcome_propagates_without_retry_or_next_query(monkeypatch):
    first = document()
    engine, saved, _, _ = storage(monkeypatch, fail_material_write=True)
    adapter, calls, queries = reader([first], [[first]])
    with pytest.raises(RuntimeError, match="uncertain PostgreSQL"):
        digging.run_bounded_investigation(engine, object(), adapter, question="SYNTHETIC write probe")
    assert len(queries) == 1 and len(calls) == 2 and engine.adopt_snapshot.call_count == 1
    assert sum(entry["kind"] == "material" for entry in saved) == 1
    assert saved[-1]["kind"] == "material"  # No false completion/status is saved after the failure.
    adapter.client.close()


def test_reusing_execution_rechecks_current_revision_not_cached_create_receipt(monkeypatch):
    engine, saved, _, keys = storage(monkeypatch, existing_revision=8)
    adapter, calls, _ = reader([], [])
    with pytest.raises(DomainError) as error:
        digging.run_bounded_investigation(
            engine,
            object(),
            adapter,
            question="SYNTHETIC replay",
            execution_id="already-used",
        )
    assert error.value.code == "digging_execution_already_started"
    assert keys == ["digging:already-used"] and saved == [] and calls == []
    engine.adopt_snapshot.assert_not_called()
    adapter.client.close()


def test_projection_unavailable_empty_search_and_network_failure_remain_distinct(monkeypatch):
    unavailable = document()
    unavailable["projection"] = {
        "version": "clinicaltrials.protocol.v1",
        "status": "unavailable",
        "reason": "TEST: protocol missing",
        "facts": None,
        "text": None,
        "locator": None,
        "field_paths": None,
    }
    for found, expected_query, expected_records in (([], "empty", 0), ([unavailable], "completed", 1)):
        engine, _, _, _ = storage(monkeypatch)
        adapter, _, _ = reader([unavailable], [found])
        report = digging.run_bounded_investigation(
            engine, object(), adapter, question="SYNTHETIC unknown probe"
        )
        assert report["completion_status"] == "partial"
        assert report["queries"][0]["status"] == expected_query
        assert report["summary"]["record_count"] == expected_records
        engine.adopt_snapshot.assert_not_called()
        adapter.client.close()
    engine, _, _, _ = storage(monkeypatch)
    adapter, _, _ = reader([], [httpx.ConnectError("TEST unavailable")])
    report = digging.run_bounded_investigation(engine, object(), adapter, question="SYNTHETIC failed probe")
    assert report["completion_status"] == "failed" and report["queries"][0]["status"] == "failed"
    adapter.client.close()


def test_document_budget_and_duplicate_hits_do_not_repeat_source_reads(monkeypatch):
    first = document()
    engine, _, _, _ = storage(monkeypatch)
    adapter, calls, _ = reader([first], [[first]])
    report = digging.run_bounded_investigation(
        engine,
        object(),
        adapter,
        question="SYNTHETIC budget probe",
        max_documents=1,
    )
    assert len(calls) == 2 and report["completion_status"] == "partial"
    adapter.client.close()
    engine, _, _, _ = storage(monkeypatch)
    adapter, calls, _ = reader([first], [[first], [first]])
    report = digging.run_bounded_investigation(
        engine, object(), adapter, question="SYNTHETIC repeated hit probe"
    )
    assert len(calls) == 3 and report["queries"][1]["reused_records"] == 1
    assert report["summary"]["record_count"] == 1 and engine.adopt_snapshot.call_count == 1
    adapter.client.close()
