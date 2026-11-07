# 0009 Fabric declares what a source can do; MechEvidence v2 is bound, authorized and reconciled

- Status: accepted (2026-11-07), Radar side of Fabric's 0005 and MechEvidence's 0002

## Fabric's capabilities replace the radar's lists

- `GET /api/history/sources` carries `capabilities` per source (`datafabric.source_capabilities.v1`).
  `adapters/capabilities.SourceCapabilities` reads it whole or not at all. `readable` is every entry of
  `sources` and `retired_sources` whose `capabilities.readable` is true; `unavailable_sources` never count.
  Nothing is inferred from a source's group (collected, graph, retired).
- `FETCHABLE`, `ATTACHMENT_SOURCES`, `REGISTRY_DIFF_SOURCES`, `native_kind` and `SOURCE_KIND` are gone;
  `tests/test_no_source_sets.py` (an AST scan) fails if a copy comes back. Capabilities unknown means no fresh
  collection, no attachment text, no registry diff, and a note - never a built-in fallback.
- A snapshot (`progress.capability_snapshots`: which receipt) is for planning and audit. Every send, retry,
  cache hit and report render still asks Fabric (`Run.confirm_sources`), and versions are read by identity.
- Only sources Fabric offers for selection (`capabilities.select`) can be chosen for a new analysis (API 422
  `source_not_selectable`) or monitored; `legacy_documents` is readable (old references resolve through Fabric's
  alias and the same permission check) but not selectable.
- Behaviour changes, each its own commit with a before/after: fresh collection by query now includes `gdelt_doc`;
  attachment text for 20 sources instead of 3; a record without `document_evidence` is no longer promoted by the
  radar from Europe PMC's `pubTypeList` (Fabric's rule differs for "Journal Article", "research-article",
  "Case Reports", "Meta-Analysis"); BioCheck's source kind no longer falls back to five source names.

## MechEvidence v2

- `BIORADAR_MECHEVIDENCE_CONTRACT=v2` (default `v1`, unchanged). `bioradar.mech` uses the contract (key, evaluator id,
  echo, receipt validation, adopt) from the shared package `extraction_contract.mech_v2` (extraction-contract
  1.14.1), the same code MechEvidence imports. MechEvidence's published fixtures (pinned) are run against it here
  too: 12 reuse keys, 51 receipt outcomes, the identity contract, 19 negative requests.
- The radar issues an authorization (`model_attempt_authorizations`) per evaluation and answers
  `POST /internal/v1/model-attempts/authorize` before every model request and `/{attempt_id}/outcome` after
  (service token `BIORADAR_MODEL_ATTEMPT_TOKEN`). Fabric is asked now for sources and versions at each decision;
  the reservation is atomic on the authorization row; reserved, settled and refused requests are different rows.
- A receipt is adopted only if the contract rules and the radar's ledger agree (`mech/ledger.py`). Failed,
  `receipt_mismatch`, `ledger_mismatch`, `audit_incomplete` results feed no report and no cache; the receipt is
  kept in `mech_receipts_rejected`. Requests are counted from the ledger; usage only from reported provider
  counts (`llm_calls.usage_known`).
- Cache: `mech_evaluations` by the conservative phase-1 key (ordered materials with aliases and titles, scope,
  evaluator); used only after current source confirmation and visibility at the cutoff.

## Not done (follow-ups)

- BioCheck returns no raw model answers or attempt ids; to be ingested like MechEvidence's it must return per
  request `{attempt, sha256, content (raw HTTP body), status, delivered}` and take part in the authorization
  protocol. Its calls are logged with usage as reported and materials linked to its first call row.
- The service's own model requests are still not re-checked inside a v1 request (documented compatibility gap).
- Order-independent reuse (phase 2) and the plan schema with three-state scope are not implemented; an old
  string plan maps to `unknown`.
