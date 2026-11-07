"""The report written by slot (task book 08 2.2, 2.3; ADR 0014). Before the writer is asked, the program lists
the questions the report must answer as slots, J1, J2 ... in the plan's order. The writer answers in one call
with the report as sections: a paragraph per slot (with the status it states), and paragraphs of evidence,
gaps and scenarios. The program splices the paragraphs into the draft BioCheck verifies, tells BioCheck which
section each paragraph is, and later compares each slot's status with the assessment's grade of the slot.

This module holds what needs no service call: the slots, reading the writer's answer, the draft and its
section spans, and the comparison of structure. Revising and re-checking is report.py's."""

import re

from bioradar.research.domain import norm
from bioradar.research.state import RunState, WrittenItem

WRITER_KINDS = ("judgment", "evidence", "gaps", "scenarios")  # the sections the writer fills
STATUSES = ("supported", "refuted", "insufficient", "open", "conflict")  # a proposition's status (05)
ASSERTING = ("supported", "refuted")  # the paragraph states the conclusion; the others say it is unsettled
CANNOT_JUDGE = "cannot_judge"  # a slot the writer wrote no paragraph for


def slots_of(p: RunState) -> list[dict]:
    """The questions the report must answer, one slot each (J1 ...): the plan's judgment questions in order.
    A slot is a program number; the writer and assess refer to it and never invent one. The proposition
    number of a slot is taken when the result is stored (research/propositions.py), after both answered."""
    return [{"slot": f"J{n}", "question": q} for n, q in enumerate(p.get("judgments", []), 1)]


def bind(judgments: list[dict], slots: list[dict]) -> None:
    """Tie each assessed judgment to its slot, in place: by the slot it names, else by the question it
    repeats. Its question becomes the slot's - the program's wording, which the proposition is keyed by, not
    the model's. A slot answered twice keeps the first answer; a judgment that fits no slot is left unbound
    (slot "")."""
    by_slot = {s["slot"]: s for s in slots}
    by_question = {norm(s["question"]): s for s in slots}
    taken: set[str] = set()
    for j in judgments:
        slot = by_slot.get(j.get("slot")) or by_question.get(norm(j["question"]))
        if slot and slot["slot"] not in taken:
            taken.add(slot["slot"])
            j["slot"], j["question"] = slot["slot"], slot["question"]
        else:
            j["slot"] = ""


def read_written(
    answer: dict, slots: list[dict], hypotheses: list[str]
) -> tuple[list[WrittenItem], list[str]]:
    """(the writer's items, why each dropped one was dropped). One item is one paragraph of a section the
    writer fills; a judgment item may name a slot (or a hypothesis) and then states a status. An item the
    program cannot place - another kind, no text, a slot nobody gave, a slot answered twice, a slot's
    paragraph without a valid status - is dropped and said, never fitted."""
    slot_ids = {s["slot"] for s in slots}
    known, items, dropped = slot_ids | set(hypotheses), [], []
    for raw in answer.get("sections") or []:
        kind = raw.get("kind")
        slot = (raw.get("slot") or "") if kind == "judgment" else ""
        text, status = " ".join(str(raw.get("text") or "").split()), raw.get("status")
        why = None
        if kind not in WRITER_KINDS:
            why = f"{kind}: not a section the writer fills"
        elif not text:
            why = f"{kind}: no text"
        elif slot and slot not in known:
            why = f"{slot}: not a slot"
        elif slot in slot_ids and status not in STATUSES:
            why = f"{slot}: no valid status"
        elif slot and any(i.get("slot") == slot for i in items):
            why = f"{slot}: answered twice"
        if why:
            dropped.append(why)
            continue
        item: WrittenItem = {"kind": kind, "text": text}
        if slot:
            item["slot"] = slot
        if slot and status in STATUSES:
            item["status"] = status
        items.append(item)
    return items, dropped


def draft_of(items: list[WrittenItem]) -> str:
    """The draft BioCheck verifies: the items' paragraphs, a blank line apart."""
    return "\n\n".join(i["text"] for i in items)


def paragraph_spans(draft: str) -> list[tuple[int, int]]:
    """(start, end) of each paragraph of the draft: lines up to a blank line, as BioCheck cuts them."""
    return [(m.start(), m.end()) for m in re.finditer(r"\S.*?(?=\s*\n[ \t]*\n|\s*\Z)", draft, re.DOTALL)]


def spans_of(items: list[WrittenItem], draft: str) -> list[dict] | None:
    """The `sections` of a verify request: where each item's paragraph is in the draft and what section it
    belongs to, so that BioCheck knows which sentences are judgments. None when the draft no longer has one
    paragraph per item."""
    found = paragraph_spans(draft)
    if len(found) != len(items):
        return None
    return [{"kind": i["kind"], "start": s, "end": e} for i, (s, e) in zip(items, found, strict=True)]


def apply_draft(items: list[WrittenItem], draft: str) -> bool:
    """Take the paragraphs of a revised draft back into the items, in place; False (nothing changed) when
    the revision did not keep one paragraph per item."""
    found = paragraph_spans(draft)
    if len(found) != len(items):
        return False
    for item, (start, end) in zip(items, found, strict=True):
        item["text"] = draft[start:end]
    return True


def graded_status(judgment: dict) -> str:
    """The status an assessed judgment amounts to: insufficient when assess grades it so (also after the
    program capped it), else supported. Assess grades a degree of belief, not a refutation."""
    return "insufficient" if judgment["confidence"] == "insufficient" else "supported"


def mismatched(items: list[WrittenItem], judgments: list[dict]) -> list[WrittenItem]:
    """The slot paragraphs that state a conclusion where the assessment grades the same slot insufficient:
    the report would say it holds while the structure says the evidence does not carry it (task book 08
    2.3). Only the structure is compared (the slot and the status); whether the words say the same is
    BioCheck's check."""
    graded = {j["slot"]: j for j in judgments if j.get("slot")}
    return [
        i
        for i in items
        if i["kind"] == "judgment"
        and i.get("slot") in graded
        and i.get("status") in ASSERTING
        and graded_status(graded[i["slot"]]) not in ASSERTING
    ]
