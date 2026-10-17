"""
Prove that ACCOUNT_DIM + ACCOUNT_CYCLE_FACT hold exactly what CARD_ACCOUNT_SYNTHETIC holds.

  1 Columns    every original column is in exactly one table (ACCOUNT_KEY in both), as column_split.csv says
  2 Types      each column keeps its original data type
  3 Rows       cycle rows = original rows; accounts = distinct ACCOUNT_KEY in the original
  4 Keys       primary keys unique and not null; no cycle row without its account; no account without cycles
  5 Grain      every account-level column has ONE value per account in the original (else the split loses data)
  6 Rebuild    joining the two tables back reproduces the original exactly, row for row, in both directions

  python tables/verify_split.py                 # DuckDB warehouse/cards.duckdb
  python tables/verify_split.py --target lake   # the Parquet copies
  python tables/verify_split.py --target oracle # env ORACLE_DSN / ORACLE_USER / ORACLE_PASSWORD
Exit code 0 = all checks passed.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schema  # noqa: E402
from build_tables import DIM, DUCKDB_PATH, FACT, LAKE, WIDE, split_columns  # noqa: E402


class DB:
    def __init__(self, target):
        self.target = target
        if target == "oracle":
            import oracledb
            self.con = oracledb.connect(user=os.environ["ORACLE_USER"], password=os.environ["ORACLE_PASSWORD"],
                                        dsn=os.environ["ORACLE_DSN"])
            self.minus = "MINUS"
        else:
            import duckdb
            if target == "lake":
                self.con = duckdb.connect(":memory:")
                for t in (WIDE, DIM, FACT):
                    self.con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet("
                                     f"'{(LAKE / t.lower() / '*.parquet').as_posix()}')")
            else:
                self.con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
            self.minus = "EXCEPT"

    def one(self, sql):
        cur = self.con.cursor()
        cur.execute(sql)
        return cur.fetchone()

    def columns(self, table):
        if self.target == "oracle":
            sql = (f"SELECT column_name, data_type, data_precision, data_scale, char_length "
                   f"FROM user_tab_columns WHERE table_name = '{table}' ORDER BY column_id")
        else:
            sql = (f"SELECT upper(column_name), data_type, numeric_precision, numeric_scale, NULL "
                   f"FROM information_schema.columns WHERE upper(table_name) = '{table}' ORDER BY ordinal_position")
        cur = self.con.cursor()
        cur.execute(sql)
        return {r[0]: tuple(r[1:]) for r in cur.fetchall()}

    def as_text(self, col):
        return f"TO_CHAR({col})" if self.target == "oracle" else f"CAST({col} AS VARCHAR)"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", choices=["warehouse", "lake", "oracle"], default="warehouse")
    a = ap.parse_args()
    db = DB(a.target)
    cols = schema.load_columns()
    dim_cols, fact_cols = split_columns(cols)
    results = []

    def check(name, ok, detail):
        results.append((name, ok, detail))

    def guarded(name, fn):
        """Run a check; if the tables are too broken for its SQL (e.g. a column is missing), report FAIL."""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).strip().splitlines()[0][:160]
            check(name, False, f"could not run: {msg}")

    # 1 Columns ---------------------------------------------------------------
    w, d, f = db.columns(WIDE), db.columns(DIM), db.columns(FACT)
    missing = [c for c in w if c not in d and c not in f]
    extra = [c for c in list(d) + list(f) if c not in w]
    both = sorted(set(d) & set(f))
    wrong_place = [c.name for c in dim_cols if c.name not in d] + [c.name for c in fact_cols if c.name not in f]
    check("1 Columns", len(w) == len(cols) and not missing and not extra and both == ["ACCOUNT_KEY"] and not wrong_place,
          f"original {len(w)}, {DIM} {len(d)}, {FACT} {len(f)}, shared {both}, "
          f"missing {missing or 'none'}, unexpected {extra or 'none'}, misplaced {wrong_place or 'none'}")

    # 2 Types -----------------------------------------------------------------
    diff = [c for c in w if (c in d and d[c] != w[c]) or (c in f and f[c] != w[c])]
    check("2 Types", not diff, f"{len(w)} columns compared, differences: {diff or 'none'}")

    # 3 Rows ------------------------------------------------------------------
    counts = {}

    def rows():
        n_wide, n_accts = db.one(f"SELECT COUNT(*), COUNT(DISTINCT ACCOUNT_KEY) FROM {WIDE}")
        n_dim, = db.one(f"SELECT COUNT(*) FROM {DIM}")
        n_fact, = db.one(f"SELECT COUNT(*) FROM {FACT}")
        counts["wide"] = n_wide
        check("3 Rows", n_fact == n_wide and n_dim == n_accts,
              f"original {n_wide:,} rows / {n_accts:,} accounts; {FACT} {n_fact:,}; {DIM} {n_dim:,}")
    guarded("3 Rows", rows)

    # 4 Keys ------------------------------------------------------------------
    def keys():
        dim_dup, = db.one(f"SELECT COUNT(*) - COUNT(DISTINCT ACCOUNT_KEY) FROM {DIM}")
        dim_null, = db.one(f"SELECT COUNT(*) FROM {DIM} WHERE ACCOUNT_KEY IS NULL")
        fact_dup, = db.one(f"SELECT COUNT(*) - COUNT(DISTINCT ACCOUNT_HISTORY_KEY) FROM {FACT}")
        cyc_dup, = db.one(f"SELECT COUNT(*) FROM (SELECT ACCOUNT_KEY, BILLING_CYCLE_DATE FROM {FACT} "
                          f"GROUP BY ACCOUNT_KEY, BILLING_CYCLE_DATE HAVING COUNT(*) > 1) x")
        orphans, = db.one(f"SELECT COUNT(*) FROM {FACT} f WHERE NOT EXISTS "
                          f"(SELECT 1 FROM {DIM} d WHERE d.ACCOUNT_KEY = f.ACCOUNT_KEY)")
        childless, = db.one(f"SELECT COUNT(*) FROM {DIM} d WHERE NOT EXISTS "
                            f"(SELECT 1 FROM {FACT} f WHERE f.ACCOUNT_KEY = d.ACCOUNT_KEY)")
        check("4 Keys", not any([dim_dup, dim_null, fact_dup, cyc_dup, orphans, childless]),
              f"dup account keys {dim_dup}, null account keys {dim_null}, dup history keys {fact_dup}, "
              f"dup account-months {cyc_dup}, cycle rows without account {orphans}, accounts without cycles {childless}")

    guarded("4 Keys", keys)

    # 5 Grain -----------------------------------------------------------------
    def grain():
        acct_only = [c.name for c in dim_cols if c.name != "ACCOUNT_KEY"]
        exprs = ", ".join(f"COUNT(DISTINCT COALESCE({db.as_text(c)}, '<null>')) AS {c}" for c in acct_only)
        cur = db.con.cursor()
        cur.execute(f"SELECT {exprs} FROM {WIDE} GROUP BY ACCOUNT_KEY")
        bad = set()
        for row in cur.fetchall():
            bad |= {acct_only[i] for i, v in enumerate(row) if v > 1}
        check("5 Grain", not bad, f"{len(acct_only)} account-level columns; varying within an account: "
                                  f"{sorted(bad) or 'none'}")

    guarded("5 Grain", grain)

    # 6 Rebuild ---------------------------------------------------------------
    def rebuild():
        sel_wide = ", ".join(c.name for c in cols)
        sel_join = ", ".join(("d." if c.table == "account" else "f.") + c.name for c in cols)
        rebuilt = f"SELECT {sel_join} FROM {FACT} f JOIN {DIM} d ON d.ACCOUNT_KEY = f.ACCOUNT_KEY"
        lost, = db.one(f"SELECT COUNT(*) FROM (SELECT {sel_wide} FROM {WIDE} {db.minus} {rebuilt}) x")
        invented, = db.one(f"SELECT COUNT(*) FROM ({rebuilt} {db.minus} SELECT {sel_wide} FROM {WIDE}) x")
        n_rebuilt, = db.one(f"SELECT COUNT(*) FROM ({rebuilt}) x")
        check("6 Rebuild", lost == 0 and invented == 0 and n_rebuilt == counts.get("wide"),
              f"rebuilt {n_rebuilt:,} rows x {len(cols)} columns; original rows not reproduced {lost}, "
              f"rows not in original {invented}")

    guarded("6 Rebuild", rebuild)

    # per-column report (open in Excel) ----------------------------------------
    notes = {}
    import csv
    with open(schema.SPLIT_CSV, newline="") as fh:
        notes = {r["column"]: r.get("note", "") for r in csv.DictReader(fh)}
    report = Path(__file__).resolve().parent / f"split_report_{a.target}.csv"
    with open(report, "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["column_order", "column", "original_type_oracle", "local_type", "in_ACCOUNT_DIM",
                     "in_ACCOUNT_CYCLE_FACT", "expected_table", "type_matches", "status", "note"])
        for i, c in enumerate(cols, 1):
            in_d, in_f = c.name in d, c.name in f
            expected_ok = (in_d and in_f) if c.table == "key" else (in_d and not in_f) if c.table == "account" \
                else (in_f and not in_d)
            type_ok = all(x[c.name] == w.get(c.name) for x in (d, f) if c.name in x)
            wr.writerow([i, c.name, c.oracle_type, " ".join(str(v) for v in w.get(c.name, ("missing",)) if v is not None),
                         "Y" if in_d else "", "Y" if in_f else "",
                         {"key": "both", "account": DIM, "cycle": FACT}[c.table],
                         "Y" if type_ok else "N", "OK" if expected_ok and type_ok else "CHECK", notes.get(c.name, "")])

    # report ------------------------------------------------------------------
    print(f"\nSplit verification on {a.target}\n")
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:10s} {detail}")
    passed = all(ok for _, ok, _ in results)
    print(f"\n{'ALL CHECKS PASSED: the split is lossless.' if passed else 'SPLIT IS NOT LOSSLESS - see FAIL lines.'}")
    print(f"Column-by-column report: tables/{report.name}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
