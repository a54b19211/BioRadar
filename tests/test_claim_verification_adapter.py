"""Host callback and optional revision integration; authored receipts, zero external models."""

import copy
from types import SimpleNamespace

import pytest
from bioradar.adapters.biocheck import (
    _callback,
    check_biocheck,
    load_biocheck,
    supports_paragraph_verification,
)
from bioradar.api.errors import DomainError

pytestmark = pytest.mark.BOOT


def test_claim_and_revision_stages_share_callback_and_reject_non_object():
    seen = []

    def call(stage, instruction, payload, **kwargs):
        seen.append((stage, kwargs["call_id"]))
        return {"checks": []}

    invoke = _callback(call, verify_target="biocheck_recheck")
    for stage in ("verify_claim", "verify_claim_retry", "revise_claims"):
        assert invoke(stage, "synthetic", {}) == {"finish_reason": "stop", "output": {"checks": []}}
    assert [s for s, _ in seen] == [
        "biocheck_recheck_claim",
        "biocheck_recheck_claim_retry",
        "biocheck_revise_claims",
    ]
    assert all(key for _, key in seen)
    for output in (None, [], "text"):
        with pytest.raises(DomainError, match="JSON"):
            _callback(lambda *args, **kwargs: output)("verify_claim", "synthetic", {})


def test_claim_capability_is_pinned_and_keeps_old_paragraph_compatibility():
    caps = {
        "provider_version": "v",
        "paragraph_verification": {
            "argument": "mode",
            "value": "paragraph",
            "retry_on": "length",
            "grain": "cited_claims",
            "maximum_claims_per_batch": 3,
            "max_retries_per_batch": 1,
        },
    }
    core = SimpleNamespace(provider_version="v", capabilities=caps)
    assert supports_paragraph_verification(core)
    core.provider_version = "wrong"
    assert not supports_paragraph_verification(core)
    core.provider_version = "v"
    caps["paragraph_verification"] = {
        "argument": "mode",
        "value": "paragraph",
        "retry_on": "length",
        "max_retries_per_paragraph": 1,
    }
    assert supports_paragraph_verification(core)


def initial(draft):
    return {
        "draft": draft,
        "status": "FAIL",
        "problem_units": ["U1"],
        "verification_errors": [],
        "verification_method": "cited_claims",
        "claim_checks": [{"claim_id": "claim-1", "fingerprint": "fixed"}],
    }


def test_claim_revision_passes_complete_initial_review_to_selective_recheck():
    old = initial("old")
    calls = []

    def verify(packet, draft, call, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        if draft == "old":
            return copy.deepcopy(old)
        assert kwargs["previous_review"] == old
        return {**old, "draft": draft, "status": "PASS", "problem_units": []}

    core = SimpleNamespace(
        verify=verify,
        revise=lambda *args: {
            "draft": "revised",
            "changed_claims": [{"claim_id": "claim-1"}],
            "requires_verification": True,
        },
    )
    result = check_biocheck(
        None, {}, "old", lambda *args: {}, loaded=(core, "v"), verification_mode="paragraph"
    )
    assert result["final_text"] == "revised" and result["status"] == "PASS"
    assert result["initial_verification"] == old
    assert "previous_review" not in calls[0] and calls[1]["previous_review"] == old


def test_known_bad_revision_preserves_initial_and_invalid_verification_never_revises():
    old = initial("old")
    revised = []

    def revise(*args):
        revised.append(True)
        raise ValueError("synthetic rejected replacements")

    core = SimpleNamespace(verify=lambda *args, **kwargs: copy.deepcopy(old), revise=revise)
    result = check_biocheck(
        None, {}, "old", lambda *args: {}, loaded=(core, "v"), verification_mode="paragraph"
    )
    assert result["status"] == "FAIL" and result["final_text"] == "old"
    assert result["verification"] == old
    assert result["repair_error_code"] == "biocheck_revision_invalid"
    old["verification_errors"] = ["S999 does not exist"]
    revised.clear()
    result = check_biocheck(
        None, {}, "old", lambda *args: {}, loaded=(core, "v"), verification_mode="paragraph"
    )
    assert result["verification"] == old and result["final_text"] == "old" and not revised


def test_local_loader_hashes_claim_dependencies_but_never_runs_a_service(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    for name in ("evidence.py", "prompts.py"):
        (app / name).write_text("")
    (app / "core.py").write_text(
        chr(10).join(
            [
                "def generate(*a): pass",
                "def verify(*a): pass",
                "def revise(*a): pass",
            ]
        )
    )
    (app / "claims.py").write_text("from . import core" + chr(10))
    (app / "rpc.py").write_text("PROVIDER_VERSION='synthetic'" + chr(10))
    (app / "service.py").write_text("raise RuntimeError('service must not load')")
    settings = SimpleNamespace(biocheck_project_dir=tmp_path)
    core, version = load_biocheck(settings)
    assert core is not None
    (app / "service.py").write_text("raise RuntimeError('changed service must not load')")
    other, changed = load_biocheck(settings)
    assert other is not None and other is not core and changed != version
    (app / "claims.py").write_text(
        "from . import core" + chr(10) + "core.CLAIMS_CONTRACT = 'SYNTHETIC-changed'" + chr(10)
    )
    updated, claim_changed = load_biocheck(settings)
    assert updated is not None and updated is not other and claim_changed != changed
    assert updated.CLAIMS_CONTRACT == "SYNTHETIC-changed"


@pytest.mark.parametrize("stage", ["verify_claim", "verify_claim_retry", "revise_claims"])
@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_native_rpc_claim_completion_envelope_is_preserved_and_received_replay_is_free(stage, finish_reason):
    import json

    import httpx
    from bioradar.adapters.professional_rpc import execute_professional
    from test_professional_rpc import completed, required

    requests, calls = [], []
    state = {}
    operation = "revise" if stage == "revise_claims" else "verify"
    arguments = (
        {"review": {"verification_method": "cited_claims"}}
        if operation == "revise"
        else {"mode": "paragraph"}
    )
    envelope = {"finish_reason": finish_reason, "output": {"checks": []} if finish_reason == "stop" else None}

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200, json=completed(body) if body["model_receipts"] else required(body, stage=stage)
        )

    def call(*args, **kwargs):
        calls.append(kwargs["call_id"])
        return copy.deepcopy(envelope)

    with httpx.Client(base_url="http://synthetic", transport=httpx.MockTransport(handler)) as client:
        kwargs = dict(
            provider="biocheck",
            operation_id="synthetic-" + stage,
            operation=operation,
            arguments=arguments,
            call=call,
            state=state,
            checkpoint=lambda: None,
        )
        first = execute_professional(client, **kwargs)
        assert execute_professional(client, **kwargs) == first
    receipt = requests[-1]["model_receipts"][0]
    assert receipt["finish_reason"] == finish_reason and receipt["output"] == envelope["output"]
    assert len(requests) == 2 and len(calls) == 1
