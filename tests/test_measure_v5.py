"""Producer measure.v5 / checker v10 (Codex r163): a unit in the bound metric phrase is located only as the
head of the phrase's own subject - a subset named after including / excluding / such as is not the unit."""

import extraction_contract as ec
import pytest

from bioradar.research import measure_checks as mc
from bioradar.research import schemas


def test_the_producer_and_checker_versions():
    assert mc.CHECKER == ec.CHECKER_V13 == "biocheck.extraction_rules.v13"
    assert mc.PRODUCER == {"name": "bioradar.triage", "version": "measure.v8"}


def spans_of(metric, unit):
    text = "In 2018, Hospital B reported 23 " + metric + "."
    m = dict(
        object_text="Hospital B",
        metric_text=metric,
        value_text="23",
        unit_text=unit,
        period_text="2018",
        period="2018",
        period_type="interval",
        kind="flow",
        qualifier="exact",
        modality="actual",
        status="reported",
        quote=text,
    )
    return text, mc.locate(text, 0, len(text), schemas.Measure(**m).model_dump())


@pytest.mark.parametrize("metric", ["close contacts excluding children", "close contacts including children"])
def test_the_subject_head_is_located_and_the_subset_is_not(metric):
    text, (spans, why) = spans_of(metric, "contacts")
    assert why is None and text[slice(*spans["unit"])] == "contacts"
    _, (spans, why) = spans_of(metric, "children")
    assert spans is None and why == "unit_literal_not_at_the_value"
