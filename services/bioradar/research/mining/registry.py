"""Explicit, inspectable methods; implementations are bound by the pure runner."""

from dataclasses import dataclass

from bioradar.research.mining.models import ENGINE_COMPUTE_VERSION as ENGINE_COMPUTE_VERSION
from bioradar.research.mining.models import METHOD_IDS


@dataclass(frozen=True)
class MethodSpec:
    method_id: str
    version: str
    label: str
    input_groups: tuple[str, ...]
    goals: tuple[str, ...]


METHODS = {
    "lexical_emergence": MethodSpec(
        "lexical_emergence",
        "lexical-anchored-v2",
        "文本短语、组合与曝光变化",
        ("authorized_text",),
        ("M02", "M03"),
    ),
    "text_topics": MethodSpec(
        "text_topics", "topics-anchored-v2", "文本主题与语义新颖线索", ("authorized_text",), ("M02",)
    ),
    "entity_combinations": MethodSpec(
        "entity_combinations",
        "combinations-v1",
        "规范实体组合",
        ("confirmed_entities", "labels"),
        ("M02", "M03"),
    ),
    "state_transitions": MethodSpec(
        "state_transitions", "state-v1", "状态迁移与断言冲突", ("state_assertions",), ("M01",)
    ),
    "pipeline_landscape": MethodSpec(
        "pipeline_landscape", "pipeline-v1", "管线进入退出及格局", ("confirmed_projects",), ("M05",)
    ),
    "research_network": MethodSpec(
        "research_network", "network-v1", "研究合作新桥与连接假说", ("confirmed_relations",), ("M08",)
    ),
    "maturity_shift": MethodSpec(
        "maturity_shift", "maturity-v1", "条件化证据成熟度迁移", ("reviewed_assessments",), ("M04",)
    ),
    "counterevidence_impact": MethodSpec(
        "counterevidence_impact",
        "impact-v1",
        "负面、撤回与未引用反证",
        ("authorized_text", "dependencies"),
        ("M06",),
    ),
    "translation_dwell": MethodSpec(
        "translation_dwell", "dwell-v1", "转化等待与删失队列", ("confirmed_projects", "stage_times"), ("M07",)
    ),
    "event_exposure": MethodSpec(
        "event_exposure",
        "exposure-v1",
        "有源事件与条件暴露路径",
        ("events", "typed_relations"),
        ("M09", "M10"),
    ),
    "regional_comparison": MethodSpec(
        "regional_comparison", "regional-v1", "同口径区域指标比较", ("regional_indicators",), ("M10",)
    ),
}
assert tuple(METHODS) == METHOD_IDS


def methods_metadata() -> list[dict]:
    return [
        {
            "method_id": m.method_id,
            "version": m.version,
            "label": m.label,
            "input_groups": list(m.input_groups),
            "goals": list(m.goals),
        }
        for m in METHODS.values()
    ]


def method_versions(method_ids: tuple[str, ...]) -> dict[str, str]:
    return {identifier: METHODS[identifier].version for identifier in method_ids}
