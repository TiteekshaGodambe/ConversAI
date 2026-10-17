"""
Build the card-account tables locally, then split them.

  1. Generate CARD_ACCOUNT_SYNTHETIC (the original 222 columns) with one row per account per
     monthly cycle (Oct 2025 - Sep 2026 by default). The 20 semantic-layer fields and 3 companion
     fields are simulated realistically by tables/realism.py (see its docstring); the other 199
     columns keep the original Oracle value rules. Account-level columns are drawn once per
     account; cycle-level columns change every cycle. Charged-off accounts drop out ~3 cycles
     after charge-off, so a few accounts have fewer than 12 rows.
  2. Split it IN SQL, inside the database, the way you would split a real table:
       ACCOUNT_DIM        = SELECT DISTINCT ACCOUNT_KEY + account columns   (PK ACCOUNT_KEY)
       ACCOUNT_CYCLE_FACT = SELECT ACCOUNT_KEY + cycle columns              (PK ACCOUNT_HISTORY_KEY,
                                                                              FK ACCOUNT_KEY -> ACCOUNT_DIM)
  3. Run verify_split.py to prove nothing was lost.

Targets: DuckDB file warehouse/cards.duckdb and Parquet under lake/cards/ (always),
Oracle with --oracle (env ORACLE_DSN, ORACLE_USER, ORACLE_PASSWORD).

  python tables/build_tables.py                     # 5,000 accounts x 12 monthly cycles (~60,000 rows)
  python tables/build_tables.py --accounts 1000 --cycles 6
  python tables/build_tables.py --oracle
"""

import argparse
import csv
import datetime as dt
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import realism  # noqa: E402
import schema  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
DUCKDB_PATH = PROJECT / "warehouse" / "cards.duckdb"
LAKE = PROJECT / "lake" / "cards"
AS_OF = dt.date(2026, 9, 30)          # stands in for SYSDATE so runs are reproducible

WIDE, DIM, FACT = "CARD_ACCOUNT_SYNTHETIC", "ACCOUNT_DIM", "ACCOUNT_CYCLE_FACT"


def generate_wide(cols, accounts, cycles, seed):
    """One row per account per monthly cycle. The 20 semantic-layer fields (+3 companions) come from
    realism.simulate_account (consistent, calibrated); every other column keeps its original rule."""
    split = {c.name: c.table for c in cols}
    wrong = [f for f in realism.ACCOUNT_FIELDS if split.get(f) != "account"] + \
            [f for f in realism.CYCLE_FIELDS if split.get(f) != "cycle"]
    if wrong:
        raise SystemExit(f"column_split.csv disagrees with realism.py for: {wrong}")
    rng = random.Random(seed)                 # original-rule columns
    sim = random.Random(seed + 1)             # realistic fields (own stream: changes stay local)
    gens = {c.name: schema.make_generator(c, AS_OF) for c in cols}
    acct_cols = [c for c in cols if c.table in ("account", "key") and c.name not in realism.FIELDS]
    row_no, made = 0, 0
    while made < accounts:
        real_acct, real_rows = realism.simulate_account(sim, cycles)
        if not real_rows:                     # charged off and purged before the window
            continue
        made += 1
        acct = {c.name: gens[c.name](rng, made) for c in acct_cols}        # once per account
        acct.update(real_acct)
        for real in real_rows:
            row_no += 1
            row = []
            for c in cols:
                if c.name in acct:
                    row.append(acct[c.name])
                elif c.name in real:
                    row.append(real[c.name])
                else:
                    row.append(gens[c.name](rng, row_no))                  # new every cycle
            yield row


# --------------------------------------------------------------------- SQL used in every engine
def split_columns(cols):
    dim = [c for c in cols if c.table in ("key", "account")]
    fact = [c for c in cols if c.table in ("key", "cycle")]
    return dim, fact


def ddl(table, cols, dialect, pk=None):
    typ = (lambda c: c.oracle_type) if dialect == "oracle" else (lambda c: c.duckdb_type)
    lines = [f"  {c.name} {typ(c)}" for c in cols]
    if pk:
        lines.append(f"  CONSTRAINT {table}_PK PRIMARY KEY ({pk})")
    return f"CREATE TABLE {table} (\n" + ",\n".join(lines) + "\n)"


def split_sql(cols, dialect):
    dim, fact = split_columns(cols)
    d_names = ", ".join(c.name for c in dim)
    f_names = ", ".join(c.name for c in fact)
    stmts = [
        ddl(DIM, dim, dialect, pk="ACCOUNT_KEY"),
        ddl(FACT, fact, dialect, pk="ACCOUNT_HISTORY_KEY"),
        f"INSERT INTO {DIM} ({d_names}) SELECT DISTINCT {d_names} FROM {WIDE}",
        f"INSERT INTO {FACT} ({f_names}) SELECT {f_names} FROM {WIDE}",
    ]
    if dialect == "oracle":   # DuckDB cannot add a foreign key after creation; the verifier checks orphans
        stmts.append(f"ALTER TABLE {FACT} ADD CONSTRAINT {FACT}_FK FOREIGN KEY (ACCOUNT_KEY) "
                     f"REFERENCES {DIM} (ACCOUNT_KEY)")
    return stmts


# --------------------------------------------------------------------- loaders
def load_duckdb(cols, rows):
    DUCKDB_PATH.parent.mkdir(exist_ok=True)
    for p in (DUCKDB_PATH, DUCKDB_PATH.with_suffix(".duckdb.wal")):
        if p.exists():
            p.unlink()
    con = duckdb.connect(str(DUCKDB_PATH))
    con.execute(ddl(WIDE, cols, "duckdb"))
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "wide.csv"
        with open(f, "w", newline="") as fh:
            csv.writer(fh).writerows(rows)
        con.execute(f"COPY {WIDE} FROM '{f.as_posix()}' (HEADER false)")
    for s in split_sql(cols, "duckdb"):
        con.execute(s)
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (WIDE, DIM, FACT)}
    if LAKE.exists():
        shutil.rmtree(LAKE)
    for t in (WIDE, DIM, FACT):
        (LAKE / t.lower()).mkdir(parents=True)
        con.execute(f"COPY {t} TO '{(LAKE / t.lower() / 'part-0.parquet').as_posix()}' (FORMAT PARQUET)")
    con.close()
    return counts


def load_oracle(cols, rows):
    import oracledb
    con = oracledb.connect(user=os.environ["ORACLE_USER"], password=os.environ["ORACLE_PASSWORD"],
                           dsn=os.environ["ORACLE_DSN"])
    cur = con.cursor()
    for t in (FACT, DIM, WIDE):
        try:
            cur.execute(f"DROP TABLE {t} CASCADE CONSTRAINTS PURGE")
        except oracledb.DatabaseError:
            pass
    cur.execute(ddl(WIDE, cols, "oracle"))
    binds = ", ".join(f":{i + 1}" for i in range(len(cols)))
    ins = f"INSERT INTO {WIDE} ({', '.join(c.name for c in cols)}) VALUES ({binds})"
    for i in range(0, len(rows), 2000):
        cur.executemany(ins, rows[i:i + 2000])
    con.commit()
    for s in split_sql(cols, "oracle"):
        cur.execute(s)
    con.commit()
    for t in (WIDE, DIM, FACT):
        cur.callproc("DBMS_STATS.GATHER_TABLE_STATS", [os.environ["ORACLE_USER"].upper(), t])
    counts = {t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (WIDE, DIM, FACT)}
    con.close()
    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--accounts", type=int, default=5000)
    ap.add_argument("--cycles", type=int, default=12)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--oracle", action="store_true", help="also build the three tables in Oracle")
    ap.add_argument("--print-sql", action="store_true", help="print the Oracle DDL/split SQL and exit")
    a = ap.parse_args()

    cols = schema.load_columns()
    if a.print_sql:
        print(";\n\n".join([ddl(WIDE, cols, "oracle")] + split_sql(cols, "oracle")) + ";")
        return
    dim, fact = split_columns(cols)
    print(f"Columns: {len(cols)} original -> {DIM} {len(dim)} + {FACT} {len(fact)} (ACCOUNT_KEY in both)")
    rows = list(generate_wide(cols, a.accounts, a.cycles, a.seed))
    counts = load_duckdb(cols, rows)
    print(f"DuckDB  {DUCKDB_PATH.relative_to(PROJECT)}: " + ", ".join(f"{t} {n:,}" for t, n in counts.items()))
    print(f"Parquet {LAKE.relative_to(PROJECT)}/<table>/part-0.parquet")
    if a.oracle:
        counts = load_oracle(cols, rows)
        print("Oracle  : " + ", ".join(f"{t} {n:,}" for t, n in counts.items()))
    print("\nNext: python tables/verify_split.py" + (" --target oracle" if a.oracle else ""))


if __name__ == "__main__":
    main()
