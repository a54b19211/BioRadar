"""The external view of an analysis's stored state: an allow-list, not a filter.

Only fields research/state.py marks SHOWN leave the radar in the analysis detail, list and export; any other
field - including every one added later - is dropped here, before app.redact withholds, among the shown
fields, what a source that cannot be read now makes unreadable. A value typed Any (another service's
contract: BioCheck's claims, MechEvidence's evaluation, the assessment) is shown as stored, subject to the
same withholding. The monitor's exits (topics, observations) are decided by the functions at the end."""

import types
import typing
from datetime import datetime
from typing import Annotated, Any

from bioradar.research.state import MECH_NOT_RUN_REASONS, SECTION_REASONS, SHOWN, Result, RunState


def is_shown(tp) -> bool:
    return typing.get_origin(tp) is Annotated and SHOWN in typing.get_args(tp)[1:]


def shown(value, tp):
    """`value` with only the fields `tp` marks shown, recursively."""
    if value is None or tp is Any:
        return value
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin is Annotated:
        return shown(value, args[0])
    if origin in (typing.Union, types.UnionType):
        options = [a for a in args if a is not type(None)]
        return shown(value, options[0]) if len(options) == 1 else value
    if typing.is_typeddict(tp) and isinstance(value, dict):
        hints = typing.get_type_hints(tp, include_extras=True)
        return {k: shown(v, hints[k]) for k, v in value.items() if k in hints and is_shown(hints[k])}
    if origin is list and isinstance(value, list):
        return [shown(v, args[0]) for v in value]
    if origin is dict and isinstance(value, dict):
        return {k: shown(v, args[1]) for k, v in value.items()}
    return value


# Enum-like fields kept when model-written text is withheld; every other string is blanked (also a
# judgment's own question: the model writes it and may quote the material).
KEEP = {
    "id",
    "prop_id",
    "derived_from",
    "ref",
    "index",
    "check",
    "status",
    "verdict",
    "confidence",
    "credibility",
    "consequence",
    "priority",
    "alert",
    "level",
    "stance",
    "kind",
    "grounds",
    "citations",
    "found",
}


def is_reason_code(key, value) -> bool:
    """Why a mechanism did not run or a report section is empty or missing: a code the program wrote, not
    model text."""
    return key == "reason" and value in MECH_NOT_RUN_REASONS + SECTION_REASONS


def blank(value):
    """Model-written text derived from the materials, with the words removed and the structure kept."""
    if isinstance(value, dict):
        return {k: (v if k in KEEP or is_reason_code(k, v) else blank(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [blank(v) for v in value]
    return "" if isinstance(value, str) else value


def publication(status, reason=None, missing=()) -> dict:
    """v3 6.2: whether a report may be delivered, decided by the program - never by a model or the planner:
    only a report BioCheck's last check passed (PASS) and whose sections hold every item of design 1.3
    (`missing`: the items they lack, research/run/sections.py). Any other (the check did not run, did not
    answer, found problems, or an item is missing) is held, publishable false with hold pending_verification
    and the reason; the run keeps its draft and assessment for re-verification, and the external views show
    the hold instead of them."""
    if status == "PASS" and not missing:
        return {"publishable": True, "hold": None, "hold_reason": None}
    if not reason and status == "PASS":
        reason = "sections_missing:" + ",".join(missing)
    elif not reason:
        reason = "not_checked" if status in (None, "NOT_RUN") else f"biocheck_{str(status).lower()}"
    return {"publishable": False, "hold": "pending_verification", "hold_reason": reason}


def publication_of(result: dict) -> dict:
    """`result` with its publication fields: as the run stored them, else judged by its report status (a
    result stored before the gate)."""
    if "publishable" in result:
        return result
    return {**result, **publication(result["report_status"], (result.get("review") or {}).get("reason"))}


def gated(result):
    """A result as the external views may show it. A held report is not shown as a finished one: its draft
    and assessment are left out, the sentence checks and kept versions lose their words (numbers,
    verdicts and references stay) and each section keeps only its kind, status and reason. A result stored
    before the gate is judged by its report status."""
    if not result or "report_status" not in result:
        return result
    result = publication_of(dict(result))
    if result["publishable"] is False:
        if "draft" in result:
            result["draft"] = ""
        result["assessment"] = None
        if result.get("review"):
            result["review"] = blank(result["review"])
        if "sections" in result:
            result["sections"] = [
                {k: s[k] for k in ("kind", "status", "reason") if k in s} for s in result["sections"]
            ]
    return result


PROGRESS = {k: tp for k, tp in typing.get_type_hints(RunState, include_extras=True).items() if is_shown(tp)}


def progress(stored: dict | None) -> dict:
    """The shown keys of a stored progress, each present (None when the run has not written it yet)."""
    stored = stored or {}
    return {k: shown(stored.get(k), tp) for k, tp in PROGRESS.items()}


def result(stored: dict | None) -> dict | None:
    """The shown keys of a stored result, as far as they are there, behind the publication gate."""
    return gated(shown(stored, Result))


def summary(stored: dict | None) -> dict:
    """What the analysis list shows of a result: its status, usage and assessment, behind the gate."""
    out = {k: (stored or {}).get(k) for k in ("report_status", "usage", "assessment")}
    if stored is None or "report_status" not in stored:
        return out
    gate = gated({**out, **{k: stored[k] for k in ("publishable", "hold", "hold_reason") if k in stored}})
    return gate


# The monitor's exits. The triage model grades single records. What leaves the radar about them is a program
# fact at once (the lead was received), and a topic's level only behind the gate of the analysis that read the
# topic's records; the model's own verdict on a record (alert, signal, objects, reason) does not leave at all.
LEAD = "本系统于{at}收到来源{source}的线索：{title}（尚未核实）。"
# What an observation's `reason` holds when the program settled the record (research/triage.py); any other
# text is the triage model's.
PROGRAM_REASONS = ("triage_failed", "triage_deferred", "triage_unavailable", "附件未能解析")


def lead(source: str, title: str, at: datetime) -> str:
    """The fast rule path: that the radar received a lead is a program fact and is shown at once, with the
    source, the record's own title and the time it became available - and that nothing is verified."""
    return LEAD.format(at=at.isoformat(), source=source, title=title)


def is_program_reason(reason: str) -> bool:
    return reason.startswith(PROGRAM_REASONS)


def level_publication(analysis: tuple[str, dict | None] | None, owed: bool) -> dict:
    """Whether a topic's level and summary may leave the radar: the report gate applied to the topic. They
    are shown once the topic's latest analysis, the one that read its records, has a report that passed
    (`publication` of that result), and not while the topic is owed a newer analysis. `analysis` is that
    analysis's (state, result); None when the topic has none."""
    if analysis is None:
        return publication(None, "no_analysis")
    state, result = analysis
    if not result or "report_status" not in result:  # not finished, or finished without a report
        return publication(None, None if state == "completed" else f"analysis_{state}")
    stored = publication_of(result)
    if stored["publishable"] and owed:
        return publication(None, "analysis_owed")
    return {k: stored.get(k) for k in ("publishable", "hold", "hold_reason")}
