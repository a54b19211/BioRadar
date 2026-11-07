"""Does a receipt's set of model requests agree with the radar's own ledger of them?

A receipt that says it is complete (audit_complete) is the service's word. The radar allowed each request
itself (model_attempts) and was told the outcome of each, so it knows which requests were allowed and what
came back. The two must be the same set: every request the receipt archives was allowed by the radar under
that decision, in that position, and its outcome is the one the service reported then; and every request
the radar allowed is in the receipt (one allowed and not archived may have been sent and is not accounted
for). Anything else is a reason not to adopt the evaluation. Pure: rows and receipt in, problems out."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Attempt:
    """One row of the radar's ledger, as far as the comparison needs it."""

    attempt_id: str
    decision_id: str | None
    idx: int
    status: str  # prepared | settled | refused
    delivered: str | None = None
    http_status: int | None = None
    response_sha256: str | None = None
    truncated: bool | None = None


def archived(receipt: dict) -> list[dict]:
    """The requests a well-formed receipt archives, in the order it gives them."""
    return [out for call in receipt["llm_calls"] for out in call["outputs"]]


def reconcile(ledger: list[Attempt], receipt: dict, max_attempts: int) -> list[str]:
    """Differences between the radar's ledger and a well-formed receipt; empty = the same set of requests."""
    problems = []
    allowed = {row.attempt_id: row for row in ledger if row.status != "refused"}
    seen = set()
    for out in archived(receipt):
        row = allowed.get(out["attempt_id"])
        seen.add(out["attempt_id"])
        if row is None:
            problems.append(
                f"the receipt archives request {out['attempt_id']}, which the radar did not allow"
            )
            continue
        problems += _same_request(row, out)
    problems += [
        f"the radar allowed request {row.attempt_id} (number {row.idx}) and the receipt does not archive it"
        for row in allowed.values()
        if row.attempt_id not in seen
    ]
    if len(allowed) > max_attempts:
        problems.append(f"the radar allowed {len(allowed)} requests, more than the {max_attempts} it granted")
    return problems


def _same_request(row: Attempt, out: dict) -> list[str]:
    where = f"request {row.attempt_id}"
    if row.status != "settled":
        return [
            f"{where}: the receipt archives an outcome the radar was never told ({row.status} in the ledger)"
        ]
    found = []
    if out["decision_id"] != row.decision_id:
        found.append(f"{where}: decision {out['decision_id']} is not the radar's {row.decision_id}")
    if out["index"] != row.idx:
        found.append(f"{where}: number {out['index']} is not the radar's {row.idx}")
    ours = (row.delivered, row.http_status, row.response_sha256, row.truncated)
    theirs = (out["delivered"], out["status"], out["sha256"], out["truncated"])
    if ours != theirs:
        found.append(f"{where}: the receipt's outcome {theirs} is not the one the radar was told {ours}")
    return found
