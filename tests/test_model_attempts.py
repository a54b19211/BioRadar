"""The radar's authorization of each model request a service makes, and its record of the outcome: the
protocol MechEvidence speaks (model_attempt_authorization.v1, model_attempt_outcome.v1), from both sides."""

from datetime import timedelta

import pytest
from mech_support import (
    EVALUATOR,
    KEY,
    VERSION,
    Fab,
    acknowledged,
    attempt_body,
    outcome_body,
    service_client,
    started,
)
from sqlalchemy import select

from bioradar import db
from bioradar.config import settings
from bioradar.research import attempts, lease

AUTHORIZE = "/internal/v1/model-attempts/authorize"


def outcome_url(decision):
    return f"/internal/v1/model-attempts/{decision['attempt_id']}/outcome"


def stored(ref):
    with db.session() as s:
        return [(r.idx, r.status, r.reason_code, r.delivered, r.http_status) for r in attempts.ledger(s, ref)]


@pytest.fixture
def client(monkeypatch):
    return service_client(monkeypatch)


def test_a_two_request_evaluation_is_authorized_and_settled_request_by_request(client):
    analysis, ref = started()
    for n in (1, 2):
        answer = client.post(AUTHORIZE, json=attempt_body(ref, n, stage="decompose" if n == 1 else "review"))
        assert answer.status_code == 200
        decision = answer.json()
        # exactly what MechEvidence checks before it sends: the version, an echo of this attempt, key,
        # evaluator
        assert decision["contract_version"] == attempts.AUTHORIZE_V1 and decision["valid"] is True
        assert decision["attempt_id"] == f"{n:032x}" and decision["evaluation_key"] == KEY
        assert decision["evaluator_id"] == EVALUATOR["evaluator_id"] and decision["decision_id"]
        assert stored(ref)[-1][:2] == (n, "prepared")  # allowed means: may have been sent from now on
        body = outcome_body(ref, decision, n)
        ack = client.post(outcome_url(decision), json=body)
        assert ack.status_code == 200 and acknowledged(ack.json(), decision, body)
    assert stored(ref) == [(1, "settled", None, "yes", 200), (2, "settled", None, "yes", 200)]
    with db.session() as s:
        assert attempts.counts(s, ref) == {"prepared": 0, "settled": 2, "refused": 0}
        assert s.scalars(select(db.ModelAttempt.response_text)).all() == ["the raw answer"] * 2


@pytest.mark.parametrize("token", [None, "wrong"])
def test_only_the_service_with_the_token_is_answered(client, token):
    _, ref = started()
    client.headers.pop("Authorization")
    if token:
        client.headers["Authorization"] = f"Bearer {token}"
    assert client.post(AUTHORIZE, json=attempt_body(ref, 1)).status_code == 401
    assert stored(ref) == []


def test_without_a_configured_token_no_service_is_answered(client, monkeypatch):
    _, ref = started()
    monkeypatch.setattr(settings(), "model_attempt_token", "")
    assert client.post(AUTHORIZE, json=attempt_body(ref, 1)).status_code == 503


def refused(client, body, code, status=403):
    answer = client.post(AUTHORIZE, json=body)
    assert answer.status_code == status and answer.json()["valid"] is False
    assert answer.json()["reason_code"] == code and "text" not in answer.json()
    return answer.json()


def test_a_ref_authorizes_only_its_own_evaluation(client):
    """The ref of analysis A with the key, the evaluator, the model or the endpoint of another evaluation."""
    _, ref = started()
    refused(client, attempt_body(ref, 1, key="1" * 64), "evaluation_key_mismatch")
    refused(client, attempt_body(ref, 2, evaluator_id="2" * 64), "evaluator_mismatch")
    refused(client, attempt_body(ref, 3, model="another-model"), "model_mismatch")
    refused(client, attempt_body(ref, 4, endpoint_id="another-endpoint"), "model_mismatch")
    refused(client, attempt_body(ref, 5, stage="report"), "model_mismatch")
    refused(client, attempt_body("auth-unknown", 1), "unknown_authorization")
    assert [row[2] for row in stored(ref)] == ["evaluation_key_mismatch", "evaluator_mismatch"] + [
        "model_mismatch"
    ] * 3
    with db.session() as s:
        assert attempts.counts(s, ref) == {"prepared": 0, "settled": 0, "refused": 5}  # nothing was allowed


@pytest.mark.parametrize(
    "damage",
    [
        lambda b: b.pop("attempt"),
        lambda b: b.update(contract_version="model_attempt_authorization.v0"),
        lambda b: b.update(service="biocheck"),
        lambda b: b["attempt"].update(index="1"),
        lambda b: b["attempt"].update(index=True),
        lambda b: b["attempt"].update(id="short"),
        lambda b: b["attempt"].update(request_body_sha256="nothex"),
        lambda b: b.update(evaluation_key=None),
    ],
)
def test_a_request_that_is_not_the_protocol_gets_nothing(client, damage):
    _, ref = started()
    body = attempt_body(ref, 1)
    damage(body)
    refused(client, body, "malformed_request", 422)
    assert stored(ref) == []


def test_a_request_is_never_allowed_twice_and_the_numbers_only_go_up(client):
    _, ref = started()
    assert client.post(AUTHORIZE, json=attempt_body(ref, 1)).status_code == 200
    refused(client, attempt_body(ref, 1), "attempt_already_decided")  # the same attempt id again
    refused(client, attempt_body(ref, 1, id="f" * 32), "index_not_increasing")  # a new id, an old number
    assert client.post(AUTHORIZE, json=attempt_body(ref, 3)).status_code == 200
    refused(client, attempt_body(ref, 2), "index_not_increasing")
    assert [(r[0], r[1]) for r in stored(ref)] == [(1, "prepared"), (3, "prepared")]


def test_the_allowance_is_counted_by_what_was_reserved_not_by_what_came_back(client):
    _, ref = started(max_attempts=2)
    for n in (1, 2):  # allowed, never settled: they may have been sent
        assert client.post(AUTHORIZE, json=attempt_body(ref, n)).status_code == 200
    refused(client, attempt_body(ref, 3), "attempt_budget_exhausted")
    with db.session() as s:
        assert attempts.counts(s, ref) == {"prepared": 2, "settled": 0, "refused": 1}


def test_an_expired_authorization_allows_nothing(client):
    _, ref = started()
    with db.session() as s:
        s.get(db.ModelAttemptAuthorization, ref).expires_at = db.now() - timedelta(seconds=1)
    refused(client, attempt_body(ref, 1), "authorization_expired")


def test_an_authorization_cannot_outlive_the_lease_that_asked_for_it(client):
    row_id, ref = started(seconds=10**6)
    with db.session() as s:
        auth = s.get(db.ModelAttemptAuthorization, ref)
        assert auth.expires_at <= s.get(db.Analysis, row_id).lease_until


def test_when_the_execution_lost_its_lease_the_next_request_is_not_allowed(client):
    """A worker that took the analysis over has a new token; the old execution sends nothing more."""
    analysis, ref = started()
    first = client.post(AUTHORIZE, json=attempt_body(ref, 1)).json()
    with db.session() as s:
        lease.take([s.get(db.Analysis, analysis)], "successor", db.now(), lease.ANALYSIS_LEASE)
    refused(client, attempt_body(ref, 2), "lease_lost")
    # what the old execution's request 1 produced is still recorded, on its own attempt
    body = outcome_body(ref, first, 1)
    assert acknowledged(client.post(outcome_url(first), json=body).json(), first, body)
    assert [r[:2] for r in stored(ref)] == [(1, "settled"), (2, "refused")]


def test_a_lease_that_ran_out_is_a_lease_lost(client):
    analysis, ref = started()
    with db.session() as s:
        s.get(db.Analysis, analysis).lease_until = db.now() - timedelta(seconds=1)
    refused(client, attempt_body(ref, 1), "lease_lost")


def test_a_source_revoked_after_the_first_request_stops_the_second(client):
    """Revocation in the middle of an evaluation: the snapshot taken when the authorization was issued
    said the source was readable; Fabric's answer now decides."""
    _, ref = started()
    first = client.post(AUTHORIZE, json=attempt_body(ref, 1)).json()
    assert client.post(outcome_url(first), json=outcome_body(ref, first, 1)).status_code == 200
    Fab.readable = set()
    refused(client, attempt_body(ref, 2), "source_access_withdrawn")
    assert [r[:3] for r in stored(ref)] == [(1, "settled", None), (2, "refused", "source_access_withdrawn")]


def test_a_fixed_version_that_is_no_longer_visible_stops_the_request(client):
    _, ref = started()
    Fab.hidden = {VERSION}
    refused(client, attempt_body(ref, 1), "version_not_visible")


def test_when_fabric_cannot_answer_nothing_is_allowed(client):
    _, ref = started()
    Fab.down = True
    refused(client, attempt_body(ref, 1), "source_access_unconfirmed")


def test_the_sources_of_every_ancestor_material_are_checked(client):
    """The authorization names every source the materials (and their earlier rounds) came from."""
    _, ref = started(sources=("open_src", "ancestor_src"))
    Fab.readable = {"open_src"}
    refused(client, attempt_body(ref, 1), "source_access_withdrawn")
    Fab.readable = {"open_src", "ancestor_src"}
    assert client.post(AUTHORIZE, json=attempt_body(ref, 2)).status_code == 200


# ---- the outcome ----


def allowed(client, n=1, **more):
    analysis, ref = started(**more)
    return ref, client.post(AUTHORIZE, json=attempt_body(ref, n)).json()


def test_an_outcome_is_recorded_once_and_acknowledged_again_when_repeated(client):
    ref, decision = allowed(client)
    body = outcome_body(ref, decision, 1)
    for _ in range(2):
        answer = client.post(outcome_url(decision), json=body)
        assert answer.status_code == 200 and acknowledged(answer.json(), decision, body)
    changed = outcome_body(ref, decision, 1, text="another answer")
    conflict = client.post(outcome_url(decision), json=changed)
    assert conflict.status_code == 409 and conflict.json()["reason_code"] == "outcome_conflict"
    assert stored(ref) == [(1, "settled", None, "yes", 200)]


def test_an_unanswered_request_settles_with_no_text_and_no_hash(client):
    ref, decision = allowed(client)
    body = outcome_body(ref, decision, 1, text=None, delivered="unknown", status=None, error="timeout")
    answer = client.post(outcome_url(decision), json=body)
    assert answer.status_code == 200 and answer.json()["response_sha256"] is None
    assert acknowledged(answer.json(), decision, body)
    with db.session() as s:
        row = attempts.ledger(s, ref)[0]
        assert (row.delivered, row.error, row.response_text, row.response_sha256) == (
            "unknown",
            "timeout",
            None,
            None,
        )


def test_a_wrong_hash_a_wrong_decision_or_another_attempt_is_not_recorded(client):
    ref, decision = allowed(client)
    wrong_hash = dict(outcome_body(ref, decision, 1), response_sha256="0" * 64)
    answer = client.post(outcome_url(decision), json=wrong_hash)
    assert answer.status_code == 422 and answer.json()["reason_code"] == "response_hash_mismatch"
    other = dict(outcome_body(ref, decision, 1), decision_id="dec-other")
    assert client.post(outcome_url(decision), json=other).status_code == 409
    assert (
        client.post(
            "/internal/v1/model-attempts/" + "9" * 32 + "/outcome", json=outcome_body(ref, decision, 1)
        ).status_code
        == 404
    )
    mismatched = dict(outcome_body(ref, decision, 1), attempt_id="8" * 32)
    assert client.post(outcome_url(decision), json=mismatched).status_code == 404
    assert stored(ref) == [(1, "prepared", None, None, None)]  # still only reserved: it may have been sent


def test_a_refused_request_has_no_outcome(client):
    _, ref = started()
    body = attempt_body(ref, 1, key="1" * 64)
    refusal = client.post(AUTHORIZE, json=body)
    assert refusal.status_code == 403
    faked = {"decision_id": "dec-x", "attempt_id": body["attempt"]["id"]}
    answer = client.post(outcome_url(faked), json=outcome_body(ref, faked, 1))
    assert answer.status_code == 409 and answer.json()["reason_code"] == "attempt_was_refused"


def test_a_truncated_answer_keeps_the_hash_of_the_whole_and_a_partial_text(client):
    ref, decision = allowed(client)
    body = dict(
        outcome_body(ref, decision, 1, text="the first part"), response_sha256="b" * 64, truncated=True
    )
    assert client.post(outcome_url(decision), json=body).status_code == 200
    with db.session() as s:
        row = attempts.ledger(s, ref)[0]
        assert (
            row.truncated is True
            and row.response_sha256 == "b" * 64
            and row.response_text == "the first part"
        )
