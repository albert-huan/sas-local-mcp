"""Conservative SQL column provenance; expressions and nested scopes stay raw."""

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

# Nodes that can pull in columns without naming them; older sqlglot lacks some classes.
_EXPANDING = tuple(getattr(exp, name) for name in ("Pivot", "Columns", "PositionalColumn", "Parameter")
                   if hasattr(exp, name))


def referenced_columns(sql: str, tables: dict[str, list[str]],
                       extra: set[str] = frozenset()) -> dict[str, list[str]] | None:
    """Columns each registered table must supply, or None when that cannot be proven.

    Over-inclusion is harmless; omission would change results, so anything that can
    expand implicitly (stars, COLUMNS(), NATURAL joins, PIVOT, positional refs,
    columns used as whole-row structs) falls back to reading every column.
    """
    try:
        tree = parse_one(sql, read="duckdb")
    except (SqlglotError, RecursionError):
        return None
    if not isinstance(tree, exp.Query):
        return None
    relation_names = {name.lower() for name in tables}
    relation_names |= {str(t.alias_or_name).lower() for t in tree.find_all(exp.Table, exp.TableAlias, exp.CTE)}
    identifiers: set[str] = {name.lower() for name in extra}
    for node in tree.walk():
        if isinstance(node, exp.Star) and not isinstance(node.parent, exp.Count):  # COUNT(*) 不展开列
            return None
        if isinstance(node, _EXPANDING):
            return None
        if isinstance(node, exp.Join) and str(node.args.get("method") or "").upper() in {"NATURAL", "POSITIONAL"}:
            return None
        if isinstance(node, exp.Anonymous) and str(node.name).upper() in {"COLUMNS", "UNPACK"}:
            return None
        # 表函数（pragma_table_info 等）与系统目录（information_schema）会读到整张表的结构，
        # 却不会在 SQL 里点名任何列；带库名/模式名的引用同理，都按全列读取。
        if isinstance(node, exp.Table) and (node.db or node.catalog or not isinstance(node.this, exp.Identifier)):
            return None
        if isinstance(node, exp.Identifier):
            identifiers.add(str(node.this).lower())
        # A bare table name used as a column means a whole-row struct.
        if isinstance(node, exp.Column) and not node.table and node.name.lower() in relation_names:
            return None
    pruned = {}
    for name, columns in tables.items():
        keep = [col for col in columns if col.lower() in identifiers]
        pruned[name] = keep or columns[:1]  # 至少一列，保持行数
    return pruned


def inspect_query(sql: str, tables: dict[str, list[str]]) -> tuple[dict[str, tuple[str, str]], bool]:
    try:
        tree = parse_one(sql, read="duckdb")
    except (SqlglotError, RecursionError):
        return {}, False
    ordered = bool(tree.args.get("order"))
    if not isinstance(tree, exp.Select) or tree.args.get("with_") or tree.args.get("with"):
        return {}, ordered
    source = tree.args.get("from_") or tree.args.get("from")
    relations = ([source.this] if source is not None else []) + [j.this for j in tree.args.get("joins", [])]
    aliases = {}
    for relation in relations:
        if (not isinstance(relation, exp.Table) or relation.db or relation.catalog
                or relation.args.get("pivots")):
            return {}, ordered
        name = next((t for t in tables if t.lower() == relation.name.lower()), None)
        if name is None:
            return {}, ordered
        aliases[relation.alias_or_name.lower()] = name

    def resolve(column):
        candidates = ([aliases.get(column.table.lower())] if column.table else list(aliases.values()))
        hits = [(table, col) for table in candidates if table is not None
                for col in tables[table] if col.lower() == column.name.lower()]
        return hits[0] if len(hits) == 1 else None

    outputs: list[tuple[str, tuple[str, str] | None]] = []
    for projection in tree.expressions:
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(expression, exp.Star) or (isinstance(expression, exp.Column) and expression.is_star):
            star = expression.this if isinstance(expression, exp.Column) else expression
            if any(star.args.values()):
                return {}, ordered
            selected = ([aliases.get(expression.table.lower())] if isinstance(expression, exp.Column)
                        else list(aliases.values()))
            for table in selected:
                if table:
                    outputs.extend((col, (table, col)) for col in tables[table])
        else:
            outputs.append((projection.alias_or_name, resolve(expression) if isinstance(expression, exp.Column) else None))
    # Ambiguous/duplicate output names must never borrow another column's labels.
    counts = {}
    for name, _ in outputs:
        counts[name.lower()] = counts.get(name.lower(), 0) + 1
    return {name: origin for name, origin in outputs if origin and counts[name.lower()] == 1}, ordered
