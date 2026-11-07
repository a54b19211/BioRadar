"""Stage mechanism: MechEvidence evaluates the candidate mechanism against the research material read."""

from typing import TYPE_CHECKING

from bioradar.adapters import services
from bioradar.adapters.services import ServiceError
from bioradar.research.run import mech_v2, submit

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


def describe(row) -> dict:
    """What MechEvidence is told about a material besides its text: the kind of source and document the record
    is (from Fabric's material kind) and how much of the record the text is."""
    return {
        **MECH_KIND[row.material_kind],
        "text_level": "excerpt"
        if row.meta.get("window") == "selected"
        else MECH_LEVEL.get(row.meta.get("text_level"), "excerpt"),
    }


def mechanism(run: "Run"):
    target = run.p.get("mechanism")
    if not target:
        run.p["mechanism_result"] = {"status": "not_run", "reason": "no_mechanism_question"}
        return
    if not run.s.mechevidence_url:
        run.p["mechanism_result"] = {"status": "not_run", "reason": "not_configured"}
        return
    cited = {f["ref"] for f in run.p["facts"]}
    rows = [r for r in submit.evidence_rows(run) if r.ref in cited and r.material_kind in MECH_KIND]
    if not rows:
        run.p["mechanism_result"] = {"status": "not_run", "reason": "no_research_material"}
        return
    if run.s.mechevidence_contract == "v2":
        mech_v2.evaluate(run, target, rows, describe)
        return
    run.upstream_still_readable()
    texts, sent_ids = submit.send_rows(run, "mechanism", "mechevidence", rows)
    materials = [{"id": r.ref, "title": r.title, "text": texts[r.ref], **describe(r)} for r in rows]
    try:
        with submit.outbound(run, sent_ids):
            run.confirm_sources({r.source for r in rows})
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
        run.log(exc.calls, "mechevidence", reads=sent_ids)
        run.note(f"mechevidence_{exc.code}")
        run.p["mechanism_result"] = {"status": "failed", "reason": exc.code}
        return
    run.log(response.pop("llm_calls", []), "mechevidence", reads=sent_ids)
    run.p["mechanism_result"] = {"status": "completed", **response}
