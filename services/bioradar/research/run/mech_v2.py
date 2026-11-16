"""Stage mechanism over MechEvidence's version-bound contract (mechevidence.evaluate.v2).

What goes out is not just text: every material is re-read from Fabric by its binding right before the
request (a part that is unreadable or no longer the bound text is not sent), the request carries the
binding, the scope in three states and an authorization the radar issued for exactly this evaluation, and
the evaluation key (the reuse key, which MechEvidence computes and answers at POST /v2/reuse-key; the
radar keeps no copy of the rule) says what it would be a result of. MechEvidence checks its own receipts
before answering; the radar checks a receipt against the published JSON Schema, that it is the answer to
what POST /v2/reuse-key said (evaluator, key, material echo), and that the set of model requests it archives
is the set the radar allowed itself (bioradar.mech.ledger): a receipt that says its audit is complete is not
believed on that word alone. A result that is not adopted (failed,
receipt_mismatch, ledger_mismatch, audit_incomplete) feeds no report and no cache. A complete one is
cached by its key; a hit is used only after the sources it came from are confirmed readable now and every
fixed version is visible at this analysis's cutoff. Every send and every write is fenced by the analysis
lease like any other (Run.check_lease, Run.writing)."""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bioradar import db, guard
from bioradar.adapters import services
from bioradar.adapters.fabric import FabricError, noticed
from bioradar.adapters.services import ServiceError
from bioradar.material import MaterialError, reread
from bioradar.mech import receipt as receipt_format
from bioradar.mech.ledger import Attempt, reconcile
from bioradar.mech.receipt import CONTRACT_V2
from bioradar.research import attempts
from bioradar.research.run import Withheld, submit

if TYPE_CHECKING:
    from bioradar.research.pipeline import Run

log = logging.getLogger("bioradar")
UNKNOWN_WORDS = {"", "unknown", "unspecified", "not reported", "not stated", "n/a", "未知", "未说明"}
NOT_ADOPTED = ("receipt_invalid", "receipt_mismatch", "audit_incomplete", "ledger_mismatch")


class EvaluatorChanged(Exception):
    """MechEvidence answered 409: it is not the evaluator the request was prepared for."""

    def __init__(self, evaluator):
        super().__init__("evaluator_changed")
        self.evaluator = evaluator


@dataclass
class Keyed:
    """What MechEvidence answered for a request at POST /v2/reuse-key: who would evaluate it, its reuse
    key and the material echo its receipt must carry."""

    evaluator: dict
    key: str
    echo: list[dict]

    @property
    def hashes(self) -> list[str]:
        return [row["binding_sha256"] for row in self.echo]


@dataclass
class Bound:
    """One material ready to send: the evidence row, the request's material (text and binding), where it came
    from."""

    row: object
    item: dict
    parts: list[dict]


# ---- the request ----


def dimension(value) -> dict:
    """One scope dimension in its three states. A plan that said nothing (a blank, an 'unknown' word, an old
    plan's empty string) is `unknown`, never `not_required`: not knowing the scope is not the question not
    caring about it. A plan that says so (state not_required) is believed."""
    if isinstance(value, dict) and value.get("state") in {"stated", "unknown", "not_required"}:
        state, text = value["state"], value.get("value")
        if state == "stated" and isinstance(text, str) and text.strip().casefold() not in UNKNOWN_WORDS:
            return {"state": "stated", "value": text}
        return {"state": "unknown" if state == "stated" else state}
    if isinstance(value, str) and value.strip().casefold() not in UNKNOWN_WORDS:
        return {"state": "stated", "value": value}
    return {"state": "unknown"}


def scope_of(target: dict) -> dict:
    return {name: dimension(target.get(name)) for name in ("species", "system", "disease")}


def parts_of(binding: dict) -> list[dict]:
    """The fixed versions a binding draws on, as the authorization names them."""
    seen = []
    for segment in binding["segments"]:
        part = segment.get("part")
        if part is not None and all(k in part for k in ("source", "record_id", "version_id")):
            found = {k: part[k] for k in ("source", "record_id", "version_id")}
            if found not in seen:
                seen.append(found)
    return seen


def alone(item: dict) -> dict:
    """A one-material request, to ask whether this material could be sent at all."""
    return {
        "contract_version": CONTRACT_V2,
        "question": "q",
        "mechanism": "m",
        "scope": {name: {"state": "unknown"} for name in ("species", "system", "disease")},
        "materials": [item],
        "authorization_ref": "auth-check",
    }


def bind(run: "Run", rows, describe) -> list[Bound]:
    """The materials that can be sent: each re-read from Fabric by its binding. Text that names no fixed
    version (read before bindings existed), a part Fabric will not give now (revoked, not visible), a part
    whose text is no longer the bound text and a material MechEvidence refuses (POST /v2/reuse-key
    answers 422) are left out, each with a note; none of them is sent. ServiceError when MechEvidence cannot
    be asked."""
    bound = []
    for row in rows:
        material = noticed(submit.material_of(run, row), (row.meta or {}).get("clean_text_state"))
        binding = material.binding()
        if not parts_of(binding):
            run.note("mechevidence_material_unbound", row.ref)
            continue
        try:
            text = reread(binding, run.fabric.part_texts)
        except MaterialError as error:
            gone = error.code == "part_unreadable"
            run.note("mechevidence_material_unreadable" if gone else "mechevidence_material_changed", row.ref)
            continue
        except (FabricError, httpx.HTTPError):
            run.note("mechevidence_material_unreadable", row.ref)
            continue
        item = {"id": row.ref, "title": row.title, "text": text, "binding": binding, **describe(row)}
        try:
            services.mechevidence_reuse_key(run.s, alone(item))
        except ServiceError as bad:
            if not bad.body:  # no answer at all: MechEvidence cannot be asked, not a bad material
                raise
            run.note("mechevidence_material_invalid", f"{row.ref} {bad.code}")
            continue
        bound.append(Bound(row, item, parts_of(binding)))
    return bound


def keyed(run: "Run", base: dict) -> Keyed:
    """Who would evaluate this request now, its reuse key and material echo, as MechEvidence answers them
    (POST /v2/reuse-key), checked against the format it publishes (GET /v2/contract, fetched here once, before
    anything is sent)."""
    schema = receipt_format.published(run.s)
    answer = services.mechevidence_reuse_key(run.s, base)
    if receipt_format.reuse_key_problems(answer, schema) or answer["contract_version"] != CONTRACT_V2:
        raise ServiceError("mechevidence", "reuse_key_invalid")
    return Keyed(answer["evaluator"], answer["reuse_key"], answer["materials"])


# ---- the cache ----


def cached(run: "Run", keyed: Keyed, bound: list[Bound], sources: set[str]):
    """The stored complete evaluation of exactly this request by exactly this evaluator, or None. A hit is
    used only after Fabric confirms now that every source it came from can be read (a revoked source gives
    Withheld, not a stale answer) and that every fixed version it drew on is visible at this analysis's
    cutoff."""
    with db.session() as s:
        row = s.get(db.MechEvaluation, keyed.key)
        if row is None:
            return None
        found = (row.evaluator_id, row.contract_version, row.response.get("complete"), list(row.sources))
    if found[:3] != (keyed.evaluator["evaluator_id"], CONTRACT_V2, True):
        return None
    run.confirm_sources(set(found[3]) | sources)
    try:
        for item in bound:
            for part in item.parts:
                seen = run.fabric.version_visible(
                    part["source"],
                    part["record_id"],
                    part["version_id"],
                    is_demo=run.input["is_demo"],
                    cutoff=run.cutoff,
                    mode=run.replay_mode,
                )
                if not seen:
                    return None
    except (FabricError, httpx.HTTPError):
        raise Withheld("source_access_unconfirmed") from None
    return row


def store(run: "Run", keyed: Keyed, request, bound, sources, response, how):
    """The evaluation into the cache (the first complete result for a key stays) and this analysis's use."""
    key, evaluator = keyed.key, keyed.evaluator
    with run.writing() as s:
        if s.get(db.MechEvaluation, key) is None:
            try:
                with s.begin_nested():
                    s.add(
                        db.MechEvaluation(
                            reuse_key=key,
                            contract_version=CONTRACT_V2,
                            evaluator_id=evaluator["evaluator_id"],
                            evaluator=evaluator,
                            proposition={"question": request["question"], "mechanism": request["mechanism"]},
                            scope=request["scope"],
                            materials=[
                                {"id": b.item["id"], "binding_sha256": h, "binding": b.item["binding"]}
                                for b, h in zip(bound, keyed.hashes)
                            ],
                            sources=sorted(sources),
                            response={k: v for k, v in response.items() if k != "llm_calls"},
                            first_analysis_id=run.id,
                        )
                    )
            except IntegrityError:
                pass  # another analysis stored the same evaluation first; its result stays
        s.flush()
        s.add(db.MechEvaluationUse(analysis_id=run.id, reuse_key=key, how=how))


def result_of(response: dict, keyed: Keyed, bound, reused: bool) -> dict:
    """The mechanism result an analysis keeps: the evaluation without its audit, and which evaluator, key and
    material bindings it is the result of."""
    dropped = {
        "llm_calls",
        "evaluator",
        "materials",
        "scope",
        "audit_complete",
        "contract_version",
        "reuse_key",
    }
    return {
        "status": "completed",
        **{k: v for k, v in response.items() if k not in dropped},
        "contract_version": CONTRACT_V2,
        "evaluator_id": keyed.evaluator["evaluator_id"],
        "reuse_key": keyed.key,
        "reused": reused,
        "materials": [{"ref": b.item["id"], "binding_sha256": h} for b, h in zip(bound, keyed.hashes)],
    }


# ---- what the service reports and what the ledger knows ----


def reported(call, schema: dict) -> dict | None:
    """A call of a receipt or an error answer as a log record, or None when its shape is not the
    contract's (it is not believed for anything, its requests are then accounted from the ledger)."""
    if receipt_format.call_shape_problems(call, schema):
        return None
    outputs = call["outputs"]
    return {
        "stage": call["stage"],
        "model": call["model"],
        "finish_reason": call["finish_reason"] if isinstance(call["finish_reason"], str) else None,
        "usage": call["usage"] if isinstance(call["usage"], dict) else {},
        "seconds": call["seconds"],
        "error": call["error"],
        "requests": len(outputs),  # the archive, not the claim
        "http": [o["status"] if o["status"] is not None else o["error"] for o in outputs],
        "outputs": outputs,
    }


def from_ledger(row) -> dict:
    """A request the radar allowed that no trusted call accounts for: it may have been sent, so it counts as a
    request; its usage is not known; what the service told the radar about its outcome is kept."""
    settled = row.status == "settled"
    return {
        "stage": row.stage,
        "model": row.model,
        "finish_reason": None,
        "usage": {},
        "seconds": 0,
        "error": "no_receipt" if settled else "outcome_unknown",
        "requests": 1,
        "http": [row.http_status if row.http_status is not None else row.error] if settled else [],
        "outputs": [
            {
                "attempt": 1,
                "sha256": row.response_sha256,
                "content": row.response_text,
                "attempt_id": row.attempt_id,
                "delivered": row.delivered,
                "status": row.http_status,
                "truncated": row.truncated,
            }
        ]
        if settled
        else [],
    }


def account(run: "Run", ref: str, calls, ids) -> None:
    """Log the model calls of an evaluation: those the service reported in a believable shape, with their
    raw answers, and one row for every request the radar allowed that none of them accounts for. The
    material reads that fed the evaluation are linked to its first review call."""
    schema = receipt_format.published(run.s)
    trusted = [r for r in (reported(c, schema) for c in (calls if isinstance(calls, list) else [])) if r]
    covered = {o["attempt_id"] for r in trusted for o in r["outputs"]}
    with db.session() as s:
        missing = [
            r for r in attempts.ledger(s, ref) if r.status != "refused" and r.attempt_id not in covered
        ]
    records = trusted + [from_ledger(r) for r in missing]
    if not records:
        return
    made = run.log(records, "mechevidence", authorization_ref=ref)
    first = next((n for n, r in enumerate(records) if r["stage"] == "review"), 0)
    with run.writing() as s:
        for read in s.scalars(
            select(db.MaterialRead).where(db.MaterialRead.id.in_(ids), db.MaterialRead.llm_call_id.is_(None))
        ):
            read.llm_call_id = made[first]


def reject(run: "Run", ref: str, reason: str, receipt, detail: dict) -> None:
    """Keep a receipt that was not adopted, whole. Audit of a request that was sent: recorded even if the
    lease was lost meanwhile."""
    with guard.audit(), db.session() as s:
        s.add(
            db.MechReceiptRejected(
                analysis_id=run.id,
                authorization_ref=ref,
                reason=reason,
                response=receipt
                if isinstance(receipt, (dict, list))
                else {"not_an_object": repr(receipt)[:2000]},
                detail=detail,
            )
        )


def ledger_rows(ref: str) -> list[Attempt]:
    with db.session() as s:
        return [
            Attempt(
                r.attempt_id,
                r.decision_id,
                r.idx,
                r.status,
                r.delivered,
                r.http_status,
                r.response_sha256,
                r.truncated,
            )
            for r in attempts.ledger(s, ref)
        ]


def judged(receipt, keyed: Keyed, schema: dict) -> tuple[str | None, dict]:
    """(reason, detail) of a 200 receipt that is not adoptable, or (None, {}). Not the published shape:
    receipt_invalid; not the answer to what POST /v2/reuse-key said: receipt_mismatch; an audit it does not
    call complete or a cut raw answer: audit_incomplete. MechEvidence's own cross-field rules are its to
    enforce (it never returns a receipt that breaks them) and are not repeated here."""
    invalid = receipt_format.shape_problems(receipt, schema)
    if invalid:
        return "receipt_invalid", {"mismatch": [], "invalid": invalid}
    expected = {
        "contract_version": CONTRACT_V2,
        "evaluator": keyed.evaluator,
        "reuse_key": keyed.key,
        "materials": keyed.echo,
    }
    mismatch = [
        {"field": field, "expected": value, "received": receipt[field]}
        for field, value in expected.items()
        if receipt[field] != value
    ]
    if mismatch:
        return "receipt_mismatch", {"mismatch": mismatch}
    if receipt["audit_complete"] is not True or any(
        out["truncated"] for call in receipt["llm_calls"] for out in call["outputs"]
    ):
        return "audit_incomplete", {"mismatch": []}
    return None, {}


def adopt(run: "Run", request, keyed: Keyed, issued, receipt, bound, sources, ids) -> dict:
    """The mechanism result for a 200 receipt: adopted only if it is the published shape, the answer to this
    request and agrees with the radar's ledger; otherwise failed, with the receipt kept for audit."""
    reason, detail = judged(receipt, keyed, receipt_format.published(run.s))
    calls = receipt.get("llm_calls") if isinstance(receipt, dict) else []
    problems = [] if reason else reconcile(ledger_rows(issued.ref), receipt, issued.max_attempts)
    if reason is not None or problems:
        reason = reason or "ledger_mismatch"
        detail = detail or {"reconciliation": problems}
        reject(run, issued.ref, reason, receipt, detail)
        account(run, issued.ref, calls, ids)
        run.note(f"mechevidence_{reason}")
        return {"status": "failed", "reason": reason}
    account(run, issued.ref, calls, ids)
    if receipt["complete"]:
        store(run, keyed, request, bound, sources, receipt, "computed")
    else:
        run.note("mechevidence_incomplete", ", ".join(receipt["not_complete_because"]))
    return result_of(receipt, keyed, bound, False)


# ---- the stage ----


def fail(run: "Run", ref: str, exc: ServiceError, ids) -> dict:
    """A request that did not come back as a receipt: 409 means a different evaluator now; everything else is
    a failure whose model calls (and the requests the radar allowed without hearing the outcome) are still
    accounted for."""
    if exc.code == "evaluator_changed" and isinstance(exc.body.get("evaluator"), dict):
        raise EvaluatorChanged(exc.body["evaluator"])
    account(run, ref, exc.calls, ids)
    run.note(f"mechevidence_{exc.code}")
    return {"status": "failed", "reason": exc.code}


def send(run: "Run", base, keyed: Keyed, bound, sources, ids) -> dict:
    budget = max(1, min(10, run.calls_left() - 4))
    materials = [
        {"ref": b.item["id"], "binding_sha256": h, "parts": b.parts} for b, h in zip(bound, keyed.hashes)
    ]
    with run.writing() as s:
        issued = attempts.issue(
            s,
            run=run,
            evaluation_key=keyed.key,
            evaluator=keyed.evaluator,
            materials=materials,
            sources=sources,
            max_attempts=2 * budget,  # a format retry is a second request of a call
            seconds=run.s.mechevidence_authorization_seconds,
        )
    request = {
        **base,
        "expected_evaluator_id": keyed.evaluator["evaluator_id"],
        "authorization_ref": issued.ref,
        "max_llm_calls": budget,
    }
    try:
        with submit.outbound(run, ids):
            run.confirm_sources(sources)
            receipt = services.mechevidence_v2(run.s, request)
    except ServiceError as exc:
        return fail(run, issued.ref, exc, ids)
    return adopt(run, request, keyed, issued, receipt, bound, sources, ids)


def evaluate(run: "Run", target: dict, rows, describe) -> None:
    """The mechanism stage for contract v2: sets run.p["mechanism_result"]."""
    run.upstream_still_readable()
    try:
        bound = bind(run, rows, describe)
    except ServiceError as exc:
        run.note(f"mechevidence_{exc.code}")
        run.p["mechanism_result"] = {"status": "failed", "reason": exc.code}
        return
    if not bound:
        run.p["mechanism_result"] = {"status": "not_run", "reason": "no_bound_material"}
        return
    sources = run.carried({b.row.source for b in bound})
    _, ids = submit.send_rows(run, "mechanism", "mechevidence", [b.row for b in bound])
    base = {
        "contract_version": CONTRACT_V2,
        "question": run.asked,
        "mechanism": target["statement"],
        "scope": scope_of(target),
        "materials": [b.item for b in bound],
        "authorization_ref": "auth-pending",
    }
    for attempt in range(2):  # one re-key after a 409, then the evaluator is unstable
        try:
            found = keyed(run, base)
        except ServiceError as exc:
            submit.mark(run, ids, "not_sent")
            run.note(f"mechevidence_{exc.code}")
            run.p["mechanism_result"] = {"status": "failed", "reason": exc.code}
            return
        try:
            hit = cached(run, found, bound, sources)
        except Withheld:
            submit.mark(run, ids, "withheld")
            raise
        if hit is not None:
            submit.mark(run, ids, "reused")
            store(run, found, base, bound, sources, hit.response, "reused")
            run.p["mechanism_result"] = result_of(hit.response, found, bound, True)
            return
        try:
            run.p["mechanism_result"] = send(run, base, found, bound, sources, ids)
            return
        except EvaluatorChanged:
            run.note("mechevidence_evaluator_changed", f"attempt {attempt + 1}")
    submit.mark(run, ids, "sent")
    run.p["mechanism_result"] = {"status": "failed", "reason": "evaluator_unstable"}
