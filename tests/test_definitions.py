"""Proposed (provisional) definitions: unsafe proposals are refused, safe ones answer exactly like hand-written
SQL, answers are labelled, they work on BigQuery and on exports, the log works on Cloud Storage, and a
promoted definition becomes governed.
Run:  python -m pytest -q tests/test_definitions.py"""

import decimal
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import config, definitions as D, exports, pipeline  # noqa: E402
from tests import fake_bigquery  # noqa: E402

LAGGED = "LAG({c}) OVER (PARTITION BY ACCOUNT_KEY ORDER BY BILLING_CYCLE_DATE)"
BUREAU_DROP = {
    "name": "bureau_score_drop_50", "kind": "dimension", "model": "account_cycle",
    "description": "Y if the bureau score fell by 50 points or more since the previous cycle, N if not, n/a first.",
    "expr": f"CASE WHEN {LAGGED.format(c='BUREAU_SCORE')} IS NULL THEN 'n/a' "
            f"WHEN {LAGGED.format(c='BUREAU_SCORE')} - BUREAU_SCORE >= 50 THEN 'Y' ELSE 'N' END",
    "choices": ["A drop of exactly 50 counts"]}
SEP = [{"dimension": "metric_time__month", "op": "=", "value": "2026-09-01"}]


@pytest.fixture(autouse=True)
def tmp_log(tmp_path, monkeypatch):
    monkeypatch.setenv("CONVERSAI_DEFINITIONS", str(tmp_path / "definitions.json"))
    monkeypatch.setattr(config, "EXPORTS_DIR", tmp_path)
    monkeypatch.setattr(config, "EXPORT_REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(exports, "EXPORTS_DIR", tmp_path)
    return tmp_path


def num(v):
    if isinstance(v, decimal.Decimal) or (isinstance(v, (int, float)) and not isinstance(v, bool)):
        return float(v)
    return v if v is None or isinstance(v, str) else str(v)[:10]


def sql_rows(sql):
    con = duckdb.connect(":memory:")
    for t, d in (("ACCOUNT_DIM", "account_dim"), ("ACCOUNT_CYCLE_FACT", "account_cycle_fact")):
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{(ROOT / 'lake' / 'cards' / d).as_posix()}/*.parquet')")
    return sorted(tuple(num(v) for v in r) for r in con.execute(sql).fetchall())


def answer(sq, target, ids):
    out = pipeline.run(sq, target, definitions=ids, max_rows=1000)
    return out, sorted(tuple(num(v) for v in r) for r in out["result"]["rows"])


def approve(spec, target="warehouse", active=()):
    checked = D.check(spec, list(active), target)
    return D.register(checked, "test question")["id"]


# ------------------------------------------------------------------ 1-2: refused before anyone sees them
UNSAFE = [
    ({"expr": "(SELECT MAX(BUREAU_SCORE) FROM ACCOUNT_CYCLE_FACT)"}, "query|SELECT"),
    ({"expr": "BUREAU_SCORE; DROP TABLE ACCOUNT_DIM"}, "more than one statement|not allowed"),
    ({"expr": "SUM(BUREAU_SCORE)"}, "SUM"),
    ({"expr": "x.BUREAU_SCORE"}, "plain column"),
    ({"expr": "PFST_CREDIT_SCORE"}, "not a governed column"),
    ({"expr": "BEHAVIOUR_SCORE"}, "Did you mean BEHAVIOR_SCORE"),
    ({"expr": "REGEXP_EXTRACT(UPC_1, 'a')"}, "not allowed"),
    ({"expr": "read_parquet('/etc/passwd')"}, "reading files"),
    ({"expr": "LAG(BUREAU_SCORE) OVER (PARTITION BY ACCOUNT_HISTORY_KEY ORDER BY BILLING_CYCLE_DATE)"}, "Window must be"),
    ({"expr": "LAG(BUREAU_SCORE) OVER (PARTITION BY ACCOUNT_KEY ORDER BY BILLING_CYCLE_DATE "
              "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)"}, "Window must be|window frame"),
    ({"expr": "LAG(BUREAU_SCORE, 100) OVER (PARTITION BY ACCOUNT_KEY ORDER BY BILLING_CYCLE_DATE)"}, "offset"),
    ({"expr": "CASE WHEN EBILL_IND IS NULL THEN 'a' END", "model": "account_cycle"}, "not a governed column"),
    ({"model": "account", "expr": "LAG(EBILL_IND) OVER (PARTITION BY ACCOUNT_KEY ORDER BY BILLING_CYCLE_DATE)"},
     "only work on account_cycle"),
    ({"expr": "'{{ Dimension(\"x\") }}'"}, "Template"),
    ({"name": "behavior_score_trend"}, "already exists"),
    ({"name": "Bad-Name"}, "snake_case"),
    ({"description": "short"}, "description is required"),
    ({"kind": "table"}, "kind must be"),
]


def test_stored_expression_is_the_checked_one():
    """Comments (which could hide text from the checker) are dropped: the stored expr is sqlglot's own output."""
    did = approve({**BUREAU_DROP, "expr": BUREAU_DROP["expr"] + " /* ; DROP TABLE ACCOUNT_DIM */"})
    assert "DROP" not in D.get(did)["spec"]["expr"] and "/*" not in D.get(did)["yaml"]


@pytest.mark.parametrize("change,message", UNSAFE)
def test_unsafe_or_malformed_proposals_are_refused(change, message):
    with pytest.raises(D.DefinitionError, match=message):
        D.check({**BUREAU_DROP, **change}, target="warehouse")
    assert D.load_log() == {}                                     # nothing saved


def test_metric_proposals_are_checked_too():
    base = {"kind": "metric", "description": "Some metric used to test the checks of the server."}
    with pytest.raises(D.DefinitionError, match="numerator"):
        D.check({**base, "name": "ratio_one", "metric_type": "ratio", "numerator": "profit", "denominator": "accounts"},
                target="warehouse")
    with pytest.raises(D.DefinitionError, match="not one of the listed metrics"):
        D.check({**base, "name": "derived_one", "metric_type": "derived", "expr": "accounts / total_balance",
                 "metrics": ["accounts"]}, target="warehouse")
    with pytest.raises(D.DefinitionError, match="agg must be"):
        D.check({**base, "name": "simple_one", "metric_type": "simple", "agg": "median", "expr": "PYMT_AMT"},
                target="warehouse")


# ------------------------------------------------------------------ 3-4: safe ones answer like hand-written SQL
def test_dimension_matches_hand_written_sql_and_is_labelled():
    did = approve(BUREAU_DROP)
    sq = {"metrics": ["accounts"], "group_by": ["account_cycle__bureau_score_drop_50"], "where": SEP}
    want = sql_rows(f"""WITH c AS (SELECT *, {LAGGED.format(c='BUREAU_SCORE')} prev FROM ACCOUNT_CYCLE_FACT)
        SELECT CASE WHEN prev IS NULL THEN 'n/a' WHEN prev - BUREAU_SCORE >= 50 THEN 'Y' ELSE 'N' END,
               COUNT(DISTINCT ACCOUNT_KEY) FROM c WHERE BILLING_CYCLE_DATE >= DATE '2026-09-01' GROUP BY 1""")
    for target in ("warehouse", "lake"):
        out, got = answer(sq, target, [did])
        assert got == want, target
        assert out["provisional"] == [did]
        assert out["notes"][0].startswith("PROVISIONAL DEFINITION") and did in out["notes"][0]
    governed = pipeline.plan({"metrics": ["accounts"], "where": SEP}, "warehouse", definitions=[did])
    assert governed["provisional"] == [] and not any("PROVISIONAL" in n for n in governed["notes"])
    assert D.get(did)["uses"] == 2
    with pytest.raises(Exception, match="Unknown group_by"):        # not usable without approving it
        pipeline.plan(sq, "warehouse")


def test_ratio_derived_and_snapshot_metrics_match_hand_written_sql():
    late = approve({"name": "late_fee_cycles", "kind": "metric", "metric_type": "simple", "agg": "count",
                    "expr": "CASE WHEN LATE_CHARGE_ASSESSED > 0 THEN ACCOUNT_HISTORY_KEY END",
                    "description": "Billing cycles in which a late fee was charged, over the period."})
    per_acct = approve({"name": "late_fee_cycles_per_account", "kind": "metric", "metric_type": "derived",
                        "expr": "late_fee_cycles / NULLIF(accounts, 0)", "metrics": ["late_fee_cycles", "accounts"],
                        "description": "Late-fee cycles per account over the period."}, active=[late])
    ratio = approve({"name": "payment_to_balance", "kind": "metric", "metric_type": "ratio",
                     "numerator": "total_payments", "denominator": "total_balance",
                     "description": "Payments over the period divided by the balance at the latest cycle."})
    max_bal = approve({"name": "max_balance_latest", "kind": "metric", "metric_type": "simple", "agg": "max",
                       "snapshot": True, "expr": "CURR_MNTH_BALANCE_AMT",
                       "description": "Largest statement balance, at each account's latest cycle in the period."})
    q3 = [{"dimension": "metric_time__month", "op": ">=", "value": "2026-07-01"}]
    # derived (pulls in its provisional input automatically)
    _, got = answer({"metrics": ["late_fee_cycles_per_account"], "where": q3}, "warehouse", [per_acct])
    want = sql_rows("""SELECT COUNT(CASE WHEN LATE_CHARGE_ASSESSED > 0 THEN 1 END) * 1.0 / COUNT(DISTINCT ACCOUNT_KEY)
                       FROM ACCOUNT_CYCLE_FACT WHERE BILLING_CYCLE_DATE >= DATE '2026-07-01'""")
    assert got[0][0] == pytest.approx(want[0][0], rel=1e-9)
    # ratio of governed metrics, per month
    _, got = answer({"metrics": ["payment_to_balance"], "group_by": ["metric_time__month"], "where": q3},
                    "warehouse", [ratio])
    want = sql_rows("""SELECT CAST(DATE_TRUNC('month', BILLING_CYCLE_DATE) AS DATE)::VARCHAR, SUM(PYMT_AMT) / SUM(CURR_MNTH_BALANCE_AMT)
                       FROM ACCOUNT_CYCLE_FACT WHERE BILLING_CYCLE_DATE >= DATE '2026-07-01' GROUP BY 1""")
    assert [r[0] for r in got] == [r[0] for r in want]
    assert all(g[1] == pytest.approx(w[1], rel=1e-9) for g, w in zip(got, want))
    # snapshot simple metric = latest cycle per account in the period
    _, got = answer({"metrics": ["max_balance_latest"], "where": q3}, "warehouse", [max_bal])
    want = sql_rows("""SELECT MAX(CURR_MNTH_BALANCE_AMT) FROM (SELECT * FROM ACCOUNT_CYCLE_FACT
                       WHERE BILLING_CYCLE_DATE >= DATE '2026-07-01'
                       QUALIFY ROW_NUMBER() OVER (PARTITION BY ACCOUNT_KEY ORDER BY BILLING_CYCLE_DATE DESC) = 1)""")
    assert got[0][0] == pytest.approx(want[0][0], rel=1e-9)
    assert "agg: max" in D.get(max_bal)["yaml"] and "non_additive_dimension" in D.get(max_bal)["yaml"]


def test_same_name_cannot_be_proposed_twice():
    approve(BUREAU_DROP)
    with pytest.raises(D.DefinitionError, match="already proposed"):
        D.check(BUREAU_DROP, target="warehouse")


def test_rejected_definition_is_not_usable_and_reason_is_kept():
    did = approve(BUREAU_DROP)
    D.reject(did, "use a 6-month window instead")
    with pytest.raises(D.DefinitionError, match="6-month window"):
        D.check(BUREAU_DROP, target="warehouse")
    with pytest.raises(Exception, match="Unknown group_by"):
        pipeline.plan({"metrics": ["accounts"], "group_by": ["account_cycle__bureau_score_drop_50"]}, "warehouse",
                      definitions=[did])


# ------------------------------------------------------------------ BigQuery, exports, Cloud Storage log
def test_bigquery_and_export_with_first_month_note(monkeypatch):
    fake_bigquery.install()
    monkeypatch.setenv("GCP_PROJECT", "demo-proj")
    from tables import load_bigquery
    from google.cloud import bigquery
    load_bigquery.load_source(bigquery.Client(project="demo-proj"), bigquery, "demo-proj", "US")
    did = approve(BUREAU_DROP, target="bigquery")                    # checked with a (fake) dry run
    sq = {"metrics": ["accounts"], "group_by": ["account_cycle__bureau_score_drop_50"], "where": SEP}
    assert answer(sq, "bigquery", [did])[1] == answer(sq, "warehouse", [did])[1]
    scope = {"months_from": "2026-07-01", "months_to": "2026-09-01"}
    e = exports.run_export(exports.plan_export(scope, "bigquery", "bigquery"))
    out, got = answer(sq, e["id"], [did])
    assert got == answer(sq, "warehouse", [did])[1]                   # Sep has its previous cycle in the copy
    jul = {**sq, "where": [{"dimension": "metric_time__month", "op": "=", "value": "2026-07-01"}]}
    notes = pipeline.plan(jul, e["id"], definitions=[did])["notes"]
    assert any("no earlier cycle" in n for n in notes)               # first month of the export: n/a


def test_log_on_cloud_storage_next_to_export_registry(monkeypatch):
    from tests import fake_gcs
    fake_gcs.install()
    monkeypatch.delenv("CONVERSAI_DEFINITIONS")
    monkeypatch.setattr(config, "REGISTRY_URI", "gs://demo-bucket/conversai/exports.json")
    assert config.definitions_location() == "gs://demo-bucket/conversai/definitions.json"
    did = approve(BUREAU_DROP)
    assert fake_gcs.STATE["objects"][("demo-bucket", "conversai/definitions.json")]
    assert D.get(did)["status"] == "provisional"
    assert [x["id"] for x in D.list_definitions("provisional")] == [did]


# ------------------------------------------------------------------ promotion makes it governed
def test_promote_writes_yaml_and_becomes_governed(tmp_path):
    proj = tmp_path / "proj"
    shutil.copytree(ROOT, proj, ignore=shutil.ignore_patterns("sl", ".venv", "exports", "__pycache__", "lake",
                                                             "proposals", ".pytest_cache", "logs"))
    env = {**os.environ, "CONVERSAI_DEFINITIONS": str(tmp_path / "log.json"), "PYTHONUTF8": "1"}
    spec = proj / "spec.json"
    spec.write_text(json.dumps(BUREAU_DROP), encoding="utf-8")
    run = lambda *a: subprocess.run([sys.executable, "cli.py", *a], cwd=proj, env=env, capture_output=True,
                                    text=True, timeout=600)
    r = run("definitions", "propose", "@spec.json", "--yes")
    assert r.returncode == 0, r.stderr[-2000:]
    did = json.loads((tmp_path / "log.json").read_text())
    did = next(iter(did))
    r = run("definitions", "promote", did, "--yes")
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    text = (proj / "semantic" / "models" / "semantic_models.yml").read_text(encoding="utf-8")
    assert "Promoted" in text and "- name: bureau_score_drop_50" in text
    r = run("query", json.dumps({"metrics": ["accounts"], "group_by": ["account_cycle__bureau_score_drop_50"],
                                 "where": SEP}), "--yes")             # governed now: no --definitions
    assert r.returncode == 0 and "PROVISIONAL" not in r.stdout, r.stdout[-2000:] + r.stderr[-2000:]
    assert json.loads((tmp_path / "log.json").read_text())[did]["status"] == "promoted"
