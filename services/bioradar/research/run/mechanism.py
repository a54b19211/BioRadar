"""Stage mechanism: MechEvidence evaluates the candidate mechanism against the research material read."""

from typing import TYPE_CHECKING

from bioradar.adapters import services
from bioradar.adapters.services import ServiceError
from bioradar.research.run import submit

if TYPE_CHECKING:
    from bioradar.research.pipeline import Run


# MechEvidence (汪鑫怡) only evaluates research material; news, notices and posts are not sent.
MECH_KIND = {
    "primary_research": {"source_kind": "primary", "document_type": "research_article"},
    "conference_abstract": {"source_kind": "primary", "document_type": "conference_abstract"},
    "trial_registry": {"source_kind": "primary", "document_type": "trial_registration"},
    "review": {"source_kind": "review", "document_type": "review"},
    "trial": {"source_kind": "primary", "document_type": "trial_registration"},
    "outcome": {"source_kind": "primary", "document_type": "trial_results"},
}


MECH_LEVEL = {"full_text": "full", "abstract": "abstract"}


def mechanism(run: "Run"):
    target = run.p.get("mechanism")
    if not target or not run.s.mechevidence_url:
        run.p["mechanism_result"] = None if not target else {"status": "not_run", "reason": "not_configured"}
        return
    cited = {f["ref"] for f in run.p["facts"]}
    rows = [r for r in submit.evidence_rows(run) if r.ref in cited and r.material_kind in MECH_KIND]
    if not rows:
        run.p["mechanism_result"] = {"status": "not_run", "reason": "no_research_material"}
        return
    run.upstream_still_readable()
    texts, sent_ids = submit.send_rows(run, "mechanism", "mechevidence", rows)
    materials = [
        {
            "id": r.ref,
            "title": r.title,
            "text": texts[r.ref],
            **MECH_KIND[r.material_kind],
            "text_level": "excerpt"
            if r.meta.get("window") == "selected"
            else MECH_LEVEL.get(r.meta.get("text_level"), "excerpt"),
        }
        for r in rows
    ]
    try:
        with submit.outbound(run, sent_ids):
            response = services.mechevidence(
                run.s,
                {
                    "question": run.asked,
                    "mechanism": target["statement"],
                    "scope": {k: target.get(k, "") for k in ("species", "system", "disease")},
                    "max_llm_calls": max(1, min(10, run.calls_left() - 4)),
                    "materials": materials,
                },
            )
    except ServiceError as exc:
        run.log(exc.calls, "mechevidence")
        run.note(f"mechevidence_{exc.code}")
        run.p["mechanism_result"] = {"status": "failed", "reason": exc.code}
        return
    run.log(response.pop("llm_calls", []), "mechevidence")
    run.p["mechanism_result"] = {"status": "completed", **response}
