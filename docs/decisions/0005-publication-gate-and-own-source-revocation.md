# 0005 Publication gate and own-source revocation

- Status: accepted (2026-11-07; Codex: the PASS delivery gate and the per-send source permission check were accepted earlier, their fixes verified in r207/r209). An engineering decision, not a release or business-quality sign-off. Revised in place 2026-11-09 (r4): (a) and (c) no longer hide what did not pass, they show it with its status
- Design: v3.1 6.2 (输出前 / 全程: what may be shown is decided by a program gate) and 6.1 (权限: revocation
  must stop what was already read from leaking); task book 06 section 2.5 (every exit behind one gate)
- Code: `research/views.py` (`publication`, `marked`, `summary`), `research/pipeline.py`
  (`Run.result`, `Run.confirm_sources`, `Run.ask(carries=)`), `research/run/submit.py` (`service`,
  `outbound`), `research/run/{read,calculate,mechanism,assess}.py`, `app.py` (`held_notice`, `export`, `topics`,
  `observations`), `research/topics.py` (`level_gates`, `topic_view`, `observation_view`, `event_view`)
- Tests: `tests/test_publication_gate_and_source_revocation.py` (the two former strict xfails and four around them),
  `tests/test_send_recheck.py` (every request re-checked, through the real llm client),
  `tests/test_alert_exit.py` (section c)

## (a) A report BioCheck did not pass is shown marked 待核

**Before.** A report whose check did not run (BioCheck timed out, no budget) or did not pass was finished like
any other: nothing in the detail, the list or the export said it was unverified.

**Decision.** The program decides, at the end of the run, from BioCheck's status of the last check, the mark
every exit shows with the report:

- `report_status == "PASS"`: `publishable: true`, `hold: null`, `hold_reason: null`.
- anything else: `publishable: false`, `hold: "pending_verification"`, `hold_reason`: the report's own failure
  reason when there is one (`biocheck_timeout`, `no_evidence`, ...), `not_checked` when the check did not
  run without one, `biocheck_<status>` when BioCheck answered with another status (`biocheck_review`, ...).

One mark for every non-PASS outcome: in each case the next step is the same - verify again (after a
revision, or once BioCheck answers) - and the reason says why. The planner and the models never set these
fields; v3 6.2 "planner 不能自己说证据充分就越过门槛". Nothing turns a result into a PASS to let it through.

The mark hides nothing. The first version of this decision left a held report's draft, assessment and
sentence-check words out of the external views; that kept every unverified report from being read, and its
problems from showing early, so it was dropped. The run stores its draft, every version, the sentence checks
and the assessment, and the external views (`views.marked`, used by `analyses.view` for the detail and the
list) show all of it with `publishable`, `hold` and `hold_reason` beside it. The export prints
`（报告待核：核验状态 …，<reason>。…）` above the draft and ends with the sentences BioCheck did not support
(`app.held_notice`, `app.export`); the page shows 待核 and the reason beside the draft and the sentence checks
(`apps/web/`). What a source that cannot be read now makes unreadable stays withheld, 待核 or not (b, ADR 0004).
Results stored before the mark are judged by their `report_status` the same way.

**Follow-ups.** A report marked 待核 is still handed to a follow-up, labelled pending, as leads to verify only
(ADR 0006).

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
(`GET /api/fabric/sources`) per carrying call, counted in `fabric_requests` like every other request.

## (c) The monitor's exits show a received lead at once and a level only behind the gate

**Before.** The triage model's answer left the radar as it was: `/api/topics` showed the topic's level (the
highest `alert` the model gave a record) and `summary`, `/api/monitor/observations` and the topic detail showed each
record's `alert` and the model's `reason`, and the topic's `observed` events carried the model's `alert`,
`signal` and `objects`. The source-revocation mask withheld text, but nothing asked whether anything shown had
been verified.

**Decision.** The same module that holds the report gate (`research/views.py`) decides every exit of the monitor.
No new service, no new stored state; what is stored (and what scheduling reads: the triage level, `pending_reason`)
is unchanged.

- **Received lead, at once (program fact).** Every observation carries `lead`, built from the record's own fields
  with no model and no analysis: `本系统于{available_at}收到来源{source}的线索：{title}（尚未核实）。` It is withheld
  together with the title when the source cannot be read (`app.redact_observation`).
- **A topic's level and summary carry the mark of the verification.** `alert` and `summary` are always shown.
  They are verified when the topic's latest analysis (`last_analysis_id`, the one that read its records) has a
  report that passed `publication` and the topic is not owed a newer analysis (`pending_reason`): then
  `publishable` is true. Otherwise the topic carries `publishable: false`, `hold: "pending_verification"` and
  `hold_reason`: `no_analysis`, `analysis_queued|running|failed`, the report's own reason (`biocheck_review`,
  ...), or `analysis_owed`, and the page marks the level 待核.
- **The model's verdict on a record does not leave.** An observation's `alert` is null and its `reason` is empty
  unless the program wrote it (`triage_failed`, `triage_deferred`, `triage_unavailable`, `background_backfill`,
  an unparsed attachment; `views.PROGRAM_REASONS`); an `observed` event carries `alert` null, `signal` empty and
  `objects` empty. The keys stay.

The level a topic shows is still the triage level: the analysis passing says its report was verified, not
that the triage level was. Showing the analysis's own `priority` and `consequence`, each behind its judgment's
check, is a next step of task book 06 and needs the judgment-level receipt.

**Still open (not done here).** The level also reaches the exits indirectly through scheduling codes
(`alert_important|alert_urgent` in `pending_reason`, `owed` and `analysis` events, the monitor round's summary and
the topic analysis's question) and through `heat`. The stored `alert_state` / receipt and the three display times of
task book 06 section 2.5, and the page showing the hold, are later steps.

## Consequences

- The two defects of `tests/test_publication_gate_and_source_revocation.py` pass and are no longer xfail.
- External views gain `publishable`, `hold`, `hold_reason` on every result with a report status (golden view
  fixtures regenerated: only these keys added for PASS results).
- Test fakes must answer `sources()`; `FakeFabric` lists the sources of its records.
- The topic exits gain `publishable`, `hold`, `hold_reason` and lose the model's words about a record (golden view
  fixture of `test_a_revoked_source_shows_no_text_in_any_exit` regenerated); observations gain `lead`. The topic
  detail lists the topic's readings with the mark of their check (ADR 0010).
