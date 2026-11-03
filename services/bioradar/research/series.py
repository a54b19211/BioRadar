"""Cross-period series (孙博文维护研究方法; T-24).

A measurement is one value of one metric of one object for one period, quoted from one fixed record version.
Every triaged record can leave measurements, so values that mean nothing alone (one more filling line, an
ILI ratio of 4.1%) accumulate per object and metric across quarters and years. The program, not the model,
compares them: only within the same unit, repeated mentions of the same period/value/status count once,
a correction replaces the row it corrects, and only rows visible at the clock are used."""

import re
from datetime import UTC, datetime

# planned -> under construction -> operational; reported -> confirmed
RANK = {"planned": 0, "under_construction": 1, "operational": 2, "reported": 0, "confirmed": 1}
FAMILY = {"planned": "build", "under_construction": "build", "operational": "build", "reported": "case", "confirmed": "case"}
STREAK = 2  # consecutive steps in one direction (three points) make a trend
# kinds that owe the object's topic an analysis; a single step is only recorded
NOTABLE = {"status_advance", "new_high", "new_low", "streak_up", "streak_down"}


def _day(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def period_bounds(period: str, fallback: datetime):
    """(start, end) of a period written as 2026, 2026H1, 2026Q2, 2026-09, 2026-09-28 or start/end.
    An empty period means 'as of the record' (a stock reported now). None when it cannot be read."""
    p = (period or "").strip().upper().replace(" ", "")
    if not p:
        return fallback, fallback
    try:
        if "/" in p:
            start, end = p.split("/", 1)
            return _day(start), _day(end)
        if m := re.fullmatch(r"(\d{4})Q([1-4])", p):
            year, q = int(m[1]), int(m[2])
            return datetime(year, 3 * q - 2, 1, tzinfo=UTC), _month_end(year, 3 * q)
        if m := re.fullmatch(r"(\d{4})H([12])", p):
            year, h = int(m[1]), int(m[2])
            return datetime(year, 6 * h - 5, 1, tzinfo=UTC), _month_end(year, 6 * h)
        if re.fullmatch(r"\d{4}", p):
            return datetime(int(p), 1, 1, tzinfo=UTC), datetime(int(p), 12, 31, tzinfo=UTC)
        if m := re.fullmatch(r"(\d{4})-(\d{2})", p):
            return datetime(int(m[1]), int(m[2]), 1, tzinfo=UTC), _month_end(int(m[1]), int(m[2]))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p):
            return _day(p), _day(p)
    except ValueError:
        return None
    return None


def _month_end(year, month):
    nxt = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=UTC)
    return datetime.fromtimestamp(nxt.timestamp() - 86400, UTC)


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value  # SQLite returns naive values


def points(rows):
    """Visible, unsuperseded rows of one series -> one point per (period, value, status), ordered by period
    end; the earliest-available row stands for repeated mentions."""
    rows = [dict(r, period_end=_utc(r["period_end"]), available_at=_utc(r["available_at"])) for r in rows]
    seen = {}
    for r in sorted(rows, key=lambda r: r["available_at"]):
        if r.get("superseded_by"):
            continue
        seen.setdefault((r["period_end"], r["value"], r["status"]), r)
    return sorted(seen.values(), key=lambda r: (r["period_end"], RANK.get(r["status"], -1), r["available_at"]))


def changes(rows, new_id):
    """What the row `new_id` changes in its series (rows: the whole visible series including it).
    Each change names its kind and the rows it compares, so the topic event carries its provenance."""
    series = points(rows)
    at = next((i for i, r in enumerate(series) if r["id"] == new_id), None)
    if at is None:
        return [{"kind": "repeat", "rows": [new_id]}]  # same period/value/status already known
    out = []
    for i in {at, at + 1} & set(range(1, len(series))):  # the new point, and its successor if it came late
        cur, prev = series[i], series[i - 1]
        late = i != at
        same_family = FAMILY.get(cur["status"]) and FAMILY.get(cur["status"]) == FAMILY.get(prev["status"])
        if cur["value"] == prev["value"] and same_family and RANK[cur["status"]] > RANK[prev["status"]]:
            out.append({"kind": "status_advance", "from": prev["status"], "to": cur["status"], "rows": [prev["id"], cur["id"]], "late": late})
        elif cur["value"] != prev["value"]:
            delta = cur["value"] - prev["value"]
            step = {"kind": "step", "delta": delta, "rows": [prev["id"], cur["id"]], "late": late}
            if prev["value"]:
                step["ratio"] = round(delta / abs(prev["value"]), 4)
            out.append(step)
        before = [r["value"] for r in series[:i]]
        if len(before) >= 2 and cur["value"] > max(before):
            out.append({"kind": "new_high", "value": cur["value"], "previous_max": max(before), "rows": [cur["id"]], "late": late})
        if len(before) >= 2 and cur["value"] < min(before):
            out.append({"kind": "new_low", "value": cur["value"], "previous_min": min(before), "rows": [cur["id"]], "late": late})
        tail = series[max(0, i - STREAK) : i + 1]
        if len(tail) == STREAK + 1:
            steps = [b["value"] - a["value"] for a, b in zip(tail, tail[1:])]
            if all(d > 0 for d in steps):
                out.append({"kind": "streak_up", "rows": [r["id"] for r in tail], "late": late})
            elif all(d < 0 for d in steps):
                out.append({"kind": "streak_down", "rows": [r["id"] for r in tail], "late": late})
    return out
