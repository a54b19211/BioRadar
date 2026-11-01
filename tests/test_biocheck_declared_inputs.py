"""Actual native method inputs, honest adopted windows, and unchanged legacy replay."""

import copy
import hashlib
import json
from pathlib import Path
from types import ModuleType

import pytest
from bioradar.adapters.biocheck import (
    BIOCHECK_INPUT_VERSION,
    audit_biocheck_support,
    build_packet,
    run_biocheck,
)
from bioradar.api.errors import DomainError
from test_biocheck_coordination import ControlledCalls
from test_biocheck_coordination import current_biocheck as current_biocheck

pytestmark = pytest.mark.ENGINE


def evidence():
    prefix, text, suffix = "SYNTHETIC prefix 🧬 ", "A activates B.", " SYNTHETIC suffix"
    full = prefix + text + suffix
    return {
        "key": "E1",
        "text": text,
        "record_ref": {
            "adopted_version": "fixed-v1",
            "content_hash": hashlib.sha256(full.encode()).hexdigest(),
        },
        "source_context": {"text_level": "full_text"},
        "text_basis": "SYNTHETIC-clean-text",
        "start": len(prefix),
        "end": len(prefix) + len(text),
        "source_length": len(full),
        "text_sha256": hashlib.sha256(full.encode()).hexdigest(),
    }


def packet(*, version=BIOCHECK_INPUT_VERSION):
    return build_packet(
        "SYNTHETIC scoped question",
        [evidence()],
        {
            "research_scope": {"qualification": "SYNTHETIC scope"},
            "source_updates": [],
            "analysis_results": [],
        },
        input_version=version,
    )


def audit_response():
    return {
        "checks": [
            {
                "unit_id": "U1",
                "claim_index": 0,
                "verdict": "supported",
                "reason": "SYNTHETIC excerpt",
                "support": [{"evidence_id": "E1", "segment_id": "S1", "quote": "A activates B."}],
            }
        ]
    }


def checked_packet(current_biocheck, *, version=BIOCHECK_INPUT_VERSION):
    settings, loaded = current_biocheck
    data, requests, native = packet(version=version), [], ControlledCalls()

    def host(stage, instruction, payload):
        requests.append((stage, copy.deepcopy(payload)))
        return native(stage, instruction, payload)

    result = run_biocheck(settings, data, host, repair=False, loaded=loaded)
    assert result["status"] == "PASS"
    return settings, loaded, data, result["verification"], requests


def test_new_packet_window_hashes_only_adopted_text_and_preserves_original_version_hash():
    item = evidence()
    original = copy.deepcopy(item)
    data = build_packet("SYNTHETIC", [item], input_version=BIOCHECK_INPUT_VERSION)
    adopted = data["evidence"][0]
    context, window = adopted["source_context"], adopted["source_context"]["source_window"]
    assert context["original_text_level"] == "full_text"
    assert context["text_level"] == context["adopted_text_level"] == "selected_passages"
    assert (window["start"], window["end"], window["source_length"]) == (
        item["start"],
        item["end"],
        item["source_length"],
    )
    assert window["offset_basis"] == "unicode_code_points" and window["text_basis"] == item["text_basis"]
    assert window["hash_scope"] == "adopted_packet_text"
    assert window["text_sha256"] == hashlib.sha256(item["text"].encode()).hexdigest()
    assert window["source_text_sha256"] == item["text_sha256"] != window["text_sha256"]
    assert data["evidence_map"]["E1"]["record_ref"] == item["record_ref"]
    assert adopted["version"] == item["record_ref"]["adopted_version"]
    assert item == original


def test_actual_native_projection_keeps_window_metadata_and_verify_keeps_legal_assessment(current_biocheck):
    _, loaded, data, _, requests = checked_packet(current_biocheck)
    core, actual_method_hash = loaded
    assert len(actual_method_hash) == 64
    native_input = core.model_input(core.packet_input(copy.deepcopy(data)))
    adopted = native_input["evidence"][0]
    assert "text_level" not in adopted  # The actual native SOURCE_FIELDS filters this legacy top-level field.
    assert adopted["source_context"] == data["evidence"][0]["source_context"]
    actual_plan = next(payload for stage, payload in requests if stage == "biocheck_plan")
    projected = actual_plan["evidence"][0]
    received_text = "".join(segment["text"] for segment in projected["segments"])
    assert received_text == data["evidence"][0]["text"]
    assert (
        hashlib.sha256(received_text.encode()).hexdigest()
        == projected["source_context"]["source_window"]["text_sha256"]
    )
    verify = next(payload for stage, payload in requests if stage == "biocheck_check")
    assert "".join(segment["text"] for segment in verify["evidence"][0]["segments"]) == received_text
    assert verify["evidence"][0]["source_context"] == projected["source_context"]
    assert verify["assessment"] == data["assessment"]


def test_unknown_window_metadata_stays_unknown_without_guessing_full_text():
    item = evidence()
    for field in ("start", "end", "source_length", "text_sha256"):
        item.pop(field)
    result = build_packet("SYNTHETIC", [item], input_version=BIOCHECK_INPUT_VERSION)
    context = result["evidence"][0]["source_context"]
    window = context["source_window"]
    assert context["adopted_text_level"] == "selected_passages"
    assert all(
        window[field] is None
        for field in ("start", "end", "source_length", "offset_basis", "source_text_sha256")
    )
    assert window["adopted_text_length"] == len(item["text"])


@pytest.mark.parametrize(
    "fault", ["start_only", "bool_offset", "wrong_end", "short_source", "negative_source", "string_source"]
)
def test_invalid_declared_offsets_or_source_length_are_rejected(fault):
    item = evidence()
    if fault == "start_only":
        item.pop("end")
    elif fault == "bool_offset":
        item["start"] = True
    elif fault == "wrong_end":
        item["end"] += 1
    elif fault == "short_source":
        item["source_length"] = item["end"] - 1
    elif fault == "negative_source":
        item["source_length"] = -1
    else:
        item["source_length"] = str(item["source_length"])
    with pytest.raises(DomainError) as error:
        build_packet("SYNTHETIC", [item], input_version=BIOCHECK_INPUT_VERSION)
    assert error.value.code == "provider_input_invalid"


@pytest.mark.parametrize("version", ["unknown-input-v999", True, 1])
def test_unknown_input_contract_rejects_before_any_model_callback(version):
    with pytest.raises(DomainError) as error:
        build_packet("SYNTHETIC", [evidence()], input_version=version)
    assert error.value.code == "biocheck_input_version_invalid"
    with pytest.raises(DomainError) as error:
        audit_biocheck_support(
            None,
            {},
            {},
            lambda *a, **k: pytest.fail("No model callback"),
            loaded=(None, None),
            input_version=version,
        )
    assert error.value.code == "biocheck_input_version_invalid"


def test_new_audit_callback_equals_actual_native_declared_payload_without_assessment(current_biocheck):
    settings, loaded, data, review, _ = checked_packet(current_biocheck)
    actual = []
    core = loaded[0]
    rpc = ModuleType(core.__package__ + ".rpc")
    rpc.__package__ = core.__package__
    rpc.__file__ = str(Path(core.__file__).with_name("rpc.py"))
    exec(compile(Path(rpc.__file__).read_text(), rpc.__file__, "exec"), rpc.__dict__)
    declared = rpc.execute(
        {
            "schema_version": rpc.SCHEMA_VERSION,
            "operation_id": "SYNTHETIC-declared-audit",
            "operation": "audit_support",
            "arguments": {
                "packet": copy.deepcopy(data),
                "review": copy.deepcopy(review),
                "targets": [["U1", 0]],
            },
        }
    )
    assert declared["status"] == "model_required" and len(declared["provider_version"]) == 64
    expected = [declared["model_call"]["payload"]]

    def host(stage, instruction, payload):
        assert stage == "biocheck_support_audit"
        actual.append(copy.deepcopy(payload))
        return audit_response()

    before = copy.deepcopy((data, review))
    result = audit_biocheck_support(
        settings, data, review, host, loaded=loaded, input_version=BIOCHECK_INPUT_VERSION
    )
    assert actual == expected and set(actual[0]) == {"claims"}
    assert "assessment" not in actual[0] and data["assessment"]
    assert result["status"] == "PASS"
    assert (data, review) == before


def test_legacy_packet_and_received_audit_request_signature_replay_exactly(current_biocheck):
    settings, loaded, data, review, _ = checked_packet(current_biocheck, version=None)
    assert data == build_packet("SYNTHETIC scoped question", [evidence()], data["assessment"])
    assert data["evidence"][0]["source_context"]["text_level"] == "full_text"
    assert "source_window" not in data["evidence"][0]["source_context"]
    saved, sent, interrupted = {}, [], False

    def host(stage, instruction, payload, schema=None, *, call_id=None):
        nonlocal interrupted
        signature = hashlib.sha256(
            json.dumps(
                [stage, instruction, payload, schema, call_id],
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if not saved:
            assert payload["assessment"] == data["assessment"]
            saved.update(request_signature=signature, payload=copy.deepcopy(payload), output=audit_response())
            sent.append(True)
        assert signature == saved["request_signature"] and payload == saved["payload"]
        if not interrupted:
            interrupted = True
            raise RuntimeError("SYNTHETIC exit after received audit callback saved")
        return copy.deepcopy(saved["output"])

    with pytest.raises(RuntimeError):
        audit_biocheck_support(settings, data, review, host, loaded=loaded)
    original = copy.deepcopy(saved)
    result = audit_biocheck_support(settings, data, review, host, loaded=loaded, input_version=None)
    assert result["status"] == "PASS" and len(sent) == 1 and saved == original
