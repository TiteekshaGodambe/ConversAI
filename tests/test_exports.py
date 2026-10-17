"""Exports: only governed columns leave, the copy answers exactly like the source, BigQuery path works.
Run:  python -m pytest -q tests/test_exports.py"""

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import config, exports, pipeline  # noqa: E402
from tests import fake_bigquery  # noqa: E402

GOLD = yaml.safe_load((ROOT / "eval" / "gold.yaml").read_text(encoding="utf-8"))["gold"]
SAMPLE = ["E1", "E5", "M1", "M4", "H1", "H3", "H5", "H8"]          # joins, ratios, offsets, LAG, top-N


@pytest.fixture()
def tmp_exports(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPORTS_DIR", tmp_path)
    monkeypatch.setattr(config, "EXPORT_REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(exports, "EXPORTS_DIR", tmp_path)
    return tmp_path


def rows(sq, target):
    out = pipeline.run(sq, target, max_rows=1000)["result"]["rows"]
    return sorted(tuple(round(v, 6) if isinstance(v, float) else v for v in r) for r in out)


def test_only_governed_columns_leave():
    cols = exports.governed_columns()
    flat = set(cols["account"]) | set(cols["account_cycle"])
    assert "CURRENT_ACCOUNT_NBR" not in flat and "CLIENT_ID" not in flat      # never referenced by the layer
    assert {"EBILL_IND", "INTERNAL_STATUS"} <= set(cols["account"])
    assert {"CURR_MNTH_BALANCE_AMT", "NBR_DAYS_DELQ_AT_CYC"} <= set(cols["account_cycle"])


def test_cycle_level_scope_is_refused():
    with pytest.raises(exports.ExportError, match="cycle-level"):
        exports.plan_export({"account_filters": [{"dimension": "account_cycle__bureau_score", "op": "<", "value": 620}]},
                            destination="local")


def test_full_local_export_answers_like_the_source(tmp_exports):
    e = exports.run_export(exports.plan_export({}, "lake", "local"))
    for qid in SAMPLE:
        sq = GOLD[qid]["queries"][0]
        assert rows(sq, e["id"]) == rows(sq, "warehouse"), qid


def test_scoped_bigquery_export_follow_up_matches_source(tmp_exports, monkeypatch):
    fake_bigquery.install()
    monkeypatch.setenv("GCP_PROJECT", "demo-proj")
    scope = {"months_from": "2026-07-01", "months_to": "2026-09-01",
             "account_filters": [{"dimension": "account__paperless", "op": "=", "value": "N"}]}
    p = exports.plan_export(scope, "warehouse", "bigquery", "paper accounts last quarter")
    assert "BigQuery demo-proj.conversai_exports" in p["where"]
    e = exports.run_export(p)
    assert pipeline.targets()["targets"][e["id"]]["dialect"] == "bigquery"
    on_source = {"metrics": ["dq30_rate", "total_payments", "utilization_rate"], "where": [
        {"dimension": "account__paperless", "op": "=", "value": "N"},
        {"dimension": "metric_time__month", "op": ">=", "value": "2026-07-01"},
        {"dimension": "metric_time__month", "op": "<=", "value": "2026-09-01"}]}
    on_copy = {"metrics": ["dq30_rate", "total_payments", "utilization_rate"]}
    assert rows(on_copy, e["id"]) == rows(on_source, "warehouse")
    plan = pipeline.plan(on_copy, e["id"])
    assert "`demo-proj.conversai_exports." in plan["sql"] and "The source is not queried" in plan["notes"][0]
    assert plan["estimate"]["est_bytes"] > 0
    exports.delete_export(e["id"])
    assert e["id"] not in pipeline.targets()["targets"]


def test_bigquery_sql_shape():
    t = {"dialect": "bigquery", "tables": {"account": "`p.d.a`", "account_cycle": "`p.d.c`",
                                           "metricflow_time_spine": exports.TIME_SPINE["bigquery"]}}
    from engine.compiler import compile_to_sql
    from engine.dialects import bind_and_translate
    sql = bind_and_translate(compile_to_sql(GOLD["H1"]["queries"][0], pipeline.catalog()), t)
    assert "DATE_SUB(" in sql and "INTERVAL 1 MONTH" in sql and "INTERVAL '" not in sql
    assert "GROUP BY\n  1" in sql or "GROUP BY 1" in sql
    assert " __" not in sql and "(__" not in sql


def test_out_of_scope_questions_are_flagged(tmp_exports):
    scope = {"months_from": "2026-07-01", "months_to": "2026-09-01",
             "account_filters": [{"dimension": "account__paperless", "op": "=", "value": "N"}]}
    e = exports.run_export(exports.plan_export(scope, "warehouse", "local"))
    assert Path(e["files"][0]).name == "cycles_with_account.csv" and Path(e["files"][0]).exists()
    inside = pipeline.plan({"metrics": ["dq30_rate"], "where": [
        {"dimension": "metric_time__month", "op": "=", "value": "2026-08-01"}]}, e["id"], check=False)
    assert not any("OUTSIDE" in n for n in inside["notes"])
    cases = [
        {"metrics": ["accounts"], "where": [{"dimension": "account__paperless", "op": "=", "value": "Y"}]},
        {"metrics": ["accounts"], "group_by": ["account__paperless"]},
        {"metrics": ["dq30_rate"], "where": [{"dimension": "metric_time__month", "op": "=", "value": "2026-03-01"}]},
        {"metrics": ["dq30_rate"], "where": [{"dimension": "metric_time__month", "op": ">=", "value": "2026-01-01"}]},
    ]
    for sq in cases:
        notes = pipeline.plan(sq, e["id"], check=False)["notes"]
        assert any(n.startswith("OUTSIDE THE EXPORT") for n in notes), sq


def test_verify_export_scoped_and_bigquery(tmp_exports, monkeypatch):
    scope = {"months_from": "2026-07-01", "months_to": "2026-09-01",
             "account_filters": [{"dimension": "account__paperless", "op": "=", "value": "N"}]}
    local = exports.run_export(exports.plan_export(scope, "warehouse", "local"))
    res = {r["id"]: r["status"] for r in exports.verify_export(local["id"])}
    assert "DIFF" not in res.values() and res["H1"] == "EXPECTED", res
    fake_bigquery.install()
    monkeypatch.setenv("GCP_PROJECT", "demo-proj")
    bq = exports.run_export(exports.plan_export(scope, "warehouse", "bigquery"))
    res = {r["id"]: r["status"] for r in exports.verify_export(bq["id"])}
    assert "DIFF" not in res.values(), res


def test_bigquery_source_target_and_in_bigquery_export(tmp_exports, monkeypatch):
    """Phase 6a: source tables loaded into BigQuery answer like the warehouse; an export from that source
    is copied inside BigQuery (query job with a destination table, no download)."""
    st = fake_bigquery.install()
    monkeypatch.setenv("GCP_PROJECT", "demo-proj")
    sys.path.insert(0, str(ROOT / "tables"))
    import load_bigquery
    from google.cloud import bigquery
    load_bigquery.load_source(bigquery.Client(project="demo-proj", location="US"), bigquery, "demo-proj", "US")
    tgt = pipeline.targets()["targets"]["bigquery"]
    assert tgt["tables"]["account"] == "`demo-proj.conversai_source.ACCOUNT_DIM`"
    for qid in SAMPLE:
        sq = GOLD[qid]["queries"][0]
        assert rows(sq, "bigquery") == rows(sq, "warehouse"), qid
    loads_before = len(st["loads"])
    scope = {"months_from": "2026-07-01", "months_to": "2026-09-01",
             "account_filters": [{"dimension": "account__paperless", "op": "=", "value": "N"}]}
    e = exports.run_export(exports.plan_export(scope, "bigquery", "bigquery"))
    assert len(st["loads"]) == loads_before and len(st.get("copies", [])) >= 2     # copied inside BigQuery
    assert e["rows"] == {"account_cycle": 6096, "account": 2033} and e["months"] == ["2026-07", "2026-09"]
    res = {r["id"]: r["status"] for r in exports.verify_export(e["id"])}
    assert "DIFF" not in res.values(), res


def test_bigquery_source_and_in_bigquery_export(tmp_exports, monkeypatch):
    """6a: source tables loaded into BigQuery answer like the warehouse, and an export from them is
    copied inside BigQuery (a SELECT into the export table), without loading data through this machine."""
    state = fake_bigquery.install()
    monkeypatch.setenv("GCP_PROJECT", "demo-proj")
    from tables import load_bigquery
    from google.cloud import bigquery
    load_bigquery.load_source(bigquery.Client(project="demo-proj"), bigquery, "demo-proj", "US")
    assert pipeline.targets()["targets"]["bigquery"]["tables"]["account"] == "`demo-proj.conversai_source.ACCOUNT_DIM`"
    for qid in SAMPLE:
        sq = GOLD[qid]["queries"][0]
        assert rows(sq, "bigquery") == rows(sq, "warehouse"), qid
    loads_before = len(state["loads"])
    scope = {"months_from": "2026-07-01", "months_to": "2026-09-01",
             "account_filters": [{"dimension": "account__paperless", "op": "=", "value": "N"}]}
    e = exports.run_export(exports.plan_export(scope, "bigquery", "bigquery"))
    assert len(state["loads"]) == loads_before                     # nothing loaded from this machine
    assert any(c.endswith(f"{e['id']}_account_cycle") for c in state["copies"])
    assert e["rows"] == {"account": 2033, "account_cycle": 6096} and e["months"] == ["2026-07", "2026-09"]
    res = {r["id"]: r["status"] for r in exports.verify_export(e["id"])}
    assert "DIFF" not in res.values(), res


def test_gcp_source_targets_load_and_answer(monkeypatch):
    """tables/load_bigquery.py --lake: warehouse tables -> BigQuery, lake Parquet -> Cloud Storage + external
    tables. Then the `bigquery` and `gcs_lake` targets answer like the local warehouse."""
    from tests import fake_gcs
    fake_bigquery.install()
    fake_gcs.install()
    monkeypatch.setenv("GCP_PROJECT", "demo-proj")
    monkeypatch.setattr(sys, "argv", ["load_bigquery.py", "--lake"])
    from tables import load_bigquery
    load_bigquery.main()
    for qid in SAMPLE:
        sq = GOLD[qid]["queries"][0]
        assert rows(sq, "bigquery") == rows(sq, "warehouse"), qid
        assert rows(sq, "gcs_lake") == rows(sq, "warehouse"), qid


def test_cloud_target_restriction(monkeypatch):
    monkeypatch.setenv("CONVERSAI_TARGETS", "bigquery,gcs_lake")
    monkeypatch.setenv("CONVERSAI_DEFAULT_TARGET", "bigquery")
    cfg = config.load_targets()
    assert set(t for t in cfg["targets"] if not cfg["targets"][t].get("export")) == {"bigquery", "gcs_lake"}
    assert cfg["default_target"] == "bigquery"
    monkeypatch.setenv("CONVERSAI_DEFAULT_TARGET", "warehouse")
    with pytest.raises(ValueError):
        config.load_targets()


def test_registry_on_cloud_storage_survives_concurrent_writes(monkeypatch):
    from tests import fake_gcs
    fake_gcs.install()
    monkeypatch.setattr(config, "REGISTRY_URI", "gs://demo-bucket/conversai/exports.json")
    assert config.load_exports() == {}
    config.update_exports(lambda r: r.__setitem__("a", {"id": "a"}))
    blob = config._gcs_blob()
    real_upload, calls = blob.upload_from_string, {"n": 0}

    def racing_change(reg):            # someone else writes between our read and our write, once
        if calls["n"] == 0:
            config._gcs_blob().upload_from_string(json.dumps({**reg, "b": {"id": "b"}}))
        calls["n"] += 1
        reg["c"] = {"id": "c"}
    config.update_exports(racing_change)
    assert set(config.load_exports()) == {"a", "b", "c"}       # nothing lost
