"""Deterministic SQL simplification (no LLM): rewrite MetricFlow's SQL into an equivalent, leaner query
before it is translated for a target and run.

MetricFlow already applies its own strongest SQL optimizer (level O5: column pruning, sub-query merging).
On top of that, sqlglot's rule-based optimizer pushes filters down, prunes unused columns, merges and removes
redundant sub-queries and joins, and folds constant expressions - all transformations that keep the result
identical by construction. Nothing here guesses or asks a model.

Safety:
  - it rewrites the logical SQL (tables `account`, `account_cycle`, `metricflow_time_spine`) using the real
    column list (semantic/columns.yml); any error -> the original SQL is used, unchanged
  - pipeline.plan() lets the target check the simplified SQL; if that check fails, or a BigQuery dry run says
    it would scan MORE bytes than the original, the original is used
  - the original MetricFlow SQL is kept in the plan (sql_original), so you can always compare the two

  CONVERSAI_SIMPLIFY=off   turns it off (the original MetricFlow SQL runs)."""

import os

import sqlglot
import yaml
from sqlglot.optimizer import optimize
from sqlglot.optimizer.eliminate_ctes import eliminate_ctes
from sqlglot.optimizer.eliminate_joins import eliminate_joins
from sqlglot.optimizer.merge_subqueries import merge_subqueries
from sqlglot.optimizer.optimize_joins import optimize_joins
from sqlglot.optimizer.pushdown_predicates import pushdown_predicates
from sqlglot.optimizer.pushdown_projections import pushdown_projections
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.simplify import simplify
from sqlglot.optimizer.unnest_subqueries import unnest_subqueries

from .config import SEMANTIC_DIR

# Rules applied, in order. Left out on purpose: quote_identifiers (keeps names unquoted, so Oracle and
# BigQuery resolve them as before), normalize (can blow up boolean expressions), annotate_types and
# canonicalize (would add casts based on declared types), eliminate_subqueries (turns sub-queries into CTEs:
# longer, not faster).
RULES = (qualify, pushdown_projections, unnest_subqueries, pushdown_predicates, optimize_joins,
         merge_subqueries, eliminate_joins, eliminate_ctes, simplify)
SOURCE_DIALECT = "duckdb"                     # MetricFlow writes DuckDB SQL; dialects.py translates afterwards
_schema = {}


def enabled() -> bool:
    return os.environ.get("CONVERSAI_SIMPLIFY", "on").strip().lower() not in ("off", "0", "false", "no")


def _type(t: str) -> str:
    t = (t or "VARCHAR").upper()
    return "DECIMAL(38,4)" if t.startswith("DECIMAL") else t


def schema() -> dict:
    """Column names and types of the logical tables, as MetricFlow names them ("cards"."main"."<table>")."""
    if not _schema:
        cols = (yaml.safe_load((SEMANTIC_DIR / "columns.yml").read_text(encoding="utf-8")) or {})["columns"]
        tables = {t: {c: _type(v.get("type")) for c, v in cols[t].items()} for t in ("account", "account_cycle")}
        tables["metricflow_time_spine"] = {"date_day": "DATE"}
        _schema.update({"cards": {"main": tables}})
    return _schema


def _collapse_nested_trunc(tree):
    """DATE_TRUNC('MONTH', DATE_TRUNC('MONTH', d)) -> DATE_TRUNC('MONTH', d): truncating twice to the same
    unit is the same as truncating once (MetricFlow nests them for time dimensions with a grain)."""
    from sqlglot import exp
    for node in list(tree.find_all(exp.DateTrunc, exp.TimestampTrunc)):
        inner = node.this
        if type(inner) is type(node) and str(inner.args.get("unit")).upper() == str(node.args.get("unit")).upper():
            node.set("this", inner.this)
    return tree


def simplify_sql(mf_sql: str) -> tuple:
    """(simplified logical SQL, None) or (None, reason it was not simplified)."""
    if not enabled():
        return None, "off (CONVERSAI_SIMPLIFY=off)"
    try:
        tree = sqlglot.parse_one(mf_sql, read=SOURCE_DIALECT)
        out = optimize(tree, schema=schema(), dialect=SOURCE_DIALECT, rules=RULES, identify=False)
        out = _collapse_nested_trunc(out).sql(dialect=SOURCE_DIALECT)
    except Exception as exc:  # noqa: BLE001 - any doubt: keep MetricFlow's SQL
        return None, f"not simplified ({type(exc).__name__}: {str(exc)[:120]})"
    return out, None
