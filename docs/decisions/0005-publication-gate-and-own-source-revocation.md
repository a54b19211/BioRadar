# 0005 Publication gate and own-source revocation

- Status: proposed (2026-11-06), for review by Codex
- Design: v3.1 6.2 (输出前 / 全程: what may be shown is decided by a program gate) and 6.1 (权限: revocation
  must stop what was already read from leaking)
- Code: `research/views.py` (`publication`, `gated`, `summary`), `research/pipeline.py`
  (`Run.result`, `Run.confirm_sources`, `Run.ask(carries=)`), `research/run/submit.py` (`service`,
  `outbound`), `research/run/{read,calculate,mechanism,assess}.py`, `app.py` (`held_notice`, `export`)
- Tests: `tests/test_chain1_defects.py` (the two former strict xfails and four around them),
  `tests/test_send_recheck.py` (every request re-checked, through the real llm client)

## (a) Only a report BioCheck passed is deliverable

**Before.** A report whose check did not run (BioCheck timed out, no budget) or did not pass was finished like
any other: the draft and the assessment went to the detail, the list and the export.

**Decision.** The program decides, at the end of the run, from BioCheck's status of the last check:

- `report_status == "PASS"`: `publishable: true`, `hold: null`, `hold_reason: null`.
- anything else: `publishable: false`, `hold: "pending_verification"`, `hold_reason`: the report's own failure
  reason when there is one (`biocheck_timeout`, `no_evidence`, ...), `not_checked` when the check did not
  run without one, `biocheck_<status>` when BioCheck answered with another status (`biocheck_review`, ...).

One hold state for every non-PASS outcome: in each case the next step is the same - verify again (after a
revision, or once BioCheck answers) - and the reason says why. The planner and the models never set these
fields; v3 6.2 "planner 不能自己说证据充分就越过门槛".

The run still stores its draft, every version, the sentence checks and the assessment (stored data is never
changed by the gate), so a re-verification can start from them. The external views apply the gate
(`views.gated`, used by `analyses.view` for the detail and the list): for a held report the draft is empty,
the assessment is absent (null), and the review keeps its structure (sentence numbers, verdicts, citations)
without the words; the hold, its reason, the numbers, computations and mechanism evaluation stay. The export
prints `（报告待核：<reason>。…）` instead of the draft and no judgments. Results stored before the gate are
judged by their `report_status` the same way.

**Follow-ups.** A held report is still handed to a follow-up, labelled pending, as leads to verify only
(ADR 0006). The web app shows
the empty draft; showing the hold reason there is a page change (刘金彦) not made in this wave.

## (b) The run's own sources are confirmed readable before anything carries them out

**Before.** Only the earlier round's report was re-checked during a run (ADR 0004). A source of the run's own
evidence revoked after it was read still went out in the report input to BioCheck.

**Decision.** Every outbound call that carries the run's own evidence first asks Fabric for the readable
source list and requires every source of that evidence to be in it (`Run.confirm_sources`):

| Call | Sources checked |
|---|---|
| read (model) | the record being read and every record a known fact quoted |
| calculate (model) | the records of the numeric facts |
| assess (model) | every record read (the payload holds the draft and the list of records) |
| MechEvidence | the records sent |
| BioCheck generate / verify / revise, and the verification of judgments | the records in the request |

The check runs before every real HTTP request that carries the evidence, not once per logical call: for
a model call right before its first request (after the budget check, so no Fabric request when no call
would be made), and through the llm client's `before_send` hook before every later one - a resend after
a refusal (429/503), the client's format retry, and `Run.ask`'s own retry. When a later request is
blocked, the requests already sent keep their record exactly (`llm_calls` count, usage, HTTP statuses,
`llm_outputs`; error `send_blocked`), the material they carried stays `sent`, and only the blocked request
gets a new `material_reads` attempt with status `withheld`. The BioCheck and MechEvidence clients send one
request each, with no retry on the radar side; their own internal model calls are theirs to guard.
If a source is no longer listed the run stops with `source_access_withdrawn`; if
the list cannot be read, with `source_access_unconfirmed` - unknown is never "readable". Nothing is sent and
no call is spent (call counts and budgets are untouched). The material prepared for a stopped call is
recorded with the new `material_reads.status` `withheld`; earlier rows, saved progress and recovery state
are not changed, so the audit shows what went out before the revocation and what was stopped. The check runs
at each call, so a resumed run (a new worker from the saved stage) is covered the same way.

**Why stop the run instead of leaving the revoked evidence out.** The evidence was read, quoted into facts,
counted for checks and possibly cited by the draft; removing it would need every later stage to re-derive its
inputs, and a report written without it would silently differ from what the plan and the reads support.
Stopping is the conservative behaviour v3 6.1 asks for, it is one rule for every call, and the analyst can
start a new analysis on the sources still readable. Revocations are rare; the cost is one Fabric request
(`GET /api/history/sources`) per carrying call, counted in `fabric_requests` like every other request.

## Consequences

- The two defects of `tests/test_chain1_defects.py` pass and are no longer xfail.
- External views gain `publishable`, `hold`, `hold_reason` on every result with a report status (golden view
  fixtures regenerated: only these keys added for PASS results).
- Test fakes must answer `sources()`; `FakeFabric` lists the sources of its records.
