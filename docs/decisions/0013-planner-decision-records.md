# 0013 Sources and channels apart, and a rebuildable record of every planner call

- Status: accepted for the first step (2026-11-08); the graph actions, the `check_map` and the 05/06/07 states
  on checks are later steps
- Design: task book 04 sections 2.2, 2.5, 2.6 and phase 0 step 1; query-planner ADR 0002 (planner 0.2.0)
- Code: `research/run/decisions.py`, `research/run/investigate.py` (`planner_request`, `investigate`, `act`),
  `research/run/search.py` (`routes`, `channel_of`, `semantic`), `adapters/services.py` (`planner_health`),
  `adapters/fabric.py` (`semantic_search`, its `sources`), `research/state.py` (`DecisionRecord`)
- Tests: `tests/test_planner_record.py`

## Decision

- **Source and channel.** A candidate and a history item carry the real `source` and, for a search, a `channel`:
  `fabric_literal` or `weknora_semantic`. A read has no channel: it is Fabric's read of a record already found, so
  a channel declared unavailable never blocks reading what was found. The semantic search is offered once per
  source of the task and its recall is limited to that source (Fabric's `sources`), so the planner's source set
  holds for it. The request names the task's `sources` and declares all four channels each round: the graph as
  unavailable `03_not_run`, the GDELT lead list as unavailable `not_supported`, WeKnora as unavailable
  `not_configured` when semantic search is off.
- **Record.** Each planner call appends one record to `progress.decisions`: the request without `history`,
  `history_ref` (how many actions, their hash), `request_sha256` of the whole request, the whole response, the
  planner's `/health` (version, kinds, channels), and `action_index`. A call that failed, the call that stopped
  the run and a stop for the model-call limit are records too, with `stop` instead of an action. Actions are only
  appended, so the history is rebuilt from `actions[:n]` (`rebuilt_request`) and its hash says whether it is the
  request that was sent. Nothing is stored twice and no table is added.
- **Bound.** All records of an analysis together stay within 1 MiB. A round that would not fit keeps only its
  hashes and `snapshot_truncated: true`: it is honestly not rebuildable.
- **Old planner.** When the stage starts, `GET /health` must list the channels. A planner without them (before
  0.2.0) would answer the new fields with 422, so the run stops with `planner_outdated` and a note. It does not
  send less.
- **Status.** A check goes to the planner with the status the read stage left it: `answered` (with its E#
  evidence, a grounded candidate answer, not a verified one), `conflict` or `open`. `complete` only ends the
  investigation; the report stays pending until BioCheck and the publication gate say otherwise (ADR 0005).

## Consequences

- `AGENTS.md` said progress holds no requests or raw responses; the planner's records are the one exception.
  They are not marked SHOWN, so the external views do not show them.
- Golden fixtures grow: every save frame holds the records so far.
- With semantic search on, the baseline policy also gets one semantic candidate per source instead of one in all.
- The planner must be deployed before the radar.
- Not yet: the states 05, 06 and 07 give to checks (they have no `question_key` on checks, so there is no
  `check_map`), the `result()` summary of actions and decisions, and the graph actions.
