"""The records an analysis starts from (input.seed_records), looked at before the plan: each is read as the
fixed version Fabric serves, judged for relevance by the monitor's own triage (research/triage.py) unless the
monitor already did, and handed to the plan with what it actually says. A seed judged unrelated is no starting
point; when every seed is, or the plan cannot ask anything of what they say, the run ends there (`end`)."""

from typing import TYPE_CHECKING

import httpx
from sqlalchemy import select

from bioradar import db
from bioradar.adapters.fabric import NOT_VISIBLE, FabricError, time_value
from bioradar.research import prompts, schemas, triage, views
from bioradar.research.run import Failed, read, submit
from bioradar.research.run.search import doc_key

if TYPE_CHECKING:
    from bioradar.research.pipeline import Run

# What Fabric says of a seed that is not there for this reader: revoked, withdrawn, not visible at the cutoff
# (the adapter's NOT_VISIBLE; read.document says record_not_visible for the same). Anything else it says -
# a 5xx, a version that is not the one named - is a fault, not an absent seed.
ABSENT = NOT_VISIBLE | {"record_not_visible"}


def read_all(run: "Run"):
    """[(seed, the document it names)] of the seeds inside the run's scope that Fabric can serve: the pinned
    version for a seed that names one, else the record as visible at the cutoff. A seed Fabric says is not
    there (ABSENT) is noted and left out; Fabric failing in any other way, or not answering, fails the run
    (seed_unreadable) rather than passing for "no seeds"."""
    found = []
    for seed in run.input.get("seed_records", []):
        if run.scope and seed["record_id"] not in run.scope:
            continue  # outside the fixed material set: no starting point (search.add_hit)
        hit = {"source": seed["source"], "record_id": seed["record_id"], "version_id": seed.get("version_id")}
        try:
            doc = read.document(run, hit, hit["version_id"])
        except FabricError as exc:
            if exc.code not in ABSENT:
                raise Failed("seed_unreadable") from exc
            run.note("seed_unreadable", f"{seed['source']}: {seed['record_id']} ({exc.code})")
            continue
        except httpx.HTTPError as exc:
            raise Failed("seed_unreadable") from exc
        run.docs[doc_key(hit)] = doc
        found.append((seed, doc))
    return found


def judge(run: "Run", found):
    """Keep in the run's state (seed_triage) the relevance verdict of each seed: the one the monitor's triage
    stored for that fixed version, else the triage model's, asked once here and stored for the next reader.
    A seed the model could not be asked about has no verdict, and no verdict is never unrelated."""
    if not found:
        return
    docs = [doc for _, doc in found]
    verdicts = stored_verdicts(docs)
    todo = [d for d in docs if d["version_id"] not in verdicts]
    asked = {}
    for start in range(0, len(todo), triage.BATCH):
        asked.update(ask_triage(run, todo[start : start + triage.BATCH]))
    remember(run, asked)
    verdicts.update(asked)
    run.p["seed_triage"] = [
        {
            "source": d["source"],
            "record_id": d["record_id"],
            "version_id": d["version_id"],
            **verdicts[d["version_id"]],
            "dropped": verdicts[d["version_id"]]["related"] == "unrelated" and not corrects(d),
        }
        for d in docs
        if d["version_id"] in verdicts
    ]


def corrects(doc) -> bool:
    """Whether the record states a correction or retraction: such a seed is read whatever the model says of
    it, a correction is never left unread for being judged off topic."""
    return any(r["kind"] in {"corrects", "retracts"} for r in doc["relations"])


def stored_verdicts(docs) -> dict:
    """{version id: verdict} of the versions an observation holds a model's relevance for."""
    with db.session() as session:
        rows = session.scalars(
            select(db.Observation).where(
                db.Observation.version_id.in_([d["version_id"] for d in docs]),
                db.Observation.relevance.is_not(None),
            )
        )
        return {
            o.version_id: {
                "related": o.relevance,
                "direction": o.direction or "",
                "reason": "" if views.is_program_reason(o.reason) else o.reason,
                "stored": True,
            }
            for o in rows
        }


def ask_triage(run: "Run", docs) -> dict:
    """{version id: verdict} the triage model gives for these records: the monitor's prompt, answer shape and
    window of each text, no topics. One call; one that failed judged nothing."""
    payload, windows = triage.triage_payload(docs, [])
    ids = sent(run, "seed_triage", docs, windows)
    with submit.outbound(run, ids, model=True):
        result = run.ask(
            "seed_triage", prompts.TRIAGE, payload, schemas.Triage, carries={d["source"] for d in docs}
        )
    if result is None:
        run.note("seed_triage_failed", f"{len(docs)} seeds")
        return {}
    items = {item.index: item for item in result.items}
    return {
        d["version_id"]: {"related": i.related, "direction": i.direction, "reason": i.reason, "stored": False}
        for n, d in enumerate(docs, 1)
        if (i := items.get(n))
    }


def remember(run: "Run", verdicts: dict):
    """The verdicts just asked, into the observations of those versions that none judged: the groups' view
    then leaves out what was judged unrelated, and the next analysis of the record asks nothing."""
    if not verdicts:
        return
    with run.writing() as session:
        triage.keep_relevance(session, verdicts)


def dropped(run: "Run") -> set:
    """(source, record id) of the seeds that are no starting point."""
    return {(t["source"], t["record_id"]) for t in run.p.get("seed_triage", []) if t["dropped"]}


def started(run: "Run", found):
    """The seeds [(seed, document)] that stay starting points."""
    gone = dropped(run)
    return [(seed, doc) for seed, doc in found if (doc["source"], doc["record_id"]) not in gone]


def sent(run: "Run", stage, docs, windows):
    """The outbound records (material_reads) of these windows, made before the call that sends them."""
    if not docs:
        return []
    with run.writing() as session:
        return [
            submit.sent(run, session, stage, "radar", f"S{n}", d, w)
            for n, (d, w) in enumerate(zip(docs, windows), 1)
        ]


def payload(run: "Run", starts, windows):
    """The seeds as the plan model gets them: what each is and says (its title and the first part of its text,
    which may be empty or no more than the title) and what the triage made of it."""
    said = {t["version_id"]: {k: t[k] for k in ("related", "reason")} for t in run.p.get("seed_triage", [])}
    return [
        {
            "source": d["source"],
            "title": d["title"],
            "published_at": time_value(d["published_at"]),
            "material_kind": d["material_kind"],
            "verified": d["verified"],
            "text": w.text,
            "triage": said.get(d["version_id"]),
        }
        for (_, d), w in zip(starts, windows)
    ]


def end(run: "Run", starts, decision) -> bool:
    """The run's one result when the seeds leave nothing to investigate, shown as its stop: `unrelated` (the
    triage found them outside medical and public-health intelligence) or `insufficient` (the plan could not
    ask anything of what they say). It says what the seeds are: title, how much text, the triage's reason."""
    reasons = {t["version_id"]: t["reason"] for t in run.p.get("seed_triage", [])}
    lines = []
    for _, d in starts[:3]:
        line = f"{d['title'][:80]}（正文{len(d.get('text') or '')}字"
        lines.append(line + (f"；{reasons[d['version_id']]}）" if reasons.get(d["version_id"]) else "）"))
    head = "方向不相关" if decision == "unrelated" else "材料不足，未能拟出可查的问题"
    run.p["stop"] = {"decision": decision, "reason": f"{head}：" + "；".join(lines)}
    return True
