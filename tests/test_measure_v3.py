"""Producer measure.v3 / checker v8: Radar reads written numbers, whole-number tokens and the unit in the
bound metric phrase with extraction_normalize.v1 - the functions BioCheck v8 uses. Generic format cases."""

import extraction_contract as ec
import pytest

from bioradar.research import measure_checks as mc
from bioradar.research import schemas


def test_the_producer_and_checker_versions():
    # measure.v3 / v8 on its own commit (8d1412b); from measure.v4 / v9 on, normalize v2 (test_measure_v4)
    # later producers keep this one's reading; the current versions (test_measure_v5)
    assert mc.CHECKER == ec.CHECKER_V10 == "biocheck.extraction_rules.v10"
    assert mc.PRODUCER == {"name": "bioradar.triage", "version": "measure.v5"}


@pytest.mark.parametrize(
    "written,canonical",
    [
        ("7 851", "7851"),
        ("7 851", "7851"),
        ("1,444", "1444"),
        ("12 345 678", "12345678"),
        ("78 51", None),
        ("1 44 4", None),
    ],
)
def test_decimal_text_reads_grouped_thousands_and_refuses_wrong_groups(written, canonical):
    assert mc.decimal_text(written) == canonical


def measure(**over):
    base = dict(
        object_text="",
        metric_text="cases",
        value_text="7 851",
        unit_text="",
        period_text="",
        period="",
        period_type="",
        kind="cumulative",
        qualifier="exact",
        modality="actual",
        status="reported",
        quote="",
    )
    base.update(over)
    return schemas.Measure(**base).model_dump()


def spans_of(text, **over):
    m = measure(quote=text, **over)
    return mc.locate(text, 0, len(text), m)


def test_a_grouped_value_is_located_whole():
    text = "In total 7 851 cases were reported."
    spans, why = spans_of(text)
    assert why is None and text[slice(*spans["value"])] == "7 851"


def test_the_tail_of_a_grouped_number_is_never_the_value():
    spans, why = spans_of("In total 7 851 cases were reported.", value_text="851")
    assert spans is None and why == "value_literal_not_located"


def test_a_unit_inside_the_metric_phrase_bound_to_the_value_is_located():
    text = "A total of 14 close contacts were followed."
    spans, why = spans_of(text, value_text="14", unit_text="contacts", metric_text="close contacts")
    assert why is None and text[slice(*spans["unit"])] == "contacts"


@pytest.mark.parametrize(
    "text,metric",
    [
        ("A total of 14 cases and 3 contacts were followed.", "cases and 3 contacts"),
        ("A total of 14 or more contacts were followed.", "or more contacts"),
    ],
)
def test_a_unit_across_another_quantity_or_a_comparison_is_not_located(text, metric):
    spans, why = spans_of(text, value_text="14", unit_text="contacts", metric_text=metric)
    assert spans is None and why == "unit_literal_not_at_the_value"


def test_a_listed_modifier_unit_still_works_as_before():
    text = "In total 42 suspected cases were recorded."
    spans, why = spans_of(text, value_text="42", unit_text="cases", metric_text="suspected cases")
    assert why is None and text[slice(*spans["unit"])] == "cases"
