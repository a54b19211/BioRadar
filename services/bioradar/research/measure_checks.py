"""T-29 P3: a measure from triage becomes a series point only through its own extraction check.

triage -> enqueue(): a candidate per measure, bound to the fixed clean artifact the quote was read from (the
material binding), with its literal texts and final values, and a check task for the current checker.
run(): each due task re-reads that artifact from Fabric (fixed version + processor, hash recomputed, read
permission now), locates every literal in it (never guessed), sends one contract-v2 request to BioCheck
(checker v5) outside any transaction, judges the answer with the shared extraction-contract verdict, appends
the check, and only a consistent item whose values are exact and actual is handed to `adopt` (the series
logic); a stored point must read back equal to what was checked. Everything else stays a candidate.
effective(): the measurements whose candidate's latest check under the current checker put them in the
series - points from before checking, points not yet checked by the current checker and points whose
artifact is no longer readable are history, not current."""

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import extraction_contract as ec
from sqlalchemy import func, or_, select

from bioradar import db
from bioradar.adapters import services
from bioradar.adapters.fabric import CURRENT, NOT_VISIBLE, FabricError
from bioradar.material import of

CHECKER, CONTRACT = ec.CHECKER_V5, ec.CONTRACT_V2
PRODUCER = {"name": "bioradar.triage", "version": "measure.v2"}
LEASE = timedelta(minutes=2)
BACKOFF = (60, 300, 1800, 7200)  # seconds between attempts after a service or Fabric failure
REVALIDATE = timedelta(hours=24)  # an adopted point's binding and permission are checked again this often
WINDOW, MAX_CHARS = 200, 20000
STOP = "。；;！？!?\n"
MODIFIERS = r"(?:(?:suspected|confirmed|probable|laboratory-confirmed|new|human|associated)\s+){0,2}"
FULLWIDTH = str.maketrans("０１２３４５６７８９．，－", "0123456789.,-")
MAX_DIGITS, MAX_DECIMALS = 18, 6


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def decimal_text(written):
    """The canonical decimal string of a written number (thousands commas and full-width digits read, nothing
    else), or None: never through a float, never beyond 18 digits / 6 decimals."""
    t = (written or "").strip().translate(FULLWIDTH).replace("−", "-")
    if not re.fullmatch(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", t):
        return None
    try:
        d = Decimal(t.replace(",", ""))
    except InvalidOperation:
        return None
    text = format(d.normalize(), "f") if d != 0 else "0"
    whole, _, frac = text.lstrip("-").partition(".")
    if len(whole.lstrip("0") + frac) > MAX_DIGITS or len(frac) > MAX_DECIMALS:
        return None
    return text


def structured_period(period, period_type):
    """Triage's normalized period string -> the contract's period, or None (a format reading only: what it
    means is BioCheck's to check against the written period)."""
    p, t = (period or "").strip().upper(), period_type
    if not p or t not in ("point", "interval"):
        return None
    forms = (
        ("day", r"\d{4}-\d{2}-\d{2}"),
        ("month", r"\d{4}-\d{2}"),
        ("quarter", r"\d{4}Q[1-4]"),
        ("half", r"\d{4}H[12]"),
        ("year", r"\d{4}"),
    )
    if "/" in p:
        a, _, b = p.partition("/")
        for precision, form in forms[:2]:
            if re.fullmatch(form, a) and re.fullmatch(form, b) and t == "interval":
                return {"type": "interval", "precision": precision, "start": a, "end": b}
        return None
    for precision, form in forms:
        if re.fullmatch(form, p):
            return {"type": t, "precision": precision, "value": p}
    return None


def final_of(m):
    """The values the check is about and a series point would store - or (None, reason)."""
    value = decimal_text(m["value_text"])
    if value is None:
        return None, "value_not_a_supported_number"
    denominator = None
    if m.get("denominator_text"):
        denominator = decimal_text(m["denominator_text"])
        if denominator is None:
            return None, "denominator_not_a_supported_number"
    period = structured_period(m.get("period"), m.get("period_type"))
    if (m.get("period_text") or m.get("period")) and period is None:
        return None, "period_form_not_supported"
    return {
        "object": m.get("object_text") or "",
        "metric": m["metric_text"],
        "value": value,
        "unit": m.get("unit_text") or "",
        "period": period,
        "scope": m.get("scope_text") or "",
        "scope_mode": "stated"
        if m.get("scope_text")
        else "object_metric_total"
        if m.get("object_text")
        else "unknown",
        "denominator": denominator,
        "measure_kind": m["kind"],
        "qualifier": m.get("qualifier") or "exact",
        "modality": m.get("modality") or "actual",
    }, None


def clause(text, a, b):
    s = max((text.rfind(c, 0, a) for c in STOP), default=-1) + 1
    ends = [i for i in (text.find(c, b) for c in STOP) if i != -1]
    return s, (min(ends) + 1 if ends else len(text))


def locate(text, q0, q1, m):
    """Real offsets in the artifact of every literal of the measure: in the quote once, else its clause once;
    numbers as whole tokens; the unit right after the value (a count noun after at most two listed modifiers).
    (spans, None) or (None, reason) - a literal that is not where it should be is never guessed."""
    c0, c1 = clause(text, q0, q1)
    spans = {}

    def find(name, literal):
        if name in ("value", "denominator"):
            pat = r"(?<![\d.,])" + re.escape(literal) + r"(?![\d]|[.,]\d)"
        else:
            pat = re.escape(literal)
        inq = [x.start() for x in re.finditer(pat, text[q0:q1])]
        if len(inq) == 1:
            return q0 + inq[0]
        if inq:
            return None
        inc = [x.start() for x in re.finditer(pat, text[c0:c1])]
        return c0 + inc[0] if len(inc) == 1 else None

    for name, key in (
        ("value", "value_text"),
        ("denominator", "denominator_text"),
        ("object", "object_text"),
        ("metric", "metric_text"),
        ("period", "period_text"),
        ("scope", "scope_text"),
    ):
        literal = m.get(key) or ""
        if not literal:
            continue
        at = find(name, literal)
        if at is None:
            return None, f"{name}_literal_not_located"
        spans[name] = (at, at + len(literal))
    unit = m.get("unit_text") or ""
    if unit:
        v_end = spans["value"][1]
        hit = re.match(r"\s*" + MODIFIERS, text[v_end : v_end + 80], re.IGNORECASE)
        off = hit.end() if text[v_end + hit.end() :].startswith(unit) else None
        if off is None:
            return None, "unit_literal_not_at_the_value"
        spans["unit"] = (v_end + off, v_end + off + len(unit))
    return spans, None


def build(candidate, text):
    """The contract-v2 request of one candidate over its re-read artifact text, or (None, reason)."""
    m = candidate.measure
    final, why = final_of(m)
    if final is None:
        return None, why
    if not candidate.raw_sha256:
        return None, "raw_sha256_unknown"  # the subject binds the original by its hash: never sent without it
    if candidate.quote_start is None or text[candidate.quote_start : candidate.quote_end] != m["quote"]:
        return None, "quote_not_at_its_place"
    spans, why = locate(text, candidate.quote_start, candidate.quote_end, m)
    if spans is None:
        return None, why
    lo = max(0, min([candidate.quote_start] + [a for a, _ in spans.values()]) - WINDOW)
    hi = min(len(text), max([candidate.quote_end] + [b for _, b in spans.values()]) + WINDOW)
    lo, hi = (
        min(lo, clause(text, candidate.quote_start, candidate.quote_end)[0]),
        max(hi, clause(text, candidate.quote_start, candidate.quote_end)[1]),
    )
    if hi - lo > MAX_CHARS:
        return None, "context_over_20000_characters"

    def span(name):
        a, b = spans[name]
        return {"passage": "p1", "start": a, "end": b}

    def field(name, value):
        return (
            {"state": "stated", "value": value, "span": span(name)}
            if name in spans and value
            else {"state": "unknown"}
        )

    fields = {
        "object": field("object", final["object"]),
        "metric": field("metric", final["metric"]),
        "value": field("value", final["value"]),
        "unit": field("unit", final["unit"]),
        "period": {"state": "stated", "value": final["period"], "span": span("period")}
        if final["period"] and "period" in spans
        else {"state": "unknown"},
        "scope": field("scope", final["scope"]),
        "scope_mode": final["scope_mode"],
        "denominator": field("denominator", final["denominator"]),
        "measure_kind": final["measure_kind"],
        "qualifier": final["qualifier"],
        "modality": final["modality"],
    }
    if final["measure_kind"] == "other":
        return None, "measure_kind_other_is_not_checkable"
    body = {
        "contract_version": CONTRACT,
        "subject": {
            "source_id": candidate.source,
            "record_id": candidate.record_id,
            "version_id": candidate.version_id,
            "raw_sha256": candidate.raw_sha256,
            "is_demo": candidate.is_demo,
            "artifact": {
                "processor_version": candidate.processor_version,
                "text_sha256": candidate.text_sha256,
            },
        },
        "passages": [{"id": "p1", "role": "primary", "start": lo, "end": hi, "text": text[lo:hi]}],
        "items": [
            {
                "id": f"m{candidate.id}",
                "kind": "measurement",
                "producer": PRODUCER,
                "locator": {"passage": "p1", "start": candidate.quote_start, "end": candidate.quote_end},
                "quote": m["quote"],
                "fields": fields,
            }
        ],
    }
    return (body, final), None


# ---- triage side -------------------------------------------------------------------------------------------


def enqueue(session, s, doc, item, topic, at, is_demo):
    """Candidates (and their check tasks) for the measures of one triaged record. Returns how many."""
    material = of(doc)
    text = material.text
    made = 0
    for measure in item.measures[:3] if item else []:
        m = measure.model_dump()
        status, reason, part, q = "pending", None, None, None
        hits = [x.start() for x in re.finditer(re.escape(m["quote"]), text)] if m["quote"] else []
        if len(hits) != 1:
            status, reason = "not_built", "quote_not_in_text" if not hits else "quote_in_several_places"
        else:
            part, off = material.at(hits[0])
            last, last_off = material.at(hits[0] + len(m["quote"]) - 1)
            if part is None or last != part or last_off != off + len(m["quote"]) - 1:
                status, reason = "not_built", "quote_not_in_one_artifact"
            elif part.get("kind") != "clean_text" or not part.get("processor_version"):
                status, reason = "not_built", "not_a_clean_artifact"
            else:
                q = (off, off + len(m["quote"]))
        earlier = session.scalars(
            select(db.MeasurementCandidate).where(
                db.MeasurementCandidate.version_id == doc["version_id"],
                db.MeasurementCandidate.workspace_id == s.workspace_id,
                db.MeasurementCandidate.is_demo == is_demo,
            )
        )
        if any(c.measure == m for c in earlier):
            continue  # this version's measure was enqueued before (a retried triage)
        candidate = db.MeasurementCandidate(
            workspace_id=s.workspace_id,
            is_demo=is_demo,
            source=doc["source"],
            record_id=doc["record_id"],
            version_id=doc["version_id"],
            raw_sha256=(part or {}).get("raw_sha256") or doc.get("raw_sha256"),
            processor_version=(part or {}).get("processor_version"),
            text_sha256=(part or {}).get("text_sha256"),
            quote_start=q[0] if q else None,
            quote_end=q[1] if q else None,
            measure=m,
            status=status,
            reason=reason,
            topic_id=topic.id if topic else None,
            direction=item.direction,
            observed_at=at,
            corrects=[
                [r.get("target_source", doc["source"]), r["target_record_id"], r.get("target_version_id")]
                for r in doc.get("relations") or []
                if r["kind"] == "corrects"
            ],
        )
        session.add(candidate)
        session.flush()
        if status == "pending":
            session.add(
                db.MeasurementCheckTask(
                    candidate_id=candidate.id,
                    checker_version=CHECKER,
                    state="queued",
                    attempts=0,
                    next_check_at=db.now(),
                )
            )
        made += 1
    return made


def backfill(session, s):
    """A check task for the current checker for every candidate that has none (a checker upgrade): its points
    leave the effective view until the current checker confirms them again."""
    have = select(db.MeasurementCheckTask.candidate_id).where(
        db.MeasurementCheckTask.checker_version == CHECKER
    )
    n = 0
    for c in session.scalars(
        select(db.MeasurementCandidate).where(
            db.MeasurementCandidate.status != "not_built", db.MeasurementCandidate.id.not_in(have)
        )
    ):
        session.add(
            db.MeasurementCheckTask(
                candidate_id=c.id, checker_version=CHECKER, state="queued", attempts=0, next_check_at=db.now()
            )
        )
        n += 1
    return n


# ---- the check worker --------------------------------------------------------------------------------------


def _claim(is_demo, now):
    token = uuid.uuid4().hex
    with db.session() as session:
        task = session.scalars(
            select(db.MeasurementCheckTask)
            .join(db.MeasurementCandidate, db.MeasurementCandidate.id == db.MeasurementCheckTask.candidate_id)
            .where(
                db.MeasurementCheckTask.checker_version == CHECKER,
                db.MeasurementCheckTask.state.in_(("queued", "waiting", "done")),
                db.MeasurementCheckTask.next_check_at <= now,
                db.MeasurementCandidate.is_demo == is_demo,
                or_(db.MeasurementCheckTask.lease_until.is_(None), db.MeasurementCheckTask.lease_until < now),
            )
            .order_by(db.MeasurementCheckTask.next_check_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        if task is None:
            return None
        task.lease_token, task.lease_until = token, now + LEASE
        return task.candidate_id, token


def _own(session, candidate_id, token, now):
    task = session.get(db.MeasurementCheckTask, (candidate_id, CHECKER), with_for_update=True)
    return (
        task
        if task is not None
        and task.lease_token == token
        and task.lease_until
        and _utc(task.lease_until) >= now
        else None
    )


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _readback(fabric, candidate):
    """(artifact text, None) when Fabric still serves this exact artifact to this reader, else (None, why)."""
    try:
        found = fabric.clean_texts(
            candidate.source,
            [candidate.version_id],
            is_demo=candidate.is_demo,
            mode=CURRENT,
            cutoff=datetime.now(UTC).isoformat(),
            processor_version=candidate.processor_version,
        )
    except FabricError as exc:
        return None, "source_unreadable" if exc.code in NOT_VISIBLE else "fabric_unavailable"
    except Exception:  # noqa: BLE001 - a failed read is unchecked, retried, never a pass
        return None, "fabric_unavailable"
    entry = (found or {}).get(candidate.version_id) or {}
    clean = entry.get("clean") or {}
    if entry.get("state") in NOT_VISIBLE or entry.get("state") == "not_visible":
        return None, "source_unreadable"
    text = clean.get("text")
    if (
        text is None
        or clean.get("processor_version") != candidate.processor_version
        or clean.get("text_sha256") != candidate.text_sha256
        or sha256(text) != candidate.text_sha256
    ):
        return None, "artifact_changed"
    return text, None


def _append(session, candidate, *, status, gate, error=None, reason=None, body=None, response=None):
    request = None
    if body is not None:
        request = {k: v for k, v in body.items() if k != "contract_version"}
    session.add(
        db.MeasurementCheck(
            candidate_id=candidate.id,
            checker_version=CHECKER,
            contract_version=CONTRACT,
            request_sha256=ec.request_sha256(request) if request else None,
            request=request,
            response=response,
            status=status,
            error_code=error,
            gate=gate,
            gate_reason=reason,
        )
    )
    session.flush()


def _finish(task, now, state, delay=None):
    task.state, task.lease_token, task.lease_until = state, None, None
    task.attempts = task.attempts + 1 if state == "waiting" else task.attempts
    task.next_check_at = now + (delay or REVALIDATE)


def run(s, fabric, *, is_demo, adopt, limit=100, client_post=None):
    """Process due check tasks. adopt(session, s, candidate, final, check_id) stores the point and runs the
    series logic,
    returning the topics owed an analysis. Returns {topic_id: "series_change"} for the round's wake list."""
    post = client_post or (lambda body: services.biocheck_v2(s, body))
    owed = {}
    for _ in range(limit):
        now = datetime.now(UTC)
        claimed = _claim(is_demo, now)
        if claimed is None:
            break
        candidate_id, token = claimed
        with db.session() as session:
            candidate = session.get(db.MeasurementCandidate, candidate_id)
            session.expunge(candidate)
        text, why = _readback(fabric, candidate)
        built = None
        if text is not None:
            built, why = build(candidate, text)
        response, error = None, None
        if built is not None:
            try:
                response = post(built[0])  # outside any transaction
            except services.ServiceError as exc:
                error = (
                    "contract_rejected"
                    if exc.code in ("invalid_request", "http_422")
                    else "service_unavailable"
                )
        with db.session() as session:
            now = datetime.now(UTC)
            task = _own(session, candidate_id, token, now)
            if task is None:
                continue  # the lease went to another worker: its result counts, not this one
            cand = session.get(db.MeasurementCandidate, candidate_id)
            if text is None:
                _append(session, cand, status="unchecked", gate="candidate", error=why)
                _finish(
                    task,
                    now,
                    "waiting",
                    timedelta(seconds=BACKOFF[min(task.attempts, len(BACKOFF) - 1)])
                    if why == "fabric_unavailable"
                    else REVALIDATE,
                )
                continue
            if built is None:
                _append(
                    session,
                    cand,
                    status="unchecked",
                    gate="candidate",
                    error="item_not_buildable",
                    reason=why,
                )
                cand.status, cand.reason = "not_built", why
                _finish(task, now, "blocked")
                continue
            body, final = built
            if error:
                _append(session, cand, status="unchecked", gate="candidate", error=error, body=body)
                _finish(
                    task,
                    now,
                    "blocked" if error == "contract_rejected" else "waiting",
                    None
                    if error == "contract_rejected"
                    else timedelta(seconds=BACKOFF[min(task.attempts, len(BACKOFF) - 1)]),
                )
                continue
            request = {k: v for k, v in body.items() if k != "contract_version"}
            verdict = ec.verdict(response, request, CHECKER)
            if verdict:
                _append(
                    session,
                    cand,
                    status="unchecked",
                    gate="candidate",
                    error=verdict,
                    body=body,
                    response=response if isinstance(response, dict) else None,
                )
                _finish(
                    task, now, "waiting", timedelta(seconds=BACKOFF[min(task.attempts, len(BACKOFF) - 1)])
                )
                continue
            # permission and binding again before the answer is adopted (the call took time)
            again, why = _readback(fabric, cand)
            if again is None:
                _append(
                    session,
                    cand,
                    status="unchecked",
                    gate="candidate",
                    error=why,
                    body=body,
                    response=response,
                )
                _finish(task, now, "waiting", REVALIDATE)
                continue
            item = response["items"][0]
            gate_reason = None
            if item["status"] != "consistent":
                gate_reason = "not_consistent"
            elif final["qualifier"] != "exact" or final["modality"] != "actual":
                gate_reason = "approximate_or_not_actual"
            cand.status = "checked"
            if gate_reason:
                _append(
                    session,
                    cand,
                    status=item["status"],
                    gate="candidate",
                    reason=gate_reason,
                    body=body,
                    response=response,
                )
                _finish(task, now, "done")
                continue
            sp = session.begin_nested()
            _append(session, cand, status="consistent", gate="series", body=body, response=response)
            check_id = session.scalar(
                select(func.max(db.MeasurementCheck.id)).where(db.MeasurementCheck.candidate_id == cand.id)
            )
            point, topics = adopt(session, s, cand, final, check_id)
            if point is not None and not stored_equals_checked(session, point, final):
                sp.rollback()
                cand = session.get(db.MeasurementCandidate, candidate_id)
                _append(
                    session,
                    cand,
                    status="consistent",
                    gate="candidate",
                    reason="stored_values_differ",
                    body=body,
                    response=response,
                )
                _finish(session.get(db.MeasurementCheckTask, (candidate_id, CHECKER)), now, "done")
                continue
            sp.commit()
            _finish(task, now, "done")
            for t in topics:
                owed[t] = "series_change"
    return owed


def stored_equals_checked(session, point, final):
    """The stored point, read back from the database, holds exactly the checked final values."""
    session.flush()
    session.expire(point)
    row = session.get(db.Measurement, point.id)
    period = final["period"] or {}
    want_period = period.get("value") or (f"{period['start']}/{period['end']}" if period.get("start") else "")
    checks = [
        row.value_text == final["value"],
        Decimal(repr(float(row.value))) == Decimal(final["value"]),
        (row.denominator_text or None) == final["denominator"],
        row.denominator is None
        if final["denominator"] is None
        else Decimal(repr(float(row.denominator))) == Decimal(final["denominator"]),
        row.numerator is None,
        row.object == final["object"],
        row.metric == final["metric"],
        row.unit == final["unit"],
        row.kind == final["measure_kind"],
        row.scope == final["scope"],
        row.scope_mode == final["scope_mode"],
        row.period == want_period,
        row.period_type == period.get("type", ""),
        row.period_precision == period.get("precision", ""),
    ]
    return all(checks)


# ---- the effective view ------------------------------------------------------------------------------------


def latest_check():
    return (
        select(func.max(db.MeasurementCheck.id))
        .where(
            db.MeasurementCheck.candidate_id == db.Measurement.candidate_id,
            db.MeasurementCheck.checker_version == CHECKER,
        )
        .correlate(db.Measurement)
        .scalar_subquery()
    )


def effective_clause():
    """SQL condition: the point's candidate's latest check under the current checker put it in the series."""
    return db.Measurement.candidate_id.in_(
        select(db.MeasurementCheck.candidate_id).where(
            db.MeasurementCheck.id == latest_check(), db.MeasurementCheck.gate == "series"
        )
    )


def effective(session, s, is_demo, **filters):
    return (
        select(db.Measurement)
        .where(
            db.Measurement.workspace_id == s.workspace_id,
            db.Measurement.is_demo == is_demo,
            effective_clause(),
        )
        .filter_by(**filters)
    )


def is_effective(session, ids):
    """The ids of `ids` that are effective points now."""
    if not ids:
        return set()
    return set(
        session.scalars(select(db.Measurement.id).where(db.Measurement.id.in_(list(ids)), effective_clause()))
    )


def describe(check):
    return {
        "status": check.status,
        "gate": check.gate,
        "checker_version": check.checker_version,
        "check_id": check.id,
        "reason": check.gate_reason or check.error_code,
    }


def json_safe(value):
    return json.loads(json.dumps(value, default=str))
