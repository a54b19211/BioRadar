"""Stage compute: the model proposes calculations over quoted numbers; the program checks every operand
against its quote and computes."""

from decimal import Decimal
from typing import TYPE_CHECKING

from bioradar.research import compute as arithmetic
from bioradar.research import prompts, schemas

if TYPE_CHECKING:
    from bioradar.research.pipeline import Run


def compute(run: "Run"):
    numeric = [f for f in run.p["facts"] if arithmetic.numbers_in(f["quote"])]
    run.p["computations"] = []
    if not run.p.get("needs_calculation") or len(numeric) < 2:
        return
    result = run.ask(
        "calculate",
        prompts.CALCULATE,
        {
            "question": run.asked,
            "corrections": run.p["relations"],
            "facts": [
                {"id": f["id"], "ref": f["ref"], "text": f["text"], "quote": f["quote"]} for f in numeric
            ],
        },
        schemas.Calculations,
        carries=run.sources_of(f["ref"] for f in numeric),
    )
    facts = {f["id"]: f for f in numeric}
    for calc in (result.calculations if result else [])[:10]:
        rejected = rejection(calc, facts)
        if rejected:
            run.note(*rejected)
            continue
        try:
            value = arithmetic.evaluate(calc.expression, [Decimal(str(o.value)) for o in calc.operands])
        except ValueError as exc:
            run.note(str(exc), calc.label)
            continue
        run.p["computations"].append(
            {
                "id": f"A{len(run.p['computations']) + 1}",
                "label": calc.label,
                "value": arithmetic.rounded(value),
                "expression": calc.expression,
                "unit": calc.unit,
                "period": calc.period,
                "population": calc.population,
                "denominator": calc.denominator,
                "operands": [
                    # The quote travels with the result so BioCheck can check it without the record.
                    {
                        "fact": o.fact,
                        "value": o.value,
                        "ref": facts[o.fact]["ref"],
                        "quote": facts[o.fact]["quote"],
                    }
                    for o in calc.operands
                ],
            }
        )


def rejection(calc, facts):
    """(note code, detail) when a proposed calculation is not computed: an operand not in its fact's quote,
    or a percentage whose numbers' units cannot be read from their own quotes or differ."""
    if not calc.operands or any(
        o.fact not in facts or not arithmetic.appears(o.value, facts[o.fact]["quote"]) for o in calc.operands
    ):
        return "calculation_operand_not_in_source", calc.label
    share = calc.unit.strip() in {"%", "百分比"} or any(w in calc.label for w in ("比例", "占比"))
    if share and len(calc.operands) > 1:
        # a percentage needs every number's unit, read from that number's own quote, and one unit
        # for all (2 devices / 3 institutions is no proportion, C05); a unit the model only says
        # it saw does not count. The program does not check that the numerator is part of the
        # denominator's population: that stays with the reader and BioCheck.
        units = [o.unit.strip() for o in calc.operands]
        seen = all(u and u in facts[o.fact]["quote"] for u, o in zip(units, calc.operands))
        if not seen or len(set(units)) > 1:
            code = "calculation_mixed_units" if seen else "calculation_unit_unverified"
            return code, f"{calc.label}: {'、'.join(u or '?' for u in units)}"
    return None
