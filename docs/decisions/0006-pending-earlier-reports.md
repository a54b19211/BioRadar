# 0006 A pending earlier report is handed on as leads, labelled

- Status: accepted (2026-11-06; Codex ruling on follow-ups of held reports)
- Code: `research/lineage.py` (`previous_check`, `previous_for_biocheck`), `research/analyses.py` (`create`),
  `research/run/plan.py` (`ask_plan`), `research/run/report.py` (`report_input`), `research/prompts.py` (PLAN)
- Tests: `tests/test_previous_report_check.py`

## Context

A follow-up is planned and written with its parent's report in view (ADR 0004). Since the publication gate
(ADR 0005) a parent may be held - its check did not run or did not pass - yet the plan model got only
`previous["draft"][:3000]`, and BioCheck only `{draft, date}`: neither could tell a PASS report from a
pending draft.

## Decision

- A held report may still be used to continue investigating, but only as context labelled 待核 and as leads
  to verify - never as confirmed basis, and it never bypasses the output gate of the new round.
- `analyses.create` stores the parent's `report_status` with the earlier report. `lineage.previous_check`
  gives `{report_status, pending}`: pending unless the status is PASS; a record from before the status was
  kept has no status and counts as unverified (`report_status: "unknown"`, `pending: true`).
- The plan payload carries `previous_report_check` next to the draft (the draft itself unchanged, so the
  name-basis rule of the plan still reads it), and the PLAN rules say what pending means: leads, objects and
  search directions only; nothing is written as established from it.
- BioCheck's request carries the same `report_status` and `pending`, and the draft of a pending report is
  prefixed with a fixed label (【上一轮报告未通过 BioCheck 核验…只能作为待核线索…】). BioCheck's
  `PreviousReport` model (draft, date) ignores fields it does not know, so the label is what its writer and
  checker actually see until BioCheck reads the fields itself.
- What the external views show is unchanged: `progress.report` and `progress.assessment` carry no SHOWN mark,
  so the detail view cannot show a held report past the gate.

## Consequences

The follow-up keeps its full ability to build on the earlier round. BioCheck may adopt the two fields in its
request model (潘倩); the label then becomes redundant but harmless.
