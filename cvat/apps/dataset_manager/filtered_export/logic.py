# SPDX-License-Identifier: MIT
"""Bounded evaluator for the JsonLogic emitted by the annotation query builder.

Only comparison, membership and boolean rules are accepted. No Python eval,
custom functions, traversal of object attributes, or arbitrary JsonLogic code.
"""

import math
import re
from decimal import Decimal

SCALARS = (str, int, float, bool, type(None))
ARITY = {
    "and": (1, 100), "or": (1, 100), "!": (1, 1), "!!": (1, 1),
    "==": (2, 2), "!=": (2, 2), "===": (2, 2), "!==": (2, 2),
    ">": (2, 2), ">=": (2, 2), "<": (2, 3), "<=": (2, 3), "in": (2, 2),
}


def validate_rule(rule, fields: set[str]) -> None:
    remaining = 512

    def visit(node, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 16:
            raise ValueError("Filter is too complex")
        if isinstance(node, dict):
            if len(node) != 1:
                raise ValueError("Each filter expression must have exactly one operator")
            op, args = next(iter(node.items()))
            if op == "var":
                field = args[0] if isinstance(args, list) and len(args) == 1 else args
                if not isinstance(field, str) or field not in fields:
                    raise ValueError(f"Unknown or unsupported filter field: {field!r}")
                return
            if op not in ARITY:
                raise ValueError(f"Unsupported filter operator: {op}")
            args = args if isinstance(args, list) else [args]
            low, high = ARITY[op]
            if not low <= len(args) <= high:
                raise ValueError(f"Invalid number of operands for {op}")
            for index, arg in enumerate(args):
                if isinstance(arg, list) and not (op == "in" and index == 1):
                    raise ValueError("Literal lists are only supported on the right of in")
                visit(arg, depth + 1)
        elif isinstance(node, list):
            if len(node) > 100 or any(type(item) not in SCALARS for item in node):
                raise ValueError("Membership lists must contain at most 100 scalar values")
            for arg in node:
                visit(arg, depth + 1)
        elif type(node) not in SCALARS or (isinstance(node, float) and not math.isfinite(node)):
            raise ValueError("Filter values must be finite JSON scalars")
        elif isinstance(node, str) and len(node) > 4096:
            raise ValueError("Filter value is too long")

    if not isinstance(rule, dict):
        raise ValueError("An annotation filter must be a JsonLogic object")
    if rule:
        visit(rule)


def number(value):
    """JavaScript Number coercion for scalar attribute/comparison values."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except OverflowError:
            return math.inf if value > 0 else -math.inf
    if isinstance(value, str):
        # ECMAScript whitespace differs from Python str.strip(), notably BOM
        # and the ASCII information separators. Numeric separators are invalid.
        value = value.strip(" \t\r\n\v\f\u00a0\u1680\u2000\u2001\u2002\u2003"
                            "\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028"
                            "\u2029\u202f\u205f\u3000\ufeff")
        if not value:
            return 0.0
        for pattern, base in ((r"0[xX][0-9a-fA-F]+", 16), (r"0[bB][01]+", 2), (r"0[oO][0-7]+", 8)):
            if re.fullmatch(pattern, value):
                return number(int(value[2:], base))
        if re.fullmatch(r"[+-]?(?:Infinity|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)", value):
            return float(value)
    return math.nan


def truthy(value):
    # JsonLogic deliberately treats [] as false, unlike JavaScript itself.
    if isinstance(value, float) and math.isnan(value):
        return False
    return bool(value)


def _strict_equal(left, right):
    if type(left) in (int, float) and type(right) in (int, float):
        return number(left) == number(right)
    return type(left) is type(right) and left == right


def _equal(left, right):
    if left is None or right is None:
        return left is right
    if type(left) is type(right) and type(left) not in (int, float):
        return left == right
    return number(left) == number(right)


def _string(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if type(value) in (int, float):
        value = number(value)
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value == 0:
            return "0"
        if 1e-6 <= abs(value) < 1e21:
            return format(Decimal(repr(value)), "f").rstrip("0").rstrip(".") if not value.is_integer() else str(int(value))
        return re.sub(r"e([+-])0+", r"e\1", repr(value))
    return str(value)


def evaluate(rule, data):
    if not isinstance(rule, dict):
        return rule
    if not rule:
        return True
    op, args = next(iter(rule.items()))
    args = args if isinstance(args, list) else [args]
    if op == "var":
        value = data
        for part in args[0].split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return value
    if op in ("and", "or"):
        for arg in args:
            value = evaluate(arg, data)
            if (op == "and" and not truthy(value)) or (op == "or" and truthy(value)):
                return value
        return value
    values = [evaluate(arg, data) for arg in args]
    if op in ("!", "!!"):
        return not truthy(values[0]) if op == "!" else truthy(values[0])
    left, right = values[:2]
    if op in ("==", "!=", "===", "!=="):
        result = _strict_equal(left, right) if len(op) == 3 else _equal(left, right)
        return not result if op.startswith("!") else result
    if op == "in":
        if isinstance(right, list):
            return any(_strict_equal(left, item) for item in right)
        return bool(right) and _string(left) in right if isinstance(right, str) else False
    comparisons = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
                   ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}
    for a, b in zip(values, values[1:]):
        if isinstance(a, str) and isinstance(b, str):
            # JavaScript compares UTF-16 code units, not Unicode code points.
            a, b = a.encode("utf-16-be", "surrogatepass"), b.encode("utf-16-be", "surrogatepass")
        else:
            a, b = number(a), number(b)
        if not comparisons[op](a, b):
            return False
    return True
