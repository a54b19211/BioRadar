# 0012 Candidate groups and proposition numbers

- Status: accepted for the first step (2026-11-08); new-version handling and revision notes are ADR 0015; the
  case-count rule and the evidence graph are later steps
- Design: task book 05 sections 2.2, 2.3, 2.5 and phase 0 steps 1 and 2; 00 sections 3.3 and 3.8
- Code: `research/groups.py`, `research/propositions.py`, `research/topics.py` (`previous_days`, `baseline`),
  `research/pipeline.py` (`Run.display_group`), `research/analyses.py` (`execute`), `adapters/fabric.py`
  (`background_version_ids`), `config.py` (`backfill_run_ids`), `ops.py` (`select-display`), migration
  `0024_topic_prop_seq`
- Tests: `tests/test_groups.py`, `tests/test_propositions.py`, `tests_pg/test_proposition_numbers_postgres.py`

## Decision

There is no member table. A topic's analysis stores, in `analyses.result.groups`, the group its latest
material is in (the display group) as the program found it at the analysis cutoff:

- **Members** are joined by an explicit basis only: `same_record` (versions of one record), `fabric_relation`
  (reposts, supplements, corrects, retracts), `event_id`, `text_reference`. Each member carries its basis, the
  member it is joined through and `confirmed_at`, the later of the two ends' availability. The group's first
  material needs no basis; its id is a hash of that material's source and record.
- **Candidates** are materials of other groups that only a key puts near this one: pathogen and region both
  known and equal, seen within 30 days; or a place without a pathogen and the located sentence (at most 5
  groups). They are listed apart, are not counted and do not move the group's start or end. A material in more
  than one group is listed with all of them in `shared`; the groups it names are summarized in `others` with
  the ordering fields (deduplicated confirmed groups, earliest first seen, id).
- **Background**: versions listed in the `coverage.backfill.background_version_ids` of the Fabric runs named by
  `backfill_run_ids` stay members and candidates' targets but are out of the counts, the baseline and the
  first-seen time. A receipt that cannot be read is listed in `background_unread`. The monitor leaves the same
  versions out of its topics: a round records each as an observation with the program reason
  `background_backfill` (`triage.settle_background`) - not triaged, no topic - so it cannot open a topic or
  warm, alert or wake one, nor serve as a topic's first sighting. `topics.activity` and `topics.advance` also
  leave them out of their counts, and `activity` out of the days the monitor received anything on. While a
  receipt cannot be read the round cannot tell which versions that run created, so it records the new versions
  of the backfill source (`triage.BACKFILL_SOURCES`, WHO DON) as `triage_deferred`: not triaged, in no topic.
  The next round that reads every receipt settles them as above.
- **Counts** are per day: deduplicated groups (`activity_key`), how many are proven independent (none yet:
  Fabric states `same_origin` or nothing), how many are not known to be. The baseline is the rule the topic
  monitor uses (`topics.previous_days`, `topics.baseline`), not a new one.

The group the first judgment (t1) is about is not the display group of a topic, which follows that topic's
latest material. `groups.select_display` (`python -m bioradar.ops select-display --cutoff T1`, read only) groups
everything visible at t1 and picks one by three keys, each looked at only when those before it tie, none of them
looking at what came later (task book 00 1.4): the deduplicated groups among the confirmed members (more first;
a group with none is 0 and still takes part), the earliest first seen of the visible materials (earlier first),
the group id (smaller first). A group none of whose visible materials counts (members all background, no live
candidate) is listed without a rank; with no ranked group the outcome is `unverified`. A read that was cut
(`MAX_MATERIALS`) is listed as a coverage gap, never as no material. A backfill receipt that could not be read is
listed too and makes the outcome `unverified` (`reason: backfill_unread`, nothing chosen, the ranking kept as a
diagnosis): the versions of that run would be counted as news. `chosen_topics` lists the topics whose latest
visible non-background material is in the chosen group - the topics whose display group it is - and each member
row of the export lists its `topic_ids`, both from the same visible materials; the first judgment is submitted
under such a topic with cutoff t1 and replay mode `operational_replay`. The group a topic's analysis stores
(`view`) has no `topic_ids`. `submit` says what the first judgment is created with (`analyses.create`): the first
of `chosen_topics` as `topic_id`, else - a group no topic holds, as a group of leads is - the group's newest records
(at most 8) as `seed_records`, each pinned to its newest visible version; `lead_group` marks a chosen group of
leads alone (0 confirmed).
The output is the selection list (ranking, outcome, coverage) and every group with its members and candidates;
keeping it with a hash is the D1 script's job.

A completed topic analysis numbers its judgments in the transaction that stores the result: it locks the topic
row, takes `topics.next_prop_seq`, and keeps an earlier `prop_id` (looked up through the parent chain) only when
type, scope (the display group) and `question_key` (topic and normalized wording) are all equal. A new
conclusion under the same id raises `content_rev`; a changed scope starts a new id with `derived_from`.
`depends_on` points at the `material_reads` rows of the judgment's grounds and, when BioCheck checked the
conclusion, at its `llm_calls` row.

## Why

A member table would need a second place to keep what the analysis result already says at a moment, and
nothing queries members across versions yet. Rows that exist (observations, series points, the evidence read)
carry what the rules need, so the view is computed and the moment it was used is the stored list: a replay never
writes a later link back, and the list stays what the analysis saw.

## Consequences

- Pathogen and region come from proven dimensions of Fabric measurements (`disease`, `coverage`); a material
  without a measurement has none and stays a lead of unknown cause. `event_id` and `text_reference` are accepted
  when a material carries them; today nothing produces them (no `DomainEvent` producer; no text scan).
- Relations other than reposts come from the evidence analyses recorded before the cutoff, so a version no
  analysis has read has none.
- The counters are in `topics`; an analysis without a topic gets no numbers. Downgrading the migration is
  refused once a topic has handed one out.
- `topics.activity` (the monitor's per-topic trigger) still counts backfilled observations as before.
