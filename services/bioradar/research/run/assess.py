"""Stage assess: the model grades the judgments and hypotheses; BioCheck verifies what it wrote, and the
program caps every grade by what the verified report supports."""

import bisect
import re
from typing import TYPE_CHECKING

from sqlalchemy import select

from bioradar import db
from bioradar.adapters.services import ServiceError
from bioradar.research import prompts, schemas
from bioradar.research.run import submit

if TYPE_CHECKING:
    from bioradar.research.pipeline import Run


def hypothesis_stance(fact):
    """A fact's direction for its hypothesis, judged apart from its stance on the checks (an answer to a
    check can refute the hypothesis); facts saved before the field existed fall back to the check stance."""
    return fact.get("hypothesis_stance", fact["stance"])


def hypothesis_citations(draft, claims, ids):
    """{hypothesis id: citations of the supported sentences in its paragraph} for the hypotheses the
    draft writes up. A paragraph runs from the id (first one inside 「情报假设」 when that section
    exists) to the next hypothesis id or line break; ids the draft never names are left out."""
    section = max(draft.find("情报假设"), 0)
    starts = {}
    for h in ids:
        found = re.compile(re.escape(h) + r"(?!\d)").search(draft, section)
        if found:
            starts[h] = found.start()
    bounds = sorted(starts.values()) + [len(draft)]
    cited = {}
    for h, start in starts.items():
        end = bounds[bounds.index(start) + 1]
        if "\n" in draft[start:end]:
            end = draft.index("\n", start)
        cited[h] = set()
        for c in claims:
            at = c["start"] if "start" in c else draft.find(c.get("text", ""))
            if c["verdict"] == "supported" and start <= at < end:
                cited[h].update(c["citations"])
    return cited


def assess(run: "Run"):
    report = run.p["report"]
    if not report.get("draft"):
        run.p["assessment"] = None
        return
    rows = submit.evidence_rows(run)
    result = run.ask(
        "assess",
        prompts.ASSESS,
        {
            "question": run.asked,
            "judgments": run.p.get("judgments", []),
            "report": report["draft"],
            "report_status": report["status"],
            "hypotheses": [{"id": h["id"], "statement": h["statement"]} for h in run.p.get("hypotheses", [])],
            "checks": [{k: c[k] for k in ("id", "text", "status", "evidence")} for c in run.p["checks"]],
            "evidence": [
                {
                    "ref": r.ref,
                    "title": r.title,
                    "published_at": r.published_at,
                    "material_kind": r.material_kind,
                    "verified": r.verified,
                }
                for r in rows
            ],
        },
        schemas.Assessment,
    )
    if result is None:
        run.p["assessment"] = None
        return
    cited = set(re.findall(r"\[(E\d+)\]", report["draft"]))
    for judgment in result.judgments:
        judgment.grounds = [g for g in judgment.grounds if g in cited]
    # Only hypotheses the report writes up are graded; their grounds are the tagged facts cited by
    # supported sentences in their own paragraph (supporting ones, or refuting ones for refuted).
    verified = hypothesis_citations(
        report["draft"], report.get("claims", []), [h["id"] for h in run.p.get("hypotheses", [])]
    )
    result.hypotheses = [h for h in result.hypotheses if h.id in verified]
    assessment = result.model_dump()
    facts = run.p.get("facts", [])
    for h in assessment["hypotheses"]:
        stance = "refutes" if h["level"] == "refuted" else "supports"
        h["grounds"] = sorted(
            {
                f["ref"]
                for f in facts
                if f.get("hypothesis") == h["id"]
                and hypothesis_stance(f) == stance
                and f["ref"] in verified[h["id"]]
            }
        )
    check_assessment(run, assessment)
    order = ["insufficient", "possible", "probable", "confirmed"]
    for j in assessment["judgments"]:
        cap = {"supported": j["confidence"], "contradicted": "insufficient"}.get(j["check"], "possible")
        if order.index(j["confidence"]) > order.index(cap):
            j["confidence"] = cap
        if j["check"] == "contradicted":
            run.note("judgment_contradicted", j["conclusion"])
            j["conclusion"] = "原结论与已读原文矛盾，已撤下"
    # BioCheck writes hypotheses as 待验证 and never as confirmed, so the grade tops out at probable,
    # which also needs a PASS report, no verified refutation and a reason BioCheck supports;
    # refuted needs a supported reason on a verified refutation. Anything else is at most possible.
    for h in assessment["hypotheses"]:
        refuted = any(
            f.get("hypothesis") == h["id"]
            and hypothesis_stance(f) == "refutes"
            and f["ref"] in verified[h["id"]]
            for f in facts
        )
        backed = h["check"] == "supported" and h["grounds"]
        if h["level"] == "refuted":
            cap = "refuted" if backed else "possible"
        else:
            cap = "probable" if backed and report["status"] == "PASS" and not refuted else "possible"
            cap = h["level"] if order.index(h["level"]) <= order.index(cap) else cap
        if h["check"] == "contradicted":
            run.note("hypothesis_reason_contradicted", h["reason"])
            h["reason"] = "原理由与已读原文矛盾，已撤下"
        if cap != h["level"]:
            h["reason"] = f"评估给 {h['level']}，已核验报告只撑得住 {cap}"
            h["level"] = cap
    run.p["assessment"] = assessment
    watch(run)


def check_assessment(run: "Run", assessment):
    """Judgment conclusions and hypothesis reasons are written after the report, so BioCheck
    verifies them, one line each with their grounds, in one call against the same evidence. Sets
    check: supported / contradicted / unverified (also when BioCheck fails or has no budget)."""
    items = assessment["judgments"] + assessment["hypotheses"]
    if not items:
        return
    lines, starts, at = [], [], 0
    for item in items:
        # Each clause is checked on its own: a long conclusion verified whole hid a reversed figure.
        refs = "".join(f"[{g}]" for g in item["grounds"])
        text = item.get("conclusion", item.get("reason", ""))
        line = "".join(p.strip() + refs + "。" for p in re.split(r"[；;。\n]", text) if p.strip())
        lines.append(line or refs + "。")
        starts.append(at)
        at += len(line) + 1
    verdicts = [[] for _ in items]
    try:
        if run.calls_left() <= 0:
            raise ServiceError("biocheck", "no_budget")
        review = submit.service(run, "verify", dict(run.report_input(), draft="\n".join(lines)))
        for c in review["claims"]:
            n = bisect.bisect_right(starts, c["start"]) - 1
            if n >= 0 and c["verdict"] != "no_claim":
                verdicts[n].append(c["verdict"])
    except ServiceError as exc:
        run.log(exc.calls, "biocheck")
        run.note(f"assessment_biocheck_{exc.code}")
    for item, found in zip(items, verdicts):
        if "contradicted" in found:
            item["check"] = "contradicted"
        elif found and all(v == "supported" for v in found):
            item["check"] = "supported"
        else:
            item["check"] = "unverified"


def watch(run: "Run"):
    """A topic analysis leaves its hypotheses' confirming and refuting signals on the topic, so the
    monitor can tell when a later record answers one (W1..; at most 5)."""
    if not run.p.get("hypotheses"):
        return
    with db.session() as s:
        topic_id = s.scalar(
            select(db.Investigation.topic_id).where(db.Investigation.id == run.row_investigation)
        )
        if not topic_id:
            return
        items = [
            {"id": "", "hypothesis": h["id"], "direction": direction, "text": text}
            for h in run.p["hypotheses"]
            for direction in ("confirm", "refute")
            for text in h.get(direction, [])[:1]
        ][:5]
        for n, item in enumerate(items, 1):
            item["id"] = f"W{n}"
        s.add(
            db.TopicEvent(
                topic_id=topic_id,
                at=db.now(),
                kind="watch",
                detail={"items": items, "analysis_id": run.id},
            )
        )
