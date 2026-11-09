# 0016 News-index leads in the candidate-group view

- Status: accepted for the first step (2026-11-08); extends ADR 0012
- Design: task book 05 sections 2.1, 2.2, 2.8 and phase 0 step 1; 01 section 2.3; 00 section 1.4
- Code: `adapters/fabric.py` (`LEAD_SOURCES`, `Fabric.lead_coverage`), `research/triage.py` (`route`,
  `_record_batch`), `research/monitor.py`, `research/groups.py` (`_counted`, `load`, `leads`, `day_window`),
  `research/topics.py` (`activity`), `research/pipeline.py` (`Run.display_group`), `ops.py` (`select-display`), `research/views.py`,
  `research/run/seeds.py`
- Tests: `tests/test_gdelt_leads.py`, `tests/test_seed_triage.py`

## Decision

A record of a news index (`LEAD_SOURCES`: GDELT) is a lead, not a statement of its source.

- **Triaged for relevance alone.** The monitor reads the source's change feed like any other and the records go
  to the same triage as the rest (the first version of this step settled them by rule with no model call, and a
  headline about a blockade then led the first judgment of D1 into a plan with nothing to plan). `_record_batch`
  keeps of the model's answer only what a lead may carry: whether it is medical or public-health intelligence
  (`observations.relevance`: related | unrelated | uncertain, with the direction and the reason). A lead joins no
  topic, raises no alert and stays unverified whatever the model says. A version a backfill run created is still
  settled first, as `background_backfill`.
- **In the groups, confirming nothing alone.** A lead is a material like any other (`groups.load` reads the
  leads apart from the rest, the newest `MAX_LEADS`, so thousands of them never push the news out of
  `MAX_MATERIALS`). But `groups._counted` counts the members of a group only when it holds a material of another
  source: a group of leads alone - a lead by itself, or versions of one record - has 0 confirmed members, no
  start and no end, and still takes part in the ranking of `select_display` (task book 00 1.4: Codex's ruling on
  t1), after every group that has a confirmed member. Its first-seen time is the lead's. A lead joined to a
  material of another source by an explicit basis counts with that group. Nothing states such a basis for a GDELT
  record today, and a key needs a measurement a lead does not have, so a lead is no candidate either.
  `topics.activity` and the groups' baseline leave leads out of the days the monitor received news on, as they do
  background versions, so adding the source does not turn every quiet day into a received one.
- **Listed apart.** `groups.leads` gives, for each lead source the monitor reads (it has a cursor), the leads
  received on the cutoff's UTC day up to the cutoff: the count, the number that are background, and the newest 20
  as references to fixed versions (`version_id`, `source`, `record_id`, `available_at`). Title, domain, address
  and GDELT's own observation time stay in Fabric and are read back by the version. `available_at` is when the
  radar could first read it: not the article's date, and not when GDELT observed it (three different times).
- **Gaps registered.** `Run.display_group` asks Fabric for the receipts of that day's windows
  (`Fabric.lead_coverage`: `GET /api/history/records` with `window_start`/`window_end`, cut by GDELT's observation
  time) and keeps, beside the leads, the number of receipts and the gaps: a window still full at the finest size
  (`possibly_truncated`), one that `failed` or was `not_read`. No receipt, or Fabric not answering, is recorded as
  `receipts: 0` or `error`: unknown, never an empty window. The halving of windows and the truncation marks are
  Fabric's (`collection/windows.py`, `biz_rules/gdelt_news.py`); the radar only reads them. The t1 selection
  (`ops select-display`) registers the same in its `coverage.leads`, read in operational replay, the mode the
  first judgment is submitted in; a gap there changes no outcome.

- **Investigated when chosen.** No topic holds a lead, so the t1 selection of a group of leads alone has no
  `chosen_topics`. Its `submit` is then `seed_records`: the lead's fixed versions, which `analyses.create` keeps in
  the task input. Before the plan the run reads them (`seeds.read_all`), takes the stored triage verdict of each
  or asks the same triage once (`seeds.judge`), and gives the plan what they say; `plan.first_candidates` pins the
  ones not judged unrelated as critical reading candidates (read at the cutoff by version, offered to the planner
  like any seed). When every seed is unrelated, or the plan can ask nothing of what they say, the run is
  completed with that as its stop (`unrelated`, `insufficient`) and no report. Nothing else changes: no topic is
  opened, no alert or confirmed member is made, the group stays at 0 confirmed. What the investigation reads of a lead is its title, and `document` marks a
  record of a lead source as not verified (`verified: false`), so the reader, the report's appendix and BioCheck
  take it as an unverified report; any body evidence is still checked as usual. The planner's `gdelt_leads`
  channel stays `not_supported`: no lead list is searched, only the seeded versions are read.

## Why

The view is computed from rows that already exist (ADR 0012), so a lead must be an observation to be in it. That a
lead was received is a program fact; whether it is about medicine or public health at all is a matter of meaning,
which only the triage model judges, once, where every record is judged. The view and the selection read that
stored answer and never ask a model (task book 05): what was judged unrelated is no material, what was unsure
stays marked, what no model judged (its call failed) stays as it was. A topic opened on a headline would be a
topic of an unverified lead, so a lead joins none. Registering the gaps in the analysis result, where the
display group is stored, adds no table.

## Consequences

- The `gdelt_leads` channel the radar declares to query-planner (`research/run/decisions.py`) is still
  `not_supported`: reading the leads of a group back as pinned reading candidates is task book 04, not this step.
- Every lead is a group of its own, so the selection lists as many groups as there are leads (up to
  `MAX_LEADS`): the ranking and the export of `select_display` grow with the news index. Reaching `MAX_LEADS`
  sets `truncated`, a coverage gap, like `MAX_MATERIALS`. If the export is too large to read, the leads can be
  summarized there without changing the ranking.
- The coverage is read for the cutoff's day only; earlier days of the 30-day view are not covered.
- Every lead now costs its share of a triage call (a batch of 10). The leads come in thousands, so they spend the
  daily automatic budget (`daily_auto_tokens`) like any triage and are owed (`triage_deferred`) when it is used up.
  Observations made before this change keep `relevance` null: judged by no one, kept in the groups as they were;
  an analysis seeded with one asks the triage once and stores the answer (`seeds.remember`), and
  `python -m bioradar.ops retriage [--source S] [--since T] [--until T] [--limit N]` (`triage.retriage`) judges
  them in bulk before the first selection: the same triage call, the answer written to the observation's
  relevance and direction only (not placed in a topic again). Its calls count in the daily automatic budget like
  any triage, but a used-up budget does not hold it back: it is run by hand, to diagnose what is already there.
