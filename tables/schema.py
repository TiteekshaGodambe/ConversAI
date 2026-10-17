"""Reads the ORIGINAL table definition and value rules from source/synthetic_card_account_data.sql
(the Oracle script that built CARD_ACCOUNT_SYNTHETIC), so the local tables follow exactly the same
222 columns, data types and per-column generation rules."""

import csv
import random
import re
import string
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE_SQL = HERE / "source" / "synthetic_card_account_data.sql"
SPLIT_CSV = HERE / "column_split.csv"


@dataclass
class Column:
    name: str
    oracle_type: str          # as written in the original DDL, e.g. NUMBER(15,2)
    rule: str                 # original generation expression
    table: str = ""           # account | cycle | key  (from column_split.csv)

    @property
    def duckdb_type(self) -> str:
        t = self.oracle_type.upper()
        if t.startswith("VARCHAR2"):
            return "VARCHAR"
        if t == "DATE":
            return "DATE"
        if t == "NUMBER":
            return "BIGINT"
        p, _, s = t[t.index("(") + 1:-1].partition(",")
        p, s = int(p), int(s or 0)
        if s == 0 and p <= 18:
            return "BIGINT"
        return f"DECIMAL({min(p, 38)},{s})"


def load_columns() -> list[Column]:
    sql = SOURCE_SQL.read_text()
    body = sql.split("CREATE TABLE CARD_ACCOUNT_SYNTHETIC (", 1)[1].split(");", 1)[0]
    ddl = [l.strip().rstrip(",").split(None, 1) for l in body.strip().splitlines() if l.strip()]
    rules = {}
    for line in sql.split("VALUES (", 1)[1].splitlines():
        m = re.match(r"\s*(.+?),?\s+--\s+([A-Z0-9_]+)\s*$", line)
        if m:
            rules[m.group(2)] = m.group(1).strip()
    cols = [Column(n, t.strip(), rules.get(n, "")) for n, t in ddl]
    missing = [c.name for c in cols if not c.rule]
    if missing:
        raise ValueError(f"No generation rule found for: {missing}")
    split = {r["column"]: r["table"] for r in csv.DictReader(SPLIT_CSV.open())}
    unknown = [c.name for c in cols if c.name not in split]
    if unknown:
        raise ValueError(f"column_split.csv has no entry for: {unknown}")
    for c in cols:
        c.table = split[c.name]
        if c.table not in ("account", "cycle", "key"):
            raise ValueError(f"{c.name}: table must be account, cycle or key, not '{c.table}'")
    return cols


# --------------------------------------------------------------------- value rules
NUM = r"(-?\d+(?:\.\d+)?)"
PATTERNS = [
    ("seq",      re.compile(rf"^{NUM}\s*\+\s*rn$")),
    ("score",    re.compile(rf"^ROUND\({NUM}\s*\+\s*DBMS_RANDOM\.VALUE\({NUM},\s*{NUM}\)\)$")),
    ("round",    re.compile(rf"^ROUND\(DBMS_RANDOM\.VALUE\({NUM},\s*{NUM}\),\s*(\d+)\)$")),
    ("days_ago", re.compile(rf"^TRUNC\(SYSDATE\)\s*-\s*TRUNC\(DBMS_RANDOM\.VALUE\({NUM},\s*{NUM}\)\)$")),
    ("int",      re.compile(rf"^TRUNC\(DBMS_RANDOM\.VALUE\({NUM},\s*{NUM}\)\)$")),
    ("string",   re.compile(r"^DBMS_RANDOM\.STRING\('([UX])',\s*(\d+)\)$")),
    ("letter",   re.compile(rf"^CHR\({NUM}\s*\+\s*TRUNC\(DBMS_RANDOM\.VALUE\({NUM},\s*{NUM}\)\)\)$")),
    ("digits",   re.compile(r"^LPAD\(TRUNC\(DBMS_RANDOM\.VALUE\(0,\s*POWER\(10,\s*(\d+)\)\)\),\s*(\d+),\s*'0'\)$")),
    ("flag",     re.compile(rf"^CASE WHEN DBMS_RANDOM\.VALUE < {NUM} THEN 'Y' ELSE 'N' END$")),
]
ALNUM = string.ascii_uppercase + string.digits


def make_generator(col: Column, as_of: date):
    """Returns f(rng, seq_no) -> value, reproducing the original Oracle expression."""
    for kind, rx in PATTERNS:
        m = rx.match(col.rule)
        if not m:
            continue
        g = m.groups()
        if kind == "seq":
            base = int(float(g[0]))
            return lambda rng, n: base + n
        if kind == "score":
            b, lo, hi = map(float, g)
            return lambda rng, n: int(round(b + rng.uniform(lo, hi)))
        if kind == "round":
            lo, hi, d = float(g[0]), float(g[1]), int(g[2])
            return lambda rng, n: round(rng.uniform(lo, hi), d)
        if kind == "days_ago":
            lo, hi = float(g[0]), float(g[1])
            return lambda rng, n: as_of - timedelta(days=int(rng.uniform(lo, hi)))
        if kind == "int":
            lo, hi = float(g[0]), float(g[1])
            return lambda rng, n: int(rng.uniform(lo, hi))
        if kind == "string":
            chars = string.ascii_uppercase if g[0] == "U" else ALNUM
            k = int(g[1])
            return lambda rng, n: "".join(rng.choice(chars) for _ in range(k))
        if kind == "letter":
            base, lo, hi = int(float(g[0])), float(g[1]), float(g[2])
            return lambda rng, n: chr(base + int(rng.uniform(lo, hi)))
        if kind == "digits":
            p, width = int(g[0]), int(g[1])
            return lambda rng, n: str(rng.randrange(10 ** p)).zfill(width)
        if kind == "flag":
            p = float(g[0])
            return lambda rng, n: "Y" if rng.random() < p else "N"
    raise ValueError(f"{col.name}: unrecognised rule {col.rule!r}")
