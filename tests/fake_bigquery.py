"""A stand-in for google.cloud.bigquery, for tests without a Google Cloud project.

Loads (load_table_from_file with Parquet) land in an in-memory DuckDB; queries arrive in BigQuery SQL,
are parsed as BigQuery with sqlglot and run on DuckDB. That exercises ConversAI's BigQuery code path
(dataset creation, loads, expiry/labels, dry-run estimates, byte cap, BigQuery-dialect SQL) end to end.
It cannot prove BigQuery's own engine accepts every construct: run eval/run_eval.py --targets <export>
against real BigQuery for that.

  import tests.fake_bigquery as fb; fb.install()     # replaces google.cloud.bigquery in sys.modules"""

import io
import sys
import types

import duckdb
import pyarrow.parquet as pq
import sqlglot
from sqlglot import exp

STATE = {"con": None, "datasets": set(), "tables": {}, "queries": [], "loads": [], "copies": []}


def _con():
    if STATE["con"] is None:
        STATE["con"] = duckdb.connect(":memory:")
    return STATE["con"]


def _local(table_id: str) -> str:
    return "t_" + table_id.replace(".", "__").replace("-", "_")


class SchemaField:
    def __init__(self, name):
        self.name = name


class _Row:
    def __init__(self, values):
        self._v = values

    def values(self):
        return self._v


class _Result(list):
    schema = []


class QueryJobConfig:
    def __init__(self, dry_run=False, use_query_cache=True, maximum_bytes_billed=None, destination=None,
                 write_disposition=None, labels=None):
        self.dry_run, self.maximum_bytes_billed, self.labels = dry_run, maximum_bytes_billed, labels or {}
        self.destination, self.write_disposition = destination, write_disposition


class LoadJobConfig:
    def __init__(self, source_format=None, write_disposition=None):
        self.source_format, self.write_disposition = source_format, write_disposition


class SourceFormat:
    PARQUET = "PARQUET"


class Dataset:
    def __init__(self, dataset_id):
        self.dataset_id, self.location = dataset_id, None


class _Table:
    def __init__(self, table_id):
        self.table_id, self.description, self.labels, self.expires = table_id, None, {}, None


class ExternalConfig:
    def __init__(self, source_format):
        self.source_format, self.source_uris = source_format, []


class Table:
    """bigquery.Table: used for external tables (lake on Cloud Storage)."""
    def __init__(self, table_id):
        self.table_id = self.reference = table_id
        self.external_data_configuration = None


class _Job:
    def __init__(self, sql=None, dry=False):
        self.sql, self.dry, self.total_bytes_processed = sql, dry, 0

    def result(self, max_results=None):
        if self.sql is None:
            return self
        cur = _con().execute(self.sql)
        rows = cur.fetchall()
        out = _Result(_Row(list(r)) for r in rows[: max_results or len(rows)])
        out.schema = [SchemaField(d[0]) for d in cur.description]
        return out

    def to_arrow(self):
        out = _con().execute(self.sql).arrow()
        return out.read_all() if hasattr(out, "read_all") else out


class Client:
    def __init__(self, project=None, location=None):
        if not project:
            raise RuntimeError("no project")
        self.project, self.location = project, location

    def create_dataset(self, ds, exists_ok=False):
        STATE["datasets"].add(ds.dataset_id)

    def load_table_from_file(self, buf, table_id, job_config=None):
        assert job_config.source_format == "PARQUET"
        tbl = pq.read_table(io.BytesIO(buf.read()))
        _con().register("_incoming", tbl)
        _con().execute(f"CREATE OR REPLACE TABLE {_local(table_id)} AS SELECT * FROM _incoming")
        _con().unregister("_incoming")
        STATE["tables"][table_id] = _Table(table_id)
        STATE["loads"].append((table_id, tbl.num_rows))
        return _Job()

    def get_table(self, table_id):
        return STATE["tables"][table_id]

    def update_table(self, table, fields):
        STATE["tables"][table.table_id] = table

    def delete_table(self, table_id, not_found_ok=False):
        table_id = getattr(table_id, "table_id", table_id)
        STATE["tables"].pop(table_id, None)
        for kind in ("TABLE", "VIEW"):
            try:
                _con().execute(f"DROP {kind} IF EXISTS {_local(table_id)}")
            except duckdb.Error:          # the name is the other kind of object
                pass

    def create_table(self, table):
        ext = table.external_data_configuration
        if ext is None:
            raise NotImplementedError("fake: only external tables")
        import tests.fake_gcs as fake_gcs
        files = [p.as_posix() for uri in ext.source_uris for p in fake_gcs.glob(uri)]
        if not files:
            raise RuntimeError(f"no objects match {ext.source_uris}")
        _con().execute(f"CREATE OR REPLACE VIEW {_local(table.table_id)} AS SELECT * FROM read_parquet({files!r})")
        STATE["tables"][table.table_id] = _Table(table.table_id)
        return table

    def query(self, sql, job_config=None):
        STATE["queries"].append(sql)
        tree = sqlglot.parse_one(sql, read="bigquery")           # must be valid BigQuery SQL
        for t in tree.find_all(exp.Table):
            parts = [p for p in (t.catalog, t.db, t.name) if p]
            full = ".".join(parts)
            if full in STATE["tables"]:
                new = exp.to_table(_local(full))
                if t.alias:
                    new.set("alias", exp.TableAlias(this=exp.to_identifier(t.alias)))
                t.replace(new)
        if job_config is not None and getattr(job_config, "destination", None):
            dest = str(job_config.destination)
            _con().execute(f"CREATE OR REPLACE TABLE {_local(dest)} AS {tree.sql(dialect='duckdb')}")
            STATE["tables"][dest] = _Table(dest)
            STATE.setdefault("copies", []).append(dest)
            return _Job()
        job = _Job(tree.sql(dialect="duckdb"), dry=bool(job_config and job_config.dry_run))
        job.total_bytes_processed = 1234567 if job.dry else 0
        if job.dry:
            _con().execute("EXPLAIN " + job.sql)                 # validate like a dry run
        return job


def install():
    """Put this module in place of google.cloud.bigquery, leaving the rest of the google namespace alone."""
    import importlib
    for pkg in ("google", "google.cloud"):
        try:
            importlib.import_module(pkg)
        except ImportError:
            mod = types.ModuleType(pkg)
            mod.__path__ = []
            sys.modules[pkg] = mod
    bq = types.ModuleType("google.cloud.bigquery")
    for name in ("Client", "QueryJobConfig", "LoadJobConfig", "SourceFormat", "Dataset", "SchemaField",
                 "ExternalConfig", "Table"):
        setattr(bq, name, globals()[name])
    sys.modules["google.cloud.bigquery"] = bq
    sys.modules["google.cloud"].bigquery = bq
    return STATE
