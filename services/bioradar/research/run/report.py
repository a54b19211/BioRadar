"""Stage report: the report input from the evidence read, then BioCheck writes (by slot, ADR 0014), checks
and revises; after assess, reconcile settles the paragraphs the assessment disagrees with."""

from typing import TYPE_CHECKING

from bioradar.adapters.fabric import noticed
from bioradar.adapters.services import ServiceError
from bioradar.research.lineage import previous_for_biocheck
from bioradar.research.run import slots, submit
from bioradar.research.run.assess import hypothesis_stance
from bioradar.research.run.windows import report_text

if TYPE_CHECKING:
    from bioradar.research.pipeline import Run


OVERALL = {"supported": "机制获得支持", "partial": "机制部分获得支持", "insufficient": "机制证据不足"}
REVISIONS = 2  # revise -> re-verify rounds, the writer's and the reconcile's together; each only touches the
# sentences the last check flagged
MISMATCH = "assess_insufficient: 评估判本命题证据不足，本段应收窄到证据真正支持的说法"
KIND = {
    "primary_research": "research",
    "review": "research",
    "conference_abstract": "research",
    "trial_registry": "registry",
    "press_release": "company",
    "corporate_disclosure": "company",
    "official_notice": "official",
    "news_report": "news",
    "social_media": "social",
    "repost": "social",
}


def source_kind(material_kind):
    """Fabric material kind -> the six source kinds BioCheck understands; a kind Fabric did not state (or one
    not listed) is read as news, the most cautious of them, never guessed from the source's name."""
    return KIND.get(material_kind, "news")


def report_input(run: "Run"):
    """What BioCheck writes and checks the report from: the window of every record a fact quotes (and both
    ends of a correction), the program's results, the corrections and other relations between the records
    read, the research scope, the earlier report while its sources can be read, and the hypotheses."""
    run.upstream_still_readable()  # the scope and checks below may carry the earlier report's words
    rows = submit.evidence_rows(run)
    evidence = report_evidence(run, rows)
    updates, relations = source_links(run, rows)
    body = {
        "question": run.asked,
        "evidence": evidence,
        "analysis_results": analysis_results(run),
        "source_updates": updates,
        "source_relations": relations,
        "research_scope": research_scope(run),
    }
    previous = run.previous_report()
    if previous:
        body["previous_report"] = previous_for_biocheck(previous)
    if run.p.get("hypotheses"):
        body["hypotheses"] = hypotheses_input(run, {e["id"] for e in evidence})
    return body


def report_evidence(run: "Run", rows):
    """The evidence items of the report input. The writer and every BioCheck check get the same window of
    each record (run.report_windows); service() records it, with its binding, once per request that
    carries it."""
    quotes = {}
    for fact in run.p["facts"]:
        quotes.setdefault(fact["ref"], []).append(fact["quote"])
    # Both ends of a correction go to BioCheck even if one yielded no fact.
    related = {r["to"] for r in run.p["relations"]} | {r["from"] for r in run.p["relations"]}
    sent = [r for r in rows if r.ref in quotes or r.record_id in related]
    run.report_windows = []
    for r in sent:
        window = report_text(submit.material_of(run, r), quotes.get(r.ref, []))
        run.report_windows.append((r, noticed(window, (r.meta or {}).get("clean_text_state"))))
    texts = {r.ref: m.text for r, m in run.report_windows}
    return [
        {
            "id": r.ref,
            "title": r.title,
            "source": r.source,
            "published_at": r.published_at,
            "source_kind": source_kind(r.material_kind),
            "verified": r.verified,
            "text": texts[r.ref],
            # Fabric's explicit same-origin group; absent means independence is unknown, not proven
            "origin_group": (r.meta or {}).get("origin_group"),
            # The version time facts as Fabric gave them, one field each, never merged (task book 06 2.4).
            # A row stored before they were kept sends None, which says unknown: the merged available_at
            # cannot be split to fill them.
            "first_seen_at": (r.meta or {}).get("first_seen_at"),
            "proof_available_at": (r.meta or {}).get("proof_available_at"),
            "replay_mode": (r.meta or {}).get("replay_mode"),
        }
        for r in sent
    ]


def analysis_results(run: "Run"):
    """The program's calculations (A1 ...) and, when it ran, MechEvidence's evaluation."""
    results = [
        {
            "id": c["id"],
            "method": "radar_arithmetic",
            "output": {"value": c["value"], "label": c["label"]},
            "calculation_basis": {
                k: c[k] for k in ("expression", "operands", "unit", "period", "population", "denominator")
            },
            "limitations": ["程序按所列原文数值计算，口径以calculation_basis为准"],
        }
        for c in run.p.get("computations", [])
    ]
    mech = run.p.get("mechanism_result")
    if mech and mech.get("status") == "completed":
        results.append(
            {
                "id": f"A{len(results) + 1}",
                "method": "mechevidence",
                # The writer copies values into Chinese prose, so the verdict goes in as words.
                "output": {
                    "overall": OVERALL.get(mech.get("overall"), mech.get("overall")),
                    **{k: mech.get(k) for k in ("rationale", "gaps", "conflicts")},
                },
                "calculation_basis": {
                    "mechanism": run.p["mechanism"]["statement"],
                    **{
                        k: mech[k]
                        for k in ("contract_version", "evaluator_id", "reuse_key", "reused", "materials")
                        if k in mech
                    },
                },
                "limitations": ["机制评价是专业判断，不是实验原件"],
            }
        )
    return results


def source_links(run: "Run", rows):
    """(source_updates, source_relations) between the records read: a correction or retraction as old ->
    new when both ends were read; reposts and supplements keep their own field (they do not supersede
    anything), and the end not read is named by record and marked unread instead of being guessed from a
    title or a site."""
    refs = {(r.source, r.record_id): r for r in rows}

    def bound(r):
        """The (old, new) evidence rows a relation links: same source and record, and the same
        version when Fabric names one; None when either end was not read."""
        old = refs.get((r.get("to_source", r["source"]), r["to"]))
        new = refs.get((r["source"], r["from"]))
        if old and new and (not r.get("to_version") or old.version_id == r["to_version"]):
            return old, new
        return None

    updates = [
        {
            "target": pair[0].ref,
            "replacement": pair[1].ref,
            "kind": "correction" if r["kind"] == "corrects" else "retraction",
        }
        for r in run.p["relations"]
        if r["kind"] in {"corrects", "retracts"} and (pair := bound(r))
    ]
    relations = []
    for r in run.p["relations"]:
        if r["kind"] not in {"reposts", "supplements"} or (r["source"], r["from"]) not in refs:
            continue
        pair = bound(r)
        relations.append(
            {
                "kind": "repost" if r["kind"] == "reposts" else "supplement",
                "source": refs[(r["source"], r["from"])].ref,
                "target": pair[0].ref if pair else None,
                "target_record": r["to"],
                "read": bool(pair),
            }
        )
    return updates, relations


def searched_checks(p) -> set[str]:
    """The checks a completed search or read was aimed at."""
    return {i for a in p.get("actions", []) if a["status"] == "completed" for i in a["target_checks"]}


def research_scope(run: "Run"):
    """What was searched for each check and how far it got, what was not searched, and what failed."""
    searched = searched_checks(run.p)
    failed = [
        f"{a['kind']} {a['source']} {a.get('error')}" for a in run.p["actions"] if a["status"] == "failed"
    ]

    def found(c):
        if c["status"] != "open":
            return "（已找到依据）"
        return "（已找到部分依据，未答全）" if c["evidence"] else "（已检索，未找到肯定或否定依据）"

    return {
        "checked": [c["text"] + found(c) for c in run.p["checks"] if c["id"] in searched],
        "not_checked": [c["text"] for c in run.p["checks"] if c["id"] not in searched],
        "failed": failed,
        "history_cutoff": run.input.get("cutoff"),
    }


def hypotheses_input(run: "Run", cited):
    """The hypotheses and the facts tagged to them go to BioCheck, which writes and checks the 「情报假设」
    section; assess only grades what survived that check."""

    def stance(f):  # BioCheck's contract: a fact with no direction for this hypothesis is context
        return hypothesis_stance(f) if hypothesis_stance(f) in {"supports", "refutes"} else "context"

    return [
        dict(
            h,
            facts=[
                {"ref": f["ref"], "text": f["text"], "stance": stance(f)}
                for f in run.p["facts"]
                if f.get("hypothesis") == h["id"] and f["ref"] in cited
            ],
        )
        for h in run.p["hypotheses"]
    ]


def keep(report, step):
    """Every draft as soon as it exists, checked afterwards by checked(); if the check fails, the draft stays
    with status "unchecked" and the failure code, so a wrong citation can be traced."""
    report["versions"].append({"step": step, "draft": report["draft"], "status": "unchecked"})


def checked(report, review):
    version = report["versions"][-1]
    version.update(
        status=review["status"],
        claims=[
            {k: c.get(k) for k in ("index", "start", "end", "verdict", "citations", "labels")}
            for c in review["claims"]
        ],
    )
    named = {k: review[k] for k in ("checker_version", "rules_version") if k in review}
    if named:
        version["checker"] = named


def settle(report, review):
    """The report's status and the sentence checks are those of its last check."""
    report.update(
        status=review["status"],
        claims=review["claims"],
        coverage=review.get("coverage"),
        read_requests=review.get("read_requests", []),
    )


def check(run: "Run", body, report):
    """Verify the draft. When the writer wrote by slot the request says which section each paragraph is
    (BioCheck lets an inference be supported only in a judgment, scenarios or recommendation section)."""
    request = dict(body, draft=report["draft"])
    spans = slots.spans_of(report["written"], report["draft"]) if report.get("written") else None
    if spans:
        request["sections"] = spans
    review = submit.service(run, "verify", request)
    checked(report, review)
    return review


def write(run: "Run", body, report):
    """BioCheck writes the report, by slot when the plan named judgment questions: one call, the report as
    sections. A writer that answers with a draft only (an older BioCheck) leaves the sections to be made from
    the assessment (research/run/sections.py). Returns the draft's check."""
    request = dict(body)
    asked = slots.slots_of(run.p)
    if asked:
        request["slots"] = asked
    answer = submit.service(run, "generate", request)
    items, dropped = slots.read_written(answer, asked, [h["id"] for h in run.p.get("hypotheses", [])])
    for why in dropped:
        run.note("writer_section_dropped", why)
    if items:
        report.update(written=items, draft=slots.draft_of(items))
    else:
        report["draft"] = answer.get("draft", "")
        if not report["draft"]:
            raise ServiceError("biocheck", "empty_draft", "the writer gave no usable section and no draft")
    keep(report, "generate")
    return check(run, body, report)


def fixable(claims):
    return [
        c
        for c in claims
        if c["verdict"] == "contradicted"
        or c.get("scope_span")
        or (c["verdict"] == "insufficient" and c["citations"] and c["labels"])
        or (c["verdict"] == "insufficient" and not c["citations"])
    ]


def for_revise(review):
    """The review /v1/revise reads. It takes a sentence's short quotes from `quotes`; a /v2 check keeps them
    in the sentence's units, so they go back there (a quote found once or several times is found)."""
    claims = []
    for c in review["claims"]:
        if c.get("units") and not c.get("quotes"):
            quotes = [
                {"ref": q["ref"], "quote": q["quote"], "found": q["status"] in ("found", "ambiguous")}
                for u in c["units"]
                for q in u.get("quotes", [])
            ]
            c = dict(c, quotes=quotes)
        claims.append(c)
    return {"status": review["status"], "claims": claims}


def revise(run: "Run", body, report, review, step):
    """One revise -> re-verify round over `review`: the new check, or None when the revision changed nothing
    or its paragraphs cannot be put back into the sections. Once the draft changed it is not checked until
    the re-verify answers, so the report is not PASS meanwhile."""
    revised = submit.service(run, "revise", dict(body, draft=report["draft"], review=for_revise(review)))
    if not revised.get("changed"):
        return None
    if report.get("written") and not slots.apply_draft(report["written"], revised["draft"]):
        run.note("writer_sections_lost", step)
        return None
    report.update(draft=revised["draft"], revised=True, changed=revised["changed"], status="NOT_RUN")
    keep(report, step)
    return check(run, body, report)


def fail(run: "Run", report, exc: ServiceError):
    """A BioCheck request failed: the model calls it paid for are logged, the unchecked draft keeps the
    code, and a report whose draft is not the one last checked is held with it (the PASS of an earlier draft
    is not kept)."""
    run.log(exc.calls, "biocheck", reads=exc.reads)
    run.note(f"biocheck_{exc.code}")
    if report["versions"] and report["versions"][-1]["status"] == "unchecked":
        report["versions"][-1]["error"] = exc.code
    if report["status"] != "PASS":
        report["reason"] = f"biocheck_{exc.code}"
        for key in ("claims", "coverage", "read_requests"):
            report.pop(key, None)


def report(run: "Run"):
    body = run.report_input()
    report = {"status": "NOT_RUN", "draft": "", "revised": False, "versions": []}
    run.p["report"] = report
    if not body["evidence"]:
        report["reason"] = "no_evidence"
        return
    try:
        review = write(run, body, report)
        for n in range(REVISIONS):
            if review["status"] == "PASS" or not fixable(review["claims"]) or run.calls_left() <= 2:
                break
            again = revise(run, body, report, review, f"revise_{n + 1}")
            if again is None:
                break
            review = again
    except ServiceError as exc:
        fail(run, report, exc)
        return
    settle(report, review)


def downgraded(report, bad):
    """The last check's sentences, with the supported ones inside the mismatched paragraphs marked
    insufficient for going beyond their evidence (label STR), as the sentence check marks such a sentence:
    that is the kind of sentence the revise step narrows."""
    spans = dict(zip((id(i) for i in report["written"]), slots.paragraph_spans(report["draft"])))
    inside = [spans[id(i)] for i in bad]
    return [
        dict(
            c,
            verdict="insufficient",
            labels=list(dict.fromkeys([*c.get("labels", []), "STR"])),
            reason=MISMATCH,
            scope_span=None,
        )
        if c["verdict"] == "supported" and any(s <= c["start"] < e for s, e in inside)
        else c
        for c in report["claims"]
    ]


def reconcile(run: "Run"):
    """After assess (task book 08 2.3): a paragraph that states its slot's conclusion where the assessment
    grades the slot insufficient is revised and re-checked, in the revisions the report stage left. A
    paragraph the revision rewrote is then the weaker claim (its status becomes insufficient); one it could
    not (no revision left, no budget, nothing changed) stays inconsistent, and the judgment section is held
    (research/run/sections.py). A report that is not PASS is held already and is left alone."""
    report, assessment = run.p.get("report") or {}, run.p.get("assessment") or {}
    items = report.get("written")
    if not items or report.get("status") != "PASS" or not report.get("claims"):
        return
    used = sum(1 for v in report["versions"] if v["step"].startswith("revise"))
    body = None
    try:
        for n in range(used, REVISIONS):
            bad = slots.mismatched(items, assessment.get("judgments", []))
            if report["status"] != "PASS" or not bad or run.calls_left() <= 2:
                return
            body = body or run.report_input()
            before = {id(i): i["text"] for i in bad}
            again = revise(
                run,
                body,
                report,
                {"status": report["status"], "claims": downgraded(report, bad)},
                f"revise_{n + 1}",
            )
            if again is None:
                return
            for item in bad:
                if item["text"] != before[id(item)]:
                    item["status"] = "insufficient"
            settle(report, again)
    except ServiceError as exc:
        fail(run, report, exc)
