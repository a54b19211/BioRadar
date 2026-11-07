"""The series kernel (series.py): periods and numbers read strictly; what a new value changes in its
series."""

from datetime import UTC, datetime

from bioradar.research import series


def test_periods_and_numbers_are_read_strictly():
    at = datetime(2026, 9, 3, tzinfo=UTC)
    assert series.period_bounds("2026Q2", at) == (
        datetime(2026, 4, 1, tzinfo=UTC),
        datetime(2026, 6, 30, tzinfo=UTC),
    )
    assert series.period_bounds("2026h1", at)[1] == datetime(2026, 6, 30, tzinfo=UTC)
    assert series.period_bounds("2026-02", at)[1] == datetime(2026, 2, 28, tzinfo=UTC)
    assert series.period_bounds("", at) == (at, at)  # a stock "as of" the record
    assert series.period_bounds("去年下半年", at) is None
    periods = ("2026", "2026Q4", "2026H1", "2026-10", "2026-10-05/2026-10-11", "")
    assert [series.grain(p) for p in periods] == ["Y", "Q", "H", "M", "R", "A"]
    assert not series.quoted(1, "2026Q1有12条")  # the 1 of Q1 is not a value


def row(id_, period, value, status, available, superseded=None):
    start, end = series.period_bounds(period, None)
    return {
        "id": id_,
        "value": value,
        "status": status,
        "period": period,
        "period_start": start,
        "period_end": end,
        "available_at": datetime(2026, 1, available, tzinfo=UTC),
        "superseded_by": superseded,
    }


def kinds(found):
    return sorted(c["kind"] for c in found)


def test_a_restated_plan_is_not_growth_and_stages_are_not_compared_as_values():
    rows = [row(1, "2026Q1", 2, "planned", 1), row(2, "2026Q2", 2, "planned", 2)]
    assert kinds(series.changes(rows, 2)) == []  # the same plan restated
    rows.append(row(3, "2026Q3", 2, "under_construction", 3))
    assert kinds(series.changes(rows, 3)) == ["status_advance"]
    rows.append(row(4, "2026Q4", 3, "operational", 4))
    [advance] = series.changes(rows, 4)  # 2 being built -> 3 operating: a new stage, not "+50%"
    assert (advance["kind"], advance["from_value"], advance["to_value"]) == ("status_advance", 2, 3)
    rows.append(row(5, "2026Q4", 3, "operational", 5))  # another record repeats Q4
    assert kinds(series.changes(rows, 5)) == ["repeat"]
    rows.append(row(6, "2027Q1", 4, "operational", 6))
    assert kinds(series.changes(rows, 6)) == ["step"]
    plan_then_run = [row(1, "2026Q1", 10, "planned", 1), row(2, "2026Q2", 3, "operational", 2)]
    assert kinds(series.changes(plan_then_run, 2)) == ["status_advance"]  # not -70%


def test_a_stock_series_is_compared_not_summed_and_a_gap_breaks_the_trend():
    rows = [row(1, "2026Q1", 3, "operational", 1), row(2, "2026Q2", 4, "operational", 2)]
    rows.append(row(3, "2026Q3", 5, "operational", 3))
    found = series.changes(rows, 3)
    assert kinds(found) == ["new_high", "step", "streak_up"]
    assert [c.get("delta") for c in found if c["kind"] == "step"] == [1]  # 5 lines now, not 3+4+5
    gap = [row(1, "2026Q1", 3, "operational", 1), row(2, "2026Q3", 5, "operational", 2)]
    gap.append(row(3, "2026Q4", 6, "operational", 3))
    assert "streak_up" not in kinds(series.changes(gap, 3))  # Q2 is missing


def test_a_year_is_not_compared_with_a_quarter():
    rows = [row(1, "2026", 100, "operational", 1), row(2, "2026Q4", 25, "operational", 2)]
    assert series.changes(rows, 2) == []  # not -75%


def test_two_values_for_one_period_are_a_conflict_not_a_trend():
    rows = [row(1, "2026Q1", 1, "operational", 1), row(2, "2026Q2", 2, "operational", 2)]
    rows.append(row(3, "2026Q2", 3, "operational", 3))  # another source says 3 for the same Q2
    assert [c["kind"] for c in series.changes(rows, 3)] == ["conflict"]
    rows.append(row(4, "2026Q3", 4, "operational", 4))
    assert "streak_up" not in kinds(series.changes(rows, 4))  # Q2 is unresolved


def test_a_correction_counts_from_when_it_was_visible_and_revises_every_later_point():
    early = [row(1, "2026Q1", 1, "operational", 1), row(2, "2026Q2", 2, "operational", 2, superseded=5)]
    early += [row(3, "2026Q3", 3, "operational", 3), row(4, "2026Q4", 4, "operational", 4)]
    assert [p["value"] for p in series.points(early)] == [1, 2, 3, 4]  # as of day 4: Q2 is still 2
    corrected = early + [row(5, "2026Q2", 10, "operational", 5)]
    assert [p["value"] for p in series.points(corrected)] == [1, 10, 3, 4]
    found = series.changes(corrected, 5)
    # Q3 and Q4 are evaluated again: the rise to Q4 and its new high no longer hold
    assert [c["rows"] for c in found if c["kind"] == "revised"] == [[3], [4]]
    q4 = next(c for c in found if c["kind"] == "revised" and c["rows"] == [4])
    assert "streak_up" in q4["was"] and "streak_up" not in q4["now"] and "new_high" not in q4["now"]
    assert series.notable(found)
