"""Pre-run seal proofs use synthetic metadata only, never private experiment answers."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "protocol_controls", Path(__file__).with_name("test_experiment_protocol.py")
)
controls = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controls)
protocol = controls.protocol


def metadata(code_revision="frozen-offline-code"):
    registry = controls._bytes(
        {
            "components": [
                {"name": "radar", "revision": code_revision, "source_sha256": "a" * 64},
                {"name": "provider", "revision": "offline-provider", "source_sha256": "b" * 64},
            ]
        }
    )
    readback = {
        "instance_sha256": "c" * 64,
        "observed_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "native_analysis_count": 0,
        "first_native_analysis_id": None,
        "first_native_analysis_created_at": None,
    }
    preflight = controls._bytes(
        {
            "instance_id": "OFFLINE-INSTANCE",
            "instance_sha256": readback["instance_sha256"],
            "native_analysis_count": 0,
            "observed_at": readback["observed_at"],
            "native_readback": readback,
            "native_readback_sha256": protocol.native_receipt_digest(readback),
        }
    )
    return registry, preflight


def test_seal_refuses_to_claim_pre_run_without_code_registry_and_zero_native_preflight():
    _, inputs, _ = controls._experiment()
    with pytest.raises(ValueError, match="registry|preflight"):
        protocol.seal(*inputs, code_revision="frozen-offline-code")


def test_seal_and_public_request_bind_registry_and_actual_runtime_without_private_answers(tmp_path):
    _, inputs, _ = controls._experiment()
    registry, preflight = metadata()
    sealed = protocol.seal(
        *inputs, code_revision="frozen-offline-code", code_registry=registry, runtime_preflight=preflight
    )
    assert sealed["commitments"]["code_registry_sha256"] == protocol._hash(registry)
    assert sealed["commitments"]["runtime_preflight_sha256"] == protocol._hash(preflight)
    assert sealed["runtime_preflight"]["native_analysis_count"] == 0
    request = protocol.prepare(sealed, *inputs[:3], tmp_path / "blind")
    assert request["code_registry_sha256"] == protocol._hash(registry)
    assert request["runtime_instance_sha256"] == "c" * 64
    assert "answers_sha256" not in json.dumps(request)


def test_seal_rejects_an_already_used_runtime_or_a_registry_for_other_radar_code():
    _, inputs, _ = controls._experiment()
    registry, preflight = metadata()
    nonempty = json.loads(preflight)
    nonempty["native_analysis_count"] = 1
    with pytest.raises(ValueError, match="zero|0"):
        protocol.seal(
            *inputs,
            code_revision="frozen-offline-code",
            code_registry=registry,
            runtime_preflight=controls._bytes(nonempty),
        )
    with pytest.raises(ValueError, match="revision"):
        protocol.seal(
            *inputs, code_revision="different-code", code_registry=registry, runtime_preflight=preflight
        )


def test_grade_checks_actual_first_native_time_even_when_reported_run_start_is_later():
    manifest, inputs, endpoints = controls._experiment()
    runs, reviews = controls._runs_and_reviews(manifest, inputs, endpoints)
    early = json.loads(runs[0])
    early["started_at"] = (datetime.fromisoformat(manifest["sealed_at"]) + timedelta(seconds=2)).isoformat()
    early["first_native_analysis_created_at"] = (
        datetime.fromisoformat(manifest["sealed_at"]) - timedelta(seconds=2)
    ).isoformat()
    early["native_analysis_readback"]["first_native_analysis_created_at"] = early[
        "first_native_analysis_created_at"
    ]
    early["native_analysis_readback_sha256"] = protocol.native_receipt_digest(
        early["native_analysis_readback"]
    )
    result = protocol.evaluate(manifest, *inputs, [controls._bytes(early), runs[1]], reviews)
    assert "first_native_analysis_not_after_seal" in result["runs"][0]["issues"]


def test_grade_rejects_missing_or_mismatched_native_code_and_runtime_proofs():
    manifest, inputs, endpoints = controls._experiment()
    runs, reviews = controls._runs_and_reviews(manifest, inputs, endpoints)
    for field, invalid, issue in [
        ("first_native_analysis_id", None, "first_native_analysis_id_missing"),
        ("native_analysis_readback_sha256", None, "native_analysis_readback_missing"),
        ("runtime_instance_sha256", "f" * 64, "runtime_instance_mismatch"),
        ("code_registry_sha256", "e" * 64, "code_registry_sha256_mismatch"),
    ]:
        changed = json.loads(runs[0])
        changed[field] = invalid
        result = protocol.evaluate(manifest, *inputs, [controls._bytes(changed), runs[1]], reviews)
        assert issue in result["runs"][0]["issues"]
        assert result["acceptance"] != "passed"


def test_native_readback_content_cannot_be_replaced_by_later_flat_time_or_an_arbitrary_digest():
    manifest, inputs, endpoints = controls._experiment()
    runs, reviews = controls._runs_and_reviews(manifest, inputs, endpoints)
    forged = json.loads(runs[0])
    forged["native_analysis_readback"] = {
        "instance_sha256": manifest["runtime_preflight"]["instance_sha256"],
        "observed_at": (datetime.fromisoformat(manifest["sealed_at"]) + timedelta(seconds=3)).isoformat(),
        "native_analysis_count": 1,
        "first_native_analysis_id": forged["first_native_analysis_id"],
        "first_native_analysis_created_at": (
            datetime.fromisoformat(manifest["sealed_at"]) - timedelta(seconds=1)
        ).isoformat(),
    }
    encoded = json.dumps(
        forged["native_analysis_readback"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    forged["native_analysis_readback_sha256"] = protocol._hash(encoded)
    result = protocol.evaluate(manifest, *inputs, [controls._bytes(forged), runs[1]], reviews)
    assert "native_readback_created_at_mismatch" in result["runs"][0]["issues"]
    assert "first_native_analysis_not_after_seal" in result["runs"][0]["issues"]
    forged["native_analysis_readback_sha256"] = "0" * 64
    result = protocol.evaluate(manifest, *inputs, [controls._bytes(forged), runs[1]], reviews)
    assert "native_analysis_readback_hash_mismatch" in result["runs"][0]["issues"]
