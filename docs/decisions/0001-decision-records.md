# 0001 Decision records instead of review references in code

- Status: accepted (2026-11-06)

## Context

Up to November 2026 most fixes came out of review rounds, and the code carried the round that asked for them:
"(Codex r40)", "(T-24)", "measure.v6 (B r2)", and change logs above constants listing what each version
changed. 157 such references were spread over `services/` and `db/`. They say *who asked when*, which the git
log already records, and they age badly: a reader cannot tell which of them still describe the code.

## Decision

- Comments and docstrings explain why the code is the way it is today. They do not cite review rounds or task
  numbers.
- A reason that is only understandable with its history (a version sequence, a design trade-off that was
  argued) gets a short record here, `docs/decisions/NNNN-<topic>.md`, and the code points to it.
- Concrete failures that justify a rule stay in the comment as the failure itself ("an older deferred
  version read by record id never got done and blocked the queue head"), not as a round number.
- Version identifiers that are part of the data (a stored row's producer and `checker_version`) stay as they
  were written: they name which rules produced a stored result, and a reader shows them as stored, never
  rewritten as a later version.

## Consequences

The reference strip was mechanical and then read line by line; no reason was dropped. The git history keeps
the original wording and the review round of every change.
