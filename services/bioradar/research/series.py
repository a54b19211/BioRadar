"""Cross-period series (孙博文维护研究方法; T-24).

A measurement is one value of one metric of one object for one period, quoted from one fixed record version.
Every triaged record can leave measurements, so values that mean nothing alone (one more filling line, an
ILI ratio of 4.1%) accumulate per object and metric across quarters and years. The program, not the model,
compares them:
- only within one series: object, metric, unit, kind (stock/flow/...), scope and period grain (a year is never
  compared with a quarter);
- values only between points of the same status (a planned 10 lines is not 'down 70%' to 3 operating); a
  status advance is the same value reaching a later stage;
- repeated mentions of the same period/value/status count once; two different values for the same period and
  status are a conflict and are left out of trends until one is corrected;
- a correction replaces what it corrects from when the correction itself was visible, and only rows visible
  at the clock are used, so a replay never sees a later value."""

import re
from datetime import UTC, datetime, timedelta

VERSION = "series-v2"  # written into each series event, so a later rule change is visible in old events
# planned -> under construction -> operational; reported -> confirmed
RANK = {"planned": 0, "under_construction": 1, "operational": 2, "reported": 0, "confirmed": 1}
FAMILY = {
    "planned": "build",
    "under_construction": "build",
    "operational": "build",
    "reported": "case",
    "confirmed": "case",
}
STREAK = 2  # consecutive steps in one direction (three points) make a trend
# kinds that owe an analysis; a single step is only recorded
NOTABLE = {"status_advance", "new_high", "new_low", "streak_up", "streak_down", "conflict"}


def _day(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def grain(period: str) -> str:
    """Y, H, Q, M, D, R (an explicit range) or A (as of the record)."""
    p = (period or "").strip().upper().replace(" ", "")
    if not p:
        return "A"
    if "/" in p:
        return "R"
    if re.fullmatch(r"\d{4}Q[1-4]", p):
        return "Q"
    if re.fullmatch(r"\d{4}H[12]", p):
        return "H"
    if re.fullmatch(r"\d{4}", p):
        return "Y"
    if re.fullmatch(r"\d{4}-\d{2}", p):
        return "M"
    return "D"


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


NUMBER = re.compile(r"(?<![\d.,A-Za-z])([-−]?)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?")


def quoted(value: float, quote: str) -> bool:
    """The value is one of the numbers written in the quote (3.0 is 3, 1,444 is 1444, -3 keeps its sign), not
    a digit inside 12 or 2026 and not the 1 of Q1."""
    return any(
        float(("-" if sign else "") + whole.replace(",", "") + frac) == value
        for sign, whole, frac in NUMBER.findall(quote)
    )


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value  # SQLite returns naive values


def points(rows):
    """Visible, unsuperseded rows of one series -> one point per (period, value, status), in period order;
    the earliest-available row stands for repeated mentions."""
    rows = [
        dict(
            r,
            period_start=_utc(r["period_start"]),
            period_end=_utc(r["period_end"]),
            available_at=_utc(r["available_at"]),
        )
        for r in rows
    ]
    visible = {r["id"] for r in rows}
    seen = {}
    for r in sorted(rows, key=lambda r: r["available_at"]):
        if r.get("superseded_by") in visible:
            continue  # a correction replaces the row only from when the correction itself was visible
        seen.setdefault((r["period_start"], r["period_end"], r["value"], r["status"]), r)
    return sorted(
        seen.values(), key=lambda r: (r["period_end"], RANK.get(r["status"], -1), r["available_at"])
    )


def adjacent(points_):
    """A trend needs consecutive periods: no gap, no overlap, no as-of stock."""
    return all(
        a["period"]
        and b["period"]
        and timedelta(0) < b["period_start"] - a["period_end"] <= timedelta(days=1)
        for a, b in zip(points_, points_[1:])
    )


def _key(p):
    return p["period_start"], p["period_end"], p["status"]


class _Index:
    """Per series, computed once: where each period/status first holds two different values (a conflict
    from that point on), and which points have each status. Keeps evaluate() linear in the series."""

    def __init__(self, series):
        self.series, self.by_key, self.by_status, self.conflict_from = series, {}, {}, {}
        values = {}
        for i, p in enumerate(series):
            key = _key(p)
            self.by_key.setdefault(key, []).append(i)
            self.by_status.setdefault(p["status"], []).append(i)
            values.setdefault(key, set()).add(p["value"])
            if len(values[key]) == 2 and key not in self.conflict_from:
                self.conflict_from[key] = i

    def conflicted(self, j, i):
        """Whether point j's period is in conflict among the points up to i."""
        return self.conflict_from.get(_key(self.series[j]), len(self.series)) <= i


def evaluate(series, i, index=None):
    """What point i of an ordered series shows against the points before it."""
    index = index or _Index(series)
    cur = series[i]
    if index.conflicted(i, i):
        return [{"kind": "conflict", "rows": [series[j]["id"] for j in index.by_key[_key(cur)] if j <= i]}]
    out = []
    family = FAMILY.get(cur["status"])
    first_at_stage = index.by_status[cur["status"]][0] == i
    if family and first_at_stage:
        prev = next(
            (
                series[j]
                for j in range(i - 1, -1, -1)
                if FAMILY.get(series[j]["status"]) == family
                and RANK[series[j]["status"]] < RANK[cur["status"]]
                and not index.conflicted(j, i)
            ),
            None,
        )
        if prev is not None:
            # the first point at a later stage: reported with both values, never as a rise or fall
            out.append(
                {
                    "kind": "status_advance",
                    "from": prev["status"],
                    "to": cur["status"],
                    "from_value": prev["value"],
                    "to_value": cur["value"],
                    "rows": [prev["id"], cur["id"]],
                }
            )
    # values are compared only between points of the same status, leaving out conflicting periods
    same = [series[j] for j in index.by_status[cur["status"]] if j <= i and not index.conflicted(j, i)]
    if len(same) < 2:
        return out
    prev = same[-2]
    if cur["value"] != prev["value"]:
        delta = cur["value"] - prev["value"]
        step = {"kind": "step", "delta": delta, "rows": [prev["id"], cur["id"]]}
        if prev["value"]:
            step["ratio"] = round(delta / abs(prev["value"]), 4)
        out.append(step)
    before = [p["value"] for p in same[:-1]]
    if len(before) >= 2 and cur["value"] > max(before):
        out.append(
            {"kind": "new_high", "value": cur["value"], "previous_max": max(before), "rows": [cur["id"]]}
        )
    if len(before) >= 2 and cur["value"] < min(before):
        out.append(
            {"kind": "new_low", "value": cur["value"], "previous_min": min(before), "rows": [cur["id"]]}
        )
    tail = same[-(STREAK + 1) :]
    if len(tail) == STREAK + 1 and adjacent(tail):
        steps = [b["value"] - a["value"] for a, b in zip(tail, tail[1:])]
        if all(d > 0 for d in steps):
            out.append({"kind": "streak_up", "rows": [p["id"] for p in tail]})
        elif all(d < 0 for d in steps):
            out.append({"kind": "streak_down", "rows": [p["id"] for p in tail]})
    return out


def _summary(found):
    return sorted((c["kind"], c.get("delta")) for c in found)


def changes(rows, new_id):
    """What the row `new_id` changes in its series (rows: the visible series including it, one grain).
    A value that arrives late or corrects an earlier one can change what every later point showed, so
    each later point is evaluated again and a `revised` entry says what it showed before and now.
    Each change names the rows it compares, so the topic event carries its provenance."""
    new = next((r for r in rows if r["id"] == new_id), None)
    if new is not None:  # a year is never compared with a quarter, whoever passes the rows in
        rows = [r for r in rows if grain(r["period"]) == grain(new["period"])]
    series = points(rows)
    at = next((i for i, r in enumerate(series) if r["id"] == new_id), None)
    if at is None:
        return [{"kind": "repeat", "rows": [new_id]}]  # same period/value/status already known
    index = _Index(series)
    out = evaluate(series, at, index)
    later = {p["id"] for p in series[at + 1 :]}
    if not later:  # the usual case, a value appended at the end: nothing to re-evaluate
        return out
    earlier = points([r for r in rows if r["id"] != new_id])  # the series as it was without this row
    before_index = _Index(earlier)
    was = {
        p["id"]: _summary(evaluate(earlier, k, before_index))
        for k, p in enumerate(earlier)
        if p["id"] in later
    }
    for j in range(at + 1, len(series)):
        now = evaluate(series, j, index)
        if _summary(now) != was.get(series[j]["id"], []):
            before = sorted({k for k, _ in was.get(series[j]["id"], [])})
            out.append(
                {
                    "kind": "revised",
                    "rows": [series[j]["id"]],
                    "was": before,
                    "now": sorted({c["kind"] for c in now}),
                }
            )
    return out


def notable(found):
    """Whether these changes owe an analysis: a new state, extreme, trend or conflict, or a revision that
    makes or unmakes one."""
    return any(
        c["kind"] in NOTABLE or (c["kind"] == "revised" and NOTABLE & (set(c["was"]) | set(c["now"])))
        for c in found
    )
