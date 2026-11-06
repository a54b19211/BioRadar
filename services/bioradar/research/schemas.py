"""Model output shapes. Small on purpose: at most two levels, ids are chosen from lists the program gives."""

from typing import Literal

from pydantic import BaseModel, Field


class PlanCheck(BaseModel):
    text: str = Field(description="要查清的一件事，一句话")
    required: bool = Field(description="不查清就不能回答问题时为true")
    objects: list[int] = Field(
        default_factory=list, description="这件事涉及哪些对象（objects 的序号，从1开始）"
    )


class PlanObject(BaseModel):
    name: str = Field(description="问题点名的具体对象，原文写法")
    kind: Literal["company", "drug", "target", "indication", "trial", "place", "event", "other"] = "other"


class PlanQuery(BaseModel):
    terms: list[str] = Field(description="1到3个检索词，同时出现才命中；每个2字以上")
    checks: list[int] = Field(description="这组检索词服务的待查问题序号（从1开始）")


class PlanMechanism(BaseModel):
    statement: str = Field(description="需要评价的候选机制，一句话")
    species: str = ""
    system: str = ""
    disease: str = ""


class PlanHypothesis(BaseModel):
    statement: str = Field(description="可检验的情报假设，一句话，不写成已证实")
    alternatives: list[str] = Field(default_factory=list, description="竞争解释")
    confirm: list[str] = Field(default_factory=list, description="出现就能支持它的后续信号")
    refute: list[str] = Field(default_factory=list, description="出现就会推翻它的信号")


class Plan(BaseModel):
    hypotheses: list[PlanHypothesis] = Field(
        default_factory=list, description="问题来自一条信号时的可检验假设，不超过3条；其他问题留空"
    )
    objects: list[PlanObject] = Field(default_factory=list, description="问题点名的具体对象，每个一项")
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
    object: str = Field(
        default="",
        description="这条事实说的是哪个对象：给出的对象名之一，或材料原文里出现的具体地点、机构、事件名（原文写法）；"
        "不涉及具体对象留空",
    )
    hypothesis: str = Field(default="", description="这条事实支持或反驳哪条假设（如H1）；无关留空")
    hypothesis_stance: Literal["supports", "refutes", "none"] = Field(
        default="none", description="对这条假设本身的作用，与对待查问题的 stance 分开判断"
    )


class Reading(BaseModel):
    relevant: bool
    facts: list[ReadFact] = Field(default_factory=list)
    new_terms: list[list[str]] = Field(default_factory=list, description="值得追查的新检索词组，每组1到3个词")
    answered: list[str] = Field(
        default_factory=list,
        description="读完这篇后已经完整回答的待查问题编号（问题要求的每个对象、每个期间都有原文依据）",
    )


class Operand(BaseModel):
    fact: str = Field(description="数值来自哪条事实，如F3")
    value: float = Field(description="该事实原文里的数值")
    unit: str = Field(default="", description="这个数的单位，按原文，如 套、家、人、万元")


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


class HypothesisLevel(BaseModel):
    id: str = Field(description="假设编号，如H1")
    level: Literal["insufficient", "possible", "probable", "confirmed", "refuted"]
    reason: str = Field(default="", description="依据已核验报告的一句话理由")


class Assessment(BaseModel):
    alert: Literal["none", "normal", "important", "urgent"]
    judgments: list[Judgment] = Field(default_factory=list)
    hypotheses: list[HypothesisLevel] = Field(default_factory=list, description="只给出现过的假设编号评级")


class Dim(BaseModel):
    """One dimension of the business domain a value belongs to, as the record writes it (T-24)."""

    kind: Literal[
        "disease",
        "population",
        "date_basis",
        "indication",
        "product_or_class",
        "milestone_definition",
        "entity",
        "stage",
        "denominator_definition",
    ] = Field(
        description="维度：disease 病种/病原，population 人群或队列，"
        "date_basis 日期口径（按报告日/发病日…），indication 适应症，product_or_class 产品或类别，"
        "milestone_definition 里程碑定义，entity 企业或资产，"
        "stage 阶段（计划/在建/投运/实际产出），"
        "denominator_definition 比例的分母是什么（检测人数、全部病例…）"
    )
    text: str = Field(
        description="原文写法，逐字；原文没写就不要列这一项（不要猜，不要用专题名称或发布者代替）"
    )
    source: Literal["quote", "window", "heading", "table_header", "table_title"] = Field(
        default="quote", description="text 抄自哪里，只能是这次实际给你的材料"
    )


class Measure(BaseModel):
    """One measured value as the record states it (T-24 / T-29 P3). Each *_text is copied verbatim from the
    record and only used to find where it is written; the value checked and stored is the final one: the
    number written in value_text, the period in its normalized form, object / metric / scope as written (no
    synonyms). A measure enters a series only after BioCheck confirms every field at its place."""

    object_text: str = Field(default="", description="对象原文写法（逐字，原文里的那个词），没写就留空")
    metric_text: str = Field(
        description="指标原文写法（逐字），包括限定语，如“suspected cases of anthrax in humans”"
    )
    value_text: str = Field(description="数值原文写法（逐字，如“1,444”“26”），必须出现在 quote 里")
    unit_text: str = Field(default="", description="紧跟数值的单位原文（个、件、例、%、cases），没有就留空")
    period_text: str = Field(
        default="", description="期间原文写法（逐字，如“2026年4月”“截至2023年12月”），没写就留空"
    )
    period: str = Field(
        default="",
        description="期间规范写法：2026-04、2026Q2、2026H1、2026、2026-04-15、2022-09/2023-01、2020-01-22/2020-02-11；没写留空",
    )
    period_type: Literal["point", "interval", ""] = Field(
        default="", description="point 截至某时点 / interval 某期间内；没写期间留空"
    )
    scope_text: str = Field(default="", description="统计口径或覆盖范围原文（逐字），没写就留空")
    denominator_text: str = Field(default="", description="比例的分母原文（逐字），原文写出时才填")
    kind: Literal["stock", "flow", "cumulative", "rate", "other"] = Field(
        default="other", description="stock 某时点的存量/flow 当期新增/cumulative 累计/rate 比例"
    )
    qualifier: Literal["exact", "approximate", "at_least", "at_most", "more_than", "less_than"] = Field(
        default="exact", description="约、近、超过、至少等写在数值旁时如实标出"
    )
    modality: Literal["actual", "planned", "projected"] = Field(
        default="actual", description="原文说的是实际发生，还是计划、预计"
    )
    status: Literal["planned", "under_construction", "operational", "reported", "confirmed", "other"] = (
        "other"
    )
    quote: str = Field(description="含这个数值的连续原文，逐字复制，不超过120字")
    family: Literal["case_count", "milestone_count", "capacity", "efficacy", ""] = Field(
        default="",
        description="指标族：case_count 病例/公共卫生计数，milestone_count 注册/管线里程碑计数，"
        "capacity 产业产能，efficacy 研究疗效/临床终点；都不是就留空",
    )
    dims: list[Dim] = Field(
        default_factory=list,
        max_length=6,
        description="这个数值所属的病种、人群、日期口径等，原文写明的才列，每种最多一项",
    )
    sources: dict[
        Literal["period", "object", "scope", "denominator"],
        Literal["quote", "window", "heading", "table_header", "table_title"],
    ] = Field(
        default_factory=dict,
        description="period/object/scope/denominator 的 *_text 抄自哪里：quote（就在 quote 里）、"
        "window（窗口正文别处）、heading / table_header / table_title（随窗口给出的上下文）；"
        "在 quote 里可以不填。只能写这次实际给你的材料",
    )


class TriageItem(BaseModel):
    index: int = Field(description="记录序号，从1开始")
    topic: int | None = Field(default=None, description="归入已有专题的序号；新事件填null")
    new_topic: str = Field(default="", description="不属于已有专题且值得跟踪时，给新专题一句话标题")
    direction: Literal["pipeline", "science", "business", "public_health"] = Field(
        default="public_health", description="新专题的研究方向"
    )
    alert: Literal["none", "normal", "important", "urgent"]
    signal: Literal[
        "capacity",
        "hiring",
        "financing",
        "deal",
        "filing",
        "approval",
        "rejection",
        "trial_start",
        "trial_result",
        "trial_stop",
        "publication",
        "policy",
        "outbreak",
        "safety",
        "other",
    ] = Field(default="other", description="信号类型")
    objects: list[PlanObject] = Field(default_factory=list, description="记录说到的具体对象")
    watch: str | None = Field(default=None, description="归入已有专题时，命中该专题哪条待验证信号（如W2）")
    closure: bool = Field(default=False, description="这条记录宣布该事件已结案或可以停止跟踪")
    measures: list[Measure] = Field(default_factory=list, description="可跨期比较的数值，最多3个")
    reason: str = ""


class ExtractItem(BaseModel):
    index: int = Field(description="窗口序号，从1开始")
    measures: list[Measure] = Field(
        default_factory=list, description="这个窗口原文里的数值，最多3个；没有就空列表"
    )


class Extraction(BaseModel):
    """T-31: the measures of each continuation window (every window gets one item)."""

    items: list[ExtractItem]


class Triage(BaseModel):
    items: list[TriageItem]
