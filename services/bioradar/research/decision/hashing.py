"""Content identities, separate from submission IDs and server audit timestamps."""

from __future__ import annotations

from bioradar.research.decision.models import DecisionInput, DecisionPolicy
from bioradar.research.mining.models import canonical_hash


def decision_input_hash(decision_input: DecisionInput) -> str:
    value = decision_input.model_dump(mode="json", exclude={"input_id"})
    for name, keys in (
        ("metrics", ("metric_id",)),
        ("options", ("option_id",)),
        ("scenarios", ("scenario_id",)),
        ("outcomes", ("option_id", "scenario_id", "metric_id")),
        ("constraints", ("constraint_id",)),
    ):
        value[name] = sorted(value[name], key=lambda item: tuple(item[key] for key in keys))
    value["evidence_snapshot_hashes"] = sorted(value["evidence_snapshot_hashes"])

    def sort_evidence(node: object) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key == "evidence" and isinstance(child, list):
                    child.sort(key=canonical_hash)
                else:
                    sort_evidence(child)
        elif isinstance(node, list):
            for child in node:
                sort_evidence(child)

    sort_evidence(value)
    return canonical_hash(value)


def decision_policy_hash(policy: DecisionPolicy) -> str:
    value = policy.model_dump(mode="json")
    value["pareto_metric_ids"] = sorted(value["pareto_metric_ids"])
    return canonical_hash(value)
