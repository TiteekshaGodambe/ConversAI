"""Run-time adapters: validate (EXPLAIN / dry run) and execute SQL on each target. Read-only.

Every adapter offers:
  validate(sql)          -> size estimate or None (raises on bad SQL)
  execute(sql, max_rows) -> {"columns", "rows", "truncated"}   (for answers)
  fetch_arrow(sql)       -> pyarrow.Table with every row        (for exports only)"""

import datetime as dt
import decimal
import os
import re
import uuid

import duckdb

from .config import MAX_ROWS

_READ_ONLY = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_FORBIDDEN = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|ATTACH|INSTALL|LOAD)\b",
                        re.IGNORECASE)


class TargetUnavailable(RuntimeError):
    """The engine cannot be reached or is not configured (as opposed to a bad query)."""


_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


def _guard(sql: str) -> None:
    # Keywords inside quoted literals (file paths, filter values) are data, not commands.
    bare = _STRING_LITERAL.sub("''", sql)
    if not _READ_ONLY.match(bare) or _FORBIDDEN.search(bare) or ";" in bare.strip().rstrip(";"):
        raise PermissionError("Only read-only SELECT queries may run.")


def _jsonable(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, dt.datetime) and v.time() == dt.time(0):
        return v.date().isoformat()          # month/day grains come back as midnight timestamps
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


class DuckDBTarget:
    def __init__(self, cfg):
        self.cfg = cfg

    def _con(self):
        path = self.cfg["path"]
        return duckdb.connect(path, read_only=path != ":memory:")

    def validate(self, sql):
        _guard(sql)
        with self._con() as con:
            con.execute("EXPLAIN " + sql)
        return None   # DuckDB gives no cheap size estimate; fine for local files

    def execute(self, sql, max_rows=MAX_ROWS):
        _guard(sql)
        with self._con() as con:
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(max_rows + 1)
        return {"columns": cols, "rows": [[_jsonable(v) for v in r] for r in rows[:max_rows]],
                "truncated": len(rows) > max_rows}

    def fetch_arrow(self, sql):
        _guard(sql)
        with self._con() as con:
            out = con.execute(sql).arrow()
            return out.read_all() if hasattr(out, "read_all") else out   # DuckDB >= 1.4 returns a stream


class OracleTarget:
    def __init__(self, cfg):
        self.cfg = cfg

    def _con(self):
        missing = [cfg for cfg in ("dsn_env", "user_env", "password_env") if not os.environ.get(self.cfg[cfg])]
        if missing:
            raise TargetUnavailable("Set " + ", ".join(self.cfg[m] for m in missing) + " to use the Oracle target.")
        try:
            import oracledb
        except ImportError as exc:
            raise TargetUnavailable("Install the Oracle driver: pip install oracledb") from exc
        try:
            return oracledb.connect(user=os.environ[self.cfg["user_env"]], password=os.environ[self.cfg["password_env"]],
                                    dsn=os.environ[self.cfg["dsn_env"]])
        except oracledb.Error as exc:
            raise TargetUnavailable(f"Cannot connect to Oracle: {exc}") from exc

    def validate(self, sql):
        _guard(sql)
        sid = "cv_" + uuid.uuid4().hex[:12]
        with self._con() as con, con.cursor() as cur:
            cur.execute(f"EXPLAIN PLAN SET STATEMENT_ID = '{sid}' FOR {sql}")
            cur.execute("SELECT cardinality, bytes, cost FROM plan_table WHERE statement_id = :s AND id = 0", s=sid)
            row = cur.fetchone()
            cur.execute("DELETE FROM plan_table WHERE statement_id = :s", s=sid)
            con.commit()
        return {"est_rows": row[0], "est_bytes": row[1], "optimizer_cost": row[2]} if row else None

    def execute(self, sql, max_rows=MAX_ROWS):
        _guard(sql)
        with self._con() as con, con.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(sql)
            cols = [d[0].lower() for d in cur.description]
            rows = cur.fetchmany(max_rows + 1)
            con.rollback()
        return {"columns": cols, "rows": [[_jsonable(v) for v in r] for r in rows[:max_rows]],
                "truncated": len(rows) > max_rows}


    def fetch_arrow(self, sql):
        import pyarrow as pa
        _guard(sql)
        with self._con() as con, con.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            con.rollback()
        return pa.table({c: [r[i] for r in rows] for i, c in enumerate(cols)})


JOB_LABELS = {"app": "conversai"}          # BigQuery console: Job history -> filter by label app:conversai


class BigQueryTarget:
    """Google BigQuery. Credentials: Application Default Credentials (gcloud auth application-default login
    on a laptop; the service account on Cloud Run). Project from cfg["project"] or the env var in project_env."""

    def __init__(self, cfg):
        self.cfg = cfg

    def _client(self):
        try:
            from google.cloud import bigquery
        except ImportError as exc:
            raise TargetUnavailable("Install the BigQuery client: pip install google-cloud-bigquery pyarrow") from exc
        project = self.cfg.get("project") or os.environ.get(self.cfg.get("project_env", "GCP_PROJECT"))
        if not project:
            raise TargetUnavailable(f"Set {self.cfg.get('project_env', 'GCP_PROJECT')} to your Google Cloud project id.")
        try:
            return bigquery, bigquery.Client(project=project, location=self.cfg.get("location"))
        except Exception as exc:  # noqa: BLE001 - usually missing credentials
            raise TargetUnavailable("Cannot reach BigQuery (" + str(exc).splitlines()[0] + "). On a laptop run: "
                                    "gcloud auth application-default login") from exc

    def validate(self, sql):
        _guard(sql)
        bigquery, client = self._client()
        job = client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False,
                                                                   labels=JOB_LABELS))
        return {"est_bytes": job.total_bytes_processed,
                "note": f"BigQuery dry run: this query would scan {job.total_bytes_processed / 1e6:,.1f} MB"}

    def execute(self, sql, max_rows=MAX_ROWS):
        _guard(sql)
        bigquery, client = self._client()
        cfg = bigquery.QueryJobConfig(maximum_bytes_billed=int(self.cfg.get("max_bytes_billed", 10 * 1024 ** 3)),
                                      labels=JOB_LABELS)
        rows = client.query(sql, job_config=cfg).result(max_results=max_rows + 1)
        cols = [f.name for f in rows.schema]
        data = [[_jsonable(v) for v in r.values()] for r in rows]
        return {"columns": cols, "rows": data[:max_rows], "truncated": len(data) > max_rows}

    def fetch_arrow(self, sql):
        _guard(sql)
        _, client = self._client()
        return client.query(sql).to_arrow()


def make_target(cfg):
    return {"duckdb": DuckDBTarget, "oracle": OracleTarget, "bigquery": BigQueryTarget}[cfg["kind"]](cfg)
