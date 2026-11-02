from decimal import Decimal

import pytest

from bioradar.research import compute


def test_numbers_with_thousands_separators():
    assert compute.numbers_in("订单 6,400 万元，共 32 台，利用率 53.2%") == [
        Decimal("6400"),
        Decimal("32"),
        Decimal("53.2"),
    ]


def test_operand_must_literally_appear_in_quote():
    assert compute.appears(30000, "上半年实际工时30000小时")
    assert not compute.appears(3000, "上半年实际工时30000小时")


def test_evaluate_arithmetic():
    assert compute.evaluate("x1 / x2 * 100", [Decimal(33250), Decimal(62500)]) == Decimal("53.2")


@pytest.mark.parametrize("expression", ["__import__('os')", "x1 ** 2", "x9 + 1", "x1 / 0"])
def test_evaluate_rejects_anything_else(expression):
    with pytest.raises(ValueError):
        compute.evaluate(expression, [Decimal(2)])
