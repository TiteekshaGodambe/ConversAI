"""
Put the source data on Google Cloud, so the `bigquery` and `gcs_lake` targets in targets.yaml work.

  set GCP_PROJECT=<your project id>
  python tables/load_bigquery.py            # warehouse tables -> BigQuery dataset conversai_source
  python tables/load_bigquery.py --lake     # also: lake Parquet -> gs://<project>-conversai-lake, and
                                            #       BigQuery external tables over them (dataset conversai_lake)
  python tables/load_bigquery.py --lake-only   # just the lake step (e.g. re-run after an upload timed out)

Reads the local copies built by tables/build_tables.py (warehouse/cards.duckdb, lake/cards/...), so the
cloud data is identical to the local data and eval/run_eval.py can check every target against the same
hand-written answers. Re-running replaces the tables. Credentials: gcloud auth application-default login.

BigQuery-only (no --lake) works in the free BigQuery sandbox. --lake needs Cloud Storage, which needs a
billing account linked to the project (the free trial credit is fine).
"""

import argparse
import io
import os
import sys
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import yaml

ROOT = Path(__file__).resolve().parent.parent
TABLES = ("ACCOUNT_DIM", "ACCOUNT_CYCLE_FACT")
LAKE_DIRS = {"ACCOUNT_DIM": "account_dim", "ACCOUNT_CYCLE_FACT": "account_cycle_fact"}


def bq_settings():
    cfg = yaml.safe_load((ROOT / "targets.yaml").read_text(encoding="utf-8"))
    t = cfg["targets"]["bigquery"]
    project = os.environ.get(t.get("project_env", "GCP_PROJECT"))
    if not project:
        sys.exit("Set GCP_PROJECT to your Google Cloud project id first (set GCP_PROJECT=...).")
    return project, t.get("location", "US")


def load_source(client, bigquery, project, location):
    ds = bigquery.Dataset(f"{project}.conversai_source")
    ds.location = location
    ds.description = "ConversAI source tables (copied from warehouse/cards.duckdb by tables/load_bigquery.py)"
    client.create_dataset(ds, exists_ok=True)
    con = duckdb.connect(str(ROOT / "warehouse" / "cards.duckdb"), read_only=True)
    for t in TABLES:
        out = con.execute(f"SELECT * FROM {t}").arrow()
        tbl = out.read_all() if hasattr(out, "read_all") else out
        buf = io.BytesIO()
        pq.write_table(tbl, buf)
        buf.seek(0)
        table_id = f"{project}.conversai_source.{t}"
        client.load_table_from_file(buf, table_id, job_config=bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.PARQUET, write_disposition="WRITE_TRUNCATE")).result()
        print(f"  BigQuery  {table_id}: {tbl.num_rows:,} rows, {tbl.num_columns} columns")
    con.close()


CHUNK = 8 * 1024 * 1024          # upload in 8 MB pieces: on a slow line only a stalled piece is retried


def upload(bucket, name, path):
    """Upload a file in resumable 8 MB chunks (5 minutes allowed per chunk). Skips files already there
    with the same size, so a re-run after a network failure only sends what is missing."""
    size = path.stat().st_size
    existing = bucket.get_blob(name)
    if existing is not None and existing.size == size:
        print(f"  skip      gs://{bucket.name}/{name} (already uploaded, {size / 1e6:,.1f} MB)")
        return
    print(f"  upload    gs://{bucket.name}/{name} ({size / 1e6:,.1f} MB) ...", flush=True)
    bucket.blob(name, chunk_size=CHUNK).upload_from_filename(str(path), timeout=300)


def load_lake(client, bigquery, project, location):
    try:
        from google.cloud import storage
    except ImportError:
        sys.exit("Install the Cloud Storage client: pip install google-cloud-storage")
    gcs = storage.Client(project=project)
    bucket_name = f"{project}-conversai-lake"
    bucket = gcs.lookup_bucket(bucket_name)
    if bucket is None:
        bucket = gcs.create_bucket(bucket_name, location=location)
        print(f"  created bucket gs://{bucket_name} ({location})")
    ds = bigquery.Dataset(f"{project}.conversai_lake")
    ds.location = location
    ds.description = "ConversAI lake: Parquet files on Cloud Storage, queried in place (external tables)"
    client.create_dataset(ds, exists_ok=True)
    for t, folder in LAKE_DIRS.items():
        files = sorted((ROOT / "lake" / "cards" / folder).glob("*.parquet"))
        if not files:
            sys.exit(f"No Parquet files in lake/cards/{folder}: run python tables/build_tables.py first.")
        for f in files:
            upload(bucket, f"cards/{folder}/{f.name}", f)
        uri = f"gs://{bucket_name}/cards/{folder}/*.parquet"
        ext = bigquery.ExternalConfig("PARQUET")
        ext.source_uris = [uri]
        table = bigquery.Table(f"{project}.conversai_lake.{t}")
        table.external_data_configuration = ext
        client.delete_table(table.reference, not_found_ok=True)
        client.create_table(table)
        print(f"  lake      {project}.conversai_lake.{t} -> {uri} ({len(files)} file(s))")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lake", action="store_true", help="also upload the lake to Cloud Storage (needs billing)")
    ap.add_argument("--lake-only", action="store_true", help="only the lake step (BigQuery source already loaded)")
    a = ap.parse_args()
    try:
        from google.cloud import bigquery
    except ImportError:
        sys.exit("Install the BigQuery client: pip install google-cloud-bigquery pyarrow")
    project, location = bq_settings()
    client = bigquery.Client(project=project, location=location)
    print(f"\nProject {project}, location {location}")
    if not a.lake_only:
        load_source(client, bigquery, project, location)
    if a.lake or a.lake_only:
        load_lake(client, bigquery, project, location)
    print("\nCheck: python eval\\run_eval.py --targets bigquery" + (",gcs_lake" if (a.lake or a.lake_only) else ""))


if __name__ == "__main__":
    main()
