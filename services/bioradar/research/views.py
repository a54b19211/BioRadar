"""The external view of an analysis's stored state: an allow-list, not a filter.

Only fields research/state.py marks SHOWN leave the radar in the analysis detail, list and export; any other
field - including every one added later - is dropped here, before app.redact withholds, among the shown
fields, what a source that cannot be read now makes unreadable. A value typed Any (another service's
contract: BioCheck's claims, MechEvidence's evaluation, the assessment) is shown as stored, subject to the
same withholding."""

import types
import typing
from typing import Annotated, Any

from bioradar.research.state import SHOWN, Result, RunState


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


def blank(value):
    """Model-written text derived from the materials, with the words removed and the structure kept."""
    if isinstance(value, dict):
        return {k: (v if k in KEEP else blank(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [blank(v) for v in value]
    return "" if isinstance(value, str) else value


def publication(status, reason=None) -> dict:
    """v3 6.2: whether a report may be delivered, decided by the program - never by a model or the planner:
    only a report BioCheck's last check passed (PASS). Any other (the check did not run, did not answer, or
    found problems) is held, publishable false with hold pending_verification and the reason; the run keeps
    its draft and assessment for re-verification, and the external views show the hold instead of them."""
    if status == "PASS":
        return {"publishable": True, "hold": None, "hold_reason": None}
    if not reason:
        reason = "not_checked" if status in (None, "NOT_RUN") else f"biocheck_{str(status).lower()}"
    return {"publishable": False, "hold": "pending_verification", "hold_reason": reason}


def gated(result):
    """A result as the external views may show it. A held report is not shown as a finished one: its draft
    and assessment are left out and the sentence checks and kept versions lose their words (numbers,
    verdicts and references stay). A result stored before the gate is judged by its report status."""
    if not result or "report_status" not in result:
        return result
    result = dict(result)
    if "publishable" not in result:
        result.update(publication(result["report_status"], (result.get("review") or {}).get("reason")))
    if result["publishable"] is False:
        if "draft" in result:
            result["draft"] = ""
        result["assessment"] = None
        if result.get("review"):
            result["review"] = blank(result["review"])
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
