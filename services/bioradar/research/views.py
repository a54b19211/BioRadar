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


PROGRESS = {k: tp for k, tp in typing.get_type_hints(RunState, include_extras=True).items() if is_shown(tp)}


def progress(stored: dict | None) -> dict:
    """The shown keys of a stored progress, each present (None when the run has not written it yet)."""
    stored = stored or {}
    return {k: shown(stored.get(k), tp) for k, tp in PROGRESS.items()}


def result(stored: dict | None) -> dict | None:
    """The shown keys of a stored result, as far as they are there."""
    return shown(stored, Result)
