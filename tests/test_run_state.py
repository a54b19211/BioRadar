"""RunState declares every key a run stores, and progress written before the newer fields existed resumes."""

import json
import pathlib
import types
import typing

from conftest import FakeFabric
from test_pipeline import RECORDS, fake_model, first_policy

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.config import settings
from bioradar.research import analyses, state
from bioradar.research.pipeline import Run

GOLDEN = pathlib.Path(__file__).parent / "golden" / "pipeline"


def undeclared(value, tp, path="progress"):
    """Paths in `value` that `tp` does not declare (TypedDict keys, recursively; Any ends the walk)."""
    if tp is typing.Any or value is None:
        return []
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin is typing.Annotated:
        return undeclared(value, args[0], path)
    if origin in (typing.Union, types.UnionType):
        options = [undeclared(value, t, path) for t in args if t is not type(None)]
        return min(options, key=len)
    if origin is typing.Literal:
        return [] if value in args else [f"{path}={value!r}"]
    if typing.is_typeddict(tp):
        hints = typing.get_type_hints(tp, include_extras=True)
        if not isinstance(value, dict):
            return [f"{path}: not an object"]
        out = []
        for key, item in value.items():
            out += undeclared(item, hints[key], f"{path}.{key}") if key in hints else [f"{path}.{key}"]
        return out
    if origin is list:
        return [p for i, item in enumerate(value) for p in undeclared(item, args[0], f"{path}[]")]
    if origin is dict:
        return [p for item in value.values() for p in undeclared(item, args[1], f"{path}{{}}")]
    return []


def test_every_key_a_run_stores_is_declared_in_run_state():
    missing = set()
    for fixture in sorted(GOLDEN.glob("*.json")):
        recorded = json.loads(fixture.read_text(encoding="utf-8"))
        progresses = [f["progress"] for f in recorded["saves"]] + [a["progress"] for a in recorded["final"]]
        for progress in progresses:
            missing.update(undeclared(progress, state.RunState))
    assert not missing, sorted(missing)


def test_an_unknown_key_is_reported():
    assert undeclared({"checks": [{"id": "Q1", "summary": "x"}], "draft_notes": []}, state.RunState) == [
        "progress.checks[].summary",
        "progress.draft_notes",
    ]


# Saved before used_previous, plan_basis, objects, found_objects, hit positions, hit sketches, fact
# hypothesis stances, check objects, relation targets and evidence bindings existed; paused after investigate.
OLD = {
    "checks": [
        {
            "id": "Q1",
            "text": "当前PFS疗效数值",
            "required": True,
            "importance": 1.0,
            "status": "answered",
            "evidence": ["E1", "E2"],
        }
    ],
    "queries": [{"terms": ["PFS"], "checks": ["Q1"]}],
    "judgments": ["是否为重要进展"],
    "needs_calculation": True,
    "mechanism": None,
    "actions": [
        {
            "kind": "search",
            "source": "external_archive",
            "args": {"terms": ["PFS"]},
            "target_checks": ["Q1"],
            "gain": 0.3,
            "status": "completed",
            "found": 2,
            "new": 2,
        },
        {
            "kind": "read",
            "source": "external_archive",
            "args": {"record_id": "B41"},
            "target_checks": ["Q1"],
            "gain": 1.0,
            "status": "completed",
            "ref": "E1",
        },
        {
            "kind": "read",
            "source": "external_archive",
            "args": {"record_id": "B31"},
            "target_checks": ["Q1"],
            "gain": 0.0,
            "status": "completed",
            "ref": "E2",
        },
    ],
    "hits": [
        {
            "source": "external_archive",
            "record_id": r,
            "title": t,
            "checks": ["Q1"],
            "critical": False,
            "read": True,
        }
        for r, t in (("B41", "XQ-17 试验结果更正"), ("B31", "XQ-17 随机试验初步结果"))
    ],
    "facts": [
        {
            "id": "F1",
            "ref": "E1",
            "text": "PFS风险比",
            "quote": "HR为0.69",
            "checks": ["Q1"],
            "stance": "supports",
        },
        {
            "id": "F2",
            "ref": "E2",
            "text": "PFS风险比",
            "quote": "HR为0.62",
            "checks": ["Q1"],
            "stance": "supports",
        },
    ],
    "relations": [{"from": "B41", "source": "external_archive", "to": "B31", "kind": "corrects"}],
    "pages": {"external_archive|PFS": {"pages": 1, "found": 2, "checks": ["Q1"], "cursor": None, "new": 2}},
    "refs": {"E1": "external_archive|B41", "E2": "external_archive|B31"},
    "retries": 0,
    "stop": {"decision": "complete", "reason": "test"},
    "notes": [],
}


def test_progress_saved_by_an_older_version_resumes_unchanged(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", fake_model([]))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    sent = []

    def biocheck(s, operation, body):
        sent.append((operation, body))
        if operation == "generate":
            return {"draft": "同一试验更正后PFS的HR为0.69[E1]。", "llm_calls": []}
        return {"status": "PASS", "claims": [], "coverage": {"verdict": "complete"}, "llm_calls": []}

    monkeypatch.setattr(services, "biocheck", biocheck)
    row = analyses.create(
        question="XQ-17 的PFS结果现在应怎样判断？", direction="pipeline", is_demo=True, enqueue=False
    )
    docs = {d["record_id"]: d for d in FakeFabric(RECORDS).docs}
    with db.session() as s:
        stored = s.get(db.Analysis, row.id)
        stored.progress, stored.stage = json.loads(json.dumps(OLD)), "compute"
        for ref, record_id in (("E1", "B41"), ("E2", "B31")):
            d = docs[record_id]
            s.add(
                db.Evidence(
                    analysis_id=row.id,
                    ref=ref,
                    source=d["source"],
                    record_id=record_id,
                    version_id=d["version_id"],
                    title=d["title"],
                    published_at=d["published_at"],
                    available_at=str(d["available_at"]),
                    material_kind=d["material_kind"],
                    verified=True,
                    text=d["text"],
                    meta={
                        "url": None,
                        "window": "full",
                        "text_level": "full_text",
                        "relations": [],
                        "events": [],
                    },
                )
            )
    result = Run(analyses.get(row.id), settings(), fabric=FakeFabric(RECORDS)).run()

    assert result["report_status"] == "PASS"
    assert result["computations"][0]["value"] == 31.0  # (1 - 0.69) * 100 from the old facts
    generate = sent[0][1]
    assert generate["source_updates"] == [{"target": "E2", "replacement": "E1", "kind": "correction"}]
    saved = analyses.get(row.id).progress
    assert {k: saved[k] for k in OLD} == OLD  # nothing of the old state renamed, filled in or dropped
    assert "used_previous" not in saved  # an absent field stays absent; the run does not invent it
