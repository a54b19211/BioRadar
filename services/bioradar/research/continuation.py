"""Bounded continuation reading of a fixed version's cleaned text.

Triage reads a record's first window. A record is not "read" because of that: every fixed version
triaged gets a reading plan (record_reads) over its cleaned artifact - windows of WINDOW characters, each
overlapping the previous one - and this worker reads the windows after the first, a bounded number per
round, for measures only (topics and alerts stay with triage). Every window has a state, every attempt an
append-only row and, when it is sent, a real material_read; what was not read is said: capped (per-record
limit), budget_spent (the model budget ran out - read in a later round), source_unreadable (permission
withdrawn - the plan pauses), failed_final (bounded retries spent), skipped_no_digit (only when
configured: no digit, so no written number to extract).

Windows after the first end at a real boundary (paragraph or table row, else sentence) and start at one
inside the overlap; heading and table header go along as context with their own offsets, never as window
text. The measures found go to measure_checks.enqueue with the window that was read: a quote is located
only inside it (its offset in the artifact disambiguates a repeated sentence), and one found again in an
overlap - same artifact (processor, text hash), same span, value and metric - is enqueued once. A sentence
longer than the overlap that crosses a border is in no window whole: it is not extracted (the tests say so).
Skipped windows (only when configured) count as unread. read_until is the contiguous front of windows read;
coverage is the union of what was read. Read is not "every fact extracted"."""

import re
from datetime import timedelta

from sqlalchemy import select

from bioradar import db
from bioradar.adapters import llm
from bioradar.adapters.fabric import CURRENT, NOT_VISIBLE, FabricError
from bioradar.config import Settings
from bioradar.material import Material, of
from bioradar.research import analyses, lease, measure_checks, prompts, schemas
from bioradar.research.clock import utc

WINDOW = measure_checks.TRIAGE_WINDOW  # = triage.TRIAGE_CHARACTERS: window 0 is the triage window
LEASE = timedelta(minutes=2)
MAX_ATTEMPTS = 3
RETRY = timedelta(minutes=5)
TO_READ = ("pending", "failed", "budget_spent")
READ = ("sent",)  # a skipped window is not read
DIGIT = re.compile(r"[0-9０-９]")
BUDGET_ERRORS = ("request_budget", "http_error:429", "http_error:503")


SENTENCE_END = "。！？!?；;"


def _is_cut(text, p, line_only):
    """Whether position p (between text[p-1] and text[p]) is a boundary."""
    c = text[p - 1]
    if c == "\n":
        return True
    if line_only:
        return False
    return c in SENTENCE_END or (c == "." and (p == len(text) or text[p] in " \n"))


def _cut_end(text, lo, hi):
    """The last boundary in (lo, hi]: a line end first, else a sentence end; hi when there is none."""
    for line_only in (True, False):
        for p in range(hi, lo, -1):
            if _is_cut(text, p, line_only):
                return p
    return hi


def _cut_start(text, lo, hi):
    """The first boundary in [lo, hi): a line start first, else a sentence start; lo when there is none."""
    for line_only in (True, False):
        for p in range(max(lo, 1), hi):
            if _is_cut(text, p, line_only):
                return p
    return lo


def windows_of(text, overlap):
    """[(start, end)]: window 0 is [0, WINDOW) as triage read it; each later one starts at a boundary inside
    the last `overlap` characters of the one before and ends at the last boundary of its WINDOW characters
    (keeping at least two thirds of them)."""
    n = len(text)
    out = [(0, min(WINDOW, n))]
    while out[-1][1] < n:
        a_prev, b_prev = out[-1]
        a = _cut_start(text, max(b_prev - overlap, a_prev + 1), b_prev)
        b = min(a + WINDOW, n)
        if b < n:
            b = _cut_end(text, a + WINDOW * 2 // 3, b)
        out.append((a, b))
    return out


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


def plan(session, s: Settings, doc, topic, direction, at, is_demo, supersedes=None):
    """The reading plan of one triaged version (once per version). Window 0 is what triage read.
    supersedes: the plan of another cleaned artifact of this version that this artifact's plan replaces
    - one per artifact; every window is owed, the first too (triage read the other artifact), so nothing
    of the old plan's coverage counts for this one; the old plan is kept as it is and no longer read on."""
    material = of(doc)
    part, why = _clean_part(material)
    if supersedes is not None and part is None:
        return None  # an upgrade replaces a plan only with a plan of one cleaned artifact
    key = db.artifact_key((part or {}).get("processor_version"), (part or {}).get("text_sha256"))
    exists = select(db.RecordRead.id).where(
        db.RecordRead.workspace_id == s.workspace_id,
        db.RecordRead.is_demo == is_demo,
        db.RecordRead.version_id == doc["version_id"],
    )
    if supersedes is not None:
        exists = exists.where(db.RecordRead.artifact_key == key)
    if session.scalar(exists.limit(1)):
        return None
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
        topic_id=topic.id if topic else (supersedes.topic_id if supersedes is not None else None),
        direction=direction or "public_health",
        observed_at=at,
        relations=doc.get("relations") or [],
        artifact_key=key,
        supersedes_id=supersedes.id if supersedes is not None else None,
    )
    if part is None:
        read.state, read.reason, read.windows_total, read.read_until = "not_planned", why, 0, min(WINDOW, n)
        session.add(read)
        session.flush()
        return read
    spans = windows_of(material.text, s.continuation_overlap_chars)
    read.windows_total = len(spans)
    read.read_until = min(WINDOW, n)
    session.add(read)
    session.flush()
    triage_read = session.scalar(
        select(db.MaterialRead.id)
        .where(db.MaterialRead.stage == "triage", db.MaterialRead.version_id == doc["version_id"])
        .order_by(db.MaterialRead.id.desc())
        .limit(1)
    )
    for k, (a, b) in enumerate(spans):
        first = k == 0 and supersedes is None  # triage read window 0 of this artifact, not of an upgrade's
        state = "sent" if first else "pending" if k <= s.continuation_max_windows else "capped"
        session.add(
            db.RecordReadWindow(
                read_id=read.id,
                window_index=k,
                start_char=a,
                end_char=b,
                state=state,
                attempts=0,
                next_at=db.now(),
            )
        )
    if supersedes is None:
        session.add(
            db.RecordReadAttempt(
                read_id=read.id, window_index=0, outcome="sent_by_triage", material_read_id=triage_read
            )
        )
    else:
        read.read_until = 0  # nothing of this artifact was read yet
        supersedes.superseded_by_id = read.id
    session.flush()
    _settle(session, read)
    return read


# ---- the worker ----------------------------------------------------------------------------------------


def _claim(s, is_demo, now, taken, per_round, per_call):
    """Up to per_call windows, round-robin over the sources and, inside a source, over its plans in arrival
    order (each plan's lowest windows first); no plan more than per_round windows this round - a long table
    or a busy source does not starve the others."""
    token = lease.new_token()
    with db.session() as session:
        rows = session.execute(
            select(db.RecordReadWindow, db.RecordRead.source)
            .join(db.RecordRead, db.RecordRead.id == db.RecordReadWindow.read_id)
            .where(
                db.RecordRead.workspace_id == s.workspace_id,
                db.RecordRead.is_demo == is_demo,
                db.RecordRead.state == "reading",
                db.RecordRead.superseded_by_id.is_(None),  # a replaced plan is not read on
                db.RecordReadWindow.state.in_(TO_READ),
                db.RecordReadWindow.next_at <= now,
                lease.claimable(db.RecordReadWindow, now),
            )
            .order_by(db.RecordRead.created_at, db.RecordRead.id, db.RecordReadWindow.window_index)
            .limit(500)
            .with_for_update(skip_locked=True)
        ).all()
        by_source = {}  # source -> {read_id: [windows]}, both in arrival order
        for w, source in rows:
            by_source.setdefault(source, {}).setdefault(w.read_id, []).append(w)
        picked = []
        while len(picked) < per_call:
            moved = False
            for plans in by_source.values():
                if len(picked) >= per_call:
                    break
                for rid in list(plans):
                    if taken.get(rid, 0) >= per_round or not plans[rid]:
                        continue
                    picked.append(plans[rid].pop(0))
                    taken[rid] = taken.get(rid, 0) + 1
                    plans[rid] = plans.pop(rid)  # this source's next pick is its next plan
                    moved = True
                    break
            if not moved:
                break
        lease.take(picked, token, now, LEASE)
        return [(w.read_id, w.window_index) for w in picked], token


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
    state = entry.get("state")
    if state in NOT_VISIBLE or state in ("not_visible", "not_visible_or_not_processed"):
        return None, "source_unreadable"
    if state != "processed":
        # not processed, empty or a state this reader does not know: nothing is sent on old fields - the
        # windows stay owed and are tried again later (no attempt spent)
        return None, "artifact_not_ready"
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
    """The nearest markdown heading before a, with its offset in the artifact (context, not window text)."""
    best = None
    for m in re.finditer(r"(?m)^#{1,6} [^\n]*", text[:a]):
        best = {"at": m.start(), "text": m.group(0)[:200]}
    return best


def run(s: Settings, fabric, *, is_demo, budget=False, now=None):
    """One round: at most continuation_calls_per_round model calls. Returns how many windows were sent."""
    if not s.continuation_enabled:
        return 0
    taken, sent = {}, 0
    for _ in range(s.continuation_calls_per_round):
        if budget and analyses.over_auto_budget():
            break
        at = now or db.now()
        keys, token = _claim(s, is_demo, at, taken, s.continuation_per_round, s.continuation_windows_per_call)
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
            w = lease.own(session, db.RecordReadWindow, key, token, now)
            if w is None:
                continue
            read = session.get(db.RecordRead, w.read_id)
            why = problems[w.read_id]
            if why:
                lease.release(w)
                if why in ("fabric_unavailable", "artifact_not_ready"):
                    w.next_at = now + RETRY  # the window waits; no attempt spent
                elif why == "source_unreadable":
                    w.state, read.state, read.reason = "source_unreadable", "paused_unreadable", why
                else:
                    w.state, read.state, read.reason = "failed_final", "failed", why
                _attempt(session, w, why)
                continue
            text = texts[w.read_id]
            # an explicit saving, never "no data": a skipped window stays unread in the coverage
            if s.continuation_skip_no_digit and not DIGIT.search(text[w.start_char : w.end_char]):
                lease.release(w)
                w.state = "skipped_no_digit"
                _attempt(session, w, "skipped_no_digit")
                continue
            material = Material.part(text, read.part)
            windows.append((key, read.id, material, w.start_char, w.end_char))
        for read in {session.get(db.RecordRead, rid) for rid in reads}:
            _settle(session, read)
    if not windows:
        return 0
    slices = [m.slice(a, b) for _, _, m, a, b in windows]
    contexts = [_contexts(m, a, b) for _, _, m, a, b in windows]
    payload = {
        "windows": [
            {
                "index": i,
                "title": reads[rid].title,
                "position": f"{a}-{b} / {len(m)}",
                # context, sent as recorded below: the text of each bound slice, with its display offset
                **{name: ({"at": at, "text": c.text} if c else None) for name, (at, c) in ctx.items()},
                "text": sl.text,
            }
            for i, ((_, rid, m, a, b), sl, ctx) in enumerate(zip(windows, slices, contexts), 1)
        ]
    }
    with db.session() as session:

        def sent(material, ref, rid):
            return db.material_read(
                material,
                stage="extract",
                service="radar",
                ref=ref,
                source=reads[rid].source,
                record_id=reads[rid].record_id,
                version_id=reads[rid].version_id,
            )

        mrows = [sent(sl, f"W{i}", rid) for i, ((_, rid, _, _, _), sl) in enumerate(zip(windows, slices), 1)]
        # each context of a window is its own send of the same call: W{i}.heading, W{i}.table_header ...
        crows = [
            sent(c, f"W{i}.{name}", rid)
            for i, ((_, rid, _, _, _), ctx) in enumerate(zip(windows, contexts), 1)
            for name, (_, c) in ctx.items()
            if c is not None
        ]
        session.add_all(mrows + crows)
        session.flush()
        mids = [r.id for r in mrows]
        all_ids = mids + [r.id for r in crows]
    try:
        result, call = llm.chat_json(s, "extract", prompts.EXTRACT, payload, schemas.Extraction)
    except BaseException:
        with db.session() as session:
            for r in session.scalars(select(db.MaterialRead).where(db.MaterialRead.id.in_(all_ids))):
                r.status = "failed"
        raise
    items = {it.index: it for it in (result.items if result else []) if 1 <= it.index <= len(windows)}
    sent = 0
    done = db.now()  # the model call took time: a lease that ran out meanwhile belongs to another round
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
            requests=call.get("requests") or 1,
            http=call.get("http"),
        )
        session.add(llm_row)
        session.flush()
        db.keep_outputs(session, llm_row, call)
        for r in session.scalars(select(db.MaterialRead).where(db.MaterialRead.id.in_(all_ids))):
            r.llm_call_id, r.status = llm_row.id, "sent" if result else "failed"
        for i, ((key, rid, material, a, b), mid) in enumerate(zip(windows, mids), 1):
            w = lease.own(session, db.RecordReadWindow, key, token, done)
            if w is None:
                continue  # the lease ran out during the call: another round owns the window now
            read = session.get(db.RecordRead, rid)
            lease.release(w)
            item = items.get(i)
            if result is None or item is None:
                if call["error"] in BUDGET_ERRORS:
                    w.state = "budget_spent"  # no attempt spent: read when the budget allows
                else:
                    lease.fail(
                        w,
                        now,
                        due="next_at",
                        delay=RETRY,
                        max_attempts=MAX_ATTEMPTS,
                        retry_state="failed",
                        final_state="failed_final",
                    )
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
                utc(read.observed_at),
                read.is_demo,
                overlap=True,
                window=(a, b),
                material_read_id=mid,
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


CAPTION = re.compile(r"\s*(?:Table|TABLE)\s+\d+|\s*表\s*\d+")


def _contexts(material, a, b):
    """The context of window [a, b) as bound slices, each with its display offset: the nearest heading before
    it, and - when the window starts inside a table's rows - that table's header and the "Table N" / "表N"
    caption line right above the table. {name: (at, Material) or (None, None)}."""
    out = {"heading": (None, None), "table_header": (None, None), "table_title": (None, None)}
    h = _heading(material.text, a)
    if h:
        out["heading"] = (h["at"], material.slice(h["at"], h["at"] + len(h["text"])))
    found = material.table_header(a, b)
    if found and found[0]:
        spans, _whole = found
        header = Material.join("", [material.slice(x, y) for x, y in spans])
        out["table_header"] = (spans[0][0], header.slice(0, 600))
        text = material.text
        end = text.rfind("\n", 0, spans[0][0])  # the line break before the header row
        while end > 0:
            start = text.rfind("\n", 0, end) + 1
            line = text[start:end]
            if line.strip():
                if CAPTION.match(line):
                    out["table_title"] = (start, material.slice(start, min(end, start + 300)))
                break  # only the first non-empty line above the table can be its caption
            end = start - 1
    return out


def _table_header(material, a, b):
    """The header of the table the window starts in, with its offset (context, not window text)."""
    found = material.table_header(a, b)
    if not found or not found[0]:
        return None
    spans, _whole = found
    return {"at": spans[0][0], "text": "".join(material.text[x:y] for x, y in spans)[:600]}


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
    until = 0  # the contiguous front: a failed window before a read one is a hole, not read
    for w in ws:
        if w.state not in READ:
            break
        until = w.end_char
    read.read_until = until
    if states <= set(READ):
        read.state, read.reason = "complete", None
    elif not states & set(TO_READ):
        if "failed_final" in states:
            read.state, read.reason = "failed", "retries_spent"
        else:  # capped and / or skipped windows: text not read, said so
            read.state = "capped"
            read.reason = "+".join(x for x in ("capped", "skipped_no_digit") if x in states)
    read.updated_at = db.now()


def resume(session, read_id):
    """A paused plan (permission withdrawn) reads on once its source is readable again: its unreadable windows
    go back to pending."""
    read = session.get(db.RecordRead, read_id)
    if read is None or read.state != "paused_unreadable" or read.superseded_by_id is not None:
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


def extend(session, read_id, windows):
    """Read on past the cap: the next `windows` capped windows (lowest first) become owed again - an explicit
    continuation or a later budget; nothing beyond the cap is ever dropped. Returns how many were reopened."""
    read = session.get(db.RecordRead, read_id)
    if read is None or read.state not in ("capped", "reading") or read.superseded_by_id is not None:
        return 0
    capped = list(
        session.scalars(
            select(db.RecordReadWindow)
            .where(db.RecordReadWindow.read_id == read_id, db.RecordReadWindow.state == "capped")
            .order_by(db.RecordReadWindow.window_index)
            .limit(windows)
        )
    )
    for w in capped:
        w.state, w.next_at = "pending", db.now()
    if capped:
        read.state, read.reason = "reading", None
        _settle(session, read)
    return len(capped)


def coverage(session, read):
    """Characters of the artifact by what happened to them: read by a model (union, overlaps once), skipped
    (no digit - unread), not read; and how many read windows gave no measure (read, nothing extracted)."""
    ws = list(session.scalars(select(db.RecordReadWindow).where(db.RecordReadWindow.read_id == read.id)))
    empty = session.scalars(
        select(db.RecordReadAttempt.window_index).where(
            db.RecordReadAttempt.read_id == read.id,
            db.RecordReadAttempt.outcome == "sent",
            db.RecordReadAttempt.measures_found == 0,
        )
    ).all()

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
    skipped = union(("sent", "skipped_no_digit")) - model_read  # skipped and not read by another window
    return {
        "version_id": read.version_id,
        "state": read.state,
        "reason": read.reason,
        "total_chars": read.total_chars,
        "model_read_chars": model_read if read.state != "not_planned" else read.read_until,
        "skipped_no_digit_chars": skipped,  # part of unread_chars
        "unread_chars": read.total_chars - (model_read if read.state != "not_planned" else read.read_until),
        "windows_read_without_measures": len(set(empty)),
        # unread characters by why (a character in several unread windows counts under each of them)
        "unread_by_reason": {
            st: union(("sent", st)) - model_read
            for st in (
                "pending",
                "budget_spent",
                "source_unreadable",
                "failed",
                "failed_final",
                "capped",
                "skipped_no_digit",
            )
            if any(w.state == st for w in ws)
        },
        "read_until": read.read_until,
        "windows": {st: sum(1 for w in ws if w.state == st) for st in sorted({w.state for w in ws})},
    }


def periodic_resume(session, s, fabric, is_demo, now):
    """Paused plans whose source reads again are resumed (checked with one bounded read per plan)."""
    n = 0
    for read in list(
        session.scalars(
            select(db.RecordRead)
            .where(
                db.RecordRead.workspace_id == s.workspace_id,
                db.RecordRead.is_demo == is_demo,
                db.RecordRead.state == "paused_unreadable",
                db.RecordRead.superseded_by_id.is_(None),
            )
            .limit(20)
        )
    ):
        text, why = _readback(fabric, read, now)
        if text is not None and resume(session, read.id):
            n += 1
    return n
