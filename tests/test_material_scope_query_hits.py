"""Projection-only unread hits stay distinct from failed source requests."""

import copy
import json

from bioradar.research.investigations.kdd import material_scope, scope_markdown


def test_successful_budget_limited_hits_are_fixed_unread_not_source_failures():
    def ref(name, version):
        return {
            "provider": "data_fabric_history",
            "object_type": "source_record",
            "object_id": json.dumps(["who_don", name, False]),
            "adopted_version": version * 64,
            "content_hash": "a" * 64,
            "version_source": "provider",
            "authorization_ref": "SYNTHETIC projection only",
        }

    def original(name, version):
        return {
            "source_id": "who_don",
            "source_record_id": name,
            "version_id": version * 64,
            "raw_sha256": "a" * 64,
        }

    read, previous_unread = ref("read", "1"), ref("fixed-unread", "2")
    hit = original("new-hit", "3")
    fixed = {
        "query_policy": {"schema_version": "query-planner.external.v2"},
        "evidence": [{"record_ref": read}, {"record_ref": previous_unread}],
    }
    progress = {
        "evidence": [{"record_ref": read}],
        "queries": [
            {
                "provider": "fabric",
                "status": "read",
                "action_id": "actual-query",
                "plan": {"source_id": "who_don"},
                "records": [hit, copy.deepcopy(hit), original("read", "1")],
                "usage": {"adoption_stop_reason": "analysis_evidence_budget"},
            },
            {
                "provider": "fabric",
                "status": "unavailable",
                "plan": {"source_id": "who_don"},
                "error_code": "history_source_unavailable",
                "records": [original("not-a-successful-hit", "4")],
            },
        ],
    }
    before = copy.deepcopy((fixed, progress))
    scope = material_scope(fixed, progress)
    assert scope["fixed_version_count"] == 3 and scope["read_version_count"] == 1
    assert len(scope["unread_versions"]) == 2
    unread_hit = next(x for x in scope["unread_versions"] if x["source_record_id"] == "new-hit")
    assert unread_hit["query_hit"] is True
    assert unread_hit["adoption_stop_reason"] == "analysis_evidence_budget"
    assert unread_hit["query_action_ids"] == ["actual-query"]
    assert unread_hit["version_id"] == hit["version_id"] and unread_hit["raw_sha256"] == hit["raw_sha256"]
    assert "record_ref" not in unread_hit  # No synthetic grant/adoption is created by a view projection.
    assert len(scope["source_failures"]) == 1
    assert scope["source_failures"][0]["status"] == "unavailable"
    text = scope_markdown(scope)
    assert "成功补查命中但未读：who_don:new-hit（本轮原文片段容量已满；analysis_evidence_budget）" in text
    assert text.endswith("\n\n---\n\n")
    assert (fixed, progress) == before


def test_full_fixed_versions_include_superseded_inputs_without_rewriting_saved_model_scope():
    def ref(name, version, grant="SYNTHETIC saved grant"):
        return {
            "provider": "data_fabric_history",
            "object_type": "source_record",
            "object_id": json.dumps(["who_don", name, False]),
            "adopted_version": version * 64,
            "content_hash": "a" * 64,
            "version_source": "provider",
            "authorization_ref": grant,
        }

    historical, current, excluded = ref("notice", "1"), ref("notice", "2"), ref("other", "3")
    read_alias = ref("notice", "2", "SYNTHETIC second saved grant")
    fixed_refs = [historical, current, excluded, copy.deepcopy(current)]
    fixed = {
        "query_policy": {"schema_version": "query-planner.external.v2"},
        "evidence": [{"record_ref": current}],
    }
    preparation = {
        "input_count": 3,
        # Two distinct analysis units can reuse one current original. Counting
        # these rows as input identities would silently lose its historical version.
        "units": [
            {"members": [{"record_ref": current}]},
            {"members": [{"record_ref": current}]},
        ],
        "excluded_records": [{"record_ref": excluded, "reason": "not_this_direction"}],
    }
    progress = {
        "evidence": [{"record_ref": current}, {"record_ref": read_alias}],
        "queries": [],
        "kdd_source_artifact_id": "sources:fixed",
        "kdd_artifacts": [
            {"id": "dataset:current", "kind": "analysis_dataset", "source_artifact_id": "sources:fixed"}
        ],
        "kdd_results": {"dataset:current": preparation},
        "report_research_scopes": {"0": {"material_scope": {"fixed_version_count": 3, "legacy": True}}},
    }
    before = copy.deepcopy((fixed, progress, fixed_refs))
    scope = material_scope(fixed, progress, fixed_version_refs=fixed_refs)
    assert scope["fixed_version_count"] == 3 and scope["read_version_count"] == 1
    assert len(scope["unread_versions"]) == 2
    assert {x["version_id"] for x in scope["unread_versions"]} == {"1" * 64, "3" * 64}
    historical_unread = next(x for x in scope["unread_versions"] if x["version_id"] == "1" * 64)
    assert historical_unread["record_ref"] == historical
    assert scope["fixed_version_count"] == scope["read_version_count"] + len(scope["unread_versions"])
    assert scope["source_failures"] == []
    assert (fixed, progress, fixed_refs) == before
    prepared = copy.deepcopy(progress)
    prepared["kdd_source_version_refs"] = fixed_refs
    assert material_scope(fixed, prepared) == scope
