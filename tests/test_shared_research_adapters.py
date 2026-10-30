"""Shared-provider boundaries: no real models, private credentials or upstream writes."""

import copy
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters.biocheck import (
    build_packet,
    check_biocheck,
    run_biocheck,
)
from bioradar.adapters.biocheck import (
    provider_status as biocheck_status,
)
from bioradar.adapters.weknora import search_candidates
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from pydantic import ValidationError

pytestmark = pytest.mark.ENGINE


@pytest.fixture
def installed_biocheck(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "__init__.py").write_text("raise RuntimeError('Must not execute package init')")
    (app / "model.py").write_text("raise RuntimeError('Must not load model client')")
    (app / "evidence.py").write_text("# SYNTHETIC pure evidence dependency\n")
    (app / "prompts.py").write_text("INSTRUCTION = 'SYNTHETIC provider instruction'\n")
    (app / "core.py").write_text(
        "from .prompts import INSTRUCTION\n"
        "def generate(packet, call, method='bound'):\n"
        "    plan = call('plan_' + method, INSTRUCTION, packet)\n"
        "    value = call('write_' + method, INSTRUCTION, packet)\n"
        "    return dict(value, plan=plan)\n"
        "def verify(packet, draft, call):\n"
        "    return call('verify', INSTRUCTION, dict(packet, draft=draft))\n"
        "def revise(packet, review, call):\n"
        "    return call('revise', INSTRUCTION, dict(packet, review=review))\n"
    )
    return Settings(biocheck_project_dir=tmp_path)


def packet():
    return build_packet(
        "SYNTHETIC question",
        [
            {
                "key": "e1",
                "text": "SYNTHETIC evidence",
                "start": 0,
                "end": 18,
                "text_basis": "source_clean_text",
                "record_ref": {"adopted_version": "fixed-v1", "object_id": "native-1"},
            }
        ],
        {"false_value": False, "zero": 0, "unknown": None},
    )


def review(draft, *, status="PASS", problems=()):
    return {
        "status": status,
        "draft": draft,
        "problem_units": list(problems),
        "units": [],
        "coverage": {"verdict": "complete"},
    }


def test_packet_preserves_selection_versions_and_unknown_values_without_mutation():
    original = packet()
    assert original["evidence"][0]["id"] == "e1"
    assert original["evidence"][0]["version"] == "fixed-v1"
    assert original["evidence_map"]["e1"]["start"] == 0
    assert original["evidence_map"]["e1"]["record_ref"]["object_id"] == "native-1"
    assert original["assessment"] == {"false_value": False, "zero": 0, "unknown": None}
    with pytest.raises(DomainError) as failure:
        build_packet("question", [{"key": "e1", "text": "No immutable version"}])
    assert failure.value.code == "provider_input_invalid"


def test_biocheck_revision_rechecks_actual_text_and_has_a_distinct_durable_stage(installed_biocheck):
    stages = []
    data = packet()
    before = copy.deepcopy(data)

    def call(stage, instruction, payload):
        stages.append(stage)
        assert instruction == "SYNTHETIC provider instruction"
        if stage == "biocheck_check":
            return review(payload["draft"], status="FAIL", problems=["U1"])
        if stage == "biocheck_revise":
            return {"draft": "Corrected [e1]", "changed_units": ["U1"]}
        return review(payload["draft"])

    result = check_biocheck(installed_biocheck, data, "Wrong [e1]", call)
    assert stages == ["biocheck_check", "biocheck_revise", "biocheck_recheck"]
    assert result["status"] == "PASS"
    assert result["final_text"] == result["verification"]["draft"] == "Corrected [e1]"
    assert result["initial_verification"]["draft"] == "Wrong [e1]"
    assert result["text_sha256"] == hashlib.sha256(b"Corrected [e1]").hexdigest()
    assert result["evidence_map"] == before["evidence_map"] and data == before
    assert not (installed_biocheck.biocheck_project_dir / "app" / "__pycache__").exists()


def test_biocheck_run_uses_upstream_generation_and_the_same_callback(installed_biocheck):
    stages = []

    def call(stage, instruction, payload):
        stages.append(stage)
        if stage == "biocheck_plan":
            return {"points": ["SYNTHETIC"]}
        if stage == "biocheck_write":
            return {"draft": "SYNTHETIC answer [e1]"}
        return review(payload["draft"])

    result = run_biocheck(installed_biocheck, packet(), call)
    assert stages == ["biocheck_plan", "biocheck_write", "biocheck_check"]
    assert result["status"] == "PASS" and result["generated"]["plan"]["points"]


def test_unavailable_provider_never_calls_a_model_or_claims_a_pass(tmp_path):
    settings = Settings(biocheck_project_dir=tmp_path)
    calls = []
    result = check_biocheck(settings, packet(), "Draft [e1]", lambda *a: calls.append(a))
    assert not biocheck_status(settings)["available"]
    assert result["status"] == "unavailable" and result["verification"] is None
    assert result["final_text"] == "Draft [e1]" and not calls


def test_biocheck_review_cannot_be_reused_for_different_delivered_text(installed_biocheck):
    result = check_biocheck(
        installed_biocheck,
        packet(),
        "Actual [e1]",
        lambda *a: review("Different draft containing PRIVATE-SOURCE"),
    )
    assert result["status"] == "failed" and result["verification"] is None
    assert "PRIVATE-SOURCE" not in json.dumps(result)


def test_biocheck_budget_and_unknown_outcomes_propagate_without_retry(installed_biocheck):
    stages = []

    def call(stage, instruction, payload):
        stages.append(stage)
        raise DomainError("model_call_outcome_unconfirmed", "Unknown outcome", 409)

    with pytest.raises(DomainError) as failure:
        check_biocheck(installed_biocheck, packet(), "Draft [e1]", call)
    assert failure.value.code == "model_call_outcome_unconfirmed"
    assert stages == ["biocheck_check"]


def test_biocheck_coverage_gap_does_not_trigger_an_unbounded_rewrite(installed_biocheck):
    stages = []

    def call(stage, instruction, payload):
        stages.append(stage)
        return review(payload["draft"], status="REVIEW")

    result = check_biocheck(installed_biocheck, packet(), "Limited [e1]", call)
    assert result["status"] == "REVIEW"
    assert stages == ["biocheck_check"] and result["revision"] is None


def test_provider_source_changes_have_a_new_version_without_importing_model(installed_biocheck):
    before = biocheck_status(installed_biocheck)["version"]
    source = installed_biocheck.biocheck_project_dir / "app" / "prompts.py"
    source.write_text("INSTRUCTION = 'Another SYNTHETIC instruction'\n")
    assert biocheck_status(installed_biocheck)["version"] != before


def weknora_settings():
    return Settings(
        weknora_url="http://127.0.0.1:8080",
        weknora_api_key="SYNTHETIC-SERVICE-KEY",
        weknora_knowledge_base_ids=["kb-synthetic"],
    )


class FabricReader:
    def __init__(self):
        self.queries, self.reads = [], []
        self.original = {
            "source_id": "legacy_documents",
            "source_record_id": "legacy:pubmed:synthetic-uuid",
            "is_demo": False,
            "version_id": "a" * 64,
            "raw_sha256": "b" * 64,
            "facts": {"source_site": "pubmed"},
            "raw_content": "Fixed Fabric original",
        }

    def _get(self, path, params):
        self.queries.append((path, params))
        return {"schema_version": "datafabric.history.collection.v1", "items": [self.original]}

    def read_version(self, binding, *, mode, cutoff):
        self.reads.append(binding)
        return copy.deepcopy(self.original)

    def ref(self, original):
        return SimpleNamespace(model_dump=lambda **kw: {"adopted_version": original["version_id"]})


def test_weknora_candidates_bind_to_exact_fabric_record_and_ignore_chunk_offsets():
    requests = []
    reader = FabricReader()

    def handler(request):
        requests.append(request)
        assert request.headers["X-API-Key"] == "SYNTHETIC-SERVICE-KEY"
        assert json.loads(request.content) == {
            "query": "SYNTHETIC query",
            "knowledge_base_ids": ["kb-synthetic"],
        }
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "id": "chunk-1",
                        "knowledge_id": "k-1",
                        "start_at": 987,
                        "content": "Stale or independently parsed snippet",
                        "metadata": {"source_site": "pubmed", "source_uuid": "synthetic-uuid"},
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = search_candidates(
            weknora_settings(),
            "SYNTHETIC query",
            reader=reader,
            source_ids=["legacy_documents", "pubmed"],
            cutoff=datetime.now(UTC),
            client=client,
        )
    assert len(requests) == 1 and len(result["records"]) == 1
    assert reader.queries[0][1]["source_record_ids"] == ["legacy:pubmed:synthetic-uuid"]
    assert reader.reads[0]["version_id"] == "a" * 64
    assert result["candidates"][0]["binding_status"] == "bound"
    assert result["candidates"][0]["search_version_verified"] is False
    assert "content" not in result["candidates"][0] and "start_at" not in result["candidates"][0]
    assert result["usage"]["embedding_rerank_tokens"] is None
    assert result["lookup_requests"] == result["version_reads"] == 1
    assert reader.records_read == 1


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({}, "unbound"),
        ({"source_site": "pubmed", "source_uuid": "synthetic-uuid"}, "not_authorized"),
    ],
)
def test_weknora_unmatched_or_unselected_candidate_is_never_adopted(metadata, expected):
    reader = FabricReader()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [{"id": "chunk", "content": "PRIVATE-SNIPPET", "metadata": metadata}],
                },
            )
        )
    ) as client:
        result = search_candidates(
            weknora_settings(),
            "SYNTHETIC",
            reader=reader,
            source_ids=["pubmed"],
            client=client,
        )
    assert result["candidates"][0]["binding_status"] == expected
    assert not result["records"] and not reader.reads and not reader.queries
    assert "PRIVATE-SNIPPET" not in json.dumps(result)


def test_weknora_hash_mismatch_does_not_adopt_a_different_version():
    reader = FabricReader()
    metadata = {
        "source_id": "legacy_documents",
        "source_record_id": reader.original["source_record_id"],
        "version_id": "a" * 64,
        "raw_sha256": "c" * 64,
    }
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [{"id": "chunk", "metadata": metadata}],
                },
            )
        )
    ) as client:
        result = search_candidates(
            weknora_settings(),
            "SYNTHETIC",
            reader=reader,
            source_ids=["legacy_documents", "pubmed"],
            client=client,
        )
    assert result["candidates"][0]["binding_reason"] == "version_hash_mismatch"
    assert not result["records"] and not reader.queries


def test_weknora_transport_unknown_is_not_empty_and_is_never_retried():
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("PRIVATE-DETAIL", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = search_candidates(weknora_settings(), "SYNTHETIC", client=client)
    assert result["status"] == "unconfirmed" and len(requests) == 1
    assert "PRIVATE-DETAIL" not in json.dumps(result)


def test_weknora_http_error_does_not_echo_private_body_or_credentials():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                401,
                json={"detail": "PRIVATE-DETAIL"},
            )
        )
    ) as client:
        result = search_candidates(weknora_settings(), "SYNTHETIC", client=client)
    assert result["status"] == "failed" and result["http_status"] == 401
    assert "PRIVATE-DETAIL" not in json.dumps(result)
    assert "SYNTHETIC-SERVICE-KEY" not in json.dumps(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"analysis_max_calls": 33},
        {"analysis_max_queries": 33},
        {"analysis_max_tokens": 10_000_001},
        {"analysis_max_seconds": 1801},
        {"weknora_url": "http://:password@localhost"},
        {"weknora_url": "http://localhost:invalid"},
        {"weknora_url": "http://localhost/\n"},
        {"biocheck_project_dir": "relative-provider"},
    ],
)
def test_shared_provider_settings_cannot_escape_local_scope_or_global_ceiling(changes):
    with pytest.raises(ValidationError):
        Settings(**changes)


@pytest.fixture
def installed_mech(tmp_path):
    package = tmp_path / "mechevidence"
    package.mkdir()
    (package / "__init__.py").write_text("raise RuntimeError('Do not load package init')")
    (package / "model.py").write_text("raise RuntimeError('Do not load model client')")
    (package / "models.py").write_text(
        "from pydantic import BaseModel\n"
        "class Case(BaseModel):\n"
        "    question: str\n"
        "    mechanism: str\n"
        "    scope: dict\n"
        "    materials: list[dict]\n"
    )
    (package / "assess.py").write_text(
        "def evaluate(case, call_json):\n"
        "    try:\n"
        "        value = call_json('local_review', 'SYNTHETIC instructions',\n"
        "            case.model_dump(), {'type': 'object', 'required': ['observations']})\n"
        "    except Exception as exc:\n"
        "        raise RuntimeError('Upstream wraps callback errors') from exc\n"
        "    return {'overall': {'verdict': 'insufficient'}, 'local_review': value}\n"
    )
    return Settings(mech_evidence_project_dir=tmp_path)


def mechanism_packet(*, kind="primary"):
    return build_packet(
        "SYNTHETIC causal question",
        [
            {
                "key": "e1",
                "text": "SYNTHETIC experiment",
                "source_kind": kind,
                "record_ref": {
                    "adopted_version": "fixed-v1",
                    "object_id": '["pubmed","123456",false]',
                },
            }
        ],
        {"mechanism": "SYNTHETIC A causes B", "mechanism_scope": {"species": "human"}},
    )


def test_mechanism_uses_original_strict_schema_on_the_shared_callback(installed_mech):
    from bioradar.adapters.mech_evidence import evaluate_mechanism, provider_status

    calls = []

    def call(stage, instruction, payload, schema):
        calls.append((stage, instruction, payload, schema))
        return {"observations": []}

    result = evaluate_mechanism(installed_mech, mechanism_packet(), call)
    assert provider_status(installed_mech)["available"]
    assert result["status"] == "completed" and result["overall"]["verdict"] == "insufficient"
    assert calls[0][0] == "mech_local_review"
    assert calls[0][3] == {"type": "object", "required": ["observations"]}
    material = calls[0][2]["materials"][0]
    assert material["record_id"] == "123456" and material["version"] == "fixed-v1"
    assert material["text_level"] == "selected_passages"
    assert not (installed_mech.mech_evidence_project_dir / "mechevidence" / "__pycache__").exists()


def test_mechanism_missing_source_kind_is_needs_input_and_never_assumed_primary(installed_mech):
    from bioradar.adapters.mech_evidence import evaluate_mechanism

    calls = []
    result = evaluate_mechanism(installed_mech, mechanism_packet(kind=None), lambda *a: calls.append(a))
    assert result["status"] == "needs_input" and not calls
    assert result["missing_fields"] == ["materials.e1.source_kind"]


def test_nonmechanism_question_does_not_invent_a_causal_candidate(installed_mech):
    from bioradar.adapters.mech_evidence import evaluate_mechanism

    calls = []
    result = evaluate_mechanism(installed_mech, packet(), lambda *a: calls.append(a))
    assert result["status"] == "not_requested" and result["overall"] is None and not calls


def test_mechanism_recovers_unknown_outcome_wrapped_by_upstream_without_retry(installed_mech):
    from bioradar.adapters.mech_evidence import evaluate_mechanism

    calls = []

    def call(*args):
        calls.append(args)
        raise DomainError("model_call_outcome_unconfirmed", "Unknown model outcome", 409)

    with pytest.raises(DomainError) as failure:
        evaluate_mechanism(installed_mech, mechanism_packet(), call)
    assert failure.value.code == "model_call_outcome_unconfirmed" and len(calls) == 1


def test_explicit_mechanism_case_cannot_replace_the_fixed_original(installed_mech):
    from bioradar.adapters.mech_evidence import evaluate_mechanism

    data = mechanism_packet()
    data["mechanism_case"] = {
        "question": data["question"],
        "mechanism": "SYNTHETIC",
        "scope": {},
        "materials": [{"id": "e1", "text": "Unadopted original", "version": "fixed-v1"}],
    }
    calls = []
    result = evaluate_mechanism(installed_mech, data, lambda *a: calls.append(a))
    assert result["status"] == "needs_input"
    assert result["error_code"] == "mech_evidence_material_mismatch" and not calls


def test_run_biocheck_can_respect_a_revision_already_used_by_the_investigation(installed_biocheck):
    stages = []

    def call(stage, instruction, payload):
        stages.append(stage)
        if stage == "biocheck_plan":
            return {"points": ["SYNTHETIC"]}
        if stage == "biocheck_write":
            return {"draft": "Unresolved [e1]"}
        return review(payload["draft"], status="FAIL", problems=["U1"])

    result = run_biocheck(installed_biocheck, packet(), call, repair=False)
    assert result["status"] == "FAIL" and result["revision"] is None
    assert stages == ["biocheck_plan", "biocheck_write", "biocheck_check"]


@pytest.mark.parametrize(
    "publication_types,source_id,expected,basis",
    [
        ({"pubType": ["Journal Article", "Review"]}, "pubmed", "review", "fabric_publication_types"),
        (["Randomized Controlled Trial"], "pubmed", "primary", "explicit_primary_study_publication_type"),
        ([], "clinicaltrials", "primary", "original_trial_registration_not_experimental_result"),
    ],
)
def test_mechanism_maps_actual_fabric_type_metadata_without_equating_registry_to_experiment(
    installed_mech,
    publication_types,
    source_id,
    expected,
    basis,
):
    from bioradar.adapters.mech_evidence import evaluate_mechanism

    data = mechanism_packet(kind=None)
    data["evidence"][0]["publication_types"] = publication_types
    data["evidence_map"]["e1"]["record_ref"]["object_id"] = json.dumps([source_id, "synthetic", False])
    result = evaluate_mechanism(installed_mech, data, lambda *a: {"observations": []})
    assert result["status"] == "completed"
    assert result["material_type_mapping"][0] == {
        "evidence_id": "e1",
        "source_kind": expected,
        "basis": basis,
    }


def test_mechanism_evaluates_known_material_and_retains_untyped_material_as_a_gap(installed_mech):
    from bioradar.adapters.mech_evidence import evaluate_mechanism

    data = mechanism_packet()
    data["evidence"].append(
        {
            "id": "e2",
            "text": "Unclassified SYNTHETIC article",
            "version": "fixed-v2",
            "publication_types": ["Journal Article"],
        }
    )
    data["evidence_map"]["e2"] = {"version": "fixed-v2"}
    calls = []

    def call(stage, instruction, payload, schema):
        calls.append(payload)
        return {"observations": []}

    result = evaluate_mechanism(installed_mech, data, call)
    assert result["status"] == "completed" and result["coverage"]["status"] == "partial"
    assert result["coverage"]["evaluated_evidence_ids"] == ["e1"]
    assert result["coverage"]["untyped_evidence_ids"] == ["e2"]
    assert result["missing_fields"] == ["materials.e2.source_kind"]
    assert [item["id"] for item in calls[0]["materials"]] == ["e1"]
    assert "e2" in result["evidence_map"]


def test_exact_legacy_version_uses_selected_wrapper_and_fabric_permission_check():
    reader = FabricReader()
    metadata = {
        "source_id": "legacy_documents",
        "source_record_id": reader.original["source_record_id"],
        "version_id": "a" * 64,
    }
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [{"id": "chunk", "metadata": metadata}],
                },
            )
        )
    ) as client:
        result = search_candidates(
            weknora_settings(),
            "SYNTHETIC",
            reader=reader,
            source_ids=["legacy_documents"],
            client=client,
        )
    assert result["candidates"][0]["binding_status"] == "bound"
    assert len(result["records"]) == len(reader.reads) == 1
    assert reader.reads[0]["source_id"] == "legacy_documents"
    assert reader.reads[0]["version_id"] == "a" * 64


def test_optional_repair_budget_preserves_the_checked_original_not_unchecked_revision(installed_biocheck):
    stages = []

    def call(stage, instruction, payload):
        stages.append(stage)
        if stage == "biocheck_plan":
            return {"points": ["SYNTHETIC"]}
        if stage == "biocheck_write":
            return {"draft": "Checked but unresolved [e1]"}
        if stage == "biocheck_check":
            return review(payload["draft"], status="FAIL", problems=["U1"])
        if stage == "biocheck_revise":
            return {"draft": "Candidate revision [e1]", "changed_units": ["U1"]}
        raise DomainError("analysis_token_budget", "No budget to dispatch another request", 422)

    result = run_biocheck(installed_biocheck, packet(), call)
    assert result["status"] == "FAIL"
    assert result["final_text"] == result["verification"]["draft"] == "Checked but unresolved [e1]"
    assert result["repair_error_code"] == "analysis_token_budget"
    assert result["revision"] is None
    assert result["unverified_revision"]["draft"] == "Candidate revision [e1]"
    assert stages[-1] == "biocheck_recheck"


def test_unknown_recheck_outcome_is_not_optional_budget_fallback(installed_biocheck):
    def call(stage, instruction, payload):
        if stage == "biocheck_check":
            return review(payload["draft"], status="FAIL", problems=["U1"])
        if stage == "biocheck_revise":
            return {"draft": "Candidate revision [e1]"}
        raise DomainError("model_call_outcome_unconfirmed", "Recheck outcome unknown", 409)

    with pytest.raises(DomainError) as failure:
        check_biocheck(installed_biocheck, packet(), "Original [e1]", call)
    assert failure.value.code == "model_call_outcome_unconfirmed"


def test_weknora_binding_cannot_bypass_existing_fabric_record_limit():
    reader = FabricReader()
    reader.max_records, reader.records_read = 0, 0
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "id": "chunk",
                            "metadata": {"source_site": "pubmed", "source_uuid": "synthetic-uuid"},
                        }
                    ],
                },
            )
        )
    ) as client:
        result = search_candidates(
            weknora_settings(),
            "SYNTHETIC",
            reader=reader,
            source_ids=["legacy_documents", "pubmed"],
            client=client,
        )
    assert result["candidates"][0]["binding_reason"] == "history_read_budget"
    assert result["lookup_requests"] == result["version_reads"] == 0
    assert not reader.queries and not reader.reads and not result["records"]


def test_weknora_uses_remaining_overall_time_for_the_transport():
    requests = []

    def handler(request):
        requests.append(request)
        assert 0 < request.extensions["timeout"]["read"] <= 0.25
        assert 0 < request.extensions["timeout"]["connect"] <= 0.25
        return httpx.Response(200, json={"success": True, "data": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = search_candidates(
            weknora_settings(),
            "SYNTHETIC",
            timeout_seconds=0.25,
            client=client,
        )
    assert result["status"] == "empty" and len(requests) == 1


def test_weknora_nonfinite_score_is_a_protocol_failure_before_any_adoption():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=(b'{"success":true,"data":[{"id":"chunk","score":NaN}]}'))
        )
    ) as client:
        result = search_candidates(weknora_settings(), "SYNTHETIC", client=client)
    assert result["status"] == "failed" and result["error_code"] == "weknora_protocol_error"
    assert not result["records"] and not result["candidates"]


def test_weknora_configuration_fingerprint_freezes_kb_scope_without_exposing_credentials():
    from bioradar.adapters.weknora import provider_status

    before = weknora_settings()
    after = before.model_copy(update={"weknora_knowledge_base_ids": ["different-kb"]})
    status = provider_status(before)
    assert status["version"] != provider_status(after)["version"]
    assert "SYNTHETIC-SERVICE-KEY" not in json.dumps(status)


def test_administrator_can_reuse_existing_remote_weknora_instance():
    settings = Settings(weknora_url="http://100.125.40.22:8080/")
    assert settings.weknora_url == "http://100.125.40.22:8080"
    with pytest.raises(ValidationError):
        Settings(weknora_url="http://user:secret@100.125.40.22:8080")
    with pytest.raises(ValidationError):
        Settings(data_fabric_url="http://100.125.40.22:8080")


def test_biocheck_loaded_snapshot_survives_source_edits_for_the_whole_method(installed_biocheck):
    from bioradar.adapters.biocheck import load_biocheck

    loaded = load_biocheck(installed_biocheck)
    version = loaded[1]
    instructions = []

    def call(stage, instruction, payload):
        instructions.append(instruction)
        if stage == "biocheck_plan":
            (installed_biocheck.biocheck_project_dir / "app" / "prompts.py").write_text(
                "INSTRUCTION = 'Source changed during an active method'\n"
            )
            return {"points": ["SYNTHETIC"]}
        if stage == "biocheck_write":
            return {"draft": "SYNTHETIC final [e1]"}
        return review(payload["draft"])

    result = run_biocheck(installed_biocheck, packet(), call, loaded=loaded)
    assert result["status"] == "PASS" and result["provider_version"] == version
    assert instructions == ["SYNTHETIC provider instruction"] * 3
    assert biocheck_status(installed_biocheck)["version"] != version


def test_mechanism_uses_the_loaded_module_not_a_later_status_observation(installed_mech):
    from bioradar.adapters.mech_evidence import (
        evaluate_mechanism,
        load_mech_evidence,
        provider_status,
    )

    loaded = load_mech_evidence(installed_mech)
    path = installed_mech.mech_evidence_project_dir / "mechevidence" / "assess.py"
    path.write_text(path.read_text().replace("SYNTHETIC instructions", "Later uncalled source"))
    observed = provider_status(installed_mech)
    instructions = []

    def call(stage, instruction, payload, schema):
        instructions.append(instruction)
        return {"observations": []}

    result = evaluate_mechanism(installed_mech, mechanism_packet(), call, loaded=loaded)
    assert result["status"] == "completed"
    assert result["provider_version"] == loaded[2] != observed["version"]
    assert instructions == ["SYNTHETIC instructions"]


@pytest.mark.parametrize("server_limit", [524_288, 1_048_576, 10_000_000])
def test_administrator_token_ceiling_can_increase_but_request_cannot_exceed_it(server_limit):
    from bioradar.research.investigations.analysis_models import AnalysisBudget
    from bioradar.research.investigations.analysis_service import AnalysisService

    assert Settings().analysis_max_tokens == 262144
    settings = Settings(analysis_max_tokens=server_limit)
    service = AnalysisService(SimpleNamespace(settings=settings))
    assert service._budget_limits()["max_tokens"] == server_limit
    assert service._budget_limits(AnalysisBudget(max_tokens=server_limit))["max_tokens"] == server_limit
    assert service._budget_limits(AnalysisBudget(max_tokens=262144))["max_tokens"] == 262144
    if server_limit < 10_000_000:
        with pytest.raises(DomainError) as failure:
            service._budget_limits(AnalysisBudget(max_tokens=server_limit + 1))
        assert failure.value.code == "analysis_budget_invalid"
    else:
        with pytest.raises(ValidationError):
            AnalysisBudget(max_tokens=server_limit + 1)


def model_input_limit(stage, status):
    from bioradar.adapters.reasoning_model import ModelCallError, ModelCallReceipt

    return ModelCallError(
        "model_input_budget",
        "SYNTHETIC input budget",
        ModelCallReceipt(
            call_id="synthetic-input-budget",
            stage=stage,
            input_hash="synthetic-input-hash",
            prompt_version="synthetic-prompt",
            requested_model="synthetic-model",
            configured_model_revision=None,
            status=status,
            started_at=datetime.now(UTC),
            max_output_tokens=128,
        ),
        422,
    )


@pytest.mark.parametrize("failure_stage", ["biocheck_revise", "biocheck_recheck"])
def test_optional_repair_known_unsent_input_limit_keeps_the_verified_draft(installed_biocheck, failure_stage):
    stages = []

    def call(stage, instruction, payload):
        stages.append(stage)
        if stage == "biocheck_check":
            return review(payload["draft"], status="REVIEW", problems=["U1"])
        if stage == failure_stage:
            raise model_input_limit(stage, "not_sent")
        return {"draft": "Unverified revision [e1]"}

    result = check_biocheck(installed_biocheck, packet(), "Checked original [e1]", call)
    assert result["status"] == "REVIEW"
    assert result["final_text"] == result["verification"]["draft"] == "Checked original [e1]"
    assert result["repair_error_code"] == "model_input_budget"
    assert result["revision"] is None
    if failure_stage == "biocheck_recheck":
        assert result["unverified_revision"]["draft"] == "Unverified revision [e1]"
    else:
        assert result["unverified_revision"] is None
    assert stages[-1] == failure_stage


@pytest.mark.parametrize("receipt_status", ["failed", "unconfirmed", "completed", None])
def test_optional_repair_input_error_requires_not_sent_receipt(installed_biocheck, receipt_status):
    error = (
        model_input_limit("biocheck_revise", receipt_status)
        if receipt_status is not None
        else DomainError("model_input_budget", "SYNTHETIC missing receipt", 422)
    )

    def call(stage, instruction, payload):
        if stage == "biocheck_check":
            return review(payload["draft"], status="FAIL", problems=["U1"])
        raise error

    with pytest.raises(DomainError) as failure:
        check_biocheck(installed_biocheck, packet(), "Checked original [e1]", call)
    assert failure.value is error


@pytest.mark.parametrize(
    ("types", "expected"),
    [
        (["Research Support, Non-U.S. Gov't", "research-article", "Journal Article"], "primary"),
        (["research article"], "primary"),
        (["research-article", "Review"], "review"),
        (["research-article", "Meta-Analysis"], "review"),
        (["review-article", "Journal Article"], "review"),
        (["Journal Article"], None),
    ],
)
def test_explicit_research_article_alias_does_not_promote_generic_articles(types, expected):
    from bioradar.adapters.mech_evidence import _source_kind

    kind, _ = _source_kind({"publication_types": {"pubType": types}}, "europe_pmc", "MED:39865264")
    assert kind == expected
