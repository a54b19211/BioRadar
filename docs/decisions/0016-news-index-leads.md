# 0016 News-index leads in the candidate-group view

- Status: accepted for the first step (2026-11-08); extends ADR 0012
- Design: task book 05 sections 2.1, 2.2, 2.8 and phase 0 step 1; 01 section 2.3; 00 section 1.4
- Code: `adapters/fabric.py` (`LEAD_SOURCES`, `Fabric.lead_coverage`), `research/triage.py` (`LEAD`, `route`,
  `settle_leads`), `research/monitor.py`, `research/groups.py` (`_counted`, `load`, `leads`, `day_window`),
  `research/topics.py` (`activity`), `research/pipeline.py` (`Run.display_group`), `research/views.py`
- Tests: `tests/test_gdelt_leads.py`

## Decision

A record of a news index (`LEAD_SOURCES`: GDELT) is a lead, not a statement of its source.

- **Observed by the program.** The monitor reads the source's change feed like any other; `triage.route` settles
  its records as `news_index_lead` and `settle_leads` records each as an observation with no topic, no alert
  and no model call. A version a backfill run created is still settled first, as `background_backfill`.
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
  Fabric's (`collection/windows.py`, `biz_rules/gdelt_news.py`); the radar only reads them.

## Why

The view is computed from rows that already exist (ADR 0012), so a lead must be an observation to be in it; it is
not triaged because a title in a news index is a program fact (task book 05: the group view and its counts call
no model) and a topic opened on a headline would be a topic of an unverified lead. Registering the gaps in the
analysis result, where the display group is stored, adds no table.

## Consequences

- The `gdelt_leads` channel the radar declares to query-planner (`research/run/decisions.py`) is still
  `not_supported`: reading the leads of a group back as pinned reading candidates is task book 04, not this step.
- Every lead is a group of its own, so the selection lists as many groups as there are leads (up to
  `MAX_LEADS`): the ranking and the export of `select_display` grow with the news index. Reaching `MAX_LEADS`
  sets `truncated`, a coverage gap, like `MAX_MATERIALS`. If the export is too large to read, the leads can be
  summarized there without changing the ranking.
- The coverage is read for the cutoff's day only; earlier days of the 30-day view are not covered.
