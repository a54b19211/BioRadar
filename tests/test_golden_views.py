"""Golden characterization of what leaves the API about an analysis (detail, list, export, topics), compared
byte for byte with tests/golden/views/:

- every scenario of test_access.py and test_llm_and_api.py again, with each output of app.redact /
  redact_summaries / redact_topics recorded as it is returned;
- every scenario of test_pipeline.py, then each analysis it left viewed three ways: every source readable,
  the source of its first evidence revoked, and Fabric's source list unreadable.

Locks the external views for the move to an allow-list view (new fields hidden by default). A deliberate
change regenerates the fixtures with BIORADAR_GOLDEN_UPDATE=1 and the diff is reviewed in that commit."""

import inspect
import os
import pathlib

import pytest
import test_access
import test_llm_and_api
import test_pipeline
from sqlalchemy import select
from test_access import fabric_sources  # noqa: F401 - the fixture its scenarios take
from test_golden_pipeline import normalized

from bioradar import app, db
from bioradar.research import analyses

FIXTURES = pathlib.Path(__file__).parent / "golden" / "views"
API = [(m, n) for m in (test_access, test_llm_and_api) for n in sorted(vars(m))]
API = [(m.__name__, n) for m, n in API if n.startswith("test_") and callable(getattr(m, n))]
PIPELINE = sorted(n for n, f in vars(test_pipeline).items() if n.startswith("test_") and callable(f))
MODULES = {"test_access": test_access, "test_llm_and_api": test_llm_and_api}


def call(scenario, request):
    scenario(**{p: request.getfixturevalue(p) for p in inspect.signature(scenario).parameters})


def compare(name, text):
    path = FIXTURES / f"{name}.json"
    if os.environ.get("BIORADAR_GOLDEN_UPDATE") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
    assert path.exists(), f"no golden fixture for {name}: run with BIORADAR_GOLDEN_UPDATE=1"
    assert path.read_bytes().decode() == text


@pytest.mark.parametrize("module,name", API)
def test_api_scenarios_show_exactly_what_the_golden_fixture_holds(module, name, request, monkeypatch):
    outputs = []
    for fn in ("redact", "redact_summaries", "redact_topics"):
        original = getattr(app, fn)

        def recorded(*args, _fn=fn, _original=original, **kwargs):
            out = _original(*args, **kwargs)
            outputs.append({"exit": _fn, "value": out})
            return out

        monkeypatch.setattr(app, fn, recorded)
    call(getattr(MODULES[module], name), request)
    compare(f"{module}.{name}", normalized({"scenario": name, "outputs": outputs}))


def views(readable):
    """Every analysis's detail, list entry and export as the API gives them with `readable` sources."""
    out = []
    with db.session() as s:
        rows = list(s.scalars(select(db.Analysis).order_by(db.Analysis.created_at, db.Analysis.id)))
    for row in rows:
        out.append(
            {
                "detail": app.redact(analyses.view(row, full=True)),
                "summary": app.redact_summaries([analyses.view(row)]),
                "export": app.export(row.id),
            }
        )
    return out


@pytest.mark.parametrize("name", PIPELINE)
def test_pipeline_results_are_shown_exactly_as_the_golden_fixture_holds(name, request, monkeypatch):
    call(getattr(test_pipeline, name), request)
    with db.session() as s:
        sources = sorted(set(s.scalars(select(db.Evidence.source))))
        first = s.scalar(select(db.Evidence.source).order_by(db.Evidence.id))
    shown = {}
    for case, readable in (
        ("readable", set(sources)),
        ("first_source_revoked", set(sources) - {first}),
        ("source_list_unreadable", None),
    ):
        monkeypatch.setattr(app, "readable_sources", lambda readable=readable: readable)
        shown[case] = views(readable)
    compare(f"test_pipeline.{name}", normalized({"scenario": name, "views": shown}))
