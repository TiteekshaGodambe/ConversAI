"""Same semantic query, every engine, same answer. Oracle is included when ORACLE_DSN is set.
Run:  python -m pytest -q tests   (or: python tests/test_consistency.py)

The full accuracy check (semantic layer vs hand-written SQL, all 24 questions) is eval/run_eval.py."""

import json
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import pipeline  # noqa: E402

QUERIES = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((ROOT / "examples").glob("*.json"))]
GOLD = [sq for g in yaml.safe_load((ROOT / "eval" / "gold.yaml").read_text(encoding="utf-8"))["gold"].values()
        for sq in g.get("queries", [])]
TARGETS = ["warehouse", "lake"] + (["oracle"] if os.environ.get("ORACLE_DSN") else [])
DUCKDB_ONLY = re.compile(r"DATE_TRUNC|TIMESTAMP_TRUNC|\brange\(|QUALIFY|CAST\('\d{4}-|::", re.IGNORECASE)


def norm(res):
    return sorted(tuple(round(float(v), 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)[:10]
                        for v in row) for row in res["rows"])


def test_targets_agree():
    for sq in QUERIES:
        answers = {t: norm(pipeline.run(sq, t)["result"]) for t in TARGETS}
        first = next(iter(answers.values()))
        assert first, f"empty result for {sq}"
        for t, a in answers.items():
            assert a == first, f"{t} disagrees for {json.dumps(sq)}"


def test_oracle_sql_is_oracle_shaped():
    """Every gold query translates to Oracle without DuckDB-only syntax or '_' identifiers."""
    for sq in GOLD:
        sql = pipeline.plan(sq, "oracle", check=False)["sql"]
        assert not DUCKDB_ONLY.search(sql), sql
        assert not re.search(r"(?<![\w'])_\w", sql), sql


def test_hidden_metrics_not_exposed():
    cat = pipeline.catalog()
    assert cat.hidden_metrics and not set(cat.hidden_metrics) & set(cat.metrics)
    assert "metric_time__day" not in cat.groupable        # billing data is monthly


def test_point_in_time_note():
    sq = {"metrics": ["accounts"], "where": [{"dimension": "account_cycle__bureau_score", "op": "<", "value": 620}]}
    assert "ANY cycle" in pipeline.plan(sq, check=False)["notes"][0]


if __name__ == "__main__":
    test_targets_agree()
    test_oracle_sql_is_oracle_shaped()
    test_hidden_metrics_not_exposed()
    test_point_in_time_note()
    print(f"OK: {len(QUERIES)} example queries agree on {', '.join(TARGETS)}; "
          f"{len(GOLD)} gold queries translate cleanly to Oracle")
