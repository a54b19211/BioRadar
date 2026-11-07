# 0015 New versions of the records propositions rest on

- Status: accepted for the first step (2026-11-08); skipping a re-judgment on a field-level difference (task book
  05 2.6 step 3) waits for a source that gives one - none does yet
- Design: task book 05 section 2.6 and phase 0 step 3; 08 sections 2.2 (`revision`), 2.6 and phase 0 step 5;
  00 sections 1.1 and 3.7
- Code: `research/revision.py`, `research/propositions.py` (`standing`), `research/scheduling.py`
  (`version_batch`, `version_basis`), `research/analyses.py` (`reassessing`, `execute`), `research/monitor.py`,
  `research/run/plan.py` (`judgment_questions`), `adapters/fabric.py` (`version_differences`, `read_at`),
  `app.py` (`Followup.about`)
- Tests: `tests/test_new_versions.py`

## Decision

**Arrival.** A monitoring round looks, with no model call, at every new version that touches a topic: an earlier
version of its record was placed in the topic or read by an analysis of it, or the version corrects or retracts such
a record. It asks Fabric's `/api/history/version-differences` at the mode and cutoff the version was read at (the
pair `Fabric.complete` stamps on each document as `read_at`, the one its `/records` answer was given at). The result
is one `version` event of the topic: the cause (`version` or `correction`), Fabric's kind of difference and its
facts, why (`reason`), how it counts (`sample`), and `affected`, the propositions that rest on the record - those of
the topic's latest judgments (`propositions.standing`, through the parent chain) whose `depends_on.material_reads`
include a read of the older version. An event is written once per version and cause.

| Fabric says | `reason` | `sample` | propositions that rest on the record |
|---|---|---|---|
| a correction or retraction is stated (any kind, text the same or not) | `source_corrected` | by the kind | judged again, first |
| `raw_changed` | `pending_review` | `real_update` | judged again |
| `serialization_only` | `pending_review` | `engineering_reuse` | judged again |
| `projection_only`, text, located fields or update time moved | `processing_upgrade` | none | judged again |
| `projection_only`, the parser version alone | `processing_upgrade` | none | stand; the event says so |
| no answer, `first_adoption`, `not_visible` | `pending_review` | none | judged again |

No source gives the radar a field-level difference (Fabric names the kind of change, not which cited field moved),
so nothing is skipped on the strength of a hash and every source is treated as WHO DON is: left for review. A
processing upgrade is no update of the source and is never a `real_update` sample, so it does not trigger judgment
E; only the propositions whose basis it moved are judged again.

**The owed analysis.** When propositions are to be judged again the topic is owed an analysis (`owe`, reason
`new_version`, dated by the version's availability). It is the ordinary topic analysis (`scheduling`), told which
propositions to judge and why, with the new versions pinned as seeds; `input.reassess` holds
`[{prop_id, question, reason, triggers}]` and the plan's judgment questions are exactly those questions
(`plan.judgment_questions`), so the proposition numbers are reused by the rule of ADR 0012 and `content_rev`
rises only when the conclusion changed. The events it was handed are bounded and recorded like corrections.

**The revision section.** A completed analysis with `reassess` gets a `revision` section (`revision.revise`): one
line for every proposition the earlier rounds stand on - `before` and `after` (status, confidence, `content_rev`),
`status` (`revised`, `maintained` after it was checked, `open` when the analysis did not judge a proposition it
was asked about, or `maintained` with reason `unaffected`), `reason` and `trigger`. An earlier result is never
written to; the old version is read through `parent_id`.

**Follow-up about a proposition.** `POST /api/analyses/{id}/followup` takes an optional `about` (a `prop_id`). It
must be a proposition the analysis stands on, its own or an earlier round's, and the analysis must be readable now;
otherwise 422 (`about_not_in_analysis`, `about_analysis_not_readable`). The follow-up inherits sources, scope,
replay mode and cutoff as any follow-up does, so naming a proposition widens nothing; it judges that one
proposition (`reassess`, reason `followup`) and its result has the revision section.

## Why

Version identity covers the parser and the whole projection, so a new `version_id` says nothing about whether the
text a proposition quotes changed. The radar already knows which reads a proposition rests on (`depends_on`), so
the work is bounded to those, and the stored facts are the audit of why a proposition was or was not judged again.
Reusing the topic's debt (`owed`), `propositions` and the parent chain (`lineage`) keeps one state system.

## Consequences

- A new version of a record no proposition rests on gets an event and no analysis; the next revision section
  lists every proposition as maintained.
- The lookups by record (`observations`, `material_reads`) are not indexed; they are two queries per round and
  per 500 records, and only run for versions of records, and corrections, the round brought.
- `affected` is resolved against the latest judgments when the version arrives. An analysis of the topic still
  running then is not looked at.
- The proposition status is `slots.graded_status` (supported or insufficient) with `open` for "not judged
  again"; the `conflict`, `refuted` states of task book 05 are not stored on a judgment yet.
- Whether a version is the first real update of the display group (judgment E as one of the three formal
  analyses, task book 00 1.1) is read from the events: the first with `sample` `real_update`.
