"""Conservative SQL column provenance; expressions and nested scopes stay raw."""

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError


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
