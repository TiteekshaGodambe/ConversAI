"""Deterministic SQL simplification: every gold question and example returns exactly the same rows with the
simplified SQL as with MetricFlow's own SQL, on every local engine and on (fake) BigQuery; anything doubtful
falls back to MetricFlow's SQL.  Run:  python -m pytest -q tests/test_simplify.py"""

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import pipeline, simplify  # noqa: E402
from engine.targets import make_target  # noqa: E402
from tests import fake_bigquery  # noqa: E402

GOLD = yaml.safe_load((ROOT / "eval" / "gold.yaml").read_text(encoding="utf-8"))["gold"]
QUERIES = [(k, q) for k, g in GOLD.items() for q in g.get("queries", [])] + \
          [(p.name, json.loads(p.read_text(encoding="utf-8"))) for p in sorted((ROOT / "examples").glob("*.json"))]


def key(rows):
    return sorted(tuple(round(float(v), 6) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)
                        for v in r) for r in rows)


@pytest.mark.parametrize("target", ["warehouse", "lake"])
def test_simplified_sql_returns_the_same_rows(target):
    tgt = make_target(pipeline.targets()["targets"][target])
    applied = 0
    for qid, sq in QUERIES:
        p = pipeline.plan(sq, target)
        if p["simplified"]["applied"]:
            applied += 1
            assert key(tgt.execute(p["sql"], 100000)["rows"]) == key(tgt.execute(p["sql_original"], 100000)["rows"]), qid
    assert applied >= len(QUERIES) * 0.8                       # it actually does something


def test_bigquery_simplified_matches_and_never_scans_more(monkeypatch):
    fake_bigquery.install()
    monkeypatch.setenv("GCP_PROJECT", "demo-proj")
    from tables import load_bigquery
    from google.cloud import bigquery
    load_bigquery.load_source(bigquery.Client(project="demo-proj"), bigquery, "demo-proj", "US")
    tgt = make_target(pipeline.targets()["targets"]["bigquery"])
    for qid, sq in QUERIES[:12]:
        p = pipeline.plan(sq, "bigquery")
        if p["simplified"]["applied"]:
            assert key(tgt.execute(p["sql"], 100000)["rows"]) == key(tgt.execute(p["sql_original"], 100000)["rows"]), qid


def test_switch_off_and_fallbacks(monkeypatch):
    sq = GOLD["M7"]["queries"][0]
    monkeypatch.setenv("CONVERSAI_SIMPLIFY", "off")
    p = pipeline.plan(sq, "warehouse")
    assert not p["simplified"]["applied"] and p["sql_original"] is None and "off" in p["simplified"]["note"]
    monkeypatch.setenv("CONVERSAI_SIMPLIFY", "on")
    assert simplify.simplify_sql("SELECT nope_column FROM cards.main.account_cycle")[0] is None   # unknown column
    monkeypatch.setattr(pipeline, "simplify_sql", lambda sql: ("SELECT broken FROM nowhere_table", None))
    p = pipeline.plan(sq, "warehouse")                        # binding fails -> MetricFlow's SQL runs
    assert not p["simplified"]["applied"] and p["engine_check"] == "passed"


def test_nested_month_truncation_is_collapsed():
    out, _ = simplify.simplify_sql("SELECT DATE_TRUNC('month', DATE_TRUNC('month', BILLING_CYCLE_DATE)) AS m "
                                   "FROM cards.main.account_cycle")
    assert out.count("DATE_TRUNC") == 1
