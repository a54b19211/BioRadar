# 0011 The report as sections, and the five-item check

- Status: accepted for the first step (2026-11-08); the model-written sections and the slots are later steps
- Design: v3.1 1.3 (what every report must say), 6.4; task book 08 sections 2.2, 2.7 and the phase 0 step 1
- Code: `research/run/sections.py`, `research/pipeline.py` (`Run.result`), `research/views.py` (`publication`,
  `gated`), `research/state.py` (`Section`, `SectionItem`, `VisibleRecord`, `SECTION_REASONS`)
- Tests: `tests/test_report_sections.py`

## Decision

A finished analysis stores `result.sections`: the report as sections of fixed kinds, made by the program from the
run's state without a model call. The free-text `draft` is unchanged and still what BioCheck wrote and checked.

| kind | made from | design 1.3 item |
|---|---|---|
| `question` | the question as asked | - |
| `judgment` | the assessment's judgments (conclusion, consequence) and graded hypotheses | main judgment, business impact |
| `evidence` | the facts read, for and against each check; a check in conflict lists both sides | evidence for and against |
| `gaps` | the scope of the searches, each check not searched or not answered, failed searches, why the mechanism did not run | limits |
| `scenarios` | what the assessment says is still unproven, and the signals that confirm or refute each graded hypothesis | the evidence that would change the judgment |
| `visibility` | cutoff, records read, the key gaps with their numbers; each record's version and times in `appendix` | - |
| `recommendation` | the requester's objectives; the run takes none yet, so it is empty with reason `no_objectives` | - |

A section the run cannot fill is `missing` with a reason (`SECTION_REASONS`). `views.publication` holds a report
that has a missing section even when BioCheck's status is PASS: `publishable: false`, `hold: pending_verification`,
`hold_reason: sections_missing:<items>` (`main_judgment`, `impact`, `evidence`, `review_condition`). The limits
cannot be missing: the gaps section always states the scope the searches stood on. A judgment BioCheck found
contradicted was withdrawn by assess and does not count as a main judgment.

A held report's sections show only kind, status and reason in the external views (`views.gated`); the reason
codes are kept where text is withheld (`views.is_reason_code`).

## Why

The five items were only a request in the writer's prompt; nothing checked them. Until the writer fills slots
(task book 08, later steps), the structured results the run already has are the nearest content for each item,
and the check is on those. The sections are derived at the end of the run from the saved state, so nothing is
added to `analyses.progress` and a resumed run builds the same sections.

## Consequences

- A PASS report whose assessment has no judgment (the plan named no judgment question, or assess failed or had
  no budget) is now held. That is the rule, not a defect of the check.
- Results stored before this change have no `sections` and are shown as before.
- The business impact is the assessment's `consequence` grade; prose impact and the four uncertainty fields
  come with the next steps. The page and the export do not show sections yet.
