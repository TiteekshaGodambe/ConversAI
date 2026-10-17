"""
Evaluate the semantic layer against the question set.

  python eval/run_eval.py                          # gold mode: warehouse + lake vs independent SQL
  python eval/run_eval.py --targets warehouse,lake,oracle
  python eval/run_eval.py --llm                    # also ask Claude each question (needs ANTHROPIC_API_KEY)
  python eval/run_eval.py --only H1,H3 --show-sql

Gold mode proves the LAYER is right: for every question, the governed semantic query, compiled by
MetricFlow and bound to each target, returns the same numbers as a hand-written SQL check on the raw
tables (DuckDB warehouse/cards.duckdb).

LLM mode proves the MODEL uses the layer right: the question goes to Claude, which may only answer
with governed names; its query is run and compared with the gold query's result by column name
(same numbers = pass, even if the query is phrased differently; metrics listed under `optional` in
gold.yaml may be left out, extra metrics are ignored). Guardrail questions pass when the model refuses
or asks for clarification (cannot_answer_reason).

Writes eval/results_<mode>.csv. Exit code 0 = everything passed.
"""

import argparse
import csv
import datetime as dt
import decimal
import math
import sys
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import pipeline  # noqa: E402
from engine.compiler import SemanticQueryError  # noqa: E402
from engine.targets import TargetUnavailable  # noqa: E402

SHORTHAND = {
    "{LATEST}": "(SELECT * FROM ACCOUNT_CYCLE_FACT QUALIFY ROW_NUMBER() OVER "
                "(PARTITION BY ACCOUNT_KEY ORDER BY BILLING_CYCLE_DATE DESC) = 1)",
    "{ACTIVE}": "WRITEOFF_AMT = 0 AND CREDIT_LIMIT_AMT > 0",
    "{F}": "ACCOUNT_CYCLE_FACT",
    "{D}": "ACCOUNT_DIM",
}
REL_TOL = 1e-6


def expand(sql):
    for k, v in SHORTHAND.items():
        sql = sql.replace(k, v)
    return sql


def norm(v):
    if v is None:
        return None
    if isinstance(v, decimal.Decimal):
        v = float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()[:10]
    if isinstance(v, str) and len(v) >= 10 and v[4] == "-" and v[7] == "-":
        return v[:10]
    return v


def same_value(a, b):
    a, b = norm(a), norm(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        if a is None or b is None:
            return a is b
        return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=1e-9)
    return a == b


def same_rows(got, want, ordered):
    if len(got) != len(want):
        return False, f"{len(got)} rows vs {len(want)} expected"
    key = lambda r: tuple("" if v is None else str(norm(v) if not isinstance(norm(v), float) else round(norm(v), 6))
                          for v in r)
    if not ordered:
        got, want = sorted(got, key=key), sorted(want, key=key)
    for g, w in zip(got, want):
        if len(g) != len(w) or not all(same_value(x, y) for x, y in zip(g, w)):
            return False, f"row {list(g)} vs expected {list(w)}"
    return True, ""


def check_rows(con, sql):
    return [list(r) for r in con.execute(expand(sql)).fetchall()]


def same_answer(got_out, gold_out, gold_sq, optional=()):
    """LLM mode: does the model's result answer the question the way the gold query does?

    Columns are matched by name, not position. The model must group the same way and return every
    gold metric except those marked optional; extra metrics it adds are ignored. Values of the shared
    metrics must match row for row (rows keyed by the group-by values). A gold row whose shared
    metrics are all empty (e.g. the first month of a month-over-month change) may be absent."""
    gcols = [c.lower() for c in gold_out["result"]["columns"]]
    mcols = [c.lower() for c in got_out["result"]["columns"]]
    gb = [g.lower() for g in gold_sq.get("group_by") or []]
    if sorted(c for c in mcols if c in gb) != sorted(gb) or any(
            c not in gcols and "__" in c for c in mcols):
        return False, f"grouped by {[c for c in mcols if '__' in c]}, expected {gb}"
    required = [m for m in gold_sq["metrics"] if m not in optional]
    missing = [m for m in required if m not in mcols]
    if missing:
        return False, f"missing metric(s) {missing}"
    shared = [m for m in gold_sq["metrics"] if m in mcols]

    def keyed(out, cols):
        rows = {}
        for r in out["result"]["rows"]:
            key = tuple(str(norm(r[cols.index(g)])) for g in gb)
            rows[key] = [r[cols.index(m)] for m in shared]
        return rows

    got, want = keyed(got_out, mcols), keyed(gold_out, gcols)
    for k, w in want.items():
        if k not in got:
            if all(v is None for v in w):
                continue
            return False, f"missing row {list(k)}"
        if not all(same_value(x, y) for x, y in zip(got[k], w)):
            return False, f"row {list(k)}: {got[k]} vs expected {w} ({', '.join(shared)})"
    extra = [k for k in got if k not in want]
    if extra:
        return False, f"unexpected row(s) {extra[:3]}"
    return True, ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--targets", default="warehouse,lake")
    ap.add_argument("--llm", action="store_true", help="also ask Claude each question")
    ap.add_argument("--only", help="comma-separated question ids")
    ap.add_argument("--show-sql", action="store_true")
    a = ap.parse_args()

    questions = {q["id"]: q for q in yaml.safe_load((ROOT / "eval" / "questions.yaml").read_text(encoding="utf-8"))["questions"]}
    gold = yaml.safe_load((ROOT / "eval" / "gold.yaml").read_text(encoding="utf-8"))["gold"]
    ids = a.only.split(",") if a.only else list(questions)
    targets = a.targets.split(",")
    con = duckdb.connect(str(ROOT / "warehouse" / "cards.duckdb"), read_only=True)
    results, ok_all = [], True

    print(f"\nGold mode: semantic layer on {', '.join(targets)} vs independent SQL\n")
    for qid in ids:
        g = gold.get(qid, {})
        if not g.get("queries"):
            continue
        q = questions[qid]
        for i, (sq, chk) in enumerate(zip(g["queries"], g["checks"])):
            want = check_rows(con, chk)
            for t in targets:
                status, detail = "PASS", ""
                try:
                    out = pipeline.run(sq, t, max_rows=1000)
                    ok, detail = same_rows(out["result"]["rows"], want, g.get("ordered", False))
                    status = "PASS" if ok else "FAIL"
                    if a.show_sql:
                        print(f"---- {qid} {t}\n{out['sql']}\n")
                except TargetUnavailable as exc:
                    status, detail = "SKIP", str(exc)
                except Exception as exc:                       # noqa: BLE001 - report, keep going
                    status, detail = "FAIL", f"{type(exc).__name__}: {str(exc)[:600]}"
                ok_all &= status != "FAIL"
                label = qid + (f".{i + 1}" if len(g["queries"]) > 1 else "")
                print(f"  {status}  {label:5s} {q['level']:7s} {t:9s} {q['question'][:70]}" + (f"\n        {detail}" if detail else ""))
                results.append({"mode": "gold", "id": label, "level": q["level"], "target": t, "status": status,
                                "rows": len(want), "detail": detail, "question": q["question"]})

    if a.llm:
        from engine.nl import question_to_semantic_query
        print("\nLLM mode: question -> Claude -> semantic query -> warehouse, compared with the gold answer\n")
        for qid in ids:
            q, g = questions[qid], gold.get(qid, {})
            status, detail, sq = "PASS", "", None
            try:
                sq = question_to_semantic_query(q["question"])
                if g.get("expect_cannot_answer"):
                    status = "PASS" if sq.get("cannot_answer_reason") else "FAIL"
                    detail = sq.get("cannot_answer_reason") or f"answered instead: {sq}"
                else:
                    out = pipeline.run(sq, "warehouse", max_rows=1000)
                    gold_out = pipeline.run(g["queries"][0], "warehouse", max_rows=1000)
                    ok, detail = same_answer(out, gold_out, g["queries"][0], g.get("optional", []))
                    status = "PASS" if ok else "FAIL"
            except SemanticQueryError as exc:
                if g.get("expect_cannot_answer") or "could not map" in str(exc):
                    status = "PASS" if g.get("expect_cannot_answer") else "FAIL"
                else:
                    status = "FAIL"
                detail = str(exc)[:600]
            except Exception as exc:                           # noqa: BLE001
                status, detail = "FAIL", f"{type(exc).__name__}: {str(exc)[:600]}"
            ok_all &= status == "PASS"
            print(f"  {status}  {qid:5s} {q['level']:9s} {q['question'][:70]}\n        query: {sq}"
                  + (f"\n        {detail}" if detail else ""))
            results.append({"mode": "llm", "id": qid, "level": q["level"], "target": "warehouse", "status": status,
                            "rows": "", "detail": f"{sq} | {detail}", "question": q["question"]})

    out = ROOT / "eval" / f"results_{'llm' if a.llm else 'gold'}.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0]))
        w.writeheader()
        w.writerows(results)
    counts = {s: sum(r["status"] == s for r in results) for s in ("PASS", "FAIL", "SKIP")}
    print(f"\n{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped  ->  {out.relative_to(ROOT)}")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
