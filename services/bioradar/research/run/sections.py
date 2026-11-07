"""The report as sections (design v3.1 1.3; task book 08 2.2): the program arranges what the finished run
holds into sections of fixed kinds, with no model call, and shows which of the five things every report must
say are there.

The writer answers by slot (research/run/slots.py, ADR 0014): the main judgment is its paragraph for each
of the plan's judgment questions, with the status it states and what assess graded the same slot, and the
evidence that would change the judgment is its scenarios paragraph; its evidence and gaps paragraphs follow
the program's own items. A writer that gives a draft only leaves those two sections to be made from the
assessment (a judgment's conclusion and the consequence it was graded, a hypothesis's level; what the
assessment says is still unproven). The evidence for and against is the facts the investigation read, the
limits what it did not find or could not read. A section the run cannot fill is `missing`, with a reason, and
views.publication holds a report with a missing section as 待核 even when BioCheck passed it; so is a slot
whose paragraph states a conclusion that assess grades insufficient, which the revision in the report stage
could not settle (`slot_mismatch`).

The recommendation section is for the requester's objectives. The run takes none, so the section is empty and
says why."""

from typing import TYPE_CHECKING

from bioradar.research.run import slots, submit
from bioradar.research.run.report import searched_checks
from bioradar.research.state import RunState, Section, SectionItem, VisibleRecord, VisibleTime, WrittenItem

if TYPE_CHECKING:
    from bioradar.research.pipeline import Run

# What the gaps section says for each reason the mechanism stage did not run (MECH_NOT_RUN_REASONS in
# research/state.py; the page words them in apps/web/src/labels.ts).
MECH_NOT_RUN_TEXT = {
    "no_mechanism_question": "本题未提出机制问题",
    "not_configured": "机制评价服务未配置",
    "no_research_material": "本次材料中没有可评的原始研究",
    "no_bound_material": "可评材料都未能按固定版本重新读取",
}


UNKNOWN = "未知"
# The times of a record read, with what each is. They stay apart and none stands in for another (task book 06
# 2.4): the date the source states is not when this system could read the version.
TIME_LABELS = {
    "published_at": "发布日期（来源自述）",
    "first_seen_at": "本系统首见时间",
    "proof_available_at": "档案证明的可得时间",
    "replay_mode": "回放模式",
}


def report_sections(run: "Run") -> list[Section]:
    return build(run.p, run.input, submit.evidence_rows(run))


def build(p: RunState, inp: dict, rows) -> list[Section]:
    """The report's sections, in reading order. `rows`: the evidence rows the run read."""
    gaps = gap_items(p, inp)
    return [
        section("question", [{"text": inp["question"]}]),
        judgment_section(p),
        evidence_section(p),
        section("gaps", gaps + written_texts(p, "gaps")),
        scenarios_section(p),
        visibility_section(gaps, inp, rows),
        recommendation_section(),
    ]


def missing_items(sections: list[Section]) -> list[str]:
    """The items of design 1.3 the sections lack (main_judgment, impact, evidence, review_condition); the
    limits cannot lack: the gaps section always states the scope of what was searched."""
    return [s["reason"].removeprefix("no_") for s in sections if s["status"] == "missing"]


def section(kind: str, items: list[SectionItem], missing: str | None = None) -> Section:
    """A section of `kind` holding `items`; `missing`, a reason code, marks it missing."""
    out: Section = {"kind": kind, "status": "missing" if missing else "ok", "items": items}
    if missing:
        out["reason"] = missing
    return out


def recommendation_section() -> Section:
    """Recommendations are written only for the requester's objectives, and the run takes none: the section is
    empty, and says so."""
    return {
        "kind": "recommendation",
        "status": "empty",
        "reason": "no_objectives",
        "items": [{"text": "任务方未给目标，不写建议", "reason": "no_objectives"}],
    }


def standing_judgments(assessment: dict) -> list[dict]:
    """The assessment's judgments less those BioCheck found contradicted: assess withdrew them, the report
    does not make them."""
    return [j for j in assessment.get("judgments", []) if j.get("check") != "contradicted"]


def written_items(p: RunState) -> list[WrittenItem]:
    return (p.get("report") or {}).get("written") or []


def written_texts(p: RunState, kind: str) -> list[SectionItem]:
    """The writer's paragraphs of a section that answer no slot, as items."""
    return [{"text": w["text"]} for w in written_items(p) if w["kind"] == kind and not w.get("slot")]


def hypothesis_items(assessment: dict) -> list[SectionItem]:
    return [
        {
            "id": h["id"],
            "text": h["reason"],
            "level": h["level"],
            "check": h.get("check", "unverified"),
            "grounds": h["grounds"],
        }
        for h in assessment.get("hypotheses", [])
    ]


def slot_judgment_section(p: RunState, written: list[WrittenItem]) -> Section:
    """The judgment section of a report written by slot: for each slot its paragraph and the status it states,
    with what assess graded the same slot; a slot with no paragraph is cannot_judge, with the reason."""
    assessment = p.get("assessment") or {}
    graded = {j["slot"]: j for j in assessment.get("judgments", []) if j.get("slot")}
    items: list[SectionItem] = []
    for asked in slots.slots_of(p):
        item: SectionItem = {"slot": asked["slot"], "question": asked["question"]}
        paragraph = next((w for w in written if w.get("slot") == asked["slot"]), None)
        if paragraph is None:
            item.update(status=slots.CANNOT_JUDGE, reason="no_paragraph")
        else:
            item.update(text=paragraph["text"], status=paragraph["status"])
        if asked["slot"] in graded:
            j = graded[asked["slot"]]
            item.update(
                confidence=j["confidence"],
                basis_confidence=j.get("basis_confidence", "not_assessed"),
                consequence=j["consequence"],
                check=j.get("check", "unverified"),
                grounds=j["grounds"],
            )
        items.append(item)
    items += [{"text": w["text"]} for w in written if w["kind"] == "judgment" and not w.get("slot")]
    items += hypothesis_items(assessment)
    if all(i.get("status") == slots.CANNOT_JUDGE for i in items):
        return section("judgment", items, "no_main_judgment")
    if slots.mismatched(written, assessment.get("judgments", [])):
        return section("judgment", items, "slot_mismatch")
    return section("judgment", items, None if any(i.get("consequence") for i in items) else "no_impact")


def judgment_section(p: RunState) -> Section:
    if written_items(p):
        return slot_judgment_section(p, written_items(p))
    assessment = p.get("assessment") or {}
    judgments = standing_judgments(assessment)
    items: list[SectionItem] = [
        {
            "question": j["question"],
            "text": j["conclusion"],
            "confidence": j["confidence"],
            "basis_confidence": j.get("basis_confidence", "not_assessed"),
            "consequence": j["consequence"],
            "check": j.get("check", "unverified"),
            "grounds": j["grounds"],
        }
        for j in judgments
    ] + hypothesis_items(assessment)
    if not items:
        return section("judgment", items, "no_main_judgment")
    return section("judgment", items, None if any(j["consequence"] for j in judgments) else "no_impact")


def evidence_section(p: RunState) -> Section:
    """What the facts read say for and against each check; a check in conflict has both sides listed."""
    checks = {c["id"]: c for c in p.get("checks", [])}
    refs: dict[tuple[str, str], list[str]] = {}
    for fact in p.get("facts", []):
        if fact["stance"] in ("supports", "refutes"):
            for check in fact.get("checks", []):
                cited = refs.setdefault((check, fact["stance"]), [])
                if fact["ref"] not in cited:
                    cited.append(fact["ref"])
    items: list[SectionItem] = []
    for (check, stance), cited in refs.items():
        known = checks.get(check, {})
        items.append(
            {
                "id": check,
                "text": known.get("text", ""),
                "status": known.get("status", "open"),
                "stance": stance,
                "grounds": cited,
            }
        )
    return section("evidence", items + written_texts(p, "evidence"), None if items else "no_evidence")


def gap_items(p: RunState, inp: dict) -> list[SectionItem]:
    """The limits: what the searches stand on, then each check not searched or not answered, the searches
    that failed, and why the mechanism was not evaluated."""
    sources = "、".join(inp["sources"])
    items: list[SectionItem] = [
        {"text": f"只依据已检索的来源（{sources}）中读到的材料；没读到的不能当作不存在"}
    ]
    searched = searched_checks(p)
    for c in p.get("checks", []):
        if c["id"] not in searched:
            status = "not_searched"
        elif c["status"] == "open":
            status = "partial" if c["evidence"] else "not_found"
        else:
            continue
        items.append({"id": c["id"], "text": c["text"], "status": status})
    failed = sum(1 for a in p.get("actions", []) if a["status"] == "failed")
    if failed:
        items.append(
            {"text": f"{failed} 次检索或读取失败，对应的材料没有读到", "status": "failed", "count": failed}
        )
    mechanism = p.get("mechanism_result") or {}
    if mechanism.get("status") == "not_run":
        reason = mechanism.get("reason", "")
        text = MECH_NOT_RUN_TEXT.get(reason, "原因未记录")
        items.append({"text": f"机制评价未运行：{text}", "status": "not_run", "reason": reason})
    return items


def scenarios_section(p: RunState) -> Section:
    """What would change the judgment: what the assessment says is still unproven (and which evidence
    separates the cases), and the signals that would confirm or refute each graded hypothesis."""
    assessment = p.get("assessment") or {}
    items: list[SectionItem] = (
        written_texts(p, "scenarios")
        if written_items(p)
        else [
            {"question": j["question"], "text": j["unproven"]}
            for j in standing_judgments(assessment)
            if j["unproven"]
        ]
    )
    graded = {h["id"] for h in assessment.get("hypotheses", [])}
    for h in p.get("hypotheses", []):
        if h["id"] in graded:
            for direction, stance in (("confirm", "supports"), ("refute", "refutes")):
                items += [{"id": h["id"], "text": text, "stance": stance} for text in h.get(direction, [])]
    return section("scenarios", items, None if items else "no_review_condition")


def record_times(r) -> list[VisibleTime]:
    """The times of one record read, as its evidence row holds them: the published date, when this system
    first saw the version, when a reviewed archive proves it readable (listed only when there is such a proof)
    and the mode its read ran in. A row stored before the radar kept them has none of the last three: that is
    said as 未知, and the merged available_at is not used in their place."""
    meta = r.meta or {}
    held = {
        "published_at": r.published_at,
        "first_seen_at": meta.get("first_seen_at"),
        "proof_available_at": meta.get("proof_available_at"),
        "replay_mode": meta.get("replay_mode"),
    }
    return [
        {"kind": kind, "label": label, "value": held[kind] or UNKNOWN}
        for kind, label in TIME_LABELS.items()
        if held[kind] or kind != "proof_available_at"
    ]


def visibility_section(gaps: list[SectionItem], inp: dict, rows) -> Section:
    """What the report could see: the cutoff, how many records were read, the key gaps with their numbers;
    each record read (its version and times) in the appendix."""

    def counted(*statuses):
        return sum(i.get("count", 1) for i in gaps if i.get("status") in statuses)

    uncleaned = sum(1 for r in rows if (r.meta or {}).get("clean_text_state") not in (None, "processed"))
    key_gaps = [
        (counted("not_searched"), "个待查问题没有检索"),
        (counted("not_found", "partial"), "个待查问题检索后仍未查清"),
        (counted("failed"), "次检索或读取失败"),
        (sum(1 for r in rows if not r.verified), "份材料未经核实"),
        (uncleaned, "份材料的正文是未清洗的原始文本"),
    ]
    items: list[SectionItem] = [
        {"text": f"截止时间：{inp.get('cutoff') or '当前'}"},
        *([{"text": f"回放模式：{inp['replay_mode']}"}] if inp.get("replay_mode") else []),
        {"text": f"读取材料 {len(rows)} 份，逐份清单见附录", "count": len(rows)},
        *({"text": f"{n} {what}", "count": n} for n, what in key_gaps if n),
    ]
    appendix: list[VisibleRecord] = [
        {
            "ref": r.ref,
            "source": r.source,
            "record_id": r.record_id,
            "version_id": r.version_id,
            "published_at": r.published_at,
            "available_at": r.available_at,
            "material_kind": r.material_kind,
            "verified": r.verified,
            "times": record_times(r),
        }
        for r in rows
    ]
    return {"kind": "visibility", "status": "ok", "items": items, "appendix": appendix}
