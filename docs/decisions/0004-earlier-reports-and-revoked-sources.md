# 0004 Earlier reports and revoked sources

- Status: accepted (2026-11-06)
- Code: `research/pipeline.py` (`previous_report`, `upstream_still_readable`, `lineage`), `app.py` (`redact`,
  `hidden_by_analysis`)

## Context

A follow-up analysis is planned with its parent's report in view. That report was written from sources that
can be revoked later, and its words then travel on in the follow-up's checks, search terms and research scope.
Review found four ways this leaked: the report was checked once and remembered; a long or looping chain of
rounds was walked partly and treated as "no earlier sources"; a source revoked mid-investigation still had the
plan's terms sent to Fabric; and the withholding of an inherited report was done field by field, so a fact's
text named the earlier project while its quote came from a still-readable record.

## Decision

- The earlier report is used only while every source it was written from, through all earlier rounds, can be
  read now; this is checked at every use (plan, report) and before every model call, Fabric action and
  service request of a run that used it. A run that used it stops with `upstream_access_withdrawn`.
- A chain of rounds that loops, breaks off or is longer than `LINEAGE_DEPTH` is unknown, never "no earlier
  sources".
- In the external views, everything the model wrote in a round that inherited a now-unreadable report is
  withheld as a whole; what stays is program text (ids, codes, times), quotes whose own record is readable,
  search terms and check texts made only of the user's own words, and the stop reason.

## Consequences

Calls already sent are not taken back. Stored data is never changed by withholding; views are computed per
request from Fabric's current source list.
