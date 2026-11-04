"""A small expression evaluator: comparisons and boolean masks, no Python eval."""

import ast
import numbers
import operator

import pandas as pd

from .errors import DatasetError

_COMPARE = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
}
_ARITHMETIC = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
               ast.Div: operator.truediv, ast.Mod: operator.mod}


class RowFilter:
    def __init__(self, expression: str, columns: list[str]) -> None:
        try:
            if len(expression) > 10000:
                raise ValueError("过滤表达式过长")
            self.tree = ast.parse(expression, mode="eval").body
            nodes = list(ast.walk(self.tree))
            if len(nodes) > 500:
                raise ValueError("过滤表达式过于复杂")
            allowed = (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.List, ast.Tuple,
                       ast.Compare, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.And, ast.Or,
                       ast.Not, ast.Invert, ast.USub, ast.UAdd, ast.BitAnd, ast.BitOr,
                       ast.In, ast.NotIn, ast.Is, ast.IsNot, *tuple(_COMPARE), *tuple(_ARITHMETIC))
            if any(not isinstance(node, allowed) for node in nodes):
                raise ValueError("仅支持比较、算术、in/not in 和布尔组合；不允许函数调用或属性访问")
            # Python 的 `x in Series` 检查的是索引而不是值，会静默给出错误结果。
            for node in nodes:
                if isinstance(node, ast.Compare):
                    for op, comparator in zip(node.ops, node.comparators):
                        if isinstance(op, (ast.In, ast.NotIn)) and not (
                                isinstance(comparator, (ast.List, ast.Tuple))
                                and not any(isinstance(n, ast.Name) for n in ast.walk(comparator))):
                            raise ValueError("in/not in 右侧必须是常量列表，如 SEX in ['F', 'M']")
            self.columns = {node.id for node in nodes if isinstance(node, ast.Name)}
            missing = self.columns - set(columns)
            if missing:
                raise ValueError(f"未知变量: {sorted(missing)}")
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise DatasetError(f"where 条件无法应用: {exc}", "INVALID_FILTER") from exc

    def apply(self, frame: pd.DataFrame) -> pd.Series:
        def visit(node):
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.Name):
                return frame[node.id]
            if isinstance(node, (ast.List, ast.Tuple)):
                return [visit(value) for value in node.elts]
            if isinstance(node, ast.Compare):
                left = visit(node.left)
                result = True
                for op, comparator in zip(node.ops, node.comparators):
                    right = visit(comparator)
                    if isinstance(op, (ast.Is, ast.IsNot)):
                        if right is not None:
                            raise ValueError("is/is not 仅支持 None")
                        part = left.isna() if isinstance(left, pd.Series) else left is None
                        if isinstance(op, ast.IsNot):
                            part = ~part if isinstance(part, pd.Series) else not part
                    elif isinstance(op, (ast.In, ast.NotIn)):
                        part = left.isin(right) if isinstance(left, pd.Series) else left in right
                        if isinstance(op, ast.NotIn):
                            part = ~part if isinstance(part, pd.Series) else not part
                        if isinstance(left, pd.Series):
                            part = part & left.notna()
                    else:
                        part = _COMPARE[type(op)](left, right)
                        # pandas treats NaN != x as True; missing operands never match.
                        if isinstance(left, pd.Series):
                            part = part & left.notna()
                        if isinstance(right, pd.Series):
                            part = part & right.notna()
                    result = result & part
                    left = right
                return result
            if isinstance(node, ast.BoolOp):
                values = [visit(value) for value in node.values]
                result = values[0]
                for value in values[1:]:
                    result = (result & value) if isinstance(node.op, ast.And) else (result | value)
                return result
            if isinstance(node, ast.BinOp):
                fn = {ast.BitAnd: operator.and_, ast.BitOr: operator.or_}.get(type(node.op))
                left, right = visit(node.left), visit(node.right)
                if fn is None:
                    for value in (left, right):
                        if not (isinstance(value, numbers.Number) or
                                isinstance(value, pd.Series) and pd.api.types.is_numeric_dtype(value.dtype)):
                            raise ValueError("算术仅支持数值，不允许字符串或列表重复")
                return (fn or _ARITHMETIC[type(node.op)])(left, right)
            value = visit(node.operand)
            if isinstance(node.op, (ast.Not, ast.Invert)):
                return ~value if isinstance(value, pd.Series) else not value
            return -value if isinstance(node.op, ast.USub) else +value

        try:
            result = visit(self.tree)
            if not isinstance(result, pd.Series):
                result = pd.Series(bool(result), index=frame.index)
            if not pd.api.types.is_bool_dtype(result.dtype):
                raise ValueError("过滤表达式必须产生布尔结果")
            return result.fillna(False)
        except (TypeError, ValueError, KeyError, ZeroDivisionError, RecursionError) as exc:
            raise DatasetError(f"where 条件无法应用: {exc}", "INVALID_FILTER") from exc
