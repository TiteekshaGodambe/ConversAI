"""Exports: copy the governed data behind a question to BigQuery (or local Parquet) so the user can
analyse it, and keep answering follow-up questions from that copy only.

  plan = plan_export({"months_from": "2026-07-01", "months_to": "2026-09-01",
                      "account_filters": [{"dimension": "account__paperless", "op": "=", "value": "N"}]})
  entry = run_export(plan)          # -> registered as target entry["id"], e.g. export_20261012_184500_3fa

What is copied: only the columns the semantic layer uses (found from the compiled semantic manifest),
plus targets.yaml exports.extra_columns. Every other column of the source stays where it is.
Scope: a month range (billing cycles) and/or filters on account attributes (account__ dimensions,
evaluated with the same SQL expression the semantic layer uses). Each account's cycles inside the
range are copied whole, so snapshot and prior-month logic work on the copy as on the source.

The copy becomes a target with the same logical tables (account, account_cycle), so the SAME governed
metrics run on it; only the physical tables and the SQL dialect change."""

import datetime as dt
import io
import json
import shutil
import uuid
from pathlib import Path

import sqlglot
from sqlglot import exp

from .compiler import SemanticQueryError, _date_literal, _literal
from .config import EXPORTS_DIR, MANIFEST, ROOT, load_exports, load_targets, update_exports
from .dialects import bind_and_translate
from .targets import TargetUnavailable, make_target

LOGICAL = ("account", "account_cycle")
KEYS = {"account": ["ACCOUNT_KEY"], "account_cycle": ["ACCOUNT_HISTORY_KEY", "ACCOUNT_KEY", "BILLING_CYCLE_DATE"]}
TIME_SPINE = {
    "duckdb": "(SELECT CAST(range AS DATE) AS date_day FROM range(DATE '2015-01-01', DATE '2031-01-01', INTERVAL 1 DAY))",
    "bigquery": "(SELECT date_day FROM UNNEST(GENERATE_DATE_ARRAY(DATE '2015-01-01', DATE '2030-12-31')) AS date_day)",
}
OPS = {"=", "!=", ">", ">=", "<", "<=", "in", "not in"}


class ExportError(ValueError):
    """The export request is invalid, too large, or the destination is not set up."""


# ------------------------------------------------------------------ what may leave: governed columns
def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _columns_in(sql_expr: str) -> set:
    tree = sqlglot.parse_one(sql_expr, read="duckdb")
    return {c.name.upper() for c in tree.find_all(exp.Column)}


def governed_columns(extra: dict | None = None) -> dict:
    """{logical table: [columns]} = every column any entity, dimension or measure expression reads."""
    cols = {t: set(KEYS[t]) for t in LOGICAL}
    for sm in _manifest()["semantic_models"]:
        t = sm["name"]
        if t not in cols:
            continue
        for item in sm.get("entities", []) + sm.get("dimensions", []) + sm.get("measures", []):
            cols[t] |= _columns_in(item.get("expr") or item["name"])
    for t, extra_cols in (extra or {}).items():
        cols.setdefault(t, set()).update(c.upper() for c in extra_cols)
    return {t: KEYS[t] + sorted(c for c in cols[t] if c not in KEYS[t]) for t in LOGICAL}


def _account_dimension_exprs() -> dict:
    """account__<dimension> -> its SQL expression on the account table (categorical dims only)."""
    out = {}
    for sm in _manifest()["semantic_models"]:
        if sm["name"] == "account":
            for d in sm.get("dimensions", []):
                if d["type"] == "categorical":
                    out[f"account__{d['name']}"] = d.get("expr") or d["name"]
    return out


# ------------------------------------------------------------------ scope -> SQL (logical, DuckDB dialect)
def _scope_sql(scope: dict, cols: dict) -> dict:
    dims = _account_dimension_exprs()
    acct_conds = []
    for f in scope.get("account_filters") or []:
        name, op, value = f.get("dimension"), f.get("op"), f.get("value")
        if name not in dims:
            if str(name).startswith("account_cycle__") or str(name).startswith("metric_time"):
                raise ExportError(f"Exports are scoped by months and account attributes; '{name}' is a cycle-level "
                                  "field. Export the accounts/months, then filter on it in follow-up questions.")
            raise ExportError(f"Unknown account dimension '{name}'. Options: {', '.join(sorted(dims))}")
        if op not in OPS:
            raise ExportError(f"Unsupported operator {op!r}")
        try:
            rhs = ("(" + ", ".join(_literal(v) for v in value) + ")") if op in ("in", "not in") else _literal(value)
        except (SemanticQueryError, TypeError) as exc:
            raise ExportError(f"Bad value for {name}: {exc}") from None
        acct_conds.append(f"({dims[name]}) {op.upper()} {rhs}")

    cyc_conds = []
    try:
        if scope.get("months_from"):
            cyc_conds.append(f"DATE_TRUNC('month', BILLING_CYCLE_DATE) >= {_date_literal(scope['months_from'])}")
        if scope.get("months_to"):
            cyc_conds.append(f"DATE_TRUNC('month', BILLING_CYCLE_DATE) <= {_date_literal(scope['months_to'])}")
    except SemanticQueryError as exc:
        raise ExportError(str(exc)) from None
    if acct_conds:
        cyc_conds.append("ACCOUNT_KEY IN (SELECT ACCOUNT_KEY FROM account WHERE " + " AND ".join(acct_conds) + ")")

    cyc_where = (" WHERE " + " AND ".join(cyc_conds)) if cyc_conds else ""
    acct_where = ["ACCOUNT_KEY IN (SELECT ACCOUNT_KEY FROM account_cycle" + cyc_where + ")"] + acct_conds
    return {
        "account_cycle": f"SELECT {', '.join(cols['account_cycle'])} FROM account_cycle{cyc_where}",
        "account": f"SELECT {', '.join(cols['account'])} FROM account WHERE " + " AND ".join(acct_where),
    }


def describe_scope(scope: dict) -> str:
    parts = []
    if scope.get("months_from") or scope.get("months_to"):
        parts.append(f"billing months {scope.get('months_from') or 'start'} to {scope.get('months_to') or 'latest'}")
    for f in scope.get("account_filters") or []:
        parts.append(f"{f['dimension'].split('__', 1)[-1]} {f['op']} {f['value']}")
    return "; ".join(parts) or "all accounts, all months"


# ------------------------------------------------------------------ plan (counts, nothing copied yet)
def plan_export(scope: dict | None = None, source: str | None = None, destination: str | None = None,
                question: str = "") -> dict:
    scope = scope or {}
    cfg = load_targets()
    exp_cfg = cfg.get("exports") or {}
    source = source or cfg["default_target"]
    if source not in cfg["targets"]:
        raise ExportError(f"Unknown source '{source}'. Options: {', '.join(cfg['targets'])}")
    import os
    # precedence: the call's argument, then CONVERSAI_EXPORT_DESTINATION (set in .vscode/mcp.json), then targets.yaml
    destination = destination or os.environ.get("CONVERSAI_EXPORT_DESTINATION") or exp_cfg.get("destination", "local")
    if destination not in ("bigquery", "local"):
        raise ExportError("destination must be 'bigquery' or 'local'")
    src_cfg = cfg["targets"][source]
    cols = governed_columns(exp_cfg.get("extra_columns"))
    logical = _scope_sql(scope, cols)
    bound = {t: bind_and_translate(sql, src_cfg) for t, sql in logical.items()}
    src = make_target(src_cfg)
    counts = {}
    for t, sql in bound.items():
        out = src.execute(bind_and_translate(f"SELECT COUNT(*) AS n FROM ({logical[t]}) AS s", src_cfg), 1)
        counts[t] = int(out["rows"][0][0])
    if counts["account_cycle"] == 0:
        raise ExportError("Nothing matches this scope: no billing cycles to export.")
    limit = int(exp_cfg.get("max_rows", 5_000_000))
    if sum(counts.values()) > limit:
        raise ExportError(f"{sum(counts.values()):,} rows exceeds the export limit of {limit:,} (targets.yaml exports.max_rows).")

    eid = "export_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:3]
    if destination == "bigquery":
        bq = exp_cfg.get("bigquery") or {}
        import os
        project = bq.get("project") or os.environ.get(bq.get("project_env", "GCP_PROJECT"))
        if not project:
            raise ExportError(f"Set {bq.get('project_env', 'GCP_PROJECT')} to your Google Cloud project id first.")
        where = f"BigQuery {project}.{bq.get('dataset', 'conversai_exports')} (tables {eid}_account, {eid}_account_cycle)"
        days = int(bq.get("expiration_days", 7))
        expiry = f"tables delete themselves after {days} days" if days else "tables do not expire"
    else:
        where, expiry = (f"local files in {(EXPORTS_DIR / eid).as_posix()} (Parquet for follow-up questions, "
                         f"cycles_with_account.csv for Excel)"), "kept until deleted"
    notes = []
    if scope.get("months_from"):
        notes.append("Prior-month fields (prior_delinquency_bucket, paid_minimum_due) are n/a in the first exported month.")
    return {"id": eid, "question": question, "scope": scope, "scope_text": describe_scope(scope),
            "source": source, "destination": destination, "where": where, "expiry": expiry,
            "columns": cols, "rows": counts, "sql": bound, "notes": notes}


def approval_text(p: dict) -> str:
    cols = "; ".join(f"{t}: {', '.join(c)}" for t, c in p["columns"].items())
    return (f"Export data for your own analysis?\n\nFrom: {p['source']}\nTo: {p['where']} ({p['expiry']})\n"
            f"Scope: {p['scope_text']}\nRows: {p['rows']['account']:,} accounts, {p['rows']['account_cycle']:,} billing cycles\n"
            f"Columns (governed only): {cols}\n" + "".join(f"Note: {n}\n" for n in p["notes"])
            + f"\nAfter the export, follow-up questions in this conversation run on the copy only "
              f"(target {p['id']}); {p['source']} is not queried again.")


# ------------------------------------------------------------------ copy + register
def _write_local(eid: str, tables: dict) -> dict:
    """Parquet per logical table (what follow-up questions query) plus one CSV for Excel: every billing
    cycle with its account's attributes joined in."""
    import duckdb
    import pyarrow.parquet as pq
    folder = EXPORTS_DIR / eid
    folder.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    con.register("c", tables["account_cycle"])
    con.register("a", tables["account"])
    acct_cols = [n for n in tables["account"].column_names if n != "ACCOUNT_KEY"]
    con.execute(f"COPY (SELECT c.*, {', '.join('a.' + n for n in acct_cols)} FROM c LEFT JOIN a USING (ACCOUNT_KEY) "
                f"ORDER BY c.ACCOUNT_KEY, c.BILLING_CYCLE_DATE) TO '{(folder / 'cycles_with_account.csv').as_posix()}' "
                f"(HEADER, DELIMITER ',')")
    con.close()
    phys = {}
    for t, tbl in tables.items():
        path = folder / f"{t}.parquet"
        pq.write_table(tbl, path)
        try:
            ref = path.relative_to(ROOT).as_posix()
        except ValueError:
            ref = path.as_posix()
        phys[t] = f"read_parquet('{ref}')"
    phys["metricflow_time_spine"] = TIME_SPINE["duckdb"]
    return {"kind": "duckdb", "dialect": "duckdb", "path": ":memory:", "tables": phys}


def _bq_destination(bq: dict):
    """(bigquery module, client, project, dataset, location, expires) with the exports dataset in place."""
    import os
    try:
        from google.cloud import bigquery
    except ImportError as exc:
        raise ExportError("Install the BigQuery client: pip install google-cloud-bigquery pyarrow") from exc
    project = bq.get("project") or os.environ.get(bq.get("project_env", "GCP_PROJECT"))
    dataset, location = bq.get("dataset", "conversai_exports"), bq.get("location", "US")
    try:
        client = bigquery.Client(project=project, location=location)
        ds = bigquery.Dataset(f"{project}.{dataset}")
        ds.location = location
        client.create_dataset(ds, exists_ok=True)
    except Exception as exc:  # noqa: BLE001
        raise ExportError("Cannot reach BigQuery (" + str(exc).splitlines()[0] + "). On a laptop run: "
                          "gcloud auth application-default login") from exc
    days = int(bq.get("expiration_days", 7))
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days) if days else None
    return bigquery, client, project, dataset, location, expires


def _label_table(client, table_id: str, eid: str, description: str, expires) -> None:
    bt = client.get_table(table_id)
    bt.description = description
    bt.labels = {"conversai_export": eid.lower()}
    bt.expires = expires
    client.update_table(bt, ["description", "labels", "expires"])


def _bq_target(project, dataset, location, bq, eid) -> tuple:
    phys = {t: f"`{project}.{dataset}.{eid}_{t}`" for t in LOGICAL}
    phys["metricflow_time_spine"] = TIME_SPINE["bigquery"]
    target = {"kind": "bigquery", "dialect": "bigquery", "project": project, "location": location,
              "max_bytes_billed": bq.get("max_bytes_billed", 10 * 1024 ** 3), "tables": phys}
    console = f"https://console.cloud.google.com/bigquery?project={project}&ws=!1m4!1m3!3m2!1s{project}!2s{dataset}"
    return target, console


def _write_bigquery(eid: str, tables: dict, bq: dict, description: str) -> tuple:
    """Load Arrow tables (read from a non-BigQuery source) into the exports dataset."""
    import pyarrow.parquet as pq
    bigquery, client, project, dataset, location, expires = _bq_destination(bq)
    for t, tbl in tables.items():
        table_id = f"{project}.{dataset}.{eid}_{t}"
        buf = io.BytesIO()
        pq.write_table(tbl, buf)
        buf.seek(0)
        client.load_table_from_file(buf, table_id, job_config=bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.PARQUET, write_disposition="WRITE_TRUNCATE")).result()
        _label_table(client, table_id, eid, description, expires)
    target, console = _bq_target(project, dataset, location, bq, eid)
    return target, console, (expires.isoformat() if expires else None)


def _copy_within_bigquery(eid: str, sql: dict, bq: dict, description: str) -> tuple:
    """Source and destination are both BigQuery: run each scoped SELECT with the export table as its
    destination, so the data is copied inside BigQuery and never passes through this machine."""
    from .targets import _guard
    bigquery, client, project, dataset, location, expires = _bq_destination(bq)
    rows = {}
    for t, select in sql.items():
        _guard(select)
        table_id = f"{project}.{dataset}.{eid}_{t}"
        client.query(select, job_config=bigquery.QueryJobConfig(
            destination=table_id, write_disposition="WRITE_TRUNCATE", labels={"app": "conversai", "kind": "export"})).result()
        _label_table(client, table_id, eid, description, expires)
        rows[t] = int(list(client.query(f"SELECT COUNT(*) AS n FROM `{table_id}`").result())[0].values()[0])
    first, last = list(client.query(
        f"SELECT MIN(BILLING_CYCLE_DATE), MAX(BILLING_CYCLE_DATE) FROM `{project}.{dataset}.{eid}_account_cycle`"
    ).result())[0].values()
    target, console = _bq_target(project, dataset, location, bq, eid)
    months = [str(first)[:7], str(last)[:7]] if first else None
    return target, console, (expires.isoformat() if expires else None), rows, months


def run_export(p: dict) -> dict:
    """Copy the planned scope from the source to the destination and register it as a target."""
    cfg = load_targets()
    src_cfg = cfg["targets"][p["source"]]
    description = (f"ConversAI export {p['id']} from {p['source']}: {p['scope_text']}. "
                   f"Question: {p['question'] or '-'}")[:1000]
    console, expires, files = None, None, None
    bq = cfg["exports"].get("bigquery") or {}
    if p["destination"] == "bigquery" and src_cfg["kind"] == "bigquery":
        target, console, expires, rows, months = _copy_within_bigquery(p["id"], p["sql"], bq, description)
    else:
        tables = {t: make_target(src_cfg).fetch_arrow(sql) for t, sql in p["sql"].items()}
        if p["destination"] == "bigquery":
            target, console, expires = _write_bigquery(p["id"], tables, bq, description)
        else:
            target = _write_local(p["id"], tables)
            folder = EXPORTS_DIR / p["id"]
            files = [str(folder / f) for f in ("cycles_with_account.csv", "account.parquet", "account_cycle.parquet")]
        rows = {t: tbl.num_rows for t, tbl in tables.items()}
        ms = sorted({str(v)[:7] for v in tables["account_cycle"].column("BILLING_CYCLE_DATE").to_pylist()})
        months = [ms[0], ms[-1]] if ms else None
    entry = {"id": p["id"], "status": "ready", "created_at": dt.datetime.now().isoformat(timespec="seconds"),
             "question": p["question"], "source": p["source"], "destination": p["destination"],
             "scope": p["scope"], "scope_text": p["scope_text"], "where": p["where"],
             "rows": rows, "columns": p["columns"], "months": months, "expires": expires,
             "console_url": console, "files": files, "target": target}
    update_exports(lambda reg: reg.__setitem__(p["id"], entry))
    return entry


def list_exports() -> list:
    return [{k: e.get(k) for k in ("id", "created_at", "source", "destination", "where", "scope_text", "rows",
                                   "months", "expires", "console_url", "files", "question")}
            for e in load_exports().values()]


def delete_export(eid: str) -> str:
    e = load_exports().get(eid)
    if not e:
        raise ExportError(f"No export '{eid}'.")
    if e["destination"] == "bigquery":
        from google.cloud import bigquery
        client = bigquery.Client(project=e["target"]["project"], location=e["target"].get("location"))
        for t in LOGICAL:
            client.delete_table(e["target"]["tables"][t].strip("`"), not_found_ok=True)
    else:
        shutil.rmtree(EXPORTS_DIR / eid, ignore_errors=True)
    update_exports(lambda reg: reg.pop(eid, None))
    return f"Deleted {eid}."


__all__ = ["ExportError", "TargetUnavailable", "governed_columns", "plan_export", "approval_text", "run_export",
           "list_exports", "delete_export", "describe_scope"]


# ------------------------------------------------------------------ cross-check: copy vs source
def _scope_as_filters(scope: dict) -> list:
    """The export's scope as semantic-query filters, so the same question can be asked of the source."""
    where = [dict(f) for f in scope.get("account_filters") or []]
    if scope.get("months_from"):
        where.append({"dimension": "metric_time__month", "op": ">=", "value": scope["months_from"]})
    if scope.get("months_to"):
        where.append({"dimension": "metric_time__month", "op": "<=", "value": scope["months_to"]})
    return where


def _norm_rows(rows: list) -> list:
    return sorted((tuple(round(v, 6) if isinstance(v, float) else (str(v)[:10] if v is not None else None)
                         for v in r) for r in rows), key=lambda t: tuple("" if v is None else str(v) for v in t))


def verify_export(eid: str, queries: dict | None = None) -> list:
    """Ask every question twice: on the copy as is, and on the source with the export's scope as filters.
    Returns [{id, status, detail}] with status SAME, EXPECTED (only the first exported month differs because a
    month-over-month or prior-month value needs the month before the export) or DIFF."""
    from . import pipeline
    import yaml
    entry = load_exports().get(eid)
    if not entry:
        raise ExportError(f"No export '{eid}'.")
    if queries is None:
        gold = yaml.safe_load((ROOT / "eval" / "gold.yaml").read_text(encoding="utf-8"))["gold"]
        queries = {f"{k}" + (f".{i + 1}" if len(g["queries"]) > 1 else ""): q
                   for k, g in gold.items() for i, q in enumerate(g.get("queries") or [])}
    first = f"{entry['months'][0]}-01" if entry.get("months") else None
    extra, results = _scope_as_filters(entry.get("scope") or {}), []
    for qid, sq in queries.items():
        try:
            copy = _norm_rows(pipeline.run(sq, eid, max_rows=5000)["result"]["rows"])
            src = _norm_rows(pipeline.run({**sq, "where": list(sq.get("where") or []) + extra},
                                          entry["source"], max_rows=5000)["result"]["rows"])
        except Exception as exc:  # noqa: BLE001
            results.append({"id": qid, "status": "DIFF", "detail": f"error: {exc}"[:300]})
            continue
        if copy == src:
            results.append({"id": qid, "status": "SAME", "detail": f"{len(copy)} rows"})
            continue
        diffs = [(c, s) for c, s in zip(copy, src) if c != s]
        expected = len(copy) == len(src) and first and all(
            first in c and all(cv == sv or (cv is None and sv is not None) for cv, sv in zip(c, s)) for c, s in diffs)
        results.append({"id": qid, "status": "EXPECTED" if expected else "DIFF",
                        "detail": (f"only {first} differs: the copy has no earlier month to compare with"
                                   if expected else f"copy {copy[:3]} vs source {src[:3]}")})
    return results
