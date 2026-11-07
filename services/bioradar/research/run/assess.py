"""Stage assess: the model grades the judgments and hypotheses; BioCheck verifies what it wrote, and the
program caps every grade by what the verified report supports."""

import bisect
import re
from typing import TYPE_CHECKING

from sqlalchemy import select

from bioradar import db
from bioradar.adapters.services import ServiceError
from bioradar.research import prompts, schemas
from bioradar.research.run import slots, submit

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


ORDER = ["insufficient", "possible", "probable", "confirmed"]


def assess(run: "Run"):
    """The model grades the judgments and hypotheses from the checked report; BioCheck verifies what it
    wrote (check_assessment), and every grade is capped by what the verified report supports."""
    report = run.p["report"]
    if not report.get("draft"):
        run.p["assessment"] = None
        return
    result = ask_assessment(run, report)
    if result is None:
        run.p["assessment"] = None
        return
    assessment, verified = grounded(run, result, report)
    slots.bind(assessment["judgments"], slots.slots_of(run.p))
    answered = check_assessment(run, assessment)
    cap_judgments(run, assessment)
    cap_basis(run, assessment, answered)
    cap_hypotheses(run, assessment, verified, report["status"])
    run.p["assessment"] = assessment
    watch(run)


def ask_assessment(run: "Run", report):
    rows = submit.evidence_rows(run)
    return run.ask(
        "assess",
        prompts.ASSESS,
        {
            "question": run.asked,
            "judgments": slots.slots_of(run.p),
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
        carries={r.source for r in rows},  # the draft and the list of every record read
    )


def grounded(run: "Run", result, report):
    """(assessment, {hypothesis: verified citations}): judgment grounds only refs the report cites; only
    hypotheses the report writes up are graded, and their grounds are the tagged facts cited by supported
    sentences in their own paragraph (supporting ones, or refuting ones for refuted)."""
    cited = set(re.findall(r"\[(E\d+)\]", report["draft"]))
    for judgment in result.judgments:
        judgment.grounds = [g for g in judgment.grounds if g in cited]
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
    return assessment, verified


def cap_judgments(run: "Run", assessment):
    """A judgment BioCheck did not find supported is at most possible; one it found contradicted is
    withdrawn."""
    for j in assessment["judgments"]:
        cap = {"supported": j["confidence"], "contradicted": "insufficient"}.get(j["check"], "possible")
        if ORDER.index(j["confidence"]) > ORDER.index(cap):
            j["confidence"] = cap
        if j["check"] == "contradicted":
            run.note("judgment_contradicted", j["conclusion"])
            j["conclusion"] = "原结论与已读原文矛盾，已撤下"


def open_dependency(p, judgment) -> bool:
    """A check the judgment's own grounds answer is still open or in conflict. Only the checks this judgment
    depends on count: a gap in what other judgments rest on does not touch it."""
    status = {c["id"]: c["status"] for c in p.get("checks", [])}
    return any(
        status.get(check) in ("open", "conflict")
        for fact in p.get("facts", [])
        if fact["ref"] in judgment["grounds"]
        for check in fact.get("checks", [])
    )


def cap_basis(run: "Run", assessment, checked: bool):
    """The basis confidence of each judgment (task book 08 2.5): the model proposes high, medium or low. It is
    "not_assessed" - not an automatic low - when the model proposed none or the check of the conclusions did
    not run (`checked`), and at most low while a check the judgment depends on is open or in conflict."""
    for j in assessment["judgments"]:
        proposed = j.get("basis_confidence")
        if not checked or proposed not in ("high", "medium", "low"):
            j["basis_confidence"] = "not_assessed"
        elif open_dependency(run.p, j):
            j["basis_confidence"] = "low"


def cap_hypotheses(run: "Run", assessment, verified, report_status):
    """BioCheck writes hypotheses as 待验证 and never as confirmed, so the grade tops out at probable,
    which also needs a PASS report, no verified refutation and a reason BioCheck supports; refuted needs
    a supported reason on a verified refutation. Anything else is at most possible."""
    facts = run.p.get("facts", [])
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
            cap = "probable" if backed and report_status == "PASS" and not refuted else "possible"
            cap = h["level"] if ORDER.index(h["level"]) <= ORDER.index(cap) else cap
        if h["check"] == "contradicted":
            run.note("hypothesis_reason_contradicted", h["reason"])
            h["reason"] = "原理由与已读原文矛盾，已撤下"
        if cap != h["level"]:
            h["reason"] = f"评估给 {h['level']}，已核验报告只撑得住 {cap}"
            h["level"] = cap


def assessment_line(item: dict) -> str:
    """One conclusion or reason as a sentence with its grounds, for BioCheck to check against the same
    evidence. How a sentence is cut into the statements to check is BioCheck's; here a line is one
    sentence."""
    refs = "".join(f"[{g}]" for g in item["grounds"])
    text = item.get("conclusion", item.get("reason", "")).strip().rstrip("。.；;")
    return f"{text}{refs}。" if text else refs + "。"


def verifying_call(claim: dict, logged: list[dict]) -> int | None:
    """The llm_calls row of the call that checked this sentence: the call BioCheck names for it (`call`, a
    position in the calls of its answer), else the request's only sentence-check call. None when the answer
    does not say and there were several."""
    named = claim.get("call")
    if isinstance(named, int) and 0 <= named < len(logged):
        return logged[named]["id"]
    ids = [c["id"] for c in logged if c["stage"] == "verify_claims"]
    return ids[0] if len(ids) == 1 else None


def check_assessment(run: "Run", assessment):
    """Judgment conclusions and hypothesis reasons are written after the report, so BioCheck
    verifies them, one line each with their grounds, in one call against the same evidence; the whole text is
    a judgment section. Sets check: supported / contradicted / unverified (also when BioCheck fails or has no
    budget), and checked_by: the call that checked it, when that is known. Returns whether BioCheck
    answered."""
    items = assessment["judgments"] + assessment["hypotheses"]
    if not items:
        return True
    lines = [assessment_line(item) for item in items]
    starts = [sum(len(line) + 1 for line in lines[:n]) for n in range(len(lines))]
    draft = "\n".join(lines)
    verdicts, calls, answered = [[] for _ in items], [set() for _ in items], True
    try:
        if run.calls_left() <= 0:
            raise ServiceError("biocheck", "no_budget")
        sections = [{"kind": "judgment", "start": 0, "end": len(draft)}]
        review = submit.service(run, "verify", dict(run.report_input(), draft=draft, sections=sections))
        for c in review["claims"]:
            n = bisect.bisect_right(starts, c["start"]) - 1
            if n >= 0 and c["verdict"] != "no_claim":
                verdicts[n].append(c["verdict"])
                calls[n].add(verifying_call(c, review.get("logged", [])))
    except ServiceError as exc:
        run.log(exc.calls, "biocheck", reads=exc.reads)
        run.note(f"assessment_biocheck_{exc.code}")
        answered = False
    for item, found, made in zip(items, verdicts, calls):
        if "contradicted" in found:
            item["check"] = "contradicted"
        elif found and all(v == "supported" for v in found):
            item["check"] = "supported"
        else:
            item["check"] = "unverified"
        if len(made) == 1 and None not in made:
            item["checked_by"] = next(iter(made))
    return answered


def watch(run: "Run"):
    """A topic analysis leaves its hypotheses' confirming and refuting signals on the topic, so the
    monitor can tell when a later record answers one (W1..; at most 5)."""
    if not run.p.get("hypotheses"):
        return
    with run.writing() as s:
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
