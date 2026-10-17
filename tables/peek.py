"""
Look inside the local tables.

  python tables/peek.py                       # row counts + column lists for all three tables
  python tables/peek.py ACCOUNT_DIM           # first 5 rows of one table (first 8 columns)
  python tables/peek.py ACCOUNT_CYCLE_FACT 1000001   # every column for one account's 12 cycles
"""
import sys
from pathlib import Path

import duckdb

DB = Path(__file__).resolve().parent.parent / "warehouse" / "cards.duckdb"
TABLES = ["CARD_ACCOUNT_SYNTHETIC", "ACCOUNT_DIM", "ACCOUNT_CYCLE_FACT"]


def main():
    if not DB.exists():
        sys.exit("No database yet - run: python tables/build_tables.py")
    con = duckdb.connect(str(DB), read_only=True)
    args = sys.argv[1:]
    if not args:
        for t in TABLES:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            cols = [r[0] for r in con.execute(f"DESCRIBE {t}").fetchall()]
            print(f"\n{t}: {n:,} rows, {len(cols)} columns")
            print("  " + ", ".join(cols))
        return
    table = args[0].upper()
    if table not in TABLES:
        sys.exit(f"Table must be one of {TABLES}")
    if len(args) > 1:
        cur = con.execute(f"SELECT * FROM {table} WHERE ACCOUNT_KEY = ? ORDER BY 1", [int(args[1])])
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        print(f"{table}, ACCOUNT_KEY {args[1]}: {len(rows)} row(s)\n")
        for i, c in enumerate(cols):
            print(f"  {c:32s} " + " | ".join(str(r[i]) for r in rows))
        return
    cur = con.execute(f"SELECT * FROM {table} ORDER BY ACCOUNT_KEY LIMIT 5")
    cols = [d[0] for d in cur.description][:8]
    rows = [r[:8] for r in cur.fetchall()]
    w = [max(len(c), *(len(str(r[i])) for r in rows)) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(x) for c, x in zip(cols, w)))
    for r in rows:
        print("  ".join(str(v).ljust(x) for v, x in zip(r, w)))
    print(f"... first 8 of {len(cur.description)} columns")


if __name__ == "__main__":
    main()
