# 0002 Measure producer and checker versions

- Status: superseded by 0010 (2026-11-07): Fabric extracts and checks measurements; the radar's own chain is
  deleted and its tables are frozen history. The versions below still name what produced those rows.
- Code (until 0010): `services/bioradar/research/measure_checks.py` (`PRODUCER`, `CHECKER`, `CONTRACT`)

## Context

Triage extracts measures (`PRODUCER`, the prompt and rules) and BioCheck checks each one under the shared
`extraction-contract` (`CHECKER` under `CONTRACT`). Stored candidates, checks and series points record the
versions that produced them, so a reader of a stored result can tell which rules made it.

## Decision

A change of what triage extracts, or of how it is checked, is a new version. Results of earlier versions are
kept as history and never rewritten; points not checked by the current checker are not current
(`measure_checks.effective`). The versions so far:

| Version | What changed |
|---|---|
| measure.v6 | A measure may cite the source of its period / object / scope / denominator literal: the quote, the window, or a context the window sent (heading, table header, table title). It is looked up there only, in the candidate's own send. |
| measure.v7 | The measure also names its metric family and the dimensions the record states; a point is compared automatically only inside a complete, proven group (ADR 0003). |
| measure.v8, checker v11, contract v3 | The dimensions a measure states are located in their cited sources and sent with the item for the checker to judge each one (`extraction_normalize.v4` for the unit). |
| measure.v9, checker v16 | Found on real NMPA notices: the period literal must be the whole written period (`extraction_normalize.v5`); a complete date ending the line before (a notice's 发布时间) is its own period. Checker v16 is v15 (output measures) with that rule. |
| measure.v10 | Found on the first real local-Qwen window: the prompt names the keys of `sources` exactly; the four aliases `<field>_text` are mapped mechanically inside `sources` only and every mapping is recorded in `llm_outputs` with the raw answer. Every HTTP request of a model call is counted with its outcome (`llm_calls.http`). |

## Consequences

The version constants are the only place a version appears in code; the contract package pins the checker
(`requirements-contract.txt`, checked by `make check-install`).
