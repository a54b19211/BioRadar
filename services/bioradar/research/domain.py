"""The complete comparable domain of a Fabric measurement object (docs/decisions/0003 and 0010).
This is the radar's research method; the object and its check are Fabric's.

A number is compared automatically only inside a group whose metric family is determined and every dimension
that family requires is proven: either checked by the checker (per dimension, `dims[].check == "match"`) or
carried by a field of an object the checker found consistent, in that field's own role - with the full field
literal as the dimension's value. A dimension that is unknown, or stated but not checked, takes the point out
of automatic comparison (kept with its gaps, routed for investigation); unknown is never "not applicable".

Families and their required dimensions:
- case_count: disease, population, date_basis, case_definition, coverage (+ denominator_definition, stated
  and checked, for a rate)
- milestone_count: product_or_class, milestone_definition, date_basis (+ indication when the object or its
  metric names one)
- capacity: entity, stage, coverage, date_basis
- efficacy: never compared automatically
The family an object names must be compatible with its metric (a closed indicator list per family); the
indicator only admits the family, it never stands for a dimension."""

import hashlib
import json
import re

GATE_VERSION = "series_domain.v2"
FAMILIES = {
    "case_count": ("disease", "population", "date_basis", "case_definition", "coverage"),
    "milestone_count": ("product_or_class", "milestone_definition", "date_basis"),
    "capacity": ("entity", "stage", "coverage", "date_basis"),
}
STATED = (
    "disease",
    "population",
    "date_basis",
    "indication",
    "product_or_class",
    "milestone_definition",
    "entity",
    "stage",
    "denominator_definition",
)
INDICATOR = {
    "case_count": re.compile(
        r"病例|病人|患者|死亡|感染者|确诊|疑似|cases?\b|deaths?\b|infections?\b|patients?\b", re.IGNORECASE
    ),
    "milestone_count": re.compile(
        r"批准|注册|受理|获批|上市|审评|临床试验|approv|regist|filing|launch|clearance", re.IGNORECASE
    ),
    "capacity": re.compile(
        r"产能|产线|生产线|灌装线|产量|装置|capacity|production lines?|plants?\b|output", re.IGNORECASE
    ),
}
# an indication the metric itself names makes indication a required dimension
INDICATION_CUE = re.compile(r"适应症|适应证|indication|用于治疗|用于[^，。,;；]{1,20}的治疗", re.IGNORECASE)
# a consistent field carries a dimension only in its own role: the metric phrase names the case definition,
# the milestone, the stage or the indication; the scope is the coverage. The whole field literal is kept as
# the dimension's value, so a negation, a plan or a subgroup written with it stays part of it. The object
# proves its literal only, never a business role (product, entity): those are stated dimensions, checked one
# by one
CARRIED_BY = {
    "case_definition": (
        "metric",
        re.compile(
            r"确诊|疑似|临床诊断|死亡|住院|重症|confirmed|suspected|probable|deaths?\b|hospitali[sz]",
            re.IGNORECASE,
        ),
    ),
    "milestone_definition": ("metric", INDICATOR["milestone_count"]),
    "stage": (
        "metric",
        re.compile(
            r"计划|拟建|在建|开工|投运|投产|建成|实际产出|planned|under construction|"
            r"operational|commissioned|in operation",
            re.IGNORECASE,
        ),
    ),
    "indication": ("metric", INDICATION_CUE),
    "coverage": ("scope", None),
}


def norm(text):
    return " ".join((text or "").split()).casefold()


def group_key(family, dims):
    """The group identity: "sha256:" + the hex digest of the canonical JSON [family, [[kind, norm], ...]]
    (pairs sorted, UTF-8, no spaces) - fixed length, never truncated; no character inside a value can make
    two groups one."""
    body = json.dumps(
        [family, sorted([d["kind"], d["norm"]] for d in dims)], ensure_ascii=False, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _required(family, obj, stated):
    required = list(FAMILIES[family])
    if family == "case_count" and obj.get("measure_kind") == "rate":
        required.append("denominator_definition")
    if family == "milestone_count" and (
        "indication" in stated or INDICATION_CUE.search(obj.get("metric") or "")
    ):
        required.append("indication")  # named by the object or its metric: proven, never dropped
    return required


def _carried(kind, obj, consistent):
    """The dimension a consistent field carries in its own role, or None."""
    if kind not in CARRIED_BY or not consistent:
        return None
    field, word = CARRIED_BY[kind]
    if field == "scope" and obj.get("scope_mode") == "object_metric_total":
        field = "object"  # the object's own total
    value = obj.get(field) or ""
    if not value or (word is not None and not word.search(value)):
        return None
    proof = {"field": field, "phrase": value, "check_ref": obj["check"]["run_id"], "result": "match"}
    return {"kind": kind, "norm": norm(value), "state": "carried", "proof": proof}


def _stated(kind, stated, unlocated, check_ref):
    """The dimension as the object states it: checked, or stated and not proven (why); None if unstated."""
    d = stated.get(kind)
    if d is not None and d.get("check") == "match":
        proof = {"check_ref": check_ref, "result": "match", "span": [d.get("start"), d.get("end")]}
        return {
            "kind": kind,
            "norm": norm(d["value"]),
            "state": "checked",
            "text": d["value"],
            "proof": proof,
        }
    if d is not None:
        return {
            "kind": kind,
            "norm": norm(d["value"]),
            "state": "stated_unchecked",
            "text": d["value"],
            "why": d.get("check") or "unchecked",
        }
    if kind in unlocated:
        u = unlocated[kind]
        return {
            "kind": kind,
            "norm": norm(u["text"]),
            "state": "stated_unchecked",
            "text": u["text"],
            "why": u.get("reason") or "unlocated",
        }
    return None


def _dim(kind, obj, consistent, stated, unlocated):
    """One required dimension: carried by a consistent field (unless the object states it otherwise), else as
    stated, else unknown."""
    dim = _carried(kind, obj, consistent)
    if dim is not None and kind in stated and norm(stated[kind]["value"]) not in dim["norm"]:
        dim = None  # the object states it otherwise: a stated dimension is never replaced by a carry
    return (
        dim
        or _stated(kind, stated, unlocated, obj["check"]["run_id"])
        or {"kind": kind, "norm": "", "state": "unknown"}
    )


def _undetermined(family, reason):
    return {"family": family, "dims": [], "dims_key": group_key(family, []), "reasons": [reason]}


def series_gate(obj):
    """{family, dims, dims_key, reasons} of one Fabric measurement object (data_objects.v2). reasons
    empty = complete and proven: the point may be compared automatically in its group; otherwise each gap, and
    nothing is computed from it."""
    family = (obj.get("family") or "").strip()
    consistent = (obj.get("check") or {}).get("status") == "consistent"
    if family == "efficacy":
        return _undetermined(family, "efficacy_not_compared")
    if family not in FAMILIES or not (consistent and INDICATOR[family].search(obj.get("metric") or "")):
        return _undetermined(family, "family_not_determined")
    stated = {d["kind"]: d for d in obj.get("dims") or [] if d.get("kind") in STATED and d.get("value")}
    unlocated = {
        u["kind"]: u for u in obj.get("dims_unlocated") or [] if u.get("kind") in STATED and u.get("text")
    }
    dims = [
        _dim(k, obj, consistent, stated, unlocated)
        for k in _required(family, obj, stated.keys() | unlocated.keys())
    ]
    gaps = {"unknown": "dim_unknown", "stated_unchecked": "dim_unchecked"}
    reasons = [f"{gaps[d['state']]}:{d['kind']}" for d in dims if d["state"] in gaps]
    return {"family": family, "dims": dims, "dims_key": group_key(family, dims), "reasons": reasons}
