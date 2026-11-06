"""The complete comparable domain of a checked point (T-24, design T-24-完整域-r2, Codex 725/730/r171).

A number is compared automatically only inside a group whose metric family is determined and every dimension
that family requires is proven: either checked by the checker (v11, per dimension) or carried by a field the
checker matched, in that field's own role - with the full qualifying phrase, its span and the check that
matched it. A dimension that is unknown, or stated but not checked, takes the point out of automatic
comparison (kept as a candidate with its gaps, routed for investigation); unknown is never "not applicable".

Families and their required dimensions:
- case_count: disease, population, date_basis, case_definition, coverage (+ denominator_definition, stated
  and checked, for a rate)
- milestone_count: product_or_class, milestone_definition, date_basis (+ indication when the measure or its
  metric names one)
- capacity: entity, stage, coverage, date_basis
- efficacy: never compared automatically this round (efficacy_not_compared)
The family a measure names must be compatible with its matched metric (a closed indicator list per family);
the indicator only admits the family, it never stands for a dimension."""

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
KINDS = STATED + ("case_definition", "coverage")
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
# an indication the matched metric itself names makes indication a required dimension (Codex r171)
INDICATION_CUE = re.compile(r"适应症|适应证|indication|用于治疗|用于[^，。,;；]{1,20}的治疗", re.IGNORECASE)
# a matched field carries a dimension only in its own role: the metric phrase names the case definition, the
# milestone, the stage or the indication; the scope is the coverage. The whole field literal is kept as the
# dimension's value, so a negation, a plan or a subgroup written with it stays part of it. A matched object or
# denominator proves its literal only, never a business role (product, entity, denominator definition): those
# are stated dimensions, checked by checker v11 (Codex r171)
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


def _norm(text):
    return " ".join((text or "").split()).casefold()


def group_key(family, dims):
    """The group identity: "sha256:" + the hex digest of the canonical JSON [family, [[kind, norm], ...]]
    (pairs sorted, UTF-8, no spaces) - an identity of the full dimension values kept in Measurement.dims,
    fixed length, never truncated; no character inside a value can make two groups one (Codex r171)."""
    body = json.dumps(
        [family, sorted([d["kind"], d["norm"]] for d in dims)], ensure_ascii=False, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _dims_answered(check):
    """{kind: result} of the dimensions checker v11 answered (contract v3), from the stored response."""
    response = (check.response if check is not None else None) or {}
    return ((response.get("items") or [{}])[0]).get("dims") or {}


def _fields(check):
    """{name: (result, value, span)} of the item the check answered, from its stored request and response."""
    request = (check.request if check is not None else None) or {}
    response = (check.response if check is not None else None) or {}
    asked = ((request.get("items") or [{}])[0]).get("fields") or {}
    answered = ((response.get("items") or [{}])[0]).get("fields") or {}
    out = {}
    for name, f in asked.items():
        if not isinstance(f, dict):
            continue
        out[name] = ((answered.get(name) or {}).get("result"), f.get("value"), f.get("span"))
    return out, asked


def series_gate(measure, final, check):
    """{family, dims, dims_key, reasons} of a checked point. reasons empty = complete and proven: the point
    may be compared automatically in its group; otherwise each gap, and nothing is computed from it."""
    fields, asked = _fields(check)
    answered = _dims_answered(check)
    unlocated = {u.get("kind"): u for u in final.get("dims_unlocated") or []}
    check_id = getattr(check, "id", None)

    def matched(name):
        result, value, span = fields.get(name, (None, None, None))
        return result == "match" and bool(value) and span is not None

    family = (measure.get("family") or "").strip()
    metric = (fields.get("metric") or (None, final.get("metric")))[1] or ""
    if family == "efficacy":
        return {
            "family": family,
            "dims": [],
            "dims_key": group_key(family, []),
            "reasons": ["efficacy_not_compared"],
        }
    if family not in FAMILIES or not (matched("metric") and INDICATOR[family].search(metric)):
        return {
            "family": family,
            "dims": [],
            "dims_key": group_key(family, []),
            "reasons": ["family_not_determined"],
        }
    required = list(FAMILIES[family])
    stated = {}
    for d in measure.get("dims") or []:
        if d.get("kind") in STATED and d.get("text") and d["kind"] not in stated:
            stated[d["kind"]] = d
    if family == "case_count" and final.get("measure_kind") == "rate":
        required.append("denominator_definition")
    if family == "milestone_count" and ("indication" in stated or INDICATION_CUE.search(metric)):
        # the measure or the matched metric itself names an indication: it must be proven, never dropped
        # because the model left it out (Codex r171)
        required.append("indication")
    dims, reasons = [], []
    for kind in required:
        dim = None
        if kind in CARRIED_BY:
            field, word = CARRIED_BY[kind]
            result, value, span = fields.get(field, (None, None, None))
            if field == "scope" and asked.get("scope_mode") == "object_metric_total" and matched("object"):
                field, (result, value, span) = "object", fields["object"]  # the object's own total
            if result == "match" and value and span is not None and (word is None or word.search(value)):
                dim = {
                    "kind": kind,
                    "norm": _norm(value),
                    "state": "carried",
                    "proof": {
                        "field": field,
                        "phrase": value,
                        "span": span,
                        "check_id": check_id,
                        "result": result,
                    },
                }
            if dim is not None and kind in stated and _norm(stated[kind]["text"]) not in dim["norm"]:
                dim = None  # the measure states it otherwise: a stated dimension is never replaced by a carry
        if dim is None and kind in stated:
            got = answered.get(kind) or {}
            if got.get("result") == "match" and got.get("span"):
                # checker v11: its literal, its relation to the value and its role, from the text (Codex 740)
                dim = {
                    "kind": kind,
                    "norm": _norm(stated[kind]["text"]),
                    "state": "checked",
                    "text": stated[kind]["text"],
                    "proof": {
                        "check_id": check_id,
                        "result": "match",
                        "reason": got.get("reason"),
                        "span": got["span"],
                    },
                }
            else:
                # stated, not proven: not located in its cited source, or not matched by the checker
                dim = {
                    "kind": kind,
                    "norm": _norm(stated[kind]["text"]),
                    "state": "stated_unchecked",
                    "text": stated[kind]["text"],
                    "source": stated[kind].get("source") or "quote",
                    "why": (unlocated.get(kind) or {}).get("reason") or got.get("reason") or "not_answered",
                }
        if dim is None:
            dim = {"kind": kind, "norm": "", "state": "unknown"}
        dims.append(dim)
        if dim["state"] == "unknown":
            reasons.append(f"dim_unknown:{kind}")
        elif dim["state"] == "stated_unchecked":
            reasons.append(f"dim_unchecked:{kind}")
    return {"family": family, "dims": dims, "dims_key": group_key(family, dims), "reasons": reasons}
