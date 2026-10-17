"""Bind logical models to physical tables for a target, and translate SQL to its dialect.

MetricFlow writes DuckDB SQL against the logical dbt models (account, account_cycle).
For each target we (1) swap those for the physical tables in targets.yaml and
(2) translate to the target dialect, with fix-ups for dialects MetricFlow lacks (Oracle)."""

import re

import sqlglot
from sqlglot import exp

SOURCE_DIALECT = "duckdb"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}( 00:00:00)?$")
ORACLE_TRUNC = {"YEAR": "YYYY", "QUARTER": "Q", "MONTH": "MM", "WEEK": "IW", "DAY": "DD"}


class BindingError(ValueError):
    pass


def _physical(physical: str, dialect: str) -> exp.Expression:
    """'main.account_dim' or "read_parquet('...')" -> a table expression in `dialect`."""
    return sqlglot.parse_one(f"SELECT * FROM {physical}", read=dialect).find(exp.From).this


def _oracle_fixups(tree: exp.Expression) -> None:
    # Oracle identifiers may not start with "_" (MetricFlow uses __measure aliases)
    _rename_underscore_identifiers(tree)
    # CAST('2026-09-01' AS DATE) -> TO_DATE('2026-09-01', 'YYYY-MM-DD'): Oracle would parse the
    # string with the session's NLS_DATE_FORMAT, which differs between installs
    for node in list(tree.find_all(exp.Cast)):
        if (node.to.this in (exp.DataType.Type.DATE, exp.DataType.Type.TIMESTAMP)
                and isinstance(node.this, exp.Literal) and node.this.is_string
                and ISO_DATE.match(node.this.this)):
            node.replace(exp.Anonymous(this="TO_DATE", expressions=[
                exp.Literal.string(node.this.this[:10]), exp.Literal.string("YYYY-MM-DD")]))
    # DATE_TRUNC('month', d) -> TRUNC(d, 'MM')
    for node in list(tree.find_all(exp.DateTrunc, exp.TimestampTrunc)):
        unit = node.args.get("unit")
        unit = (unit.name if isinstance(unit, exp.Expression) else str(unit)).upper().strip("'")
        if unit not in ORACLE_TRUNC:
            raise BindingError(f"No Oracle TRUNC format for time grain {unit}")
        node.replace(exp.Anonymous(this="TRUNC", expressions=[node.this, exp.Literal.string(ORACLE_TRUNC[unit])]))


def _rename_underscore_identifiers(tree: exp.Expression) -> None:
    """MetricFlow's internal aliases start with "__" (e.g. __accounts). Oracle forbids a leading "_";
    BigQuery reserves several "_"/"__" prefixes. Prefix them with "x" on both."""
    for ident in tree.find_all(exp.Identifier):
        if ident.name.startswith("_"):
            ident.set("this", "x" + ident.name)


def _bigquery_fixups(tree: exp.Expression) -> None:
    # INTERVAL '1' MONTH -> INTERVAL 1 MONTH (BigQuery's single-part interval takes a number)
    for iv in tree.find_all(exp.Interval):
        val = iv.this
        if isinstance(val, exp.Literal) and val.is_string and val.this.lstrip("-").isdigit():
            iv.set("this", exp.Literal.number(int(val.this)))
    # d - INTERVAL n unit  ->  DATE_SUB(d, INTERVAL n unit); d + INTERVAL -> DATE_ADD. On a DATE, BigQuery's
    # "+"/"-" with an INTERVAL returns a DATETIME; DATE_SUB / DATE_ADD keep it a DATE.
    for node in list(tree.find_all(exp.Sub, exp.Add)):
        iv = node.expression
        if isinstance(iv, exp.Interval) and iv.args.get("unit") is not None:
            fn = exp.DateSub if isinstance(node, exp.Sub) else exp.DateAdd
            node.replace(fn(this=node.this, expression=iv.this, unit=exp.var(iv.args["unit"].name.upper())))
    # GROUP BY <expression that is also in the SELECT list> -> GROUP BY <position>. sqlglot's BigQuery output
    # otherwise groups by the column's alias, which is ambiguous when a joined table has a column of the
    # same name (MetricFlow's COALESCE(a.metric_time__month, b.metric_time__month) AS metric_time__month).
    for sel in tree.find_all(exp.Select):
        group = sel.args.get("group")
        if not group:
            continue
        outs = [e.unalias() if isinstance(e, exp.Alias) else e for e in sel.expressions]
        group.set("expressions", [
            exp.Literal.number(outs.index(g) + 1) if g in outs else g for g in group.expressions])
    _rename_underscore_identifiers(tree)


def bind_and_translate(sql: str, target: dict) -> str:
    tree = sqlglot.parse_one(sql, read=SOURCE_DIALECT)
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    dialect = target["dialect"]
    for tbl in list(tree.find_all(exp.Table)):
        name = tbl.name
        if name in ctes or not name:
            continue
        if name not in target["tables"]:
            raise BindingError(f"Table '{name}' has no binding for this target in targets.yaml")
        phys = _physical(target["tables"][name], dialect)
        if tbl.alias:
            phys.set("alias", exp.TableAlias(this=exp.to_identifier(tbl.alias)))
        tbl.replace(phys)
    if dialect == "oracle":
        _oracle_fixups(tree)
    elif dialect == "bigquery":
        _bigquery_fixups(tree)
    return tree.sql(dialect=dialect, pretty=True)
