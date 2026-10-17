"""One entry point used by the CLI, the MCP server and the tests."""

from .catalog import Catalog
from .compiler import compile_to_sql, validate
from .config import MAX_ROWS, load_exports, load_targets
from .dialects import bind_and_translate
from .simplify import simplify_sql
from .targets import TargetUnavailable, make_target

_cat = None


def catalog() -> Catalog:
    global _cat
    if _cat is None:
        _cat = Catalog()
    return _cat


def targets() -> dict:
    return load_targets()


def notes_for(sq: dict, cat) -> list:
    """Plain-English caveats shown with the plan (and in approval dialogs)."""
    notes = []
    snapshot = [m for m in sq["metrics"] if cat.metrics[m]["kind"] == "snapshot"]
    timed = any(g.startswith("metric_time__") for g in sq["group_by"]) or \
        any(w["dimension"].startswith("metric_time__") for w in sq["where"])
    cycle_filters = [w["dimension"] for w in sq["where"] if w["dimension"].startswith("account_cycle__")]
    if snapshot and cycle_filters and not timed:
        notes.append("Cycle-level filter (" + ", ".join(sorted(set(cycle_filters))) + ") without a month: "
                     "accounts are counted if they matched at ANY cycle in the data, at their latest such cycle. "
                     "For a point-in-time answer add metric_time__month = '<month>'.")
    elif snapshot and not timed:
        notes.append("Snapshot metrics use each account's latest billing cycle in the data.")
    return notes


def export_note(target_name: str) -> str | None:
    e = load_exports().get(target_name)
    if not e:
        return None
    return (f"Runs on export {target_name} ({e['where']}; {e['scope_text']}; copied {e['created_at'][:16]} "
            f"from {e['source']}). The source is not queried.")


TIME_DIMS = ("metric_time__", "account_cycle__billing_month__", "account_cycle__billing_cycle_date__")


def _month(v) -> str:
    return str(v)[:7]


def scope_warnings(sq: dict, entry: dict) -> list:
    """Parts of a question an export cannot answer, because its scope left that data behind.
    Each warning starts with 'OUTSIDE THE EXPORT', so the agent can tell the user."""
    out, scope = [], entry.get("scope") or {}
    first, last = (entry.get("months") or [None, None])
    if scope.get("months_from") or scope.get("months_to"):
        for w in sq["where"]:
            if not w["dimension"].startswith(TIME_DIMS) or w["op"] in ("in", "not in"):
                continue
            m, op = _month(w["value"]), w["op"]
            if (op == "=" and not first <= m <= last) or (op in (">=", ">") and m > last) \
                    or (op in ("<=", "<") and m < first):
                out.append(f"OUTSIDE THE EXPORT: {w['dimension']} {op} {w['value']} - the export only holds "
                           f"billing months {first} to {last}, so this returns nothing.")
            elif (op in (">=", ">") and m < first) or (op in ("<=", "<") and m > last):
                out.append(f"OUTSIDE THE EXPORT: {w['dimension']} {op} {w['value']} reaches beyond the export, "
                           f"which only holds billing months {first} to {last}; the answer covers those months only.")
    for f in scope.get("account_filters") or []:
        dim, kept = f["dimension"], f["value"]
        if f["op"] != "=":
            continue
        for w in sq["where"]:
            if w["dimension"] != dim:
                continue
            vals = w["value"] if isinstance(w["value"], list) else [w["value"]]
            excluded = (w["op"] == "=" and str(kept) not in map(str, vals)) or \
                       (w["op"] == "in" and str(kept) not in map(str, vals)) or \
                       (w["op"] in ("!=", "not in") and str(kept) in map(str, vals))
            if excluded:
                out.append(f"OUTSIDE THE EXPORT: {dim} {w['op']} {w['value']} - the export only holds accounts "
                           f"with {dim} = {kept}, so this returns nothing.")
        if dim in sq["group_by"]:
            out.append(f"OUTSIDE THE EXPORT: grouped by {dim}, but the export only holds {dim} = {kept}; "
                       f"other groups are not in it.")
    return out


def provisional_notes(sq: dict, cat) -> tuple:
    """(notes, ids) for the provisional definitions a query uses. Every answer built on one is labelled."""
    used = list(sq["metrics"]) + list(sq["group_by"]) + [w["dimension"] for w in sq["where"]]
    hits = {n: cat.provisional[n] for n in used if n in cat.provisional}
    notes = [f"PROVISIONAL DEFINITION (approved in chat, not yet governed): {n} - {v['description']} "
             f"[{v['id']}]" for n, v in hits.items()]
    return notes, sorted({v["id"] for v in hits.values()})


def plan(sq: dict, target_name: str | None = None, check: bool = True, definitions=None,
         overlay_pair: tuple | None = None) -> dict:
    """Semantic query -> SQL for the target (+ the engine's own check). Executes nothing.
    definitions: ids of provisional definitions this conversation has approved (engine.definitions);
    their names become usable and every answer using them is labelled PROVISIONAL."""
    cfg = targets()
    name = target_name or cfg["default_target"]
    if name not in cfg["targets"]:
        raise ValueError(f"Unknown target '{name}'. Options: {', '.join(cfg['targets'])}")
    tcfg = cfg["targets"][name]
    cat, engine = catalog(), None
    if overlay_pair:
        cat, engine = overlay_pair
    elif definitions:
        from .definitions import overlay
        o_cat, o_engine = overlay(definitions)
        if o_cat is not None:
            cat, engine = o_cat, o_engine
    sq = validate(sq, cat)
    mf_sql = compile_to_sql(sq, cat, engine)
    original = bind_and_translate(mf_sql, tcfg)
    simple_mf, why_not = simplify_sql(mf_sql)
    sql = original
    if simple_mf:
        try:
            sql = bind_and_translate(simple_mf, tcfg)
        except Exception as exc:  # noqa: BLE001 - keep MetricFlow's SQL
            why_not = f"not simplified (binding: {str(exc)[:120]})"
    estimate, check_note = None, "not requested"
    if check:
        target = make_target(tcfg)
        try:
            try:
                estimate = target.validate(sql)               # bad SQL raises here
            except TargetUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - the simplified SQL failed the engine's check
                if sql == original:
                    raise
                why_not, sql = f"not simplified (engine check: {str(exc)[:120]})", original
                estimate = target.validate(sql)
            if sql != original and isinstance(estimate, dict) and estimate.get("est_bytes") is not None:
                before = target.validate(original)            # BigQuery: never scan more than MetricFlow's SQL
                if before.get("est_bytes") is not None and estimate["est_bytes"] > before["est_bytes"]:
                    why_not, sql, estimate = "not simplified (would scan more bytes)", original, before
            check_note = "passed"
        except TargetUnavailable as exc:
            check_note = f"skipped: {exc}"
    simplified = {"applied": sql != original,
                  "note": "SQL simplified (deterministic rules, same result); original kept as sql_original"
                  if sql != original else (why_not or "no change")}
    notes = notes_for(sq, cat)
    if tcfg.get("export"):
        from .definitions import lag_notes
        entry = load_exports().get(name, {})
        notes = ([export_note(name)] + scope_warnings(sq, entry)
                 + [n.replace("in the data", "in the export") for n in notes] + lag_notes(sq, cat, entry))
    prov_notes, prov_ids = provisional_notes(sq, cat)
    return {"target": name, "dialect": tcfg["dialect"], "semantic_query": sq, "summary": cat.describe(sq),
            "notes": prov_notes + notes, "provisional": prov_ids, "sql": sql, "estimate": estimate,
            "engine_check": check_note, "simplified": simplified,
            "sql_original": original if sql != original else None}


def run(sq: dict, target_name: str | None = None, max_rows: int = MAX_ROWS, definitions=None) -> dict:
    p = plan(sq, target_name, check=True, definitions=definitions)
    if p["engine_check"] != "passed":
        raise TargetUnavailable(p["engine_check"])
    tcfg = targets()["targets"][p["target"]]
    result = make_target(tcfg).execute(p["sql"], max_rows)
    sq = p["semantic_query"]
    cat = catalog()
    if p["provisional"]:
        from .definitions import overlay, record_use
        cat = overlay(definitions)[0]
        record_use(p["provisional"])
    # Same column order on every engine: group_by first, then metrics as requested.
    want = sq["group_by"] + sq["metrics"]
    lower = [c.lower() for c in result["columns"]]
    if sorted(lower) == sorted(want):
        idx = [lower.index(c) for c in want]
        result = {**result, "columns": want, "rows": [[r[i] for i in idx] for r in result["rows"]]}
    # Offset (month-over-month) metrics make MetricFlow add a time-spine row after the last month
    # with nothing in it. Drop rows where every requested metric is empty.
    if any(cat.metrics[m]["offset"] for m in sq["metrics"]):
        idx = [i for i, c in enumerate(result["columns"]) if c.lower() in sq["metrics"]]
        rows = [r for r in result["rows"] if any(r[i] is not None for i in idx)]
        if len(rows) < len(result["rows"]):
            result = {**result, "rows": rows, "dropped_empty_rows": len(result["rows"]) - len(rows)}
    return {**p, "result": result}


def dimension_values(dimension: str, target_name: str | None = None, limit: int = 100, definitions=None) -> list:
    out = run({"metrics": ["accounts"], "group_by": [dimension], "order_by": [dimension], "limit": limit},
              target_name, definitions=definitions)
    idx = out["result"]["columns"].index(dimension) if dimension in out["result"]["columns"] else 0
    return [r[idx] for r in out["result"]["rows"]]
