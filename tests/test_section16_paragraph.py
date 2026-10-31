"""Real BioCheck methods through Radar's ledger; all provider replies are synthetic."""

import copy
from datetime import UTC, datetime

import pytest
from bioradar.adapters.biocheck import build_packet, check_biocheck
from bioradar.adapters.reasoning_model import ModelCallError, ModelCallReceipt, ModelCompletion, ModelUsage
from test_kdd_finishing_budget import local_biocheck as local_biocheck  # noqa: F401
from test_kdd_finishing_budget import state


@pytest.mark.parametrize("transport", ["local", "http"])
def test_one_truncated_paragraph_retries_only_it_and_received_restore_sends_nothing(
    local_biocheck, transport
):
    service, fixed, saved = state("pipeline")
    packet = build_packet("SYNTHETIC bounded question", saved["evidence"])
    if transport == "http":
        import json
        from pathlib import Path
        from types import ModuleType

        import httpx
        from bioradar.adapters.biocheck import load_biocheck_http
        from bioradar.adapters.professional_rpc import execute_professional

        core = local_biocheck[0]
        rpc = ModuleType(core.__package__ + ".rpc")
        rpc.__package__ = core.__package__
        rpc.__file__ = str(Path(core.__file__).with_name("rpc.py"))
        exec(compile(Path(rpc.__file__).read_text(), rpc.__file__, "exec"), rpc.__dict__)
        operations = {}
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json=rpc.execute(json.loads(req.content)))
            ),
            base_url="http://SYNTHETIC-local-service",
        )

        def execute(operation, arguments, call=None):
            return execute_professional(
                client,
                provider="biocheck",
                operation_id="SYNTHETIC-" + operation,
                operation=operation,
                arguments=arguments,
                call=call,
                state=operations.setdefault(operation, {}),
                checkpoint=lambda: None,
                provider_version=rpc.PROVIDER_VERSION,
                max_model_calls=32,
            )

        local_biocheck = load_biocheck_http(execute, rpc.PROVIDER_VERSION, capabilities=rpc.capabilities())
    draft = "SYNTHETIC source statement [E1].\n\nSYNTHETIC source statement [E1]."
    sent = []

    def model(settings, **kwargs):
        stage, payload = kwargs["stage"], kwargs["payload"]
        unit = payload["verification_request"]["unit_id"]
        sent.append((stage, unit, settings.semantic_llm_max_output_tokens))
        receipt = ModelCallReceipt(
            call_id=kwargs["call_id"],
            stage=stage,
            input_hash=kwargs["input_hash"],
            prompt_version=kwargs["prompt_version"],
            requested_model="SYNTHETIC-model",
            configured_model_revision=None,
            status="completed",
            started_at=datetime.now(UTC),
            finish_reason="stop",
            usage=ModelUsage(prompt_tokens=100, completion_tokens=10, total_tokens=110),
            max_output_tokens=settings.semantic_llm_max_output_tokens,
        )
        if stage == "biocheck_verify_paragraph" and unit == "U1":
            receipt = receipt.model_copy(
                update={"status": "failed", "finish_reason": "length", "error_code": "model_output_truncated"}
            )
            raise ModelCallError("model_output_truncated", "SYNTHETIC truncation", receipt, 502)
        if stage == "biocheck_verify_coverage":
            output = {
                "checked_unit_ids": payload["checked_unit_ids"],
                "coverage": {"verdict": "complete", "reason": "SYNTHETIC coverage"},
            }
        else:
            text = payload["units"][0]["text"]
            output = {
                "reviews": [
                    {
                        "unit_id": unit,
                        "verdict": "supported",
                        "reason": "SYNTHETIC",
                        "claims": [
                            {
                                "span": text,
                                "kind": "fact",
                                "verdict": "supported",
                                "reason": "SYNTHETIC",
                                "evidence": [{"evidence_id": "E1", "segment_id": "S1"}],
                            }
                        ],
                        "issues": [],
                        "numeric_resolutions": [],
                    }
                ],
                "coverage": {"verdict": "complete", "reason": "SYNTHETIC"},
            }
        return ModelCompletion(content="SYNTHETIC", parsed=output, receipt=receipt)

    service.model_call = model
    result = check_biocheck(
        None,
        packet,
        draft,
        service._shared_callback("SYNTHETIC", "token", fixed, saved),
        loaded=local_biocheck,
        repair=False,
        verification_mode="paragraph",
    )
    assert result["status"] == "PASS"
    assert sent == [
        ("biocheck_verify_paragraph", "U1", 12288),
        ("biocheck_verify_paragraph_retry", "U1", 16384),
        ("biocheck_verify_paragraph", "U2", 12288),
        ("biocheck_verify_coverage", None, 8192),
    ]
    persisted = copy.deepcopy(saved)
    service.model_call = lambda *a, **k: pytest.fail("Received paragraphs must not send a new POST")
    restored = check_biocheck(
        None,
        packet,
        draft,
        service._shared_callback("SYNTHETIC", "token", fixed, persisted),
        loaded=local_biocheck,
        repair=False,
        verification_mode="paragraph",
    )
    assert restored == result and restored["final_text"] == draft


def test_no_budget_preserves_original_draft(local_biocheck):
    service, fixed, saved = state("pipeline")
    fixed["budget"]["max_tokens"] = 1
    service.model_call = lambda *a, **k: pytest.fail("No budget, no POST")
    draft = "SYNTHETIC source statement [E1]."
    result = check_biocheck(
        None,
        build_packet("SYNTHETIC", saved["evidence"]),
        draft,
        service._shared_callback("SYNTHETIC", "token", fixed, saved),
        loaded=local_biocheck,
        verification_mode="paragraph",
    )
    assert result["final_text"] == draft and result["status"] == "failed"
    assert result["verification_stop_reason"] == "not_sent_budget"
