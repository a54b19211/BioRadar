"""Future proposal instructions only; literal qualification remains the unchanged v2 validator."""

import copy

from bioradar.research.datasets import narrative_v2

PROPOSAL_VERSION = "literal-narrative-proposal-v1"

UNIT_FORMS = (
    "百万元",
    "亿元",
    "万元",
    "千元",
    "元",
    "亿小时",
    "万小时",
    "千小时",
    "小时",
    "hour",
    "hours",
    "万套",
    "套",
    "台",
    "%",
    "％",
    "percent",
    "million CNY",
    "million RMB",
    "million USD",
    "CNY",
    "RMB",
    "USD",
)

DESCRIPTIONS = {
    "subject": (
        "原文中实际完整具名的经营主体span。客户/公司/本公司等泛称不能替代名字；"
        "若名字在标题，context必须同时覆盖该标题和本行，不能只放入shared_context。"
    ),
    "metric": (
        "原文实际指标名称span，不是allowed_metric_roles的规范化键。"
        "context须包含判断其订单/合同等角色的原文；不能把新签、验收、预收、收入和现金互换。"
    ),
    "value": (
        "一个完整数值token，无前后空白，不截断小数或千位组。"
        "二选一：本span含紧邻原单位则unit=null；或本span仅裸数值并提供unit的原文精确span。"
        "ordered_pair存在时必须用后一形式，且本span必须与values所选元素完全相同。"
    ),
    "unit": (
        "value含单位时必须为null；value为裸数值时选原文明示完整单位span，不能补写。"
        "例如万元/百万元不能只截万；须紧邻值、明确单位表头或完整配对列末共同单位。"
        "合法单位列表只说明可识别写法，不授予把缺失或不支持单位改写成列表项的权限。"
    ),
    "period": (
        "单一原文日历期间span。完整列为2021年H1等时，选整列并原样复制为periods元素。"
        "共同H1配年份列时此处选原文H1，period_year选对应年份列；不得从任务日期补年。"
    ),
    "period_year": (
        "仅在period本身无年份时选同一明确原文年份span；period含年份可为null。"
        "年份列布局中本span必须与periods的对应年份元素完全相同；不能挑另一列的年或合成期间。"
    ),
    "context": (
        "必填的完整最小单主体材料块span，最多2000 Unicode码点。"
        "须覆盖subject、metric、value以及完整两列；通常也含期间/单位及认定指标角色所需词。"
        "可含同主体标题和换行，不能只选指标名或局部行而把主体/数值留在块外。"
    ),
    "shared_context": (
        "仅必要时选紧邻context之前的真实共同年份/单位/报告期表头，最多1000码点、间隔最多500。"
        "不得承载本行主体/指标/数值，不得与context重叠，也不能借其他公司或其他测量的单位。"
        "若所需信息已在context内则为null。"
    ),
    "reporting_scope": ("存在明确原文合并/分部/样本口径时选其span；缺失则null，不补造集团或客户经营口径。"),
    "ordered_pair": (
        "仅原文明确分别/依次/表头列序且期间列、数值列完整等长时填写，否则null或不提议。"
        "periods按原文完整列顺序，values是全部裸数值列；每个观察只能选同一个索引。"
        "元素必须复用实际单元span，不能把全列列表当作一个value，不能省去后续H/Q/年份列。"
    ),
}

INSTRUCTION = (
    "只从本任务text选择逐字字段，start/end为该text内Unicode码点半开区间；"
    "保持原大小写、标点和完整token，不输出计算结果或重建字符串。"
    "每观察仅一个具名主体×指标×日历期间×数值；字段有歧义可不提议，observations=[]合法。"
    "context必须是覆盖具名主体、指标、数值和必要限定的完整最小单主体块，"
    "不是只选指标名；标题与本行分开时context可覆盖标题和本行。"
    "shared_context只供紧邻前方的单位/年份/报告期共同表头，不能用它代替context中的主体。"
    "value/unit严格二选一：value含原单位=>unit=null；裸value=>unit为原单位span。"
    "单位必须原文明示完整，万元/百万元不等于万；支持列表不允许补单位、换币种或改写。"
    "双期间仅明确原始列序可配对：完整年期间列时period复制整列且period_year可null；"
    "共同H1配两年份列时period=同一H1、period_year复制选定年份列。"
    "ordered_pair.periods与values均列完整原文单元，等长同索引；"
    "每条value必须与所选values元素精确相同，period或period_year必须与所选periods元素精确相同，"
    "并将两整列和末尾共同单位放在context内。不得猜缺列、把斜杠当比率或跨期间移值。"
    "没有明确分别/依次/列头，或原文不能归属主体/期间/单位时省略，不编造该关系词。"
    "客户/公司泛称不是具名经营主体，期内须原文唯一报告期，财年无明确日期边界不提议。"
    "预测/估计、收入/确认收入、订单/验收/预收/现金、标准工时/普通小时均保原角色，不升级为实绩。"
    "synthetic_examples是完全独立的合成结构示例；其主体/年份/数值/位置不得复制进本任务输出。"
)


def _require_version(version):
    if version != PROPOSAL_VERSION:
        raise ValueError("unsupported literal narrative proposal version")


def proposal_schema(*, version=PROPOSAL_VERSION):
    """Make the proposal obligations explicit without changing qualification or old schemas."""
    _require_version(version)
    result = copy.deepcopy(narrative_v2.schema())
    result["title"] = "NarrativeProposalV1"
    result["description"] = "Literal selection proposal only; source-bound qualification uses unchanged v2."
    result["required"] = ["observations"]  # An explicitly empty array is still valid.
    result["properties"]["observations"]["description"] = (
        "Only unambiguously bound literal observations; [] is valid. Do not force a nonempty result."
    )
    row = result["$defs"]["NarrativeObservationV2"]
    row["required"] = [*row["required"], "context"]
    for name, description in DESCRIPTIONS.items():
        row["properties"][name]["description"] = description
    row["properties"]["context"] = {
        "$ref": "#/$defs/LiteralSpan",
        "description": DESCRIPTIONS["context"],
    }
    pair = result["$defs"]["OrderedPair"]
    pair["properties"]["periods"]["description"] = (
        "Complete original ordered period columns, or the original year columns for a shared H1/H2. "
        "Reuse the selected whole span in row.period or row.period_year."
    )
    pair["properties"]["values"]["description"] = (
        "Complete original ordered bare-number columns. Reuse the selected exact span as row.value."
    )
    literal = result["$defs"]["LiteralSpan"]
    literal["description"] = "Exact substring of this source text; Unicode code point offsets, end exclusive."
    return result


def _span(text, value):
    start = text.index(value)
    return {"text": value, "start": start, "end": start + len(value)}


def _examples():
    """Tiny fixed synthetic examples; never built from investigation material."""
    examples = []
    for layout, text, name, periods, values in (
        (
            "whole_period_columns",
            "合成澄海公司2021年H1/2022年H1净订单分别为7/11万元。",
            "合成澄海公司",
            ("2021年H1", "2022年H1"),
            ("7", "11"),
        ),
        (
            "year_columns_with_shared_half_year",
            "合成远川公司2021/2022年H1净订单分别为5/8万元。",
            "合成远川公司",
            ("2021", "2022"),
            ("5", "8"),
        ),
    ):
        columns = {
            "periods": [_span(text, period) for period in periods],
            "values": [_span(text, value) for value in values],
        }
        rows = []
        for index in range(2):
            common_half = layout == "year_columns_with_shared_half_year"
            rows.append(
                {
                    "subject": _span(text, name),
                    "metric": _span(text, "净订单"),
                    "value": copy.deepcopy(columns["values"][index]),
                    "unit": _span(text, "万元"),
                    "period": _span(text, "H1") if common_half else copy.deepcopy(columns["periods"][index]),
                    "period_year": copy.deepcopy(columns["periods"][index]) if common_half else None,
                    "context": _span(text, text),
                    "shared_context": None,
                    "reporting_scope": None,
                    "ordered_pair": copy.deepcopy(columns),
                }
            )
        examples.append(
            {
                "is_synthetic_example": True,
                "layout": layout,
                "text": text,
                "output": {"observations": rows},
            }
        )
    return examples


def proposal_payload(record, *, version=PROPOSAL_VERSION):
    """Only new opt-in host calls use this payload; no request or receipt is replayed here."""
    _require_version(version)
    result = copy.deepcopy(narrative_v2.selection_payload(record))
    result.update(
        proposal_version=PROPOSAL_VERSION,
        qualification_contract_version=narrative_v2.VERSION,
        instruction=INSTRUCTION,
        supported_unit_forms=list(UNIT_FORMS),
        synthetic_examples=_examples(),
    )
    return result
