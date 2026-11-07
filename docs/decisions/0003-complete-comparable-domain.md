# 0003 Complete comparable domain

- Status: accepted (2026-11-06); since 0010 the gate reads Fabric's measurement objects (family, dims and their
  per-dimension check) instead of the radar's own BioCheck requests and answers - the rules are unchanged.
- Code: `services/bioradar/research/domain.py`, `series_sync._join`, `db.SeriesPoint.dims*`
- Design: `design/T-24-完整域-r2.md` (server, `devtools/design/`)

## Context

Series compare numbers across periods. Two values of "the same metric" are often not comparable: a case count
by report date and one by onset date, a rate with another denominator, two groups the same record states for
one base metric. Comparing them produced false changes.

## Decision

- A number is compared automatically only inside a group whose metric family is determined and every
  dimension that family requires is proven: checked by the checker per dimension, or carried by a field the
  checker matched, in that field's own role. Unknown is never "not applicable".
- A point with a gap is stored and effective, kept as a lead with its gaps, and never compared.
- The group key is built from the family and the proven dimensions with fixed-length hashes, so no character
  inside a value can merge two groups; a version may state the same base metric for two groups.
- Two different values for one group in one record are both kept and neither is compared ("the first one
  wins" is never applied); an upgraded artifact's point takes the old point's place (`hand_over`).

## Consequences

Fewer automatic comparisons, each one defensible; the rest go to investigation with their gaps named.
