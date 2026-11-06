"""Producer measure.v4 / checker v9 (Codex r161): Radar locates a period literal only as the whole written
period and a unit in the bound metric phrase only as its head counting noun - extraction_normalize.v2, the
functions BioCheck v9 uses. Generic format cases."""

import extraction_contract as ec
import pytest

from bioradar.research import measure_checks as mc
from bioradar.research import schemas


def test_the_producer_and_checker_versions():
    # later producers keep this one's reading; the current versions (test_measure_v5)
    assert mc.CHECKER == ec.CHECKER_V14 == "biocheck.extraction_rules.v14"
    assert mc.PRODUCER == {"name": "bioradar.triage", "version": "measure.v8"}


def spans_of(text, **over):
    base = dict(
        object_text="Hospital B",
        metric_text="close contacts",
        value_text="23",
        unit_text="contacts",
        period_text="2018",
        period="2018",
        period_type="interval",
        kind="flow",
        qualifier="exact",
        modality="actual",
        status="reported",
        quote=text,
    )
    base.update(over)
    return mc.locate(text, 0, len(text), schemas.Measure(**base).model_dump())


@pytest.mark.parametrize("prefix", ["In 2018", "During 2018"])
def test_a_whole_year_is_located(prefix):
    text = prefix + ", Hospital B reported 23 close contacts."
    spans, why = spans_of(text)
    assert why is None and text[slice(*spans["period"])] == "2018"


@pytest.mark.parametrize("prefix", ["In January 2018", "In 2018–2019", "Since 2018", "From 2017 to 2018"])
def test_a_year_that_is_part_of_another_period_is_not_located(prefix):
    spans, why = spans_of(prefix + ", Hospital B reported 23 close contacts.")
    assert spans is None and why == "period_literal_not_the_whole_period"


def test_a_chinese_year_of_a_month_is_not_located():
    text = "2018年1月，医院B共报告新增病例23例。"
    spans, why = spans_of(
        text, object_text="医院B", metric_text="新增病例", unit_text="例", period_text="2018年"
    )
    assert spans is None and why == "period_literal_not_the_whole_period"


def test_the_head_counting_noun_is_the_unit_and_an_adjective_is_not():
    text = "In 2018, Hospital B reported 23 close contacts."
    spans, why = spans_of(text)
    assert why is None and text[slice(*spans["unit"])] == "contacts"
    spans, why = spans_of(text, unit_text="close")
    assert spans is None and why == "unit_literal_not_at_the_value"
