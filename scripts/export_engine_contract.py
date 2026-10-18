"""Export the actual runtime OpenAPI; frozen V1 target contract is never modified."""

import json
from pathlib import Path

from bioradar.api.app import create_app
from bioradar.api.config import Settings
from bioradar.research.decision.models import DecisionInput, DecisionPolicy
from bioradar.research.history.models import HistoryEvaluationCreate, HistoryStudyCreate
from bioradar.research.investigations.graph import GraphBundle, GraphInvestigationCreate
from bioradar.research.mining.models import MiningPolicy, NormalizedSnapshot
from bioradar.topics.models import TopicCreate


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    contract = create_app(Settings()).openapi()
    (root / "contracts/bioradar-engine.openapi.json").write_text(
        json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for name, model in (
        ("topic-request", TopicCreate),
        ("graph-request", GraphInvestigationCreate),
        ("graph-bundle", GraphBundle),
        ("history-request", HistoryStudyCreate),
        ("history-evaluation", HistoryEvaluationCreate),
        ("engine-snapshot", NormalizedSnapshot),
        ("engine-policy", MiningPolicy),
        ("decision-input", DecisionInput),
        ("decision-policy", DecisionPolicy),
    ):
        (root / f"contracts/{name}.schema.json").write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"Exported actual runtime contract: {len(contract['paths'])} paths, plus nine input schemas")


if __name__ == "__main__":
    main()
