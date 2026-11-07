"""What goes out: every record's material sent to a model or a service is recorded with its binding
(material_reads, v3 6.1) before the call and settled after it."""

from contextlib import contextmanager
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from bioradar import db
from bioradar.adapters import services
from bioradar.adapters.fabric import noticed
from bioradar.material import Material, restore
from bioradar.research import lease
from bioradar.research.run import Withheld

if TYPE_CHECKING:
    from bioradar.research.pipeline import Run


def evidence_rows(run: "Run"):
    with db.session() as s:
        return list(
            s.scalars(select(db.Evidence).where(db.Evidence.analysis_id == run.id).order_by(db.Evidence.id))
        )


def sent(run: "Run", session, stage, service, ref, origin, material):
    """Record one record's material as it is about to go to a model or service (v3 6.1): a new row, status
    prepared, the next attempt for this stage and ref; returns its id. Earlier attempts stay as they are
    (a retry never erases what an earlier attempt sent). origin: a document or evidence row."""
    attempt = session.scalar(
        select(func.coalesce(func.max(db.MaterialRead.attempt), 0)).where(
            db.MaterialRead.analysis_id == run.id,
            db.MaterialRead.stage == stage,
            db.MaterialRead.ref == ref,
        )
    )
    get = origin.get if isinstance(origin, dict) else lambda k: getattr(origin, k)
    row = db.material_read(
        material,
        stage=stage,
        service=service,
        ref=ref,
        source=get("source"),
        record_id=get("record_id"),
        version_id=get("version_id"),
        analysis_id=run.id,
        attempt=attempt + 1,
    )
    session.add(row)
    session.flush()
    return row.id


def mark(run: "Run", ids, status):
    """Settle prepared outbound rows; a row once sent stays sent (the same report input goes to several
    BioCheck requests)."""
    if ids:
        with run.writing() as s:
            for row in s.scalars(select(db.MaterialRead).where(db.MaterialRead.id.in_(ids))):
                if row.status != "sent":
                    row.status = status


@contextmanager
def outbound(run: "Run", ids, *, model=False):
    """Around one outbound call: withheld when the run's own sources stopped it before anything went out;
    for a model call stopped after some of its requests went out, sent (what went out stays recorded as
    sent) plus a withheld attempt for the blocked request; failed if it raises otherwise; else sent, or for a
    model call not_sent when no call was made (no budget left)."""
    before = run.calls_used
    run.sending = list(ids)
    try:
        run.check_lease()
        yield
    except lease.Lost:
        raise  # superseded: another worker owns this analysis's records now
    except Withheld:
        if model and run.calls_used != before:
            mark(run, ids, "sent")
            withheld_attempt(run, ids)
        else:
            mark(run, ids, "withheld")
        raise
    except BaseException:
        mark(run, ids, "failed")
        raise
    finally:
        run.sending = []
    mark(run, ids, "not_sent" if model and run.calls_used == before else "sent")


def withheld_attempt(run: "Run", ids):
    """A new attempt row, status withheld, for each material whose next request was blocked: the same
    material as the row before it, which stays as it is."""
    with run.writing() as s:
        for row in list(s.scalars(select(db.MaterialRead).where(db.MaterialRead.id.in_(ids)))):
            attempt = s.scalar(
                select(func.max(db.MaterialRead.attempt)).where(
                    db.MaterialRead.analysis_id == row.analysis_id,
                    db.MaterialRead.stage == row.stage,
                    db.MaterialRead.ref == row.ref,
                )
            )
            s.add(
                db.MaterialRead(
                    analysis_id=row.analysis_id,
                    stage=row.stage,
                    service=row.service,
                    ref=row.ref,
                    attempt=attempt + 1,
                    status="withheld",
                    source=row.source,
                    record_id=row.record_id,
                    version_id=row.version_id,
                    sha256=row.sha256,
                    chars=row.chars,
                    binding=row.binding,
                )
            )


def material_of(run: "Run", row):
    """An evidence row's text with its binding restored; evidence read before bindings existed is
    unbound text: it claims no provenance and does not read back."""
    read_id = (row.meta or {}).get("material_read_id")
    if read_id is None:
        return Material.unbound(row.text, "read_before_binding")
    with db.session() as s:
        return restore(row.text, s.get(db.MaterialRead, read_id).binding)


def send_rows(run: "Run", stage, service, rows, window=lambda m, r: m):
    """({ref: text}, row ids) of evidence rows about to go to `service` at `stage`, each recorded with its
    binding (status prepared until the call settles it)."""
    out, ids = {}, []
    with run.writing() as s:
        for r in rows:
            material = noticed(window(material_of(run, r), r), (r.meta or {}).get("clean_text_state"))
            ids.append(sent(run, s, stage, service, r.ref, r, material))
            out[r.ref] = material.text
    return out, ids


def service(run: "Run", operation, body):
    run.upstream_still_readable()  # every BioCheck request carries the scope built from earlier work
    body["max_llm_calls"] = max(1, run.calls_left() - 1)
    # every request that carries the records is its own outbound record (stage report:<operation>, its
    # own attempt and status): a later failed request is never hidden by an earlier sent one
    ids = []
    if "evidence" in body:
        carried = {e.get("id") for e in body["evidence"]}
        with run.writing() as s:
            ids = [
                sent(run, s, f"report:{operation}", "biocheck", r.ref, r, m)
                for r, m in getattr(run, "report_windows", [])
                if r.ref in carried
            ]
    try:
        with outbound(run, ids):
            run.confirm_sources({e.get("source") for e in body.get("evidence", [])} - {None})
            response = services.biocheck(run.s, operation, body)
    except services.ServiceError as exc:
        exc.reads = ids  # the caller logs the calls the failed request paid for, with the reads that fed them
        raise
    calls = response.pop("llm_calls", [])
    made = run.log(calls, "biocheck", reads=ids)
    # which llm_calls row each of the service's calls is: a sentence check names the call that made it
    response["logged"] = [{"id": i, "stage": c.get("stage")} for i, c in zip(made, calls)]
    return response
