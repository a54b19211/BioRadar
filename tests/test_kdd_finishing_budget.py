"""Stage budgets use representative fixed packets and real local BioCheck payloads, without HTTP."""

import copy
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from bioradar.adapters.biocheck import audit_biocheck_support, build_packet, load_biocheck, run_biocheck
from bioradar.adapters.reasoning_model import (
    ModelCallError,
    ModelCallReceipt,
    ModelCompletion,
    ModelUsage,
    encode_request,
    model_parameters,
)
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis import generation_input_allowances
from bioradar.research.investigations.analysis_models import PROMPT_VERSION, ModelSynthesis
from bioradar.research.investigations.analysis_service import AnalysisService


@pytest.fixture
def local_biocheck():
    configured = os.environ.get("BIORADAR_TEST_BIOCHECK_PROJECT_DIR")
    directory = Path(configured) if configured else Path(__file__).resolve().parents[2] / "biocheck"
    if not directory.is_dir():
        if configured:
            pytest.fail("Explicit BioCheck test project is missing")
        pytest.skip("Requires installed pure BioCheck methods")
    core, version = load_biocheck(Settings(biocheck_project_dir=directory))
    assert core is not None
    return core, version


def state(direction):
    settings = Settings(
        environment="test",
        semantic_llm_enabled=True,
        semantic_llm_base_url="http://127.0.0.1:9999/v1",
        semantic_llm_api_key="SYNTHETIC-not-a-model-key",
        semantic_llm_model="SYNTHETIC-model",
        semantic_llm_max_input_bytes=131072,
        semantic_llm_max_output_tokens=32768,
        semantic_llm_timeout_seconds=180,
    )
    text = "SYNTHETIC source statement. " + ("SYNTHETIC fixed passage " + "x" * 350 + ". ") * 100
    evidence = {
        "key": "E1",
        "title": "SYNTHETIC source",
        "source_id": "synthetic-source",
        "text": text,
        "version": "fixed-v1",
        "record_ref": {"adopted_version": "fixed-v1"},
        "passages": [{"id": "E1:p001", "text": text}],
        "text_basis": "snapshot_normalized",
    }
    annotation = "SYNTHETIC fixed calculation annotation " + "y" * 42000
    calculation = {
        "id": "A1",
        "dataset_ref": {"id": "synthetic-fixed-window", "version": "result-v1"},
        "method_version": "project-condition-observation-v2"
        if direction == "pipeline"
        else "period-change-v2",
        "parameters": {"direction": direction},
        "output": {"annotation": annotation, "count": 20},
        "input_refs": [{"source_record_id": "SYNTHETIC:row", "version": "fixed-v1"}],
        "calculation_basis": {
            "analysis_unit": "project" if direction == "pipeline" else "company_metric_period",
            "unit": "bounded_units",
            "period": "fixed cutoff",
            "population": "adopted scope",
            "denominator": 20,
        },
        "limitations": ["SYNTHETIC packet-size regression; no population or scientific inference"],
    }
    reading = {
        "claims": [{"id": "C1", "text": "SYNTHETIC conditional interpretation " + "z" * 2500}],
        "conditional_answer": {"answer": "SYNTHETIC bounded answer " + "w" * 1500},
        "gaps": [],
        "explanations": [],
    }
    parameters = {**model_parameters(settings), "max_calls": 24, "job_timeout_seconds": 1860}
    fixed = {
        "question": "SYNTHETIC bounded " + direction,
        "as_of": "2026-01-01T00:00:00+00:00",
        "query_context_version": "semantic-v2",
        "execution_version": "query-step-v1",
        "query_policy": {"effective": "query_planner", "schema_version": "query-planner.external.v2"},
        "search_scope": ["synthetic-source"],
        "graph_scope": [],
        "evidence_time": {"mode": "current", "cutoff": "2026-01-01T00:00:00+00:00"},
        "providers": {"biocheck": {"available": True}},
        "prompt_version": PROMPT_VERSION,
        "model": parameters["model"],
        "model_revision": parameters["model_revision"],
        "model_parameters": parameters,
        "budget": {"max_calls": 24, "max_tokens": 524288, "max_seconds": 1800, "max_queries": 12},
    }
    progress = {
        "calls": [],
        "queries": [],
        "evidence": [evidence],
        "started_at": datetime.now(UTC).isoformat(),
        "query_policy": {"reading": reading, "checks": [], "status": "insufficient"},
        "final_report_plan": {"reserve_support_audit": True},
        "report_research_scopes": {
            "0": {
                "source_ids": ["synthetic-source"],
                "unresolved_checks": [],
                "analysis_results": [calculation],
            }
        },
    }
    service = AnalysisService.__new__(AnalysisService)
    service.settings = settings
    service._guard = lambda *args, **kwargs: None
    service._checkpoint = lambda *args, **kwargs: None
    return service, fixed, progress


def response(stage):
    draft = "SYNTHETIC source statement [E1]."
    if stage == "biocheck_plan":
        return {
            "points": [
                {
                    "intent": "Describe fixed source only",
                    "evidence_ids": ["E1"],
                    "conditions": [],
                    "binding": {"study": "SYNTHETIC source"},
                    "support": [{"evidence_id": "E1", "segment_id": "S1"}],
                }
            ]
        }
    if stage == "biocheck_write":
        return {"draft": draft}
    if stage == "biocheck_check":
        return {
            "reviews": [
                {
                    "unit_id": "U1",
                    "verdict": "supported",
                    "reason": "SYNTHETIC fixed response",
                    "claims": [
                        {
                            "span": draft,
                            "kind": "fact",
                            "verdict": "supported",
                            "reason": "SYNTHETIC fixed response",
                            "evidence": [{"evidence_id": "E1", "segment_id": "S1"}],
                        }
                    ],
                    "issues": [],
                    "numeric_resolutions": [],
                }
            ],
            "coverage": {"verdict": "complete", "reason": "SYNTHETIC bounded packet"},
        }
    if stage == "biocheck_support_audit":
        return {
            "checks": [
                {
                    "unit_id": "U1",
                    "claim_index": 0,
                    "verdict": "supported",
                    "reason": "SYNTHETIC fixed response",
                    "support": [
                        {"evidence_id": "E1", "segment_id": "S1", "quote": "SYNTHETIC source statement."}
                    ],
                }
            ]
        }
    return {"qualification": "SYNTHETIC synthesis output used only for budgeting"}


@pytest.mark.parametrize("direction", ["pipeline", "business"])
def test_stage_shaped_reserve_admits_normal_synthesis_and_complete_native_tail(
    direction, local_biocheck, monkeypatch
):
    service, fixed, progress = state(direction)
    sent = []

    def forbidden(*args, **kwargs):
        raise AssertionError("No provider/model HTTP is permitted")

    monkeypatch.setattr(httpx.Client, "send", forbidden)

    def complete(settings, **kwargs):
        encoded = encode_request(
            settings,
            stage=kwargs["stage"],
            system_prompt=kwargs["system_prompt"],
            payload=kwargs["payload"],
            response_schema=kwargs["response_schema"],
        )
        assert len(encoded) <= fixed["model_parameters"]["max_input_bytes"]
        sent.append((kwargs["stage"], len(encoded), settings.semantic_llm_max_output_tokens))
        parsed = response(kwargs["stage"])
        receipt = ModelCallReceipt(
            call_id=kwargs["call_id"],
            stage=kwargs["stage"],
            input_hash=kwargs["input_hash"],
            prompt_version=kwargs["prompt_version"],
            requested_model="SYNTHETIC-model",
            configured_model_revision=None,
            status="completed",
            started_at=datetime.now(UTC),
            max_output_tokens=settings.semantic_llm_max_output_tokens,
            usage=ModelUsage(prompt_tokens=64, completion_tokens=64, total_tokens=128),
        )
        return ModelCompletion(content=json.dumps(parsed), parsed=parsed, receipt=receipt)

    service.model_call = complete
    fixed_before = copy.deepcopy(fixed)
    reserve = service._final_token_allowance(fixed, progress)
    service._call(
        "synthetic",
        "unused-lock",
        "synthesize",
        ModelSynthesis,
        fixed,
        progress,
        service._synthesis_payload(fixed, progress),
    )
    assert sent[0][2] == fixed["model_parameters"]["max_output_tokens"] == 32768

    def callback(stage, instruction, payload, schema=None):
        output, _ = service._call(
            "synthetic",
            "unused-lock",
            stage,
            schema or {"type": "object"},
            fixed,
            progress,
            payload,
            system_prompt=instruction,
        )
        if stage == "biocheck_write":
            progress["final_candidate"] = output["draft"]
        return output

    context = service._report_context(fixed, progress, progress["query_policy"]["reading"])
    packet = build_packet(fixed["question"], progress["evidence"], context)
    result = run_biocheck(None, packet, callback, repair=False, loaded=local_biocheck)
    assert result["status"] == "PASS"
    audit = audit_biocheck_support(None, packet, result["verification"], callback, loaded=local_biocheck)
    assert audit["status"] == "PASS"
    assert [item[0] for item in sent] == [
        "synthesize",
        "biocheck_plan",
        "biocheck_write",
        "biocheck_check",
        "biocheck_support_audit",
    ]
    assert sum(size + 1024 + 128 for _, size, _ in sent[1:]) <= reserve
    assert all(cap == 32768 for _, _, cap in sent)
    assert fixed == fixed_before


def test_actual_call_still_rejects_exhausted_total_budget_before_any_invoke(monkeypatch):
    service, fixed, progress = state("pipeline")
    fixed["budget"]["max_tokens"] = 1
    service.model_call = lambda *args, **kwargs: pytest.fail("Exhausted request must not invoke a model")
    with pytest.raises(DomainError) as error:
        service._call(
            "synthetic",
            "unused-lock",
            "synthesize",
            ModelSynthesis,
            fixed,
            progress,
            service._synthesis_payload(fixed, progress),
        )
    assert error.value.code == "analysis_token_budget" and progress["calls"] == []


def test_actual_call_still_enforces_encoded_input_limit_with_not_sent_receipt(monkeypatch):
    service, fixed, progress = state("business")
    fixed["budget"]["max_tokens"] = 2000000  # Isolate the independent hard input-byte cap.
    service.model_call = None
    monkeypatch.setattr(
        httpx.Client, "send", lambda *args, **kwargs: pytest.fail("Oversized input sent HTTP")
    )
    payload = service._synthesis_payload(fixed, progress)
    payload["SYNTHETIC_oversized"] = "q" * 200000
    with pytest.raises(ModelCallError) as error:
        service._call("synthetic", "unused-lock", "synthesize", ModelSynthesis, fixed, progress, payload)
    assert error.value.code == "model_input_budget" and error.value.receipt.status == "not_sent"
    assert progress["calls"][-1]["receipt"]["request_sha256"] is None


@pytest.mark.parametrize("text_style", ["tiny", "escaped", "representative"])
def test_generation_reserve_covers_native_encoded_inputs_and_ignores_only_host_mapping(
    local_biocheck, monkeypatch, text_style
):
    service, fixed, progress = state("pipeline")
    if text_style != "representative":
        text = "SYNTHETIC source statement."
        if text_style == "escaped":
            text += ' \\"Quoted\\\" paths C:\\\\folder\\\\record.\\n中文限定条件。' * 80
        progress["evidence"][0]["text"] = text
    packet = build_packet(fixed["question"], progress["evidence"])
    original = copy.deepcopy(packet)
    packet["evidence_map"]["E1"]["host_provenance"] = {
        "qualification": "SYNTHETIC host-only metadata",
        "history": ["SYNTHETIC fixed source origin " + str(index) for index in range(1000)],
    }
    core, _ = local_biocheck
    assert core.packet_input(packet) == core.packet_input(original)
    before = copy.deepcopy(packet)
    assert generation_input_allowances(
        service.settings, packet, "", fixed["model_parameters"]["max_input_bytes"]
    ) == generation_input_allowances(
        service.settings, original, "", fixed["model_parameters"]["max_input_bytes"]
    )
    allowances = generation_input_allowances(
        service.settings, packet, "", fixed["model_parameters"]["max_input_bytes"]
    )
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("No model HTTP is allowed"))
    stages = []

    def callback(stage, instruction, payload, schema=None):
        if stage in {"biocheck_plan", "biocheck_write"}:
            assert "evidence_map" not in payload
            encoded = encode_request(
                service.settings, stage=stage, system_prompt=instruction,
                payload=payload, response_schema=schema or {"type": "object"},
            )
            kind = "plan" if stage == "biocheck_plan" else "write"
            assert len(encoded) + 1024 <= allowances[kind]
            stages.append(stage)
        return response(stage)

    report = run_biocheck(None, packet, callback, repair=False, loaded=local_biocheck)
    assert report["status"] == "PASS"
    assert stages == ["biocheck_plan", "biocheck_write"]
    assert packet == before  # Neither accounting nor native generation removes durable provenance.
