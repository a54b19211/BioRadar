"""Stage report: the report input from the evidence read, then BioCheck writes, checks and revises."""

from typing import TYPE_CHECKING

from bioradar.adapters.fabric import noticed
from bioradar.adapters.services import ServiceError
from bioradar.research.run import submit
from bioradar.research.run.assess import hypothesis_stance
from bioradar.research.run.windows import report_text

if TYPE_CHECKING:
    from bioradar.research.pipeline import Run


OVERALL = {"supported": "机制获得支持", "partial": "机制部分获得支持", "insufficient": "机制证据不足"}
REVISIONS = 2  # revise -> re-verify rounds; each only touches the sentences the last check flagged
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


SOURCE_KIND = {
    "clinicaltrials": "registry",
    "pubmed": "research",
    "europe_pmc": "research",
    "who_don": "official",
    "fda_crl": "official",
}


def source_kind(material_kind, source):
    """Fabric material kind -> the six source kinds BioCheck understands."""
    return KIND.get(material_kind) or SOURCE_KIND.get(source, "news")


def report_input(run: "Run"):
    run.upstream_still_readable()  # the scope and checks below may carry the earlier report's words
    rows = submit.evidence_rows(run)
    refs = {(r.source, r.record_id): r for r in rows}
    quotes = {}
    for fact in run.p["facts"]:
        quotes.setdefault(fact["ref"], []).append(fact["quote"])
    # Both ends of a correction go to BioCheck even if one yielded no fact.
    related = {r["to"] for r in run.p["relations"]} | {r["from"] for r in run.p["relations"]}
    sent = [r for r in rows if r.ref in quotes or r.record_id in related]
    # the writer and every BioCheck check get this window of each record; service() records it, with its
    # binding, once per request that carries it
    run.report_windows = []
    for r in sent:
        window = report_text(submit.material_of(run, r), quotes.get(r.ref, []))
        run.report_windows.append((r, noticed(window, (r.meta or {}).get("clean_text_state"))))
    texts = {r.ref: m.text for r, m in run.report_windows}
    evidence = [
        {
            "id": r.ref,
            "title": r.title,
            "source": r.source,
            "published_at": r.published_at,
            "source_kind": source_kind(r.material_kind, r.source),
            "verified": r.verified,
            "text": texts[r.ref],
            # Fabric's explicit same-origin group; absent means independence is unknown, not proven
            "origin_group": (r.meta or {}).get("origin_group"),
        }
        for r in sent
    ]
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
                "calculation_basis": {"mechanism": run.p["mechanism"]["statement"]},
                "limitations": ["机制评价是专业判断，不是实验原件"],
            }
        )

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
    # reposts and supplements keep their own field (they do not supersede anything); the end not
    # read is named by record and marked unread instead of being guessed from a title or a site
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
    searched = {i for a in run.p["actions"] if a["status"] == "completed" for i in a["target_checks"]}
    failed = [
        f"{a['kind']} {a['source']} {a.get('error')}" for a in run.p["actions"] if a["status"] == "failed"
    ]
    body = {
        "question": run.asked,
        "evidence": evidence,
        "analysis_results": results,
        "source_updates": updates,
        "source_relations": relations,
        "research_scope": {
            "checked": [
                c["text"]
                + (
                    "（已找到依据）"
                    if c["status"] != "open"
                    else "（已找到部分依据，未答全）"
                    if c["evidence"]
                    else "（已检索，未找到肯定或否定依据）"
                )
                for c in run.p["checks"]
                if c["id"] in searched
            ],
            "not_checked": [c["text"] for c in run.p["checks"] if c["id"] not in searched],
            "failed": failed,
            "history_cutoff": run.input.get("cutoff"),
        },
    }
    previous = run.previous_report()
    if previous:
        body["previous_report"] = previous
    if run.p.get("hypotheses"):
        # The hypotheses and the facts tagged to them go to BioCheck, which writes and checks the
        # 「情报假设」 section; assess only grades what survived that check.
        cited = {e["id"] for e in evidence}
        body["hypotheses"] = [
            dict(
                h,
                facts=[
                    {
                        "ref": f["ref"],
                        "text": f["text"],
                        # BioCheck's contract: a fact with no direction for this hypothesis is context
                        "stance": hypothesis_stance(f)
                        if hypothesis_stance(f) in {"supports", "refutes"}
                        else "context",
                    }
                    for f in run.p["facts"]
                    if f.get("hypothesis") == h["id"] and f["ref"] in cited
                ],
            )
            for h in run.p["hypotheses"]
        ]
    return body


def report(run: "Run"):
    body = run.report_input()
    report = {"status": "NOT_RUN", "draft": "", "revised": False, "versions": []}
    run.p["report"] = report

    def keep(step):
        """Every draft as soon as it exists, checked afterwards by checked(); if the check fails, the
        draft stays with status "unchecked" and the failure code, so a wrong citation can be traced."""
        report["versions"].append({"step": step, "draft": report["draft"], "status": "unchecked"})

    def checked(review):
        report["versions"][-1].update(
            status=review["status"],
            claims=[
                {k: c.get(k) for k in ("index", "start", "end", "verdict", "citations", "labels")}
                for c in review["claims"]
            ],
        )

    if not body["evidence"]:
        report["reason"] = "no_evidence"
        return
    try:
        report["draft"] = submit.service(run, "generate", dict(body))["draft"]
        keep("generate")
        review = submit.service(run, "verify", dict(body, draft=report["draft"]))
        checked(review)
        for n in range(REVISIONS):
            fixable = [
                c
                for c in review["claims"]
                if c["verdict"] == "contradicted"
                or c.get("scope_span")
                or (c["verdict"] == "insufficient" and c["citations"] and c["labels"])
                or (c["verdict"] == "insufficient" and not c["citations"])
            ]
            if review["status"] == "PASS" or not fixable or run.calls_left() <= 2:
                break
            revised = submit.service(run, "revise", dict(body, draft=report["draft"], review=review))
            if not revised.get("changed"):
                break
            report.update(draft=revised["draft"], revised=True, changed=revised["changed"])
            keep(f"revise_{n + 1}")
            review = submit.service(run, "verify", dict(body, draft=report["draft"]))
            checked(review)
    except ServiceError as exc:
        run.log(exc.calls, "biocheck")
        report["reason"] = f"biocheck_{exc.code}"
        if report["versions"] and report["versions"][-1]["status"] == "unchecked":
            report["versions"][-1]["error"] = exc.code
        run.note(report["reason"])
        return
    report.update(
        status=review["status"],
        claims=review["claims"],
        coverage=review.get("coverage"),
        read_requests=review.get("read_requests", []),
    )
