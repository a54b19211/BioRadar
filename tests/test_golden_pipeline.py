"""Golden characterization of the analysis pipeline: every scenario of test_pipeline.py is run again while
each Run.save() is recorded - the analysis row's stage and progress JSON and its evidence, material_reads and
llm_calls rows as stored after that save - and the whole record is compared byte for byte with the fixture in
tests/golden/pipeline/. It locks what is persisted (the format a resumed run reads back), not how the code is
arranged, so moving code must leave every fixture unchanged. A deliberate behaviour change regenerates the
fixtures with BIORADAR_GOLDEN_UPDATE=1 and the diff is reviewed in the same commit.

The planner's decision records (progress.decisions) are the one exception: every frame would hold all the
records so far, so a frame keeps only the last record's step (the number of records) and request hash.
The records themselves are tested in test_planner_record.py."""

import inspect
import json
import os
import pathlib
import re
from datetime import UTC, datetime, timedelta

import pytest
import test_pipeline
from sqlalchemy import select

from bioradar import db
from bioradar.research.pipeline import Run

FIXTURES = pathlib.Path(__file__).parent / "golden" / "pipeline"
SCENARIOS = sorted(n for n, f in vars(test_pipeline).items() if n.startswith("test_") and callable(f))
TIMES = {"created_at", "completed_at", "lease_until", "lease_token"}  # wall-clock columns, per-execution ids
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?")


def columns(row):
    return {c.key: getattr(row, c.key) for c in row.__mapper__.column_attrs if c.key not in TIMES}


def stored(session, model, analysis_id):
    rows = session.scalars(select(model).where(model.analysis_id == analysis_id).order_by(model.id))
    return [{k: v for k, v in columns(r).items() if k not in {"id", "analysis_id"}} for r in rows]


def short(progress):
    """The progress with its decision records cut to the last one's step and request hash."""
    if not progress or not progress.get("decisions"):
        return progress
    last = progress["decisions"][-1]
    return {**progress, "decisions": [{"step": last["step"], "request_sha256": last["request_sha256"]}]}


def snapshot(analysis_id, stage):
    with db.session() as s:
        row = s.get(db.Analysis, analysis_id)
        return {
            "analysis": analysis_id,
            "saved": stage,
            "stage": row.stage,
            "progress": short(row.progress),
            "evidence": stored(s, db.Evidence, analysis_id),
            "material_reads": stored(s, db.MaterialRead, analysis_id),
            "llm_calls": stored(s, db.LlmCall, analysis_id),
        }


def final_rows():
    with db.session() as s:
        analyses = s.scalars(select(db.Analysis).order_by(db.Analysis.created_at, db.Analysis.id))
        return [
            {
                k: short(v) if k == "progress" else v
                for k, v in columns(a).items()
                if k in {"id", "stage", "state", "progress", "result"}
            }
            for a in analyses
        ]


def normalized(value):
    """JSON text with run-specific identifiers (and their 8-character prefixes) numbered in order of
    appearance and times taken from the clock (within a day of now) replaced, so the text depends on
    behaviour only."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1, default=str)
    ids = list(dict.fromkeys(UUID.findall(text)))
    for n, found in enumerate(ids, 1):
        text = text.replace(found, f"<id{n}>")
    for n, found in enumerate(ids, 1):  # an id's first 8 characters, as notices name an earlier round
        text = text.replace(found[:8], f"<id{n}:8>")
    now = datetime.now(UTC)

    def clock(m):
        try:
            at = datetime.fromisoformat(m.group().replace("Z", "+00:00"))
        except ValueError:
            return m.group()
        at = at if at.tzinfo else at.replace(tzinfo=UTC)
        return "<now>" if abs(at - now) < timedelta(days=1) else m.group()

    return ISO.sub(clock, text) + "\n"


def record(name, request, monkeypatch):
    frames = []
    original = Run.save

    def save(self, stage=None):
        original(self, stage)
        frames.append(snapshot(self.id, stage))

    monkeypatch.setattr(Run, "save", save)
    scenario = getattr(test_pipeline, name)
    scenario(**{p: request.getfixturevalue(p) for p in inspect.signature(scenario).parameters})
    return normalized({"scenario": name, "saves": frames, "final": final_rows()})


@pytest.mark.parametrize("name", SCENARIOS)
def test_pipeline_persists_exactly_what_the_golden_fixture_holds(name, request, monkeypatch):
    text = record(name, request, monkeypatch)
    path = FIXTURES / f"{name}.json"
    if os.environ.get("BIORADAR_GOLDEN_UPDATE") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
    assert path.exists(), f"no golden fixture for {name}: run with BIORADAR_GOLDEN_UPDATE=1"
    assert path.read_bytes().decode() == text
