# 0010 Measurements are Fabric's: the radar consumes its objects and keeps only the topic's selection

- Status: accepted (2026-11-07). Radar side of Fabric's `docs/decisions/0006-measurements.md`; design
  `devtools/design/r2-measurement-migration.md` §4 and its revision `-r2.md` §2, §5.
- Supersedes for new data: 0002 (the radar's producer and checker versions) and the extraction part of 0003.

## Decision

- **Fabric owns every objective measurement**: extraction (its `datafabric.measure_extract.v1`, the radar's
  measure.v10 rules moved without change), BioCheck checking (v3 contract; the checker version is stored on every object), coverage
  and owed windows, corrections and retractions, processor and checker upgrades. The radar no longer extracts, checks,
  re-reads or upgrades anything: triage only classifies (topic, alert, signal, objects, watch, closure); its
  prompt asks for no measures and is shown no "known series" names, so no workspace steers the shared
  extraction. Everything triage still returns is the workspace's own state (v3.1 6.1, Observation): which
  topic, how urgent, what kind of signal, which watched signal it meets - no source-level claim is extracted
  by the radar any more. `measure_checks`, `continuation`, `upkeep`, `series_store` and the upkeep commands
  are deleted.
- **The radar keeps the investigation's own selection**: `series_points` (which topic put which Fabric object
  into which of its series), the comparable-domain gate (`domain.series_gate`, the 0003 rules, now reading the
  object's family, dimensions and their per-dimension check), the series kernel (`series.py`), the series,
  correction and record-contradiction events, and the analyses they owe.
- **`series_sync`** (one step of `monitor.tick`, no model call, so never paused by the token budget):
  - the change feed `GET /api/history/measurements/changes` is stored page by page in `measurement_inbox`; the
    page and the cursor (`monitor_cursors`, `fabric:measurements`) commit in one transaction - a round that dies
    inside a page reads it again and adds nothing; a page that is not the contract moves no cursor;
  - when triage places a version in a topic, its objects are pulled at once
    (`GET /api/history/measurements?version_ids=`); an inbox row is linked as soon as its version has a topic,
    so "measurement before topic" and "topic before measurement" end with the same links, each once
    (unique `(workspace, kind, topic, object)`, inserted under a savepoint: a concurrent writer's link stands);
  - events Fabric withholds (their source, or their cause's source, is not readable now) are counted per source
    (`measurement_withheld`). Once a pull of that source is answered again, the versions of it placed in topics
    are pulled again page by page, and then every linked object is read again (a withheld correction from that
    source may concern another source's object). The feed itself never replays them;
  - a linked object with a later event is read again: superseded, corrected or retracted -> its point leaves
    the series (kept); a correction or retraction is a `correction` event of the topic from when it was
    visible and owes `source_corrected`; a correction withdrawn brings the point back;
  - every current object is a point, whatever its check says: `check_status` is Fabric's mark (`consistent`
    已核一致, `inconsistent` 有冲突, `unchecked` 未核; an unknown unit stays `unknown`). Only a point whose domain
    is complete and proven (`domain.series_gate`: for that the check must be consistent) is compared, summed or
    trended. Fabric publishes an object unchecked and, when the check answers, a second object of the same
    reading; the first gets `rechecked_by_checker`, its point leaves and the new object's point is the current
    one - the facts of a topic are its current objects, not the `created` events. The topic detail lists them
    (`topics.readings`) with their mark.
- **Series key** = (topic, object key, metric, unit, kind, scope, scope mode, period type and precision, family,
  dimension group). The object key is Fabric's `identity.key` only when `identity.state` is `confirmed` or
  `rule_confirmed`; otherwise it is `literal:<source>|<record>|<normalised object>` built by the radar, so a
  provisional name never merges two records (Fabric 0006 and design -r2 §5, Q7).
- **What analyses read**: a topic's automatic analysis is handed the points of its latest Fabric series event
  (`scheduling.series_basis_points`) with their state as the radar last read it; when it runs, each point's
  state is read again from Fabric for those points alone (`series_sync.read_points`: at the cutoff, with the
  permission of now; a source that cannot be read gives no current point) before it reaches the planner.
- **The radar's own measurement tables are frozen**, kept with their data: `measurements`,
  `measurement_candidates`, `measurement_check_tasks`, `measurement_checks`, `record_reads`,
  `record_read_windows`, `record_read_attempts` and also `record_corrections` (its writer is gone too).
  PostgreSQL refuses every insert, update, delete and truncate on them (migration 0023, trigger
  `bioradar_frozen_history`); the session refuses ORM writes before they are sent (`db.FrozenHistory`).
  `research/measure_history.py` reads them: an analysis or a topic event that names one of their points still
  shows it as recorded (authority `radar_history`), never as current, and nothing is computed from it. A point
  carries the latest check recorded for its candidate under the `checker_version` stored on that check (the
  last checker the radar's own chain wrote is v16, so that is what its rows say); nothing maps it to another
  version. `/api/measure-coverage` still shows the recorded reading plans, each with the checker versions its
  version's recorded checks stand under.

## Behaviour changes

- A record's measures are no longer extracted by triage or continuation reading; series points come only from
  Fabric objects (today: who_don, Fabric's slice S1).
- Points of the old chain are never current any more (they were current while the radar re-checked them).
- Names are no longer aligned by showing triage the workspace's known series; series group on confirmed
  identities or within one record.
- Fabric's objects state no stage (planned / operating / reported / confirmed): every point is `other`, so the
  kernel's stage advance never fires on them.

## Not done (follow-ups)

- **Old data into Fabric** (design §5): importing the radar's checked candidates as Fabric readings
  (`bioradar.import.unverified` where the producer version is not proven), `measurement_fabric_map`, and the
  equivalence proof on copies. Until then old points stay history in the frozen tables.
- **planner `measurement_ref` / `check_ref`**: the planner contract forbids extra fields (`extra="forbid"`,
  `clean-planner/planner/models.py`), so a Fabric point reaches it without them (`check_id` null); adding them
  is a planner change first.
- The analysis cut-over to calculations that read Fabric `measurement_id`s directly (7.7.6) and the
  "measurement" source for the planner's analysis candidates.
- Fabric's checker-enablement history: in `operational_replay` an object of a checker other than the configured
  one is `checker_history_unknown` and never current (Fabric 0006).
