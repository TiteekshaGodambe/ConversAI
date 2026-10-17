"""Proposed (provisional) definitions: how the semantic layer grows from questions it cannot answer yet.

When a question needs a concept the governed layer does not define (say "accounts whose bureau score dropped
by 50+ points"), the agent does NOT write SQL. It proposes a DEFINITION in the layer's own terms:

  dimension  a categorical attribute of `account` or `account_cycle`, as a row-level SQL expression
             (bands, flags, trends against the previous cycle with LAG)
  metric     simple  (an aggregation of a row-level expression on account_cycle: sum, average, ...)
             ratio   (numerator / denominator, both existing metrics)
             derived (an arithmetic expression over existing metrics)

check() proves the proposal is safe and well-formed before a person ever sees it:
  1. shape    - a new snake_case name, a plain-English description, an allowed kind
  2. whitelist- the expression is parsed with sqlglot and every node is checked: only GOVERNED columns of
                that table (the ones the layer already reads), only simple functions (CASE, COALESCE,
                ABS, ROUND, GREATEST, ...), LAG/LEAD only over the account's own cycles, no aggregates,
                no subqueries, no other tables
  3. compile  - the definition is added to an in-memory copy of the semantic manifest, MetricFlow
                validates the copy and compiles a preview query with it
  4. target   - the preview SQL is checked by the target engine (BigQuery dry run / DuckDB EXPLAIN)

A person approves it (MCP dialog). It is then PROVISIONAL: logged (with the question, choices, approval
time), usable in that conversation, and every answer that uses it is labelled. Other conversations may reuse
it after approving it again. A data owner later promotes it into semantic/models/*.yml (python cli.py
definitions promote <id>) - from then on it is governed - or rejects it with a reason.

The SQL that runs is still written by MetricFlow; the agent only wrote the meaning."""

import copy
import datetime as dt
import json
import re
import secrets
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp

from .catalog import Catalog, ensure_manifest
from .config import MANIFEST, SEMANTIC_DIR, definitions_location, load_doc, load_targets, update_doc

MODELS = ("account", "account_cycle")
AGGS = ("sum", "count", "count_distinct", "average", "min", "max")
NAME = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
MAX_EXPR = 4000
PARTITION_COL, ORDER_COL = "ACCOUNT_KEY", "BILLING_CYCLE_DATE"

# Row-level expressions (dimensions, simple-metric expressions)
ROW_NODES = (exp.Column, exp.Identifier, exp.Literal, exp.Boolean, exp.Null, exp.Paren, exp.Case, exp.If,
             exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.And, exp.Or, exp.Not, exp.Is, exp.In,
             exp.Between, exp.Like, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg, exp.Coalesce,
             exp.Nullif, exp.Abs, exp.Greatest, exp.Least, exp.Round, exp.Floor, exp.Ceil, exp.Cast,
             exp.DataType, exp.Window, exp.Lag, exp.Lead, exp.Order, exp.Ordered)
# Derived-metric expressions: arithmetic over metric names
DERIVED_NODES = (exp.Column, exp.Identifier, exp.Literal, exp.Null, exp.Paren, exp.Case, exp.If, exp.EQ,
                 exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.And, exp.Or, exp.Not, exp.Is, exp.Add,
                 exp.Sub, exp.Mul, exp.Div, exp.Neg, exp.Coalesce, exp.Nullif, exp.Abs, exp.Greatest,
                 exp.Least, exp.Round)
FRIENDLY = {"Sum": "SUM", "Avg": "AVG", "Count": "COUNT", "Max": "MAX", "Min": "MIN", "Select": "a subquery",
            "Subquery": "a subquery", "Table": "another table", "WindowSpec": "a window frame (ROWS/RANGE ...)",
            "Anonymous": "this function", "DPipe": "|| (string concatenation)", "Block": "more than one statement",
            "Drop": "DROP", "Insert": "INSERT", "Delete": "DELETE", "Update": "UPDATE", "Create": "CREATE",
            "ReadParquet": "reading files", "CurrentDate": "today's date (results would change daily)"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class DefinitionError(ValueError):
    """The proposal is not acceptable; the message says why and what to change."""


# ================================================================== the log (local file or Cloud Storage)
def load_log() -> dict:
    return load_doc(definitions_location())


def _update_log(change) -> dict:
    return update_doc(definitions_location(), change, "definitions log")


def list_definitions(status: str | None = None) -> list:
    out = [e for e in load_log().values() if status in (None, e.get("status"))]
    return sorted(out, key=lambda e: e.get("created_at", ""), reverse=True)


def get(def_id: str) -> dict:
    e = load_log().get(def_id)
    if not e:
        raise DefinitionError(f"No definition '{def_id}'. Call list_definitions for the ids.")
    return e


def record_use(def_ids) -> None:
    """Count how often provisional definitions are used (tells the data owner what to promote first)."""
    ids = [i for i in def_ids or [] if i]
    if not ids:
        return
    now = dt.datetime.now().isoformat(timespec="seconds")

    def change(log):
        for i in ids:
            if i in log:
                log[i]["uses"] = log[i].get("uses", 0) + 1
                log[i]["last_used"] = now
    try:
        _update_log(change)
    except Exception:  # noqa: BLE001 - counting must never break an answer
        pass


# ================================================================== what a proposal may use
def _base_manifest() -> dict:
    ensure_manifest()
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def allowed_columns() -> dict:
    """{table: [columns]} a proposal may read: the governed columns (the ones the semantic layer already uses,
    plus targets.yaml exports.extra_columns). These are also exactly the columns an export copies, so a
    provisional definition works on an export too."""
    from .exports import governed_columns
    extra = (load_targets().get("exports") or {}).get("extra_columns")
    return governed_columns(extra)


def column_notes() -> dict:
    path = SEMANTIC_DIR / "columns.yml"
    if not path.exists():
        return {}
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("columns") or {}


def guide() -> dict:
    """What the agent needs to write a proposal: kinds, rules, the governed columns with meanings, examples."""
    notes = column_notes()
    cols = {t: {c: (notes.get(t, {}).get(c) or {}).get("description", "") for c in cs}
            for t, cs in allowed_columns().items()}
    return {
        "when": "Only when no governed metric or dimension (list_metrics / list_dimensions) can express the "
                "question. Reuse a provisional definition from this list first if one fits.",
        "kinds": {
            "dimension": "model (account | account_cycle) + expr: a row-level SQL expression giving a category. "
                         "Comparisons with the account's previous cycle: LAG(col) OVER (PARTITION BY ACCOUNT_KEY "
                         "ORDER BY BILLING_CYCLE_DATE) (account_cycle only).",
            "metric/simple": "model account_cycle + agg (sum | count | count_distinct | average | min | max) + expr "
                             "(row-level) + snapshot (true = balance/score at each account's latest cycle in the "
                             "period, like the governed balance metrics; false = summed over every cycle, like "
                             "payments).",
            "metric/ratio": "numerator + denominator: existing metric names.",
            "metric/derived": "expr over metric names + metrics: the metric names used.",
        },
        "rules": [
            "Use only the governed columns below, of the definition's own table (no joins, no other tables).",
            "Allowed: CASE, comparisons, AND/OR/NOT, IN, BETWEEN, LIKE, + - * / %, COALESCE, NULLIF, ABS, ROUND, "
            "FLOOR, CEIL, GREATEST, LEAST, CAST; LAG/LEAD(col[, n]) OVER (PARTITION BY ACCOUNT_KEY ORDER BY "
            "BILLING_CYCLE_DATE). No aggregates inside expr (use agg), no subqueries, no window frames.",
            "Write description as the plain-English business meaning the user will approve. Put every "
            "interpretation you chose (thresholds, what counts as 'improved', how missing values are treated) "
            "in choices, so the user can see and correct them.",
            "Category values: short words; prefix a sort number if the order matters ('1 low', '2 high').",
            "Treat the first cycle (no previous cycle, LAG IS NULL) explicitly, e.g. as 'n/a'.",
        ],
        "governed_columns": cols,
        "examples": [
            {"name": "bureau_score_drop_50", "kind": "dimension", "model": "account_cycle",
             "label": "Bureau score dropped 50+ points",
             "description": "Y if the bureau score fell by 50 points or more since the account's previous cycle, "
                            "N if not, n/a for its first cycle.",
             "expr": "CASE WHEN LAG(BUREAU_SCORE) OVER (PARTITION BY ACCOUNT_KEY ORDER BY BILLING_CYCLE_DATE) IS NULL "
                     "THEN 'n/a' WHEN LAG(BUREAU_SCORE) OVER (PARTITION BY ACCOUNT_KEY ORDER BY BILLING_CYCLE_DATE) "
                     "- BUREAU_SCORE >= 50 THEN 'Y' ELSE 'N' END",
             "choices": ["A drop of exactly 50 points counts", "Compared with the previous cycle only"]},
            {"name": "payment_to_balance_ratio", "kind": "metric", "metric_type": "ratio",
             "label": "Payments per dollar of balance", "numerator": "total_payments", "denominator": "total_balance",
             "description": "Payments in the period divided by the balance at each account's latest cycle.",
             "choices": ["Balance = latest cycle in the period, not the average balance"]},
        ],
    }


# ================================================================== 1-2: shape and expression checks
def _clean(spec: dict) -> dict:
    s = {k: v for k, v in (spec or {}).items() if v not in (None, "", [])}
    for k in ("name", "kind", "model", "metric_type", "agg", "numerator", "denominator"):
        if isinstance(s.get(k), str):
            s[k] = s[k].strip().lower()
    if isinstance(s.get("expr"), str):
        s["expr"] = " ".join(s["expr"].split())
    s["choices"] = [str(c) for c in s.get("choices") or []]
    s["synonyms"] = [str(c) for c in s.get("synonyms") or []]
    if s.get("kind") == "metric" and s.get("metric_type") == "simple":
        s["snapshot"] = bool(spec.get("snapshot"))
        s.setdefault("model", "account_cycle")
    if s.get("kind") == "metric" and s.get("metric_type") == "derived":
        s["metrics"] = [str(m).strip().lower() for m in s.get("metrics") or []]
    return s


def _taken_names(manifest: dict) -> set:
    names = {m["name"] for m in manifest["metrics"]}
    for sm in manifest["semantic_models"]:
        names |= {sm["name"]} | {d["name"] for d in sm.get("dimensions", [])} \
            | {e["name"] for e in sm.get("entities", [])} | {m["name"] for m in sm.get("measures", [])}
    return names


def _node_name(n) -> str:
    return FRIENDLY.get(type(n).__name__, type(n).__name__.upper())


def normalized(expr: str) -> str:
    """The expression as sqlglot regenerates it: what was checked is exactly what is stored (no comments or
    other text the checker did not see can reach MetricFlow)."""
    return sqlglot.parse_one(expr, read="duckdb").sql(dialect="duckdb", comments=False)


def check_row_expr(expr: str, model: str, columns: dict) -> set:
    """Validate a row-level expression for `model`. Returns the columns it reads. Raises DefinitionError."""
    if not expr:
        raise DefinitionError("expr is required: a row-level SQL expression.")
    if len(expr) > MAX_EXPR:
        raise DefinitionError(f"expr is longer than {MAX_EXPR} characters; simplify it.")
    try:
        tree = sqlglot.parse_one(expr, read="duckdb")
    except sqlglot.errors.ParseError as exc:
        raise DefinitionError(f"expr is not a valid SQL expression: {ANSI.sub('', str(exc))[:300]}") from None
    if isinstance(tree, (exp.Select, exp.Subquery, exp.Union)) or tree.find(exp.Select):
        raise DefinitionError("expr may not contain a query (SELECT); write a row-level expression.")
    used, allowed = set(), {c.upper() for c in columns.get(model, [])}
    for node in tree.walk():
        if not isinstance(node, ROW_NODES):
            raise DefinitionError(f"expr uses {_node_name(node)}, which is not allowed in a definition. "
                                  "Allowed: CASE, comparisons, AND/OR/NOT, IN, BETWEEN, LIKE, arithmetic, COALESCE, "
                                  "NULLIF, ABS, ROUND, FLOOR, CEIL, GREATEST, LEAST, CAST, LAG/LEAD over the account's "
                                  "cycles. Aggregations go in agg (metrics).")
        if isinstance(node, exp.Column):
            if node.table:
                raise DefinitionError(f"Use plain column names, not {node.sql()} (a definition reads its own table).")
            col = node.name.upper()
            if col not in allowed:
                near = [c for c in allowed if col.replace("_", "")[:6] in c.replace("_", "")]
                raise DefinitionError(f"{node.name} is not a governed column of {model}."
                                      + (f" Did you mean {', '.join(sorted(near)[:3])}?" if near else "")
                                      + f" Governed columns: {', '.join(sorted(allowed))}.")
            used.add(col)
        if isinstance(node, exp.Cast) and not node.to.is_type(*exp.DataType.NUMERIC_TYPES, *exp.DataType.TEXT_TYPES):
            raise DefinitionError("CAST only to a number or text type.")
        if isinstance(node, exp.Window):
            _check_window(node, model)
        if isinstance(node, (exp.Lag, exp.Lead)) and not isinstance(node.parent, exp.Window):
            raise DefinitionError("LAG/LEAD need OVER (PARTITION BY ACCOUNT_KEY ORDER BY BILLING_CYCLE_DATE).")
    return used


def _check_window(w: exp.Window, model: str) -> None:
    if model != "account_cycle":
        raise DefinitionError("LAG/LEAD only work on account_cycle (an account has one row in account).")
    if not isinstance(w.this, (exp.Lag, exp.Lead)):
        raise DefinitionError("Only LAG and LEAD may use OVER (...).")
    part = w.args.get("partition_by") or []
    order = (w.args.get("order").expressions if w.args.get("order") else [])
    if w.args.get("spec") or len(part) != 1 or not isinstance(part[0], exp.Column) \
            or part[0].name.upper() != PARTITION_COL or len(order) != 1 \
            or not isinstance(order[0].this, exp.Column) or order[0].this.name.upper() != ORDER_COL \
            or order[0].args.get("desc"):
        raise DefinitionError("Window must be exactly OVER (PARTITION BY ACCOUNT_KEY ORDER BY BILLING_CYCLE_DATE).")
    offset = w.this.args.get("offset")
    if offset is not None and not (isinstance(offset, exp.Literal) and offset.is_int and 1 <= int(offset.this) <= 24):
        raise DefinitionError("LAG/LEAD offset must be a whole number of cycles from 1 to 24.")


def _check_derived_expr(expr: str, metrics: list) -> None:
    if not expr or not metrics:
        raise DefinitionError("A derived metric needs expr and metrics (the metric names expr uses).")
    try:
        tree = sqlglot.parse_one(expr, read="duckdb")
    except sqlglot.errors.ParseError as exc:
        raise DefinitionError(f"expr is not a valid expression: {ANSI.sub('', str(exc))[:300]}") from None
    for node in tree.walk():
        if not isinstance(node, DERIVED_NODES):
            raise DefinitionError(f"Derived expr uses {_node_name(node)}; only arithmetic over metric names, "
                                  "NULLIF, COALESCE, ABS, ROUND, GREATEST, LEAST and CASE are allowed.")
        if isinstance(node, exp.Column) and node.name.lower() not in metrics:
            raise DefinitionError(f"{node.name} in expr is not one of the listed metrics {metrics}.")


# ================================================================== 3: the overlay manifest
def _metric_ref(name):
    return {"name": name, "filter": None, "alias": None, "offset_window": None, "offset_to_grain": None}


def _measure_ref(name):
    return {"name": name, "filter": None, "alias": None, "join_to_timespine": False, "fill_nulls_with": None}


def _type_params(**kw):
    tp = {"measure": None, "numerator": None, "denominator": None, "expr": None, "window": None,
          "grain_to_date": None, "metrics": [], "conversion_type_params": None, "cumulative_type_params": None,
          "input_measures": [], "metric_aggregation_params": None, "join_to_timespine": False,
          "fill_nulls_with": None, "is_private": None}
    tp.update(kw)
    return tp


def _latest_window(manifest: dict) -> dict:
    """The governed 'latest cycle per account' rule (non_additive_dimension), copied from the layer."""
    for sm in manifest["semantic_models"]:
        for m in sm.get("measures", []):
            if m.get("non_additive_dimension"):
                return copy.deepcopy(m["non_additive_dimension"])
    raise DefinitionError("The governed layer has no snapshot measure to copy the latest-cycle rule from.")


def measure_name(spec: dict) -> str:
    return f"{spec['name']}_measure"


def _apply(manifest: dict, spec: dict) -> None:
    """Add one checked spec to a manifest dict (in place), in the shape dbt writes semantic_manifest.json."""
    meta = {"provisional_id": spec["id"], "synonyms": spec.get("synonyms", [])}
    metrics = {m["name"]: m for m in manifest["metrics"]}
    if spec["kind"] == "dimension":
        sm = next(s for s in manifest["semantic_models"] if s["name"] == spec["model"])
        sm["dimensions"].append({"name": spec["name"], "description": spec["description"], "type": "categorical",
                                 "datatype": None, "is_partition": False, "type_params": None, "expr": spec["expr"],
                                 "metadata": None, "label": spec.get("label"), "config": {"meta": meta}})
        return
    mt = spec["metric_type"]
    if mt == "simple":
        sm = next(s for s in manifest["semantic_models"] if s["name"] == "account_cycle")
        sm["measures"].append({"name": measure_name(spec), "agg": spec["agg"], "description": spec["description"],
                               "create_metric": False, "expr": spec["expr"], "agg_params": None, "metadata": None,
                               "non_additive_dimension": _latest_window(manifest) if spec["snapshot"] else None,
                               "agg_time_dimension": None, "label": None,
                               "config": {"meta": {"kind": "snapshot" if spec["snapshot"] else "flow"}}})
        tp = _type_params(measure=_measure_ref(measure_name(spec)), input_measures=[_measure_ref(measure_name(spec))])
        kind = "snapshot" if spec["snapshot"] else "flow"
    else:
        inputs = [spec["numerator"], spec["denominator"]] if mt == "ratio" else spec["metrics"]
        measures, kinds = [], []
        for name in inputs:
            for im in metrics[name]["type_params"].get("input_measures") or []:
                if im["name"] not in [m["name"] for m in measures]:
                    measures.append(_measure_ref(im["name"]))
            kinds.append(((metrics[name].get("config") or {}).get("meta") or {}).get("kind", "flow"))
        if mt == "ratio":
            tp = _type_params(numerator=_metric_ref(spec["numerator"]), denominator=_metric_ref(spec["denominator"]),
                              input_measures=measures)
            kind = kinds[0]
        else:
            tp = _type_params(expr=spec["expr"], metrics=[_metric_ref(n) for n in inputs], input_measures=measures)
            kind = kinds[0] if len(set(kinds)) == 1 else "flow"
    manifest["metrics"].append({"name": spec["name"], "description": spec["description"], "type": mt,
                                "datatype": None, "type_params": tp, "filter": None, "metadata": None,
                                "label": spec.get("label") or spec["name"].replace("_", " "),
                                "config": {"meta": {**meta, "kind": kind}}, "time_granularity": None})


def _parse_manifest(manifest: dict):
    """Manifest dict -> MetricFlow's semantic manifest object, the way the `mf` command line loads it
    (with fallbacks for other dbt-metricflow versions)."""
    try:
        from dbt_metricflow.cli.dbt_connectors.dbt_config_accessor import dbtArtifacts
    except ImportError:
        dbtArtifacts = None
    if dbtArtifacts is not None:
        with tempfile.TemporaryDirectory(prefix="conversai_overlay_") as d:
            (Path(d) / "target").mkdir()
            (Path(d) / "target" / "semantic_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            return dbtArtifacts.build_semantic_manifest_from_dbt_project_root(Path(d))
    try:
        from metricflow_semantics.model.dbt_manifest_parser import parse_manifest_from_dbt_generated_manifest
        return parse_manifest_from_dbt_generated_manifest(manifest_json_string=json.dumps(manifest))
    except ImportError:
        from dbt_semantic_interfaces.implementations.semantic_manifest import PydanticSemanticManifest
        return PydanticSemanticManifest.parse_raw(json.dumps(manifest))


def _validate_manifest(sem) -> None:
    validator = None
    for mod in ("metricflow_semantic_interfaces", "dbt_semantic_interfaces"):
        try:
            validator = __import__(f"{mod}.validations.semantic_manifest_validator",
                                   fromlist=["SemanticManifestValidator"]).SemanticManifestValidator
            break
        except ImportError:
            continue
    if validator is None:                 # older/newer package layout: the compile step still validates
        return
    res = validator().validate_semantic_manifest(sem)
    if res.has_blocking_issues:
        raise DefinitionError("MetricFlow rejected the definition: "
                              + "; ".join(str(e) for e in res.errors)[:1500])


_overlays: "OrderedDict[str, tuple]" = OrderedDict()
_overlay_lock = threading.Lock()


def _resolve(ids, log: dict, extra: list | None = None) -> list:
    """The specs to load: the given provisional ids plus the provisional definitions they build on, in
    dependency order. Promoted / rejected ones are skipped (promoted ones are governed now)."""
    by_name = {e["spec"]["name"]: e for e in log.values() if e.get("status") == "provisional"}
    out, seen = [], set()

    def add(spec):
        if spec["id"] in seen:
            return
        for dep in spec.get("depends_on") or []:
            if dep in by_name:
                add(by_name[dep]["spec"])
        seen.add(spec["id"])
        out.append(spec)
    for i in ids or []:
        e = log.get(i)
        if e and e.get("status") == "provisional":
            add(e["spec"])
    for spec in extra or []:
        add(spec)
    return out


def overlay(ids=(), extra: list | None = None):
    """(Catalog, MetricFlow engine) for the governed layer plus the given provisional definitions.
    Cached; building one takes well under a second."""
    specs = _resolve(ids, load_log() if ids else {}, extra)
    if not specs:
        return None, None
    key = json.dumps([MANIFEST.stat().st_mtime if MANIFEST.exists() else 0, specs], sort_keys=True, default=str)
    with _overlay_lock:
        if key in _overlays:
            _overlays.move_to_end(key)
            return _overlays[key]
    manifest = _base_manifest()
    taken = _taken_names(manifest)
    for spec in specs:
        if spec["name"] in taken:
            raise DefinitionError(f"'{spec['name']}' is already defined (governed or another active definition).")
        _apply(manifest, spec)
        taken.add(spec["name"])
    sem = _parse_manifest(manifest)
    _validate_manifest(sem)
    from .compiler import overlay_engine
    engine = overlay_engine(sem)
    if engine is None:
        raise DefinitionError("Provisional definitions need the in-process MetricFlow engine "
                              "(CONVERSAI_MF_INPROCESS is off or MetricFlow failed to load).")
    result = (Catalog(manifest), engine)
    with _overlay_lock:
        _overlays[key] = result
        while len(_overlays) > 16:
            _overlays.popitem(last=False)
    return result


# ================================================================== check (1-4) + plain-English text
def _semantic_name(spec: dict) -> str:
    return f"{spec['model']}__{spec['name']}" if spec["kind"] == "dimension" else spec["name"]


def preview_query(spec: dict, latest: str | None) -> dict:
    """A small query that shows what the definition does (run only after approval)."""
    if spec["kind"] == "dimension":
        sq = {"metrics": ["accounts"], "group_by": [_semantic_name(spec)], "order_by": [_semantic_name(spec)],
              "limit": 50}
        if spec["model"] == "account_cycle" and latest:
            sq["where"] = [{"dimension": "metric_time__month", "op": "=", "value": latest}]
        return sq
    sq = {"metrics": [spec["name"]], "group_by": ["metric_time__month"], "order_by": ["-metric_time__month"],
          "limit": 3}
    return sq


def check(spec: dict, active_ids=(), target: str | None = None, run_target_check: bool = True) -> dict:
    """Run checks 1-4 on a proposal. Returns the checked proposal (not yet saved); raises DefinitionError."""
    from . import pipeline
    s = _clean(spec)
    # ---- 1. shape
    if s.get("kind") not in ("dimension", "metric"):
        raise DefinitionError("kind must be 'dimension' or 'metric'.")
    if not NAME.match(s.get("name", "")) or "__" in s["name"]:
        raise DefinitionError("name must be snake_case: 3-48 characters, a-z 0-9 _, starting with a letter, "
                              "no double underscore.")
    if len(s.get("description", "")) < 20:
        raise DefinitionError("description is required: the plain-English business meaning (at least a sentence).")
    texts = [str(v) for v in s.values() if isinstance(v, str)] + s["choices"] + s["synonyms"]
    if any("{{" in t or "{%" in t for t in texts):
        raise DefinitionError("Template markers ({{ or {%) are not allowed in a definition.")
    base = _base_manifest()
    active = _resolve(active_ids, load_log())
    taken = _taken_names(base) | {a["name"] for a in active}
    if s["name"] in taken or measure_name(s) in taken:
        raise DefinitionError(f"'{s['name']}' already exists (governed or an active provisional definition). "
                              "Use it, or pick another name.")
    clash = [e for e in load_log().values() if e["spec"]["name"] == s["name"] and e.get("status") != "promoted"]
    if clash:
        e = clash[0]
        raise DefinitionError(f"A definition named '{s['name']}' was already proposed ({e['id']}, {e['status']}"
                              + (f": {e.get('reason')}" if e.get("reason") else "") + "). "
                              + ("Reuse it with use_definition, or pick another name." if e["status"] == "provisional"
                                 else "Pick another name and address the reason."))
    cols = allowed_columns()
    used_cols: set = set()
    known_metrics = {m["name"]: m for m in base["metrics"]} | {a["name"]: a for a in active if a["kind"] == "metric"}
    s["depends_on"] = []
    if s["kind"] == "dimension":
        if s.get("model") not in MODELS:
            raise DefinitionError("model must be 'account' or 'account_cycle'.")
        used_cols = check_row_expr(s.get("expr", ""), s["model"], cols)
        s["expr"] = normalized(s["expr"])
    else:
        mt = s.get("metric_type")
        if mt == "simple":
            if s.get("model") != "account_cycle":
                raise DefinitionError("Simple metrics aggregate account_cycle rows (model: account_cycle). For "
                                      "account counts with a condition, use the governed 'accounts' metric with a filter.")
            if s.get("agg") not in AGGS:
                raise DefinitionError(f"agg must be one of {', '.join(AGGS)}.")
            used_cols = check_row_expr(s.get("expr", ""), "account_cycle", cols)
            s["expr"] = normalized(s["expr"])
        elif mt == "ratio":
            for part in ("numerator", "denominator"):
                if s.get(part) not in known_metrics:
                    raise DefinitionError(f"{part} '{s.get(part)}' is not a metric. Use names from list_metrics.")
            s["depends_on"] = [s["numerator"], s["denominator"]]
        elif mt == "derived":
            bad = [m for m in s.get("metrics", []) if m not in known_metrics]
            if bad:
                raise DefinitionError(f"Unknown metric(s) {bad}. Use names from list_metrics.")
            _check_derived_expr(s.get("expr", ""), s["metrics"])
            s["expr"] = normalized(s["expr"])
            s["depends_on"] = list(s["metrics"])
        else:
            raise DefinitionError("metric_type must be 'simple', 'ratio' or 'derived'.")
    s["id"] = f"def_{s['name'][:30]}_{secrets.token_hex(3)}"
    s["columns"] = sorted(used_cols)
    checks = ["shape: ok", "expression whitelist: ok" + (f" (reads {', '.join(s['columns'])})" if used_cols else "")]
    # ---- 3. compile on an overlay of the governed layer
    try:
        cat, engine = overlay(active_ids, extra=[s])
    except DefinitionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DefinitionError(f"MetricFlow could not load the definition: {str(exc)[:800]}") from None
    from .nl import latest_month
    latest = latest_month(None, target)
    latest = latest if re.match(r"\d{4}-\d{2}-\d{2}$", str(latest)) else None
    pq = preview_query(s, latest)
    try:
        plan = pipeline.plan(pq, target, check=run_target_check, overlay_pair=(cat, engine))
    except Exception as exc:  # noqa: BLE001
        raise DefinitionError(f"The definition does not compile: {str(exc)[:1200]}") from None
    checks.append("MetricFlow compile: ok")
    # ---- 4. target engine
    if run_target_check:
        if plan["engine_check"] != "passed":
            raise DefinitionError(f"{plan['target']} could not check the definition: {plan['engine_check']}")
        est = plan.get("estimate") or {}
        checks.append(f"{plan['target']} check: ok" + (f" ({est.get('note')})" if isinstance(est, dict) and est.get("note") else ""))
        cfg = load_targets()["targets"].get(plan["target"], {})
        if cfg.get("export"):
            from .config import load_exports
            have = {c.upper() for c in (load_exports().get(plan["target"], {}).get("columns") or {}).get(
                s.get("model", "account_cycle"), [])}
            missing = [c for c in s["columns"] if have and c not in have]
            if missing:
                raise DefinitionError(f"The export {plan['target']} does not contain {', '.join(missing)}; "
                                      "switch back to the source (use_target) to use this definition.")
    return {"spec": s, "yaml": to_yaml(s), "meaning": meaning(s), "checks": checks, "target": plan["target"],
            "preview_query": pq, "preview_sql": plan["sql"]}


def meaning(spec: dict) -> str:
    s = spec
    if s["kind"] == "dimension":
        head = f"{s['name']} (a category of each {'account' if s['model'] == 'account' else 'account, per billing cycle'})"
    elif s["metric_type"] == "simple":
        head = (f"{s['name']} = {s['agg'].upper()} of {s['expr']} "
                + ("at each account's latest cycle in the period" if s["snapshot"] else "over every cycle in the period"))
    elif s["metric_type"] == "ratio":
        head = f"{s['name']} = {s['numerator']} / {s['denominator']}"
    else:
        head = f"{s['name']} = {s['expr']}"
    return head + f"\n  {s['description']}" + "".join(f"\n  Choice: {c}" for c in s.get("choices", []))


# ================================================================== YAML (shown for approval, used to promote)
def _q(text: str) -> str:
    return json.dumps(str(text), ensure_ascii=False)


def _block(key: str, text: str, indent: int) -> list:
    pad = " " * indent
    return [f"{pad}{key}: >"] + [f"{pad}  {line}" for line in _wrap(text)]


def _wrap(text: str, width: int = 100) -> list:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return lines + ([cur] if cur else [])


def _meta_line(pad: str, kind: str | None, synonyms: list) -> str:
    parts = ([f"kind: {kind}"] if kind else []) + ([f"synonyms: [{', '.join(_q(x) for x in synonyms)}]"] if synonyms else [])
    return f"{pad}config: {{ meta: {{ {', '.join(parts)} }} }}" if parts else ""


def to_yaml(spec: dict) -> str:
    """The definition as it would appear in semantic/models/*.yml (dimension, or measure + metric)."""
    return "\n".join(t for t in _yaml_parts(spec).values() if t)


def _yaml_parts(spec: dict) -> dict:
    s, parts = spec, {}
    if s["kind"] == "dimension":
        lines = [f"- name: {s['name']}", "  type: categorical"] + _block("expr", s["expr"], 2) \
            + _block("description", s["description"], 2)
        if s.get("label"):
            lines.insert(1, f"  label: {_q(s['label'])}")
        meta = _meta_line("  ", None, s.get("synonyms", []))
        parts["dimension"] = "\n".join(lines + ([meta] if meta else []))
        return parts
    kind = None
    if s["metric_type"] == "simple":
        kind = "snapshot" if s["snapshot"] else "flow"
        m = [f"- name: {measure_name(s)}", f"  description: {_q(s['description'][:120])}", f"  agg: {s['agg']}"] \
            + _block("expr", s["expr"], 2)
        if s["snapshot"]:
            m.append("  non_additive_dimension: { name: billing_month, window_choice: max, window_groupings: [account] }")
        m.append(f"  config: {{ meta: {{ kind: {kind} }} }}")
        parts["measure"] = "\n".join(m)
        tp = f"{{ measure: {measure_name(s)} }}"
    elif s["metric_type"] == "ratio":
        tp = f"{{ numerator: {s['numerator']}, denominator: {s['denominator']} }}"
    else:
        tp = f"{{ expr: {_q(s['expr'])}, metrics: [ {', '.join('{ name: ' + m + ' }' for m in s['metrics'])} ] }}"
    lines = [f"- name: {s['name']}", f"  label: {_q(s.get('label') or s['name'].replace('_', ' '))}"] \
        + _block("description", s["description"], 2) + [f"  type: {s['metric_type']}", f"  type_params: {tp}"]
    meta = _meta_line("  ", kind, s.get("synonyms", []))
    parts["metric"] = "\n".join(lines + ([meta] if meta else []))
    return parts


# ================================================================== register / reuse / promote / reject
def register(checked: dict, question: str = "", approved_via: str = "chat approval dialog") -> dict:
    """Save an approved, checked proposal as PROVISIONAL."""
    s = checked["spec"]
    entry = {"id": s["id"], "status": "provisional", "name": s["name"], "kind": s["kind"],
             "spec": s, "yaml": checked["yaml"], "meaning": checked["meaning"], "question": question,
             "created_at": dt.datetime.now().isoformat(timespec="seconds"), "approved_via": approved_via,
             "checked_on": checked["target"], "uses": 0}
    _update_log(lambda log: log.__setitem__(s["id"], entry))
    return entry


def summary(entry: dict) -> dict:
    s = entry["spec"]
    return {"id": entry["id"], "status": entry["status"], "name": s["name"], "kind": s["kind"],
            "use_as": _semantic_name(s) if s["kind"] == "dimension" else s["name"],
            "description": s["description"], "choices": s.get("choices", []), "question": entry.get("question"),
            "created_at": entry.get("created_at"), "uses": entry.get("uses", 0), "reason": entry.get("reason")}


def approval_text(checked: dict, question: str = "") -> str:
    s = checked["spec"]
    return (f"NEW DEFINITION - provisional (not yet governed), for this conversation\n\n{checked['meaning']}\n\n"
            + (f"Asked: {question}\n" if question else "")
            + "Checks: " + "; ".join(checked["checks"]) + "\n\n"
            + f"As it would be written in the semantic layer:\n{checked['yaml']}\n\n"
            + f"After approval a small preview runs on {checked['target']} "
              f"({'accounts by ' + s['name'] if s['kind'] == 'dimension' else 'the last 3 months'}).\n"
            + "Approve this definition?")


def reject(def_id: str, reason: str) -> dict:
    def change(log):
        if def_id not in log:
            raise DefinitionError(f"No definition '{def_id}'.")
        log[def_id].update(status="rejected", reason=reason,
                           decided_at=dt.datetime.now().isoformat(timespec="seconds"))
    return update_doc(definitions_location(), change, "definitions log")[def_id]


def promote(def_id: str, models_dir: Path | None = None) -> dict:
    """Write a provisional definition into semantic/models/*.yml, rebuild the manifest (dbt parse) and mark
    it promoted. On any failure the YAML files are restored."""
    entry = get(def_id)
    if entry["status"] != "provisional":
        raise DefinitionError(f"{def_id} is {entry['status']}, not provisional.")
    s = entry["spec"]
    for dep in s.get("depends_on") or []:
        if dep not in {m["name"] for m in _base_manifest()["metrics"]}:
            raise DefinitionError(f"{def_id} builds on the provisional metric '{dep}': promote that one first.")
    models_dir = models_dir or (SEMANTIC_DIR / "models")
    sm_path, met_path = models_dir / "semantic_models.yml", models_dir / "metrics.yml"
    backup = {p: p.read_text(encoding="utf-8") for p in (sm_path, met_path)}
    stamp = dt.date.today().isoformat()
    header = f"# Promoted {stamp} from provisional definition {def_id}" \
             + (f". Asked: {entry['question'][:120]}" if entry.get("question") else "")
    parts = _yaml_parts({**s})
    try:
        text = backup[sm_path]
        if "dimension" in parts:
            text = _insert_dimension(text, s["model"], header, parts["dimension"])
        if "measure" in parts:
            text = _append_measure(text, "account_cycle", header, parts["measure"])
        sm_path.write_text(text, encoding="utf-8")
        if "metric" in parts:
            body = "\n".join("  " + line for line in parts["metric"].splitlines())
            met_path.write_text(backup[met_path].rstrip("\n") + f"\n\n  {header}\n{body}\n", encoding="utf-8")
        ensure_manifest(force=True)
        Catalog()                                   # the governed layer loads with it
    except Exception as exc:
        for p, t in backup.items():
            p.write_text(t, encoding="utf-8")
        try:
            ensure_manifest(force=True)
        except Exception:  # noqa: BLE001
            pass
        raise DefinitionError(f"Promotion failed, semantic files restored: {str(exc)[:1500]}") from None

    def change(log):
        log[def_id].update(status="promoted", decided_at=dt.datetime.now().isoformat(timespec="seconds"))
    update_doc(definitions_location(), change, "definitions log")
    return {**entry, "status": "promoted", "files": [str(sm_path), str(met_path)]}


def _model_span(lines: list, model: str) -> tuple:
    start = next((i for i, ln in enumerate(lines) if re.match(rf"^  - name: {model}\s*$", ln)), None)
    if start is None:
        raise DefinitionError(f"semantic model '{model}' not found in semantic_models.yml")
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^  - name: ", lines[i])), len(lines))
    return start, end


def _insert_dimension(text: str, model: str, header: str, block: str) -> str:
    lines = text.splitlines()
    start, end = _model_span(lines, model)
    at = next((i for i in range(start, end) if lines[i].rstrip() == "    dimensions:"), None)
    if at is None:
        raise DefinitionError(f"no 'dimensions:' list under model '{model}'")
    new = ["", "      " + header] + ["      " + ln for ln in block.splitlines()] + [""]
    return "\n".join(lines[:at + 1] + new + lines[at + 1:]) + "\n"


def _append_measure(text: str, model: str, header: str, block: str) -> str:
    lines = text.splitlines()
    start, end = _model_span(lines, model)
    if not any(lines[i].rstrip() == "    measures:" for i in range(start, end)):
        raise DefinitionError(f"no 'measures:' list under model '{model}'")
    while end > start and not lines[end - 1].strip():
        end -= 1
    new = ["", "      " + header] + ["      " + ln for ln in block.splitlines()]
    return "\n".join(lines[:end] + new + lines[end:]) + "\n"


def lag_notes(sq: dict, cat: Catalog, entry: dict) -> list:
    """On an export with a month range, dimensions that compare with the previous cycle have nothing to
    compare with in the export's first month."""
    first = (entry.get("months") or [None])[0]
    if not first or not (entry.get("scope") or {}).get("months_from"):
        return []
    used = set(sq["group_by"]) | {w["dimension"] for w in sq["where"]}
    lagged = sorted(d for d in used if re.search(r"\b(LAG|LEAD)\s*\(", cat.dimension_exprs.get(d, ""), re.I))
    return [f"{d} compares each cycle with the previous one; the export starts at {first}, so for that month "
            f"there is no earlier cycle in the copy and it shows n/a." for d in lagged]
