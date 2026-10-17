"""
Check that the 20 semantic-layer fields follow the business rules and reference ranges, and
profile them.

  python tables/check_realism.py            # DuckDB warehouse/cards.duckdb
Exit code 0 = every rule holds.
"""

import sys
from pathlib import Path

import duckdb

DB = Path(__file__).resolve().parent.parent / "warehouse" / "cards.duckdb"
F, D = "ACCOUNT_CYCLE_FACT", "ACCOUNT_DIM"

# production share of cycles by days-past-due bucket (percent), from the frequency table supplied
PROD = {0: 98.37, 30: 0.995, 60: 0.187, 90: 0.109, 120: 0.084, 150: 0.072, 180: 0.067}

RULES = [
    ("Bureau score within 0-950", f"SELECT COUNT(*) FROM {F} WHERE BUREAU_SCORE NOT BETWEEN 0 AND 950"),
    ("Behavior score within 0-999", f"SELECT COUNT(*) FROM {F} WHERE BEHAVIOR_SCORE NOT BETWEEN 0 AND 999"),
    ("Purchase APR within 15-35%", f"SELECT COUNT(*) FROM {F} WHERE LAST_MRCH_APR NOT BETWEEN 15 AND 35"),
    ("Open to buy = credit limit - balance",
     f"SELECT COUNT(*) FROM {F} WHERE ABS(OPEN_TO_BUY_AMT - (CREDIT_LIMIT_AMT - CURR_MNTH_BALANCE_AMT)) > 0.01"),
    ("Days past due are 0 or 30/31, 60/61 ...",
     f"SELECT COUNT(*) FROM {F} WHERE NBR_DAYS_DELQ_AT_CYC % 30 NOT IN (0, 1) OR NBR_DAYS_DELQ_AT_CYC = 1"),
    ("No past-due amount when current",
     f"SELECT COUNT(*) FROM {F} WHERE NBR_DAYS_DELQ_AT_CYC = 0 AND TOTAL_PAST_DUE_AMT <> 0"),
    ("Past-due amount when delinquent (before charge-off)",
     f"SELECT COUNT(*) FROM {F} WHERE NBR_DAYS_DELQ_AT_CYC BETWEEN 30 AND 179 AND TOTAL_PAST_DUE_AMT <= 0"),
    ("No payment recorded in a missed (newly delinquent) cycle",
     f"SELECT COUNT(*) FROM {F} WHERE LATE_CHARGE_ASSESSED > 0 AND PYMT_AMT <> 0"),
    ("Late fee only when delinquent",
     f"SELECT COUNT(*) FROM {F} WHERE LATE_CHARGE_ASSESSED > 0 AND NBR_DAYS_DELQ_AT_CYC = 0"),
    ("Write-off only at 180+ days past due",
     f"SELECT COUNT(*) FROM {F} WHERE WRITEOFF_AMT <> 0 AND NBR_DAYS_DELQ_AT_CYC < 180"),
    ("No spend once 60+ days past due",
     f"SELECT COUNT(*) FROM {F} WHERE NBR_DAYS_DELQ_AT_CYC >= 60 AND (NET_SALES > 0 OR CASH_ADV_AMT > 0)"),
    ("Charged-off accounts: exactly one write-off row each",
     f"SELECT COUNT(*) FROM (SELECT ACCOUNT_KEY FROM {F} WHERE WRITEOFF_AMT <> 0 GROUP BY 1 HAVING COUNT(*) > 1)"),
    ("WRITEOFF_DATE = date of the write-off cycle",
     f"SELECT COUNT(*) FROM {F} f JOIN {D} d USING (ACCOUNT_KEY) WHERE f.WRITEOFF_AMT <> 0 "
     f"AND d.WRITEOFF_DATE IS DISTINCT FROM f.BILLING_CYCLE_DATE"),
    ("EXTERNAL_STATUS 'Z' exactly for charged-off accounts",
     f"SELECT COUNT(*) FROM {D} WHERE (EXTERNAL_STATUS = 'Z') <> (WRITEOFF_DATE IS NOT NULL)"),
    ("EXTERNAL_STATUS codes: C E F I L U Z or blank",
     f"SELECT COUNT(*) FROM {D} WHERE EXTERNAL_STATUS IS NOT NULL AND EXTERNAL_STATUS NOT IN ('C','E','F','I','L','U','Z')"),
    ("INTERNAL_STATUS codes: D N O X or blank",
     f"SELECT COUNT(*) FROM {D} WHERE INTERNAL_STATUS IS NOT NULL AND INTERNAL_STATUS NOT IN ('D','N','O','X')"),
    ("INTERNAL_STATUS matches the latest cycle",
     f"""SELECT COUNT(*) FROM {D} d JOIN (SELECT * FROM {F} QUALIFY ROW_NUMBER() OVER
         (PARTITION BY ACCOUNT_KEY ORDER BY BILLING_CYCLE_DATE DESC) = 1) l USING (ACCOUNT_KEY)
         WHERE d.EXTERNAL_STATUS IS DISTINCT FROM 'Z' AND d.INTERNAL_STATUS IS DISTINCT FROM
         CASE WHEN l.NBR_DAYS_DELQ_AT_CYC > 0 AND l.CURR_MNTH_BALANCE_AMT > l.CREDIT_LIMIT_AMT THEN 'X'
              WHEN l.NBR_DAYS_DELQ_AT_CYC > 0 THEN 'D'
              WHEN l.CURR_MNTH_BALANCE_AMT > l.CREDIT_LIMIT_AMT THEN 'O'
              WHEN l.CURR_MNTH_BALANCE_AMT < 0 THEN 'N' END"""),
    ("BANKRUPTCY_CODE codes: 0 C P R X Y",
     f"SELECT COUNT(*) FROM {D} WHERE BANKRUPTCY_CODE NOT IN ('0','C','P','R','X','Y')"),
    ("'Y' (not reported bankrupt before write-off) only on charged-off accounts",
     f"SELECT COUNT(*) FROM {D} WHERE BANKRUPTCY_CODE = 'Y' AND WRITEOFF_DATE IS NULL"),
    ("Bankruptcy filing date only for bankruptcy codes C P R X",
     f"SELECT COUNT(*) FROM {D} WHERE (BANKRUPTCY_FILING_DATE IS NOT NULL) <> (BANKRUPTCY_CODE IN ('C','P','R','X'))"),
    ("EBILL_IND codes: 0 B C T X or blank",
     f"SELECT COUNT(*) FROM {D} WHERE EBILL_IND IS NOT NULL AND EBILL_IND NOT IN ('0','B','C','T','X')"),
    ("BILLING_CYCLE_DAY = day of BILLING_CYCLE_DATE",
     f"SELECT COUNT(*) FROM {F} f JOIN {D} d USING (ACCOUNT_KEY) WHERE d.BILLING_CYCLE_DAY <> DAY(f.BILLING_CYCLE_DATE)"),
    ("BILLING_YEAR_MTH = YYYYMM of BILLING_CYCLE_DATE",
     f"SELECT COUNT(*) FROM {F} WHERE BILLING_YEAR_MTH <> YEAR(BILLING_CYCLE_DATE) * 100 + MONTH(BILLING_CYCLE_DATE)"),
]


def main():
    if not DB.exists():
        sys.exit("No database yet - run: python tables/build_tables.py")
    con = duckdb.connect(str(DB), read_only=True)
    q = lambda sql: con.execute(sql).fetchall()

    print("\nBusiness rules (number of violating rows; must be 0)\n")
    ok = True
    for name, sql in RULES:
        n = q(sql)[0][0]
        ok &= n == 0
        print(f"  {'PASS' if n == 0 else 'FAIL'}  {name:62s} {n}")

    print("\nDays past due vs production (% of cycles)\n")
    print("  bucket   local   production")
    total = q(f"SELECT COUNT(*) FROM {F}")[0][0]
    shares = dict(q(f"SELECT NBR_DAYS_DELQ_AT_CYC // 30 * 30, COUNT(*) * 100.0 / {total} FROM {F} GROUP BY 1"))
    for b, ref in PROD.items():
        print(f"  {b:>6}   {shares.get(b, 0):6.3f}  {ref:6.3f}")
    print(f"  {'210+':>6}   {sum(v for k, v in shares.items() if k >= 210):6.3f}  {0.110:6.3f}")

    print("\nProfile of the 20 fields\n")
    nums = ["CREDIT_LIMIT_AMT", "CURR_MNTH_BALANCE_AMT", "OPEN_TO_BUY_AMT", "BUREAU_SCORE", "BEHAVIOR_SCORE",
            "NBR_DAYS_DELQ_AT_CYC", "TOTAL_PAST_DUE_AMT", "MIN_PYMT_AMT", "PYMT_AMT", "WRITEOFF_AMT",
            "LATE_CHARGE_ASSESSED", "LAST_MRCH_APR", "NET_SALES", "CASH_ADV_AMT"]
    print(f"  {'field':24s} {'min':>12} {'avg':>12} {'max':>12} {'non-zero %':>11}")
    for c in nums:
        mn, av, mx, nz = q(f"SELECT MIN({c}), AVG({c}), MAX({c}), AVG(CASE WHEN {c} <> 0 THEN 100.0 ELSE 0 END) FROM {F}")[0]
        print(f"  {c:24s} {float(mn):12,.2f} {float(av):12,.2f} {float(mx):12,.2f} {float(nz):10.2f}%")
    for c in ["INTERNAL_STATUS", "EXTERNAL_STATUS", "BANKRUPTCY_CODE", "EBILL_IND"]:
        vals = q(f"SELECT COALESCE({c}, 'blank'), COUNT(*) FROM {D} GROUP BY 1 ORDER BY 2 DESC")
        print(f"  {c:24s} " + ", ".join(f"{v} {n:,}" for v, n in vals))
    months = q(f"SELECT MIN(BILLING_CYCLE_DATE), MAX(BILLING_CYCLE_DATE), COUNT(DISTINCT BILLING_YEAR_MTH) FROM {F}")[0]
    print(f"  {'BILLING_CYCLE_DATE':24s} {months[0]} to {months[1]} ({months[2]} months)")
    wo = q(f"SELECT COUNT(*), MIN(WRITEOFF_DATE), MAX(WRITEOFF_DATE) FROM {D} WHERE WRITEOFF_DATE IS NOT NULL")[0]
    print(f"  {'WRITEOFF_DATE':24s} {wo[0]:,} charged-off accounts, {wo[1]} to {wo[2]}")

    print("\nRelationships (sanity)\n")
    for label, sql in [
        ("Avg bureau score: current vs delinquent (30-179 days)",
         f"SELECT AVG(BUREAU_SCORE) FILTER (WHERE NBR_DAYS_DELQ_AT_CYC = 0), "
         f"AVG(BUREAU_SCORE) FILTER (WHERE NBR_DAYS_DELQ_AT_CYC BETWEEN 30 AND 179) FROM {F}"),
        ("30+ rate (%): bureau < 620 vs 740+",
         f"SELECT AVG(CASE WHEN NBR_DAYS_DELQ_AT_CYC >= 30 THEN 100.0 ELSE 0 END) FILTER (WHERE BUREAU_SCORE < 620), "
         f"AVG(CASE WHEN NBR_DAYS_DELQ_AT_CYC >= 30 THEN 100.0 ELSE 0 END) FILTER (WHERE BUREAU_SCORE >= 740) FROM {F}"),
        ("Portfolio utilization % (balance / limit, open accounts)",
         f"SELECT SUM(CURR_MNTH_BALANCE_AMT) * 100 / SUM(CREDIT_LIMIT_AMT), NULL FROM {F} WHERE CREDIT_LIMIT_AMT > 0"),
    ]:
        a, b = q(sql)[0]
        print(f"  {label:58s} {float(a):8.2f}" + (f" vs {float(b):8.2f}" if b is not None else ""))

    print(f"\n{'ALL RULES HOLD.' if ok else 'SOME RULES FAILED - see FAIL lines.'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
