"""T-31: bounded continuation reading of a fixed version's cleaned text.

Triage reads a record's first window. A record is not "read" because of that: every fixed version
triaged gets a reading plan (record_reads) over its cleaned artifact - windows of WINDOW characters, each
overlapping the previous one - and this worker reads the windows after the first, a bounded number per
round, for measures only (topics and alerts stay with triage). Every window has a state, every attempt an
append-only row and, when it is sent, a real material_read; what was not read is said: capped (per-record
limit), budget_spent (the model budget ran out - read in a later round), source_unreadable (permission
withdrawn - the plan pauses), failed_final (bounded retries spent), skipped_no_digit (only when
configured: no digit, so no written number to extract).

The measures found go to measure_checks.enqueue, which locates each quote in the WHOLE artifact text, so a
sentence crossing a window border lands at one place; one found again in an overlap is not enqueued twice."""

import re
import uuid
from datetime import timedelta

from sqlalchemy import or_, select

from bioradar import db
from bioradar.adapters import llm
from bioradar.adapters.fabric import CURRENT, NOT_VISIBLE, FabricError
from bioradar.config import Settings
from bioradar.material import Material, of
from bioradar.research import analyses, measure_checks, prompts, schemas

WINDOW = 1500  # monitor.TRIAGE_CHARACTERS is this: window 0 is the triage window
LEASE = timedelta(minutes=2)
MAX_ATTEMPTS = 3
RETRY = timedelta(minutes=5)
TO_READ = ("pending", "failed", "budget_spent")
READ = ("sent", "skipped_no_digit")
DIGIT = re.compile(r"[0-9０-９]")
BUDGET_ERRORS = ("request_budget", "http_error:429", "http_error:503")


def window_starts(n, overlap):
    step = WINDOW - overlap
    starts = [0]
    while starts[-1] + WINDOW < n:
        starts.append(starts[-1] + step)
    return starts


def _clean_part(material: Material):
    """The cleaned artifact the whole material is, or (None, why) - a plan covers exactly one artifact."""
    if len(material.segments) != 1:
        return None, "composite_material"
    text, part, start = material.segments[0]
    if part is None or start != 0:
        return None, "composite_material"
    if part.get("kind") != "clean_text" or not part.get("processor_version"):
        return None, "not_a_clean_artifact"
    return part, None


def plan(session, s: Settings, doc, topic, direction, at, is_demo):
    """The reading plan of one triaged version (once per version). Window 0 is what triage read."""
    exists = session.scalar(
        select(db.RecordRead.id).where(
            db.RecordRead.workspace_id == s.workspace_id,
            db.RecordRead.is_demo == is_demo,
            db.RecordRead.version_id == doc["version_id"],
        )
    )
    if exists:
        return None
    material = of(doc)
    part, why = _clean_part(material)
    n = len(material)
    read = db.RecordRead(
        workspace_id=s.workspace_id,
        is_demo=is_demo,
        source=doc["source"],
        record_id=doc["record_id"],
        version_id=doc["version_id"],
        title=(doc.get("title") or "")[:500],
        part={k: v for k, v in (part or {}).items() if k != "text_sha256"},
        processor_version=(part or {}).get("processor_version"),
        text_sha256=(part or {}).get("text_sha256"),
        total_chars=n,
        window_chars=WINDOW,
        overlap_chars=s.continuation_overlap_chars,
        topic_id=topic.id if topic else None,
        direction=direction or "public_health",
        observed_at=at,
        relations=doc.get("relations") or [],
    )
    if part is None:
        read.state, read.reason, read.windows_total, read.read_until = "not_planned", why, 0, min(WINDOW, n)
        session.add(read)
        session.flush()
        return read
    starts = window_starts(n, s.continuation_overlap_chars)
    read.windows_total = len(starts)
    read.read_until = min(WINDOW, n)
    session.add(read)
    session.flush()
    triage_read = session.scalar(
        select(db.MaterialRead.id)
        .where(db.MaterialRead.stage == "triage", db.MaterialRead.version_id == doc["version_id"])
        .order_by(db.MaterialRead.id.desc())
        .limit(1)
    )
    for k, a in enumerate(starts):
        state = "sent" if k == 0 else "pending" if k <= s.continuation_max_windows else "capped"
        session.add(
            db.RecordReadWindow(
                read_id=read.id,
                window_index=k,
                start_char=a,
                end_char=min(a + WINDOW, n),
                state=state,
                attempts=0,
                next_at=db.now(),
            )
        )
    session.add(
        db.RecordReadAttempt(
            read_id=read.id, window_index=0, outcome="sent_by_triage", material_read_id=triage_read
        )
    )
    session.flush()
    _settle(session, read)
    return read


# ---- the worker ----------------------------------------------------------------------------------------


def _claim(is_demo, now, taken, per_round, per_call):
    """Up to per_call windows, round-robin over the plans in arrival order (each plan's lowest windows
    first), no plan more than per_round windows this round - a long table does not starve the records after
    it."""
    token = uuid.uuid4().hex
    with db.session() as session:
        rows = session.execute(
            select(db.RecordReadWindow, db.RecordRead.created_at)
            .join(db.RecordRead, db.RecordRead.id == db.RecordReadWindow.read_id)
            .where(
                db.RecordRead.is_demo == is_demo,
                db.RecordRead.state == "reading",
                db.RecordReadWindow.state.in_(TO_READ),
                db.RecordReadWindow.next_at <= now,
                or_(db.RecordReadWindow.lease_until.is_(None), db.RecordReadWindow.lease_until < now),
            )
            .order_by(db.RecordRead.created_at, db.RecordRead.id, db.RecordReadWindow.window_index)
            .limit(500)
            .with_for_update(skip_locked=True)
        ).all()
        groups = {}
        for w, _ in rows:
            groups.setdefault(w.read_id, []).append(w)
        picked = []
        while len(picked) < per_call:
            moved = False
            for rid, ws in groups.items():
                if len(picked) >= per_call:
                    break
                if taken.get(rid, 0) >= per_round or not ws:
                    continue
                picked.append(ws.pop(0))
                taken[rid] = taken.get(rid, 0) + 1
                moved = True
            if not moved:
                break
        for w in picked:
            w.lease_token, w.lease_until = token, now + LEASE
        return [(w.read_id, w.window_index) for w in picked], token


def _own(session, key, token, now):
    w = session.get(db.RecordReadWindow, key, with_for_update=True)
    ok = (
        w is not None
        and w.lease_token == token
        and w.lease_until
        and measure_checks._utc(w.lease_until) >= now
    )
    return w if ok else None


def _attempt(session, w, outcome, **fields):
    session.add(
        db.RecordReadAttempt(read_id=w.read_id, window_index=w.window_index, outcome=outcome, **fields)
    )


def _readback(fabric, read, now):
    """(text, None) when Fabric still serves this exact artifact to this reader, else (None, why)."""
    try:
        found = fabric.clean_texts(
            read.source,
            [read.version_id],
            is_demo=read.is_demo,
            mode=CURRENT,
            cutoff=now.isoformat(),
            processor_version=read.processor_version,
        )
    except FabricError as exc:
        return None, "source_unreadable" if exc.code in NOT_VISIBLE else "fabric_unavailable"
    except Exception:  # noqa: BLE001 - transient: the windows wait, no attempt is spent
        return None, "fabric_unavailable"
    entry = (found or {}).get(read.version_id) or {}
    clean = entry.get("clean") or {}
    if entry.get("state") in NOT_VISIBLE or entry.get("state") == "not_visible":
        return None, "source_unreadable"
    text = clean.get("text")
    if (
        text is None
        or clean.get("processor_version") != read.processor_version
        or clean.get("text_sha256") != read.text_sha256
        or measure_checks.sha256(text) != read.text_sha256
    ):
        return None, "artifact_changed"
    return text, None


def _heading(text, a):
    """The nearest markdown heading line that starts before a (the window's section), or ""."""
    best = ""
    for m in re.finditer(r"(?m)^#{1,6} [^\n]*", text[:a]):
        best = m.group(0)
    return best[:200]


def run(s: Settings, fabric, *, is_demo, budget=False, now=None):
    """One round: at most continuation_calls_per_round model calls. Returns how many windows were sent."""
    if not s.continuation_enabled:
        return 0
    taken, sent = {}, 0
    for _ in range(s.continuation_calls_per_round):
        if budget and analyses.over_auto_budget():
            break
        at = now or db.now()
        keys, token = _claim(is_demo, at, taken, s.continuation_per_round, s.continuation_windows_per_call)
        if not keys:
            break
        sent += _read(s, fabric, keys, token, at)
    return sent


def _read(s, fabric, keys, token, now):
    with db.session() as session:
        reads = {k[0]: session.get(db.RecordRead, k[0]) for k in keys}
        for r in reads.values():
            session.expunge(r)
    texts, problems = {}, {}
    for rid, read in reads.items():
        texts[rid], problems[rid] = _readback(fabric, read, now)
    windows = []  # (key, read, material slice, start, end)
    with db.session() as session:
        for key in keys:
            w = _own(session, key, token, now)
            if w is None:
                continue
            read = session.get(db.RecordRead, w.read_id)
            why = problems[w.read_id]
            if why:
                w.lease_token = w.lease_until = None
                if why == "fabric_unavailable":
                    w.next_at = now + RETRY  # the window waits; no attempt spent
                elif why == "source_unreadable":
                    w.state, read.state, read.reason = "source_unreadable", "paused_unreadable", why
                else:
                    w.state, read.state, read.reason = "failed_final", "failed", why
                _attempt(session, w, why)
                continue
            text = texts[w.read_id]
            if s.continuation_skip_no_digit and not DIGIT.search(text[w.start_char : w.end_char]):
                w.state, w.lease_token, w.lease_until = "skipped_no_digit", None, None
                _attempt(session, w, "skipped_no_digit")
                continue
            material = Material.part(text, read.part)
            windows.append((key, read.id, material, w.start_char, w.end_char))
        for read in {session.get(db.RecordRead, rid) for rid in reads}:
            _settle(session, read)
    if not windows:
        return 0
    slices = [m.slice(a, b) for _, _, m, a, b in windows]
    payload = {
        "windows": [
            {
                "index": i,
                "title": reads[rid].title,
                "position": f"{a}-{b} / {len(m)}",
                "heading": _heading(m.text, a),
                "table_header": _table_header(m, a, b),
                "text": sl.text,
            }
            for i, ((_, rid, m, a, b), sl) in enumerate(zip(windows, slices), 1)
        ]
    }
    with db.session() as session:
        mrows = [
            db.material_read(
                sl,
                stage="extract",
                service="radar",
                ref=f"W{i}",
                source=reads[rid].source,
                record_id=reads[rid].record_id,
                version_id=reads[rid].version_id,
            )
            for i, ((_, rid, _, _, _), sl) in enumerate(zip(windows, slices), 1)
        ]
        session.add_all(mrows)
        session.flush()
        mids = [r.id for r in mrows]
    try:
        result, call = llm.chat_json(s, "extract", prompts.EXTRACT, payload, schemas.Extraction)
    except BaseException:
        with db.session() as session:
            for r in session.scalars(select(db.MaterialRead).where(db.MaterialRead.id.in_(mids))):
                r.status = "failed"
        raise
    items = {it.index: it for it in (result.items if result else []) if 1 <= it.index <= len(windows)}
    sent = 0
    with db.session() as session:
        u = call["usage"]
        llm_row = db.LlmCall(
            stage="extract",
            service="radar",
            model=call["model"],
            error=call["error"],
            finish_reason=call["finish_reason"],
            prompt_tokens=u.get("prompt_tokens", 0),
            completion_tokens=u.get("completion_tokens", 0),
            total_tokens=u.get("total_tokens", 0),
            seconds=call["seconds"],
        )
        session.add(llm_row)
        session.flush()
        for r in session.scalars(select(db.MaterialRead).where(db.MaterialRead.id.in_(mids))):
            r.llm_call_id, r.status = llm_row.id, "sent" if result else "failed"
        for i, ((key, rid, material, a, b), mid) in enumerate(zip(windows, mids), 1):
            w = _own(session, key, token, now)
            if w is None:
                continue  # the lease ran out during the call: another round owns the window now
            read = session.get(db.RecordRead, rid)
            w.lease_token = w.lease_until = None
            item = items.get(i)
            if result is None or item is None:
                if call["error"] in BUDGET_ERRORS:
                    w.state = "budget_spent"  # no attempt spent: read when the budget allows
                else:
                    w.attempts += 1
                    w.state = "failed_final" if w.attempts >= MAX_ATTEMPTS else "failed"
                    w.next_at = now + RETRY
                _attempt(session, w, call["error"] or "no_item", material_read_id=mid, llm_call_id=llm_row.id)
                continue
            doc = {
                "source": read.source,
                "record_id": read.record_id,
                "version_id": read.version_id,
                "raw_sha256": read.part.get("raw_sha256"),
                "material": material,
                "relations": read.relations or [],
            }
            topic = session.get(db.Topic, read.topic_id) if read.topic_id else None
            triage_like = schemas.TriageItem(
                index=1, alert="none", direction=read.direction, measures=item.measures
            )
            made = measure_checks.enqueue(
                session,
                s,
                doc,
                triage_like,
                topic,
                measure_checks._utc(read.observed_at),
                read.is_demo,
                overlap=True,
            )
            w.state = "sent"
            sent += 1
            _attempt(
                session,
                w,
                "sent",
                material_read_id=mid,
                llm_call_id=llm_row.id,
                measures_found=len(item.measures),
                measures_enqueued=made,
            )
        for read in {session.get(db.RecordRead, rid) for _, rid, _, _, _ in windows}:
            _settle(session, read)
    return sent


def _table_header(material, a, b):
    found = material.table_header(a, b)
    if not found:
        return ""
    spans, _whole = found
    return "".join(material.text[x:y] for x, y in spans)[:600]


def _settle(session, read):
    """The plan's state and read_until from its windows."""
    if read.state in ("not_planned", "paused_unreadable", "failed"):
        return
    ws = list(
        session.scalars(
            select(db.RecordReadWindow)
            .where(db.RecordReadWindow.read_id == read.id)
            .order_by(db.RecordReadWindow.window_index)
        )
    )
    states = {w.state for w in ws}
    until = 0
    for w in ws:
        if w.state not in READ:
            break
        until = w.end_char
    read.read_until = until
    if states <= set(READ):
        read.state = "complete"
    elif not states & set(TO_READ):
        read.state = "capped" if "capped" in states else "failed"
    read.updated_at = db.now()


def resume(session, read_id):
    """A paused plan (permission withdrawn) reads on once its source is readable again: its unreadable windows
    go back to pending."""
    read = session.get(db.RecordRead, read_id)
    if read is None or read.state != "paused_unreadable":
        return False
    for w in session.scalars(
        select(db.RecordReadWindow).where(
            db.RecordReadWindow.read_id == read_id, db.RecordReadWindow.state == "source_unreadable"
        )
    ):
        w.state, w.next_at = "pending", db.now()
    read.state, read.reason = "reading", None
    _settle(session, read)
    return True


def coverage(session, read):
    """Characters of the artifact by what happened to them: read by a model, skipped (no digit), not read."""
    ws = list(session.scalars(select(db.RecordReadWindow).where(db.RecordReadWindow.read_id == read.id)))

    def union(states):
        spans = sorted((w.start_char, w.end_char) for w in ws if w.state in states)
        total, end = 0, -1
        for a, b in spans:
            a = max(a, end)
            if b > a:
                total += b - a
                end = b
        return total

    model_read = union(("sent",))
    covered = union(READ)
    return {
        "version_id": read.version_id,
        "state": read.state,
        "reason": read.reason,
        "total_chars": read.total_chars,
        "model_read_chars": model_read if read.state != "not_planned" else read.read_until,
        "skipped_no_digit_chars": covered - model_read,
        "unread_chars": read.total_chars - (covered if read.state != "not_planned" else read.read_until),
        "read_until": read.read_until,
        "windows": {st: sum(1 for w in ws if w.state == st) for st in sorted({w.state for w in ws})},
    }


def periodic_resume(session, fabric, is_demo, now):
    """Paused plans whose source reads again are resumed (checked with one bounded read per plan)."""
    n = 0
    for read in list(
        session.scalars(
            select(db.RecordRead)
            .where(db.RecordRead.is_demo == is_demo, db.RecordRead.state == "paused_unreadable")
            .limit(20)
        )
    ):
        text, why = _readback(fabric, read, now)
        if text is not None and resume(session, read.id):
            n += 1
    return n
