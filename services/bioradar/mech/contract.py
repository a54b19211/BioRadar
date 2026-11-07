"""MechEvidence's version-bound evaluation contract (mechevidence.evaluate.v2) as the radar uses it.

The rules (evaluator id, reuse key, echo, receipt validation, literature identity, adopt) are the shared
package's: extraction_contract.mech_v2, the module MechEvidence imports too, which reproduces MechEvidence's
published fixtures (tests/test_mech_contract.py runs them against it here as well). This module only adds
what is the radar's own: it never sends claims or joins of its own, and refuses to key or adopt a request
that does.
"""

from extraction_contract.mech_v2 import contract as shared
from extraction_contract.mech_v2.contract import (  # noqa: F401 - the radar's contract surface
    CONTRACT_V2,
    INCOMPLETE_CODES,
    binding_hashes,
    call_shape_problems,
    canonical,
    digest,
    evaluator_id_of,
    material_echo,
    material_inputs,
    normal_scope,
    receipt_mismatches,
)


class UnsupportedRequest(ValueError):
    """A request the radar never builds (it asks MechEvidence to decompose, or gives its own claims)."""


def _radar_request(request: dict) -> dict:
    if request.get("claims") is not None or request.get("joins"):
        raise UnsupportedRequest("the radar sends no claims or joins of its own")
    return request


def reuse_key(request: dict, evaluator: dict) -> str:
    return shared.reuse_key(_radar_request(request), evaluator)


def adopt(request: dict, receipt, expected_evaluator_id: str) -> dict:
    """What the radar records for a 200 receipt: {"result": mechanism_result, "rejected": audit row or None}
    (shared rules). A completed result is not yet adopted by the radar: it must also agree with the radar's
    own attempt ledger (bioradar.mech.ledger.reconcile)."""
    return shared.adopt(_radar_request(request), receipt, expected_evaluator_id)
