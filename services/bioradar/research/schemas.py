"""Model output shapes. Small on purpose: at most two levels, ids are chosen from lists the program gives."""

from typing import Literal

from pydantic import BaseModel, Field


class PlanCheck(BaseModel):
    text: str = Field(description="要查清的一件事，一句话")
    required: bool = Field(description="不查清就不能回答问题时为true")


class PlanQuery(BaseModel):
    terms: list[str] = Field(description="1到3个检索词，同时出现才命中；每个2字以上")
    checks: list[int] = Field(description="这组检索词服务的待查问题序号（从1开始）")


class PlanMechanism(BaseModel):
    statement: str = Field(description="需要评价的候选机制，一句话")
    species: str = ""
    system: str = ""
    disease: str = ""


class Plan(BaseModel):
    checks: list[PlanCheck]
    queries: list[PlanQuery]
    judgments: list[str] = Field(
        default_factory=list, description="最后要给出明确判断的问题，如病原性质、是否人传人"
    )
    needs_calculation: bool = Field(default=False, description="回答需要程序计算比例、增减或利用率时为true")
    mechanism: PlanMechanism | None = None


class ReadFact(BaseModel):
    text: str = Field(description="这篇材料说了什么，忠实转述，保留对象、人群、时间、数值和单位")
    quote: str = Field(description="材料中支持这条转述的连续原文，逐字复制，不超过120字")
    checks: list[str] = Field(default_factory=list, description="相关的待查问题编号，如Q1")
    stance: Literal["supports", "refutes", "limits", "context"] = Field(
        description="对相关待查问题：提供肯定依据/提供否定依据/提供限制条件/仅背景"
    )


class Reading(BaseModel):
    relevant: bool
    facts: list[ReadFact] = Field(default_factory=list)
    new_terms: list[list[str]] = Field(default_factory=list, description="值得追查的新检索词组，每组1到3个词")
    answered: list[str] = Field(
        default_factory=list, description="读完这篇后已经完整回答的待查问题编号（问题要求的每个对象、每个期间都有原文依据）"
    )


class Operand(BaseModel):
    fact: str = Field(description="数值来自哪条事实，如F3")
    value: float = Field(description="该事实原文里的数值")


class Calculation(BaseModel):
    label: str = Field(description="算的是什么，如2026年上半年设备利用率")
    operands: list[Operand] = Field(description="按顺序对应表达式里的x1、x2……")
    expression: str = Field(description="只用x1、x2…、数字和+-*/()，如 x1 / x2 * 100")
    unit: str = ""
    period: str = ""
    population: str = Field(default="", description="计算覆盖的总体或样本，如三家模拟企业")
    denominator: str = ""


class Calculations(BaseModel):
    calculations: list[Calculation] = Field(default_factory=list)


class Judgment(BaseModel):
    question: str
    conclusion: str = Field(description="一句话结论，未证实的写明待证实")
    confidence: Literal["insufficient", "possible", "probable", "confirmed"]
    credibility: Literal["low", "medium", "high"] = Field(description="真实性置信")
    consequence: Literal["low", "medium", "high"] = Field(description="若属实的潜在后果")
    priority: Literal["low", "medium", "high", "urgent"] = Field(description="核实优先级")
    grounds: list[str] = Field(default_factory=list, description="依据编号，如E2")
    unproven: str = Field(default="", description="还没有证实的部分和最能区分的下一项证据")


class Assessment(BaseModel):
    alert: Literal["none", "normal", "important", "urgent"]
    judgments: list[Judgment] = Field(default_factory=list)


class TriageItem(BaseModel):
    index: int = Field(description="记录序号，从1开始")
    topic: int | None = Field(default=None, description="归入已有专题的序号；新事件填null")
    new_topic: str = Field(default="", description="不属于已有专题且值得跟踪时，给新专题一句话标题")
    direction: Literal["pipeline", "science", "business", "public_health"] = Field(
        default="public_health", description="新专题的研究方向"
    )
    alert: Literal["none", "normal", "important", "urgent"]
    closure: bool = Field(default=False, description="这条记录宣布该事件已结案或可以停止跟踪")
    reason: str = ""


class Triage(BaseModel):
    items: list[TriageItem]
