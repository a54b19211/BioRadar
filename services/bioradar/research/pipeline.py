"""Fixed research stages, not another agent runtime or DAG engine.

B01 supplies persistence/control before any stage is executed. Provider ports are
declared here so unavailable research cannot be confused with a completed report.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class StageDefinition:
    key: str
    label: str
    provider_capabilities: tuple[str, ...]


STAGES = (
    StageDefinition("coverage", "检查资料覆盖", ("data.batch", "entity.resolve")),
    StageDefinition("retrieve", "调查取证", ("evidence.retrieve",)),
    StageDefinition("compare", "计算与比较", ()),
    StageDefinition("assess", "形成条件化判断", ("mechanism.evaluate",)),
    StageDefinition("compose", "生成成果草稿", ("answer.compose",)),
    StageDefinition("check", "核验与提出修订", ("answer.check", "answer.revise")),
)
