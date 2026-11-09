# 0014 Verify by assertion unit, and the report written by slot

- Status: accepted for the first step (2026-11-08); the writer's side in BioCheck, `[R#]` receipts, follow-ups
  that point at a proposition and new-version handling are later steps
- Design: task book 06 sections 2.2, 3 and phase 0 step 3; 08 sections 2.2, 2.3, 2.5 and phase 0 steps 2 and 3;
  00 sections 1.5 and 3.5; BioCheck ADR 0004
- Code: `adapters/services.py` (`biocheck`, `verify_v2_body`), `research/run/slots.py`, `research/run/report.py`,
  `research/run/assess.py`, `research/run/sections.py`, `research/run/submit.py` (`service`),
  `research/propositions.py` (`number`), `config.py` (`biocheck_verify`)
- Tests: `tests/test_report_slots.py`, `tests/test_basis_confidence.py`

## Decision

- **Verify goes to `POST /v2/verify`** (`BIORADAR_BIOCHECK_VERIFY`, default `v2`; `v1` is the way back until the
  switch is done). `/v2/verify` rejects a field it does not name, so the adapter sends it the earlier report as
  draft and date and the research scope as its three lists. Every request carries `sections`: `[{kind, start, end}]`,
  the offsets of each paragraph of the draft and the section it is (`judgment`, `evidence`, `gaps`, `scenarios`).
  The conclusions of the assessment are checked as one `judgment` section. The radar no longer cuts those
  conclusions into clauses: the cut is BioCheck's. The receipt stays where the v1 receipt was: the last check's
  sentences, with their `segments`, `units`, `form_check` and `semantic_split`, in `report.claims`; each kept
  version records the `checker_version` and `rules_version` that checked it.
- **Slots.** Before the writer is asked the program lists the plan's judgment questions as slots J1, J2 ... A slot is a
  program number: the writer and assess refer to it. The proposition number of a slot (`prop_id`, `question_key`,
  `content_rev`) is taken when the result is stored (ADR 0012) and then stamped on the slot's item in the judgment
  section; the writer is not told it, because it does not exist yet. Assess gets the slots, names the slot of each
  judgment, and the judgment's question becomes the slot's wording. Today every slot is a `free` question; the
  templates of 05 (`need_check`, `case_count` ...) are not built, so J1..J5 of the chain are not mapped to them.
- **The writer** answers one `generate` request, which carries `slots`, with `sections`: one paragraph per item,
  `kind` judgment | evidence | gaps | scenarios; a judgment item names its slot (or a hypothesis) and the status it
  states (`supported | refuted | insufficient | open | conflict`). The program drops what it cannot place (another kind,
  no text, an unknown slot, a slot answered twice, a slot paragraph without a status) and says so in `notes`; it
  splices the paragraphs into the draft. A slot without a paragraph is `cannot_judge` (`no_paragraph`). The
  recommendation section stays the program's (empty, `no_objectives`). A writer that answers with a draft only
  leaves the judgment and scenarios sections to be made from the assessment, as before.
- **One structure, three places.** Report, assess and the stored result agree on a slot when the paragraph does not
  state a conclusion (`supported`, `refuted`) that assess grades insufficient. Only that structure is compared; whether
  the words say the same is BioCheck's check. After assess, `report.reconcile` marks the supported sentences of a
  mismatched paragraph insufficient (label STR, reason `assess_insufficient`) and sends them through the existing
  revise -> re-verify, in the revisions the report stage left (two in all); a rewritten paragraph takes the status
  insufficient. A paragraph left inconsistent makes the judgment section `missing` with `slot_mismatch`, and the
  publication gate holds the report (`sections_missing:slot_mismatch`). The opposite case, a paragraph that says
  "insufficient" under a higher grade, is not held: the report understates.
- **Basis confidence.** A judgment's `basis_confidence` is the model's proposal (high, medium, low). The program
  records `not_assessed` when none was proposed or when BioCheck did not check the conclusions, and caps it at low
  while a check that the judgment's own grounds answer is open or in conflict. A gap elsewhere does not touch it.
  `confidence` and `credibility` are as they were.
- **`depends_on.checked_by`** points at the call that checked the conclusion: the `llm_calls` row of the
  `verify_claims` call BioCheck names for the sentence (`call`, a position in the calls of its answer), else the
  request's only `verify_claims` call; unknown when neither. It was the analysis's last BioCheck call, which is the
  coverage check.

## What the radar needs from BioCheck

1. `/v2/verify`: a `sections` field (`kind`, `start`, `end`, offsets into `draft`) and, for a sentence inside a
   section other than judgment, scenarios or recommendation, `fact_section=True` in `unit_rules.check_sentence`.
2. `/v1/generate`: accept `slots` and answer with `sections` (above), with the structure in its prompt.
3. `/v2/verify`: `call` on each claim, the position in `llm_calls` of the `verify_claims` call that checked it.
4. `/v1/revise`: read a `/v2/verify` review (quotes from the units; `scope_span` on units). The radar puts the
   units' quotes back into `quotes` meanwhile (`report.for_revise`).

## Consequences

- Default `v2` needs a BioCheck that knows `sections`; one that does not answers every verify with 400
  `invalid_request`. Deploy BioCheck first, or set `BIORADAR_BIOCHECK_VERIFY=v1`.
- With `v1` the conclusions are no longer cut into clauses by the radar, so a long conclusion is one sentence for
  BioCheck's v1 check.
- Each evidence item carries `first_seen_at`, `proof_available_at` (null: no proof) and `replay_mode` (the mode
  its read ran in), as Fabric gave them. The merged `available_at` stays an internal clock for cutoffs and is
  never sent: `/v2/verify` rejects it. An evidence row stored before these were kept sends null.
- Each evidence item carries `window`: which characters of the record's whole text its `text` holds - `full`, or
  `excerpt` with `chars`, `record_chars` and `ranges` (`[start, end)` in the whole text) - and `unknown` for a row
  stored before the read kept `evidence.meta.record_layout`; the reader gets the same for its window. BioCheck
  reads `unknown` as an excerpt, so "the text has no X" is never taken for "the record has no X". `/v2/verify`
  refuses an unknown field: deploy BioCheck with `window` first.
- Not done: `[R#]` receipts, per-slot scope and time (the run's own for all slots, so nothing to compare).
