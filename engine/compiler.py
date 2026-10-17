"""Semantic query (names only) -> validated -> MetricFlow -> SQL in MetricFlow's dialect (DuckDB).

MetricFlow runs inside this process: its engine is loaded once (a few seconds) and kept, so each
compile takes milliseconds. It is reloaded automatically when the semantic manifest changes.
If the in-process engine cannot be loaded, every compile falls back to the `mf query --explain`
command line (slower: a new process each time). Set CONVERSAI_MF_INPROCESS=0 to force that."""

import contextlib
import datetime as dt
import os
import re
import sys
import threading

from .catalog import Catalog
from .config import MANIFEST, SEMANTIC_DIR, cli_error, run_cli

OPS = {"=", "!=", ">", ">=", "<", "<=", "in", "not in"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class SemanticQueryError(ValueError):
    """The semantic query names something the layer does not define, or is malformed."""


def _literal(v) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    raise SemanticQueryError(f"Unsupported filter value: {v!r}")


def _dim_ref(name: str, cat: Catalog) -> str:
    if name in cat.dimensions:
        return "{{ Dimension('" + name + "') }}"
    base, _, grain = name.rpartition("__")
    if base == "metric_time" or base in cat.time_dimensions:
        return "{{ TimeDimension('" + base + "', '" + grain + "') }}"
    raise SemanticQueryError(f"Unknown dimension: {name}")


def validate(sq: dict, cat: Catalog) -> dict:
    if not isinstance(sq, dict):
        raise SemanticQueryError("Semantic query must be an object.")
    if sq.get("cannot_answer_reason"):
        raise SemanticQueryError("Model could not map the question to governed metrics: "
                                 + sq["cannot_answer_reason"])
    metrics = sq.get("metrics") or []
    if not metrics:
        raise SemanticQueryError("At least one metric is required.")
    bad = [m for m in metrics if m not in cat.metrics]
    if bad:
        raise SemanticQueryError(f"Unknown metric(s): {bad}. Call list_metrics for valid names "
                                 "(or list_definitions: a provisional definition must be approved in this conversation).")
    group_by = sq.get("group_by") or []
    bad = [g for g in group_by if g not in cat.groupable]
    if bad:
        raise SemanticQueryError(f"Unknown group_by: {bad}. Call list_dimensions for valid names "
                                 "(or list_definitions: a provisional definition must be approved in this conversation).")
    where = sq.get("where") or []
    for w in where:
        if w.get("op") not in OPS:
            raise SemanticQueryError(f"Unsupported operator: {w.get('op')}")
        if w.get("dimension") not in cat.groupable:
            raise SemanticQueryError(f"Unknown filter dimension: {w.get('dimension')}")
        if w["op"] in ("in", "not in") and not isinstance(w.get("value"), list):
            raise SemanticQueryError("'in' / 'not in' need a list value.")
    allowed_order = set(metrics) | set(group_by)
    for o in sq.get("order_by") or []:
        if o.lstrip("-") not in allowed_order:
            raise SemanticQueryError(f"order_by '{o}' must be one of the requested metrics or group_by.")
    limit = sq.get("limit")
    if limit is not None and (not isinstance(limit, int) or not 1 <= limit <= 1000):
        raise SemanticQueryError("limit must be an integer between 1 and 1000.")
    return {"metrics": metrics, "group_by": group_by, "where": where,
            "order_by": sq.get("order_by") or [], "limit": limit}


def _date_literal(v) -> str:
    """Time filters get a typed literal (DATE '2026-09-01'): never compared as text, so they do not
    depend on an engine's session date format (Oracle NLS_DATE_FORMAT)."""
    try:
        d = dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        raise SemanticQueryError(f"Time filter value must be an ISO date like 2026-09-01, got {v!r}") from None
    return f"DATE '{d.isoformat()}'"


def where_clause(w: dict, cat: Catalog) -> str:
    ref = _dim_ref(w["dimension"], cat)
    lit = _date_literal if "TimeDimension(" in ref else _literal
    if w["op"] in ("in", "not in"):
        return f"{ref} {w['op'].upper()} (" + ", ".join(lit(v) for v in w["value"]) + ")"
    return f"{ref} {w['op']} {lit(w['value'])}"


_engine = {"mf": None, "cfg": None, "mtime": None, "broken": False}
_engine_lock = threading.Lock()


def _mf_engine():
    """The in-process MetricFlow engine, (re)built when semantic_manifest.json changes. None = use the CLI."""
    if os.environ.get("CONVERSAI_MF_INPROCESS", "1") == "0" or _engine["broken"]:
        return None
    mtime = MANIFEST.stat().st_mtime if MANIFEST.exists() else None
    if _engine["mf"] is not None and _engine["mtime"] == mtime:
        return _engine["mf"]
    try:
        from dbt_metricflow.cli.cli_configuration import CLIConfiguration
        with contextlib.redirect_stdout(sys.stderr):      # stdout is the MCP channel: keep it clean
            cfg = CLIConfiguration()
            cfg.setup(dbt_profiles_path=SEMANTIC_DIR, dbt_project_path=SEMANTIC_DIR, configure_file_logging=False)
            mf = cfg.mf
        _engine.update(mf=mf, cfg=cfg, mtime=mtime)
        return mf
    except Exception as exc:                              # noqa: BLE001 - fall back to the command line
        print(f"[conversai] in-process MetricFlow unavailable, using the CLI: {exc}", file=sys.stderr)
        _engine["broken"] = True
        return None


def warm_up() -> None:
    """Load the MetricFlow engine ahead of the first question (the MCP server calls this at start)."""
    with _engine_lock:
        _mf_engine()


def overlay_engine(semantic_manifest):
    """A MetricFlow engine for another semantic manifest (the governed one plus provisional definitions),
    sharing the governed engine's SQL client. None if the in-process engine is not available."""
    with _engine_lock:
        if _mf_engine() is None:
            return None
        import dbt_metricflow.cli.cli_configuration as cc        # the classes the CLI itself uses
        with contextlib.redirect_stdout(sys.stderr):
            return cc.MetricFlowEngine(semantic_manifest_lookup=cc.SemanticManifestLookup(semantic_manifest),
                                       sql_client=_engine["cfg"].sql_client)


def compile_to_sql(sq: dict, cat: Catalog | None = None, engine=None) -> str:
    """Semantic query -> the SQL MetricFlow generates for it (DuckDB dialect). Nothing is executed.
    engine: an overlay engine (provisional definitions); default = the governed engine."""
    cat = cat or Catalog()
    q = validate(sq, cat)
    wheres = [where_clause(w, cat) for w in q["where"]]
    with _engine_lock:
        mf = engine or _mf_engine()
        if mf is not None:
            from metricflow.engine.metricflow_engine import MetricFlowQueryRequest
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    req = MetricFlowQueryRequest.create(
                        metric_names=q["metrics"], group_by_names=q["group_by"] or None,
                        where_constraints=wheres or None, order_by_names=q["order_by"] or None,
                        limit=q["limit"])
                    return mf.explain(mf_request=req).sql_statement.without_descriptions.sql.strip()
            except Exception as exc:                      # noqa: BLE001 - MetricFlow rejected the query
                raise SemanticQueryError(f"MetricFlow could not compile the query:\n{exc}"[:3000]) from None
    return _compile_with_cli(q, wheres)


def _compile_with_cli(q: dict, wheres: list) -> str:
    """Fallback: run `mf query --explain` as a separate process."""
    args = ["query", "--metrics", ",".join(q["metrics"])]
    if q["group_by"]:
        args += ["--group-by", ",".join(q["group_by"])]
    for w in wheres:
        args += ["--where", w]
    if q["order_by"]:
        args += ["--order", ",".join(q["order_by"])]
    if q["limit"]:
        args += ["--limit", str(q["limit"])]
    args.append("--explain")
    r = run_cli("dbt_metricflow.cli.main", args)
    out = ANSI.sub("", r.stdout or "")
    marker = "generated dataflow plan):"
    if r.returncode != 0 or marker not in out:
        raise SemanticQueryError(f"MetricFlow could not compile the query:\n{cli_error(r)}")
    return out.split(marker, 1)[1].strip()
