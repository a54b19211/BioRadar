"""The authorization and attempt ledger for the model requests a professional service makes (MechEvidence v2).

The radar owns it: before it calls the service it issues an authorization (issue) that binds the analysis,
the execution (lease token) that asked, the evaluation key, the evaluator, the fixed materials and every
source they came from, the mode and cutoff, an expiry and how many requests it allows. The service asks
(authorize) before EVERY model HTTP request and sends only on a decision that echoes its attempt;
afterwards it hands over what happened (settle). Fabric stays the authority on source permission and
version visibility: each decision asks it now, and what was true when the authorization was issued
authorises nothing.

A prepared row means the request may have been sent from the moment it was allowed, whether or not an outcome
ever arrives. Reserved requests, requests known to be sent, and requests known not to have been sent are
different rows and different counts; the number of decisions is never the provider's request count.

Wire format: model_attempt_authorization.v1 and model_attempt_outcome.v1 (MechEvidence's authorization.py)."""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, select

from bioradar import db
from bioradar.adapters.capabilities import CapabilityError, SourceCapabilities
from bioradar.adapters.fabric import FabricError
from bioradar.research import lease
from bioradar.research.clock import utc

AUTHORIZE_V1 = "model_attempt_authorization.v1"
OUTCOME_V1 = "model_attempt_outcome.v1"
SERVICE = "mechevidence"
DECISION_SECONDS = 60  # a decision is for the request about to be sent, not for later
ARCHIVE_LIMIT = 256 * 1024  # the most of a raw answer a service archives whole
STAGES = {"decompose", "review"}
HEX64 = re.compile(r"[0-9a-f]{64}")
ID = re.compile(r"[A-Za-z0-9_-]{8,64}")


class Refused(Exception):
    """The request is not allowed (authorize) or the outcome is not recorded (settle); `code` says why and
    carries no material text. `status` is the HTTP status the API answers with."""

    def __init__(self, code: str, status: int = 403):
        super().__init__(code)
        self.code, self.status = code, status


@dataclass(frozen=True)
class Issued:
    ref: str
    expires_at: datetime
    max_attempts: int


def issue(
    session,
    *,
    run,
    evaluation_key: str,
    evaluator: dict,
    materials: list[dict],
    sources: set[str],
    max_attempts: int,
    seconds: int,
) -> Issued:
    """Write the authorization of one evaluation in the caller's (lease-fenced) session. The expiry never
    passes the analysis's own lease: an authorization cannot outlive the execution that asked for it."""
    now = db.now()
    expires = now + timedelta(seconds=seconds)
    row = session.get(db.Analysis, run.id)
    if run.lease_token is not None and row.lease_until is not None:
        expires = min(expires, utc(row.lease_until))
    investigation = session.get(db.Investigation, row.investigation_id)
    ref = "auth-" + secrets.token_hex(16)
    session.add(
        db.ModelAttemptAuthorization(
            ref=ref,
            service=SERVICE,
            workspace_id=investigation.workspace_id,
            analysis_id=run.id,
            lease_token=run.lease_token,
            evaluation_key=evaluation_key,
            evaluator_id=evaluator["evaluator_id"],
            evaluator=evaluator,
            materials=materials,
            sources=sorted(sources),
            is_demo=bool(run.input["is_demo"]),
            mode=run.replay_mode,
            cutoff=run.cutoff,
            expires_at=expires,
            max_attempts=max_attempts,
        )
    )
    session.flush()
    return Issued(ref, expires, max_attempts)


# ---- authorize: before every model HTTP request ----


def _field(body: dict, name: str, kind, pattern=None):
    value = body.get(name)
    if not isinstance(value, kind) or isinstance(value, bool) and kind is int:
        raise Refused("malformed_request", 422)
    if pattern is not None and not pattern.fullmatch(value):
        raise Refused("malformed_request", 422)
    return value


def parse_authorize(body) -> dict:
    """The fields of an authorization request, or Refused(malformed_request) for anything off."""
    if (
        not isinstance(body, dict)
        or body.get("contract_version") != AUTHORIZE_V1
        or body.get("service") != SERVICE
    ):
        raise Refused("malformed_request", 422)
    attempt = body.get("attempt")
    if not isinstance(attempt, dict):
        raise Refused("malformed_request", 422)
    return {
        "ref": _field(body, "authorization_ref", str, re.compile(r"[\x21-\x7e]{1,512}")),
        "key": _field(body, "evaluation_key", str, HEX64),
        "evaluator_id": _field(body, "evaluator_id", str, HEX64),
        "id": _field(attempt, "id", str, ID),
        "stage": _field(attempt, "stage", str),
        "index": _field(attempt, "index", int),
        "body_sha256": _field(attempt, "request_body_sha256", str, HEX64),
        "model": _field(attempt, "model", str),
        "endpoint_id": _field(attempt, "endpoint_id", str),
    }


def _bound(auth, want: dict, now) -> str | None:
    """What the authorization itself rules out: another evaluation, another model, an expiry, a lease lost."""
    model = (auth.evaluator.get("model_config") or {}) if isinstance(auth.evaluator, dict) else {}
    if want["key"] != auth.evaluation_key:
        return "evaluation_key_mismatch"
    if want["evaluator_id"] != auth.evaluator_id:
        return "evaluator_mismatch"
    if want["stage"] not in STAGES or (want["model"], want["endpoint_id"]) != (
        model.get("model"),
        model.get("endpoint_id"),
    ):
        return "model_mismatch"
    if utc(auth.expires_at) <= now:
        return "authorization_expired"
    return None


def _lease_held(session, auth, now) -> bool:
    """The execution that asked still holds the analysis (no token: a run made without fencing)."""
    if auth.lease_token is None:
        return True
    return lease.held(session.get(db.Analysis, auth.analysis_id), auth.lease_token, now)


def _fabric_says(fabric, auth) -> str | None:
    """Fabric's answer now: every source readable, every fixed version visible at the mode and cutoff."""
    try:
        readable = SourceCapabilities.parse(fabric.sources()).readable
        if not set(auth.sources) <= readable:
            return "source_access_withdrawn"
        cutoff = utc(auth.cutoff) if auth.cutoff else None
        for material in auth.materials:
            for part in material["parts"]:
                seen = fabric.version_visible(
                    part["source"],
                    part["record_id"],
                    part["version_id"],
                    is_demo=auth.is_demo,
                    cutoff=cutoff,
                    mode=auth.mode,
                )
                if not seen:
                    return "version_not_visible"
    except (FabricError, httpx.HTTPError, CapabilityError):
        return "source_access_unconfirmed"
    return None


def authorize(fabric, body) -> dict:
    """The decision for one model request, or Refused(code). Fabric is asked first (outside the lock); the
    checks that must not race and the reservation are one transaction on the authorization row, so two
    requests cannot both take the last place and a repeated request gets no second permission."""
    want = parse_authorize(body)
    now = db.now()
    with db.session() as s:
        auth = s.get(db.ModelAttemptAuthorization, want["ref"])
        if auth is None or auth.service != SERVICE:
            raise Refused("unknown_authorization")
        early = _bound(auth, want, now)
    reason = early or _fabric_says(fabric, auth)
    with db.session() as s:
        auth = s.get(db.ModelAttemptAuthorization, want["ref"], with_for_update=True)
        reason = reason or _bound(auth, want, db.now())
        if reason is None and not _lease_held(s, auth, db.now()):
            reason = "lease_lost"
        decision = _reserve(s, auth, want, reason)
    if isinstance(decision, str):
        raise Refused(decision)
    return decision


def _reserve(s, auth, want: dict, reason: str | None):
    """Record the decision on the locked authorization: a prepared row, or a refused one; returns the answer
    body, or the refusal code."""
    rows = list(s.scalars(select(db.ModelAttempt).where(db.ModelAttempt.authorization_ref == auth.ref)))
    if any(r.attempt_id == want["id"] for r in rows):
        return "attempt_already_decided"
    if any(r.idx >= want["index"] for r in rows):
        return "index_not_increasing"
    taken = sum(1 for r in rows if r.status in {"prepared", "settled"})
    reason = reason or ("attempt_budget_exhausted" if taken >= auth.max_attempts else None)
    now = db.now()
    decision_id = None if reason else "dec-" + secrets.token_hex(16)
    expires = min(utc(auth.expires_at), now + timedelta(seconds=DECISION_SECONDS))
    s.add(
        db.ModelAttempt(
            authorization_ref=auth.ref,
            attempt_id=want["id"],
            decision_id=decision_id,
            idx=want["index"],
            stage=want["stage"],
            request_body_sha256=want["body_sha256"],
            model=want["model"],
            endpoint_id=want["endpoint_id"],
            status="refused" if reason else "prepared",
            reason_code=reason,
            decided_at=now,
            expires_at=None if reason else expires,
        )
    )
    if reason:
        return reason  # recorded all the same: the transaction commits, the caller answers no
    return {
        "contract_version": AUTHORIZE_V1,
        "valid": True,
        "decision_id": decision_id,
        "attempt_id": want["id"],
        "evaluation_key": auth.evaluation_key,
        "evaluator_id": auth.evaluator_id,
        "expires_at": expires.isoformat(),
    }


# ---- settle: after every model HTTP request ----


def _is(kind):
    return lambda value: isinstance(value, kind) and not isinstance(value, bool)


def _or_none(check):
    return lambda value: value is None or check(value)


# the fields of model_attempt_outcome.v1 and what each must be (a bool is not an int)
OUTCOME_FIELDS = {
    "authorization_ref": _is(str),
    "decision_id": _is(str),
    "attempt_id": _is(str),
    "delivered": lambda v: v in {"yes", "no", "unknown"},
    "status": _or_none(_is(int)),
    "error": _or_none(_is(str)),
    "response_sha256": _or_none(lambda v: isinstance(v, str) and HEX64.fullmatch(v) is not None),
    "truncated": lambda v: isinstance(v, bool),
    "response_text": _or_none(_is(str)),
}


def parse_outcome(body) -> dict:
    if (
        not isinstance(body, dict)
        or body.get("contract_version") != OUTCOME_V1
        or body.get("service") != SERVICE
    ):
        raise Refused("malformed_request", 422)
    if not all(check(body.get(name)) for name, check in OUTCOME_FIELDS.items()):
        raise Refused("malformed_request", 422)
    digest, text = body["response_sha256"], body["response_text"]
    if text is not None:
        if digest is None or (
            not body["truncated"] and hashlib.sha256(text.encode("utf-8")).hexdigest() != digest
        ):
            raise Refused("response_hash_mismatch", 422)
        if body["truncated"] and len(text.encode("utf-8")) > ARCHIVE_LIMIT:
            raise Refused("malformed_request", 422)
    return body


def settle(attempt_id: str, body) -> dict:
    """Record the outcome of a request the radar allowed, and acknowledge it field by field. Idempotent
    for the same outcome; a different one for a settled request is a conflict. A late outcome of an
    execution that has since lost its lease is recorded on its own attempt (that is audit, not a send)."""
    outcome = parse_outcome(body)
    with db.session() as s:
        row = s.scalar(
            select(db.ModelAttempt).where(db.ModelAttempt.attempt_id == attempt_id).with_for_update()
        )
        if row is None or outcome["attempt_id"] != attempt_id:
            raise Refused("unknown_attempt", 404)
        if row.status == "refused":
            raise Refused("attempt_was_refused", 409)
        if (outcome["authorization_ref"], outcome["decision_id"]) != (row.authorization_ref, row.decision_id):
            raise Refused("decision_mismatch", 409)
        same = (row.delivered, row.http_status, row.response_sha256, row.truncated) == (
            outcome["delivered"],
            outcome["status"],
            outcome["response_sha256"],
            outcome["truncated"],
        )
        if row.status == "settled" and not same:
            raise Refused("outcome_conflict", 409)
        if row.status == "prepared":
            row.delivered, row.http_status, row.error = (
                outcome["delivered"],
                outcome["status"],
                outcome["error"],
            )
            row.response_sha256, row.truncated = outcome["response_sha256"], outcome["truncated"]
            row.response_text, row.status, row.settled_at = outcome["response_text"], "settled", db.now()
    return {
        "contract_version": OUTCOME_V1,
        "recorded": True,
        "attempt_id": attempt_id,
        "decision_id": outcome["decision_id"],
        "response_sha256": outcome["response_sha256"],
    }


# ---- the ledger as the run reads it ----


def ledger(session, ref: str) -> list:
    """The attempts of one authorization, in request order."""
    return list(
        session.scalars(
            select(db.ModelAttempt)
            .where(db.ModelAttempt.authorization_ref == ref)
            .order_by(db.ModelAttempt.idx)
        )
    )


def counts(session, ref: str) -> dict[str, int]:
    """Reserved (may have been sent), settled (known to have been sent or not, with its outcome), refused
    (known not to have been sent): different things, counted apart."""
    rows = session.execute(
        select(db.ModelAttempt.status, func.count())
        .where(db.ModelAttempt.authorization_ref == ref)
        .group_by(db.ModelAttempt.status)
    ).all()
    found = {status: n for status, n in rows}
    return {k: found.get(k, 0) for k in ("prepared", "settled", "refused")}
