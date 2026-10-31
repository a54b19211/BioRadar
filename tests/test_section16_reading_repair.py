"""Section 16 compact repair boundaries, using the host ledger and synthetic inputs only."""

import copy

import pytest
from bioradar.api.errors import DomainError
from bioradar.research.investigations.reading_execution import validated_policy_output
from test_reading_reference_repair import setup


def test_repair_input_is_bounded_by_the_actual_original_prompt_tokens(monkeypatch):
    service, fixed, progress, calls, _ = setup(monkeypatch)
    invoke = service.model_call

    def small_actual_prompt(settings, **kwargs):
        completion = invoke(settings, **kwargs)
        completion.receipt.usage.prompt_tokens = 100
        completion.receipt.usage.total_tokens = 150
        return completion

    service.model_call = small_actual_prompt
    previous = copy.deepcopy(progress["query_policy"]["reading"])
    assert not service._policy_reading("SYNTHETIC", "token", fixed, progress, optional=True)
    assert [c["stage"] for c in calls] == ["read_plan"]
    entry = next(iter(progress["reading_repairs"].values()))
    assert entry["status"] == "not_sent"
    assert entry["original_input_token_estimate"] == {"tokens": 100, "basis": "received_prompt_tokens"}
    assert entry["repair_input_token_estimate"]["tokens"] > 20
    assert entry["max_input_ratio"] == 0.2
    assert progress["query_policy"]["reading"] == previous
    assert not service._policy_reading("SYNTHETIC", "token", fixed, progress, optional=True)
    assert len(calls) == 1


def test_one_compact_repair_per_analysis_cannot_restart_for_a_later_read(monkeypatch):
    service, fixed, progress, calls, bad = setup(monkeypatch)
    assert service._policy_reading("SYNTHETIC", "token", fixed, progress, optional=True)
    original = copy.deepcopy(progress["calls"][0])
    original.update(call_key="SYNTHETIC-later-reading", call_id="SYNTHETIC-later")
    progress["calls"].append(original)
    count = len(calls)
    with pytest.raises(DomainError) as failure:
        validated_policy_output(
            service,
            "SYNTHETIC",
            "token",
            fixed,
            progress,
            bad,
            original["payload"],
            original["call_key"],
            [],
        )
    assert failure.value.code == "analysis_passage_invalid"
    assert len(calls) == count
    later = progress["reading_repairs"][original["call_key"]]
    assert later["status"] == "invalid"
    assert later["repair_error_code"] == "analysis_reading_repair_exhausted"
    assert progress["calls"][0]["output"] == bad


def test_field_patch_cannot_change_an_undamaged_claim_or_hide_raw_output(monkeypatch):
    service, fixed, progress, calls, bad = setup(monkeypatch)
    invoke = service.model_call

    def rewrites_text(settings, **kwargs):
        result = invoke(settings, **kwargs)
        if kwargs["stage"] == "read_plan_repair":
            result.parsed["changes"].append(
                {
                    "path": ["claims", 0, "text"],
                    "operation": "replace",
                    "value": "SYNTHETIC altered judgment",
                }
            )
        return result

    service.model_call = rewrites_text
    previous = copy.deepcopy(progress["query_policy"]["reading"])
    assert not service._policy_reading("SYNTHETIC", "token", fixed, progress, optional=True)
    assert progress["calls"][0]["output"] == bad
    assert progress["query_policy"]["reading"] == previous
    assert next(iter(progress["reading_repairs"].values()))["status"] == "invalid"
    assert len(calls) == 2


@pytest.mark.parametrize("schema", ["query-planner.external.v2", "query-planner.external.v1"])
def test_new_kdd_fixed_document_read_is_not_a_new_search_query(monkeypatch, schema):
    from bioradar.research.investigations.reading_execution import execute_document_read

    service, fixed, progress, _, _ = setup(monkeypatch)
    service.reader = None
    service._authorize_query_sources = lambda *args: None
    fixed.update(is_demo=False)
    fixed["query_policy"].update(schema_version=schema, direction="research")
    fixed["budget"]["max_queries"] = 1
    progress["queries"] = [{"kind": "document_search", "status": "empty", "provider": "fabric"}]
    scope = {
        "direction": "research",
        "as_of": fixed["evidence_time"]["cutoff"],
        "mode": "current",
        "is_demo": False,
    }
    record = {"source_record_id": "SYNTHETIC-source", "version_id": "fixed-v1"}
    action = {
        "id": "SYNTHETIC-document-read",
        "kind": "document_read",
        "arguments": {
            "provider": "fabric",
            "source_id": "synthetic-source",
            "scope": scope,
            "records": [record],
        },
    }
    progress["query_policy"]["decisions"] = [
        {
            "policy_request": {
                "reading_manifest": [
                    {
                        **record,
                        "source_id": "synthetic-source",
                        "status": "unread",
                    }
                ]
            }
        }
    ]
    if schema.endswith("v2"):
        assert (
            execute_document_read(service, "SYNTHETIC", "token", fixed, progress, action, 0)["status"]
            == "unavailable"
        )
    else:
        with pytest.raises(DomainError) as failure:
            execute_document_read(service, "SYNTHETIC", "token", fixed, progress, action, 0)
        assert failure.value.code == "analysis_query_budget"
