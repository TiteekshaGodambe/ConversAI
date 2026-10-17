"""
Realistic, internally consistent values for the 20 semantic-layer fields (plus 3 companion fields).

Every other column keeps the original Oracle generator rule. These fields are instead simulated
account by account, month by month, so they agree with each other:

  balance(t) = balance(t-1) - payment + net sales + cash advance + interest + late fee
  open to buy = credit limit - balance
  delinquency rolls 30 -> 60 -> 90 ... (or cures), calibrated to the production frequency table
  past due = missed minimum payments while delinquent, 0 when current
  charge-off at 180 days past due: WRITEOFF_AMT = balance, WRITEOFF_DATE set, EXTERNAL_STATUS 'Z'
  charged-off accounts keep ageing (210, 240, 270) with zero balance, then drop out, as in production

Code lists and ranges come from the business references supplied:
  EXTERNAL_STATUS  C Closed, E Revoked, F Frozen, I Interest Accrual Prohibited, L Lost, U Stolen,
                   Z Charged Off, blank Normal
  INTERNAL_STATUS  D Delinquent, N Credit Balance, O Overlimit, X Delinquent & Overlimit, blank Normal
  BANKRUPTCY_CODE  0 Not bankrupt, C Reaffirm confirmed, P Reaffirm pending, R Reaffirmed,
                   X Bankrupt, Y Not reported as bankrupt prior to write-off
  EBILL_IND        0, B, C, T, X (B = hardcopy + electronic); blank = does not participate
  BUREAU_SCORE     0-950          BEHAVIOR_SCORE 0-999 (probability of payment: higher is better)
  NBR_DAYS_DELQ_AT_CYC  multiples of 30 (about 27% shifted by +1 day), shares as in production
"""

import datetime as dt
import math

ACCOUNT_FIELDS = ["INTERNAL_STATUS", "EXTERNAL_STATUS", "EBILL_IND", "BANKRUPTCY_CODE",
                  "BANKRUPTCY_FILING_DATE", "WRITEOFF_DATE", "BILLING_CYCLE_DAY"]
CYCLE_FIELDS = ["BILLING_CYCLE_DATE", "BILLING_YEAR_MTH", "CREDIT_LIMIT_AMT", "CURR_MNTH_BALANCE_AMT",
                "OPEN_TO_BUY_AMT", "BUREAU_SCORE", "BEHAVIOR_SCORE", "NBR_DAYS_DELQ_AT_CYC",
                "TOTAL_PAST_DUE_AMT", "MIN_PYMT_AMT", "PYMT_AMT", "WRITEOFF_AMT", "LATE_CHARGE_ASSESSED",
                "LAST_MRCH_APR", "NET_SALES", "CASH_ADV_AMT"]
FIELDS = set(ACCOUNT_FIELDS + CYCLE_FIELDS)

LAST_MONTH = (2026, 9)          # newest billing month in the data
BURN_IN = 24                    # months simulated before the window, so delinquency starts in steady state

# Probability that a delinquent account rolls to the next bucket (else it cures), from production:
# share(60)/share(30) = 0.19, share(90)/share(60) = 0.58, then 0.77, 0.86, 0.93
ROLL = {30: 0.19, 60: 0.58, 90: 0.77, 120: 0.86, 150: 0.93}
ROLL_IN_BASE = 0.0068           # calibrated so ~1.0% of cycles sit at 30 days, as in production
PLUS_ONE_DAY = 0.27             # share of each bucket reported as 31, 61, 91 ... days

EXTERNAL_OTHER = [("C", 0.025), ("F", 0.004), ("L", 0.004), ("E", 0.003), ("U", 0.002), ("I", 0.002)]
EBILL = [(None, 0.40), ("0", 0.05), ("B", 0.35), ("C", 0.10), ("T", 0.05), ("X", 0.05)]


def _pick(rng, items):
    x = rng.random()
    for value, p in items:
        x -= p
        if x < 0:
            return value
    return None


def _month_dates(n, day):
    y, m = LAST_MONTH
    out = []
    for _ in range(n):
        out.append(dt.date(y, m, day))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return list(reversed(out))


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def simulate_account(rng, cycles):
    """Returns (account-level values, [cycle-level values per cycle]) for one account."""
    day = rng.randint(1, 28)
    months = _month_dates(BURN_IN + cycles, day)

    bureau_base = _clip(rng.gauss(705, 85), 0, 950)
    behav_base = _clip(620 + (bureau_base - 705) + rng.gauss(0, 40), 0, 999)
    if bureau_base < 600:
        apr, limit = 34.99, rng.randrange(500, 3001, 100)
    elif bureau_base < 670:
        apr, limit = 29.99, rng.randrange(1000, 6001, 100)
    elif bureau_base < 740:
        apr, limit = 25.99, rng.randrange(2000, 12001, 100)
    else:
        apr, limit = 21.99, rng.randrange(5000, 25001, 100)
    apr = round(apr - rng.choice([0, 1, 2, 3]), 3)
    payer = _pick(rng, [("transactor", 0.30), ("revolver", 0.55), ("minimum", 0.15)])
    ext = _pick(rng, EXTERNAL_OTHER)                       # closed / frozen ... (most accounts: None)
    roll_in = ROLL_IN_BASE * math.exp((700 - bureau_base) / 75)
    spend_on = ext not in ("C", "E", "F")

    bal = round(limit * _clip(rng.betavariate(2, 5) + (700 - bureau_base) / 900, 0.0, 0.95), 2)
    min_due = round(max(25.0, bal * 0.02), 2) if bal > 0 else 0.0
    dpd, past_due, missed_last, charged_off, writeoff_date = 0, 0.0, False, False, None
    rows = []

    for i, d in enumerate(months):
        pay = cash = sales = interest = late = wo = 0.0
        if charged_off:
            dpd += 30                                       # keeps ageing after charge-off
            bal, min_due, past_due = 0.0, 0.0, 0.0
        else:
            # 1. payment against last statement, or a missed payment
            if dpd == 0:
                missed = min_due > 0 and rng.random() < roll_in
            else:
                missed = rng.random() < ROLL.get(dpd, 0.93)
            if missed:
                dpd += 30
                past_due = round(past_due + min_due, 2)
                late = 41.0 if missed_last else 30.0
            else:
                if dpd > 0:                                 # cures: pays everything past due + this minimum
                    pay = past_due + min_due
                elif payer == "transactor":
                    pay = bal
                elif payer == "minimum":
                    pay = min_due
                else:
                    pay = min_due + max(0.0, bal - min_due) * rng.uniform(0.05, 0.40)
                if rng.random() < 0.01:                     # occasional overpayment -> credit balance
                    pay += rng.uniform(5, 60)
                pay = round(max(0.0, min(pay, bal + 60)), 2)
                dpd, past_due = 0, 0.0
            missed_last = missed

            # 2. new activity (blocked when 60+ days past due, closed/frozen/revoked, or over limit)
            open_to_buy = limit - bal
            if spend_on and dpd < 60 and open_to_buy > 0:
                if rng.random() < 0.75:
                    cap = open_to_buy + (limit * 0.08 if rng.random() < 0.03 else 0)  # occasional overlimit approval
                    sales = round(min(cap, rng.gammavariate(2.0, limit * 0.06)), 2)
                    if rng.random() < 0.05:
                        sales -= round(rng.uniform(10, 120), 2)   # returns can exceed purchases
                if rng.random() < (0.08 if bureau_base < 620 else 0.03):
                    cash = round(min(max(open_to_buy - sales, 0), rng.uniform(100, 1000)), 2)

            # 3. interest on the unpaid balance (transactors who paid in full pay none)
            revolving = max(0.0, bal - pay)
            if revolving > 0.005:
                interest = round(revolving * apr / 1200, 2)
            bal = round(bal - pay + sales + cash + interest + late, 2)

            # 4. charge-off at 180 days past due
            if dpd >= 180:
                wo, charged_off, writeoff_date = bal, True, d
            min_due = 0.0 if bal <= 0 else round(min(bal, max(25.0, bal * 0.01 + interest + late)), 2)

        if i < BURN_IN:
            if charged_off and dpd >= 270:
                break
            continue
        stmt_bal = 0.0 if charged_off and wo == 0 else bal
        cur_apr = max(apr, 29.99) if dpd >= 60 and not charged_off else apr
        bureau = _clip(round(bureau_base + rng.gauss(0, 8) - 25 * min(dpd, 180) / 30), 0, 950)
        behav = _clip(round(behav_base + rng.gauss(0, 15) - 90 * min(dpd, 180) / 30), 0, 999)
        rows.append({
            "BILLING_CYCLE_DATE": d,
            "BILLING_YEAR_MTH": d.year * 100 + d.month,
            "CREDIT_LIMIT_AMT": 0 if charged_off and wo == 0 else limit,
            "CURR_MNTH_BALANCE_AMT": stmt_bal,
            "OPEN_TO_BUY_AMT": round((0 if charged_off and wo == 0 else limit) - stmt_bal, 2),
            "BUREAU_SCORE": int(bureau),
            "BEHAVIOR_SCORE": int(behav),
            "NBR_DAYS_DELQ_AT_CYC": dpd + (1 if dpd and rng.random() < PLUS_ONE_DAY else 0),
            "TOTAL_PAST_DUE_AMT": past_due if dpd and not (charged_off and wo == 0) else 0.0,
            "MIN_PYMT_AMT": min_due,
            "PYMT_AMT": pay,
            "WRITEOFF_AMT": wo,
            "LATE_CHARGE_ASSESSED": late,
            "LAST_MRCH_APR": cur_apr,
            "NET_SALES": sales,
            "CASH_ADV_AMT": cash,
        })
        if wo:                                              # the charge-off cycle itself is reported, then zeroed
            bal = 0.0
        if charged_off and dpd >= 270:                      # production stops reporting soon after
            break

    if not rows:
        return None, []                                     # charged off and gone before the window
    last = rows[-1]
    if charged_off:
        internal = "D"
    else:
        delinquent = last["NBR_DAYS_DELQ_AT_CYC"] > 0
        over = last["CURR_MNTH_BALANCE_AMT"] > last["CREDIT_LIMIT_AMT"]
        internal = ("X" if delinquent and over else "D" if delinquent else "O" if over
                    else "N" if last["CURR_MNTH_BALANCE_AMT"] < 0 else None)
    if charged_off:
        bk = "X" if rng.random() < 0.25 else "Y"
    else:
        bk = _pick(rng, [("X", 0.004), ("C", 0.002), ("P", 0.002), ("R", 0.002)]) or "0"
    bk_date = None
    if bk in ("X", "C", "P", "R"):
        bk_date = (writeoff_date or months[-1]) - dt.timedelta(days=rng.randint(30, 900))
    account = {
        "INTERNAL_STATUS": internal,
        "EXTERNAL_STATUS": "Z" if charged_off else ext,
        "EBILL_IND": _pick(rng, EBILL),
        "BANKRUPTCY_CODE": bk,
        "BANKRUPTCY_FILING_DATE": bk_date,
        "WRITEOFF_DATE": writeoff_date,
        "BILLING_CYCLE_DAY": day,
    }
    return account, rows
