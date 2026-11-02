"""Deterministic arithmetic for analysis results. The model proposes; this module computes."""

import ast
import re
from decimal import Decimal, DivisionByZero, InvalidOperation

NUMBER = re.compile(r"-?\d+(?:[,，]\d{3})*(?:\.\d+)?")


def numbers_in(text: str) -> list[Decimal]:
    return [Decimal(m.group().replace(",", "").replace("，", "")) for m in NUMBER.finditer(text or "")]


def appears(value: float, text: str) -> bool:
    """True when the operand is literally one of the numbers in the quoted source text."""
    target = Decimal(str(value))
    return any(n == target for n in numbers_in(text))


_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}


def evaluate(expression: str, operands: list[Decimal]) -> Decimal:
    """Evaluate +-*/ over x1..xn and plain numbers. Raises ValueError for anything else."""
    names = {f"x{i}": v for i, v in enumerate(operands, 1)}

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -walk(node.operand)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return Decimal(str(node.value))
        if isinstance(node, ast.Name) and node.id in names:
            return names[node.id]
        raise ValueError("expression_not_allowed")

    try:
        return walk(ast.parse(expression, mode="eval"))
    except (SyntaxError, DivisionByZero, InvalidOperation, ZeroDivisionError) as exc:
        raise ValueError("expression_invalid") from exc


def rounded(value: Decimal) -> float:
    return float(round(value, 4))
