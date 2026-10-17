# ConversAI: unified semantic layer, local edition

One set of business definitions (MetricFlow YAML) answers questions on **any** engine.
The model never writes SQL: it picks governed metric and dimension names, MetricFlow
compiles the SQL, and the engine binds the real tables and translates to each dialect.

```
question ─▶ semantic query (names only) ─▶ MetricFlow SQL ─▶ bind tables + translate ─▶ engine check ─▶ approve ─▶ run
             engine/nl.py or the MCP client    engine/compiler.py   engine/dialects.py        engine/targets.py
```

| Folder / file | What it is |
|---|---|
| `tables/` | Builds `CARD_ACCOUNT_SYNTHETIC` (222 columns) and splits it into `ACCOUNT_DIM` + `ACCOUNT_CYCLE_FACT` |
| `semantic/models/semantic_models.yml` | The two tables (`account`, `account_cycle`), their join key, dimensions, measures, synonyms |
| `semantic/models/metrics.yml` | 37 governed metrics (+7 hidden building blocks): delinquency, utilization, payments, charge-offs, APR, spend |
| `targets.yaml` | Where each logical table lives per engine: `warehouse` (DuckDB), `lake` (Parquet), `oracle` |
| `eval/` | 27 test questions (easy / medium / hard / guardrail), gold answers, evaluation runner |
| `engine/` | Catalog, compiler, dialect binding/translation, executors, question → semantic query |
| `cli.py` | Try everything from the terminal |
| `mcp_server.py` | MCP server for VS Code Copilot / Claude Desktop (approval dialog before any query runs) |
| `tests/test_consistency.py` | Same question, every engine, same answer; Oracle SQL shape checks |

## 0. Source tables: the original 222-column table, split in two

`tables/` rebuilds the original `CARD_ACCOUNT_SYNTHETIC` (same 222 columns and data types as the
Oracle script in `tables/source/`) with one row per account per monthly cycle (default 5,000
accounts x 12 cycles, Oct 2025 - Sep 2026), and splits it into:

| Table | Grain | Columns | Key |
|---|---|---|---|
| `ACCOUNT_DIM` | one row per account | 27 = `ACCOUNT_KEY` + 26 account-level columns | `ACCOUNT_KEY` |
| `ACCOUNT_CYCLE_FACT` | one row per account per billing cycle | 196 = `ACCOUNT_KEY` + 195 cycle-level columns | `ACCOUNT_HISTORY_KEY`; `ACCOUNT_KEY` → `ACCOUNT_DIM` |

**Realistic fields.** The 20 semantic-layer fields (listed in `eval/questions.yaml`) plus
`BANKRUPTCY_FILING_DATE`, `BILLING_CYCLE_DAY` and `BILLING_YEAR_MTH` are simulated month by month by
`tables/realism.py`, using the business code lists and ranges (statuses, bankruptcy codes, e-bill
codes, bureau score 0-950, behavior score 0-999) and the production days-past-due distribution.
Balances, payments, fees, delinquency and charge-offs agree with each other. The other 199 columns
keep the original random rules.

`tables/column_split.csv` decides which table each column belongs to (`account`, `cycle` or `key`).

```powershell
python tables\build_tables.py                    # ~1 minute: 5,000 accounts x 12 monthly cycles
python tables\verify_split.py                    # 6 split checks; exit code 0 = lossless
python tables\check_realism.py                   # 23 business-rule checks + profile of the 20 fields
python tables\peek.py ACCOUNT_CYCLE_FACT 1000001 # one account's 12 statements
python tables\build_tables.py --oracle           # optional: same tables in Oracle (ORACLE_* env vars)
```
`verify_split.py` writes `tables\split_report_<target>.csv` (one line per column) for review.
Oracle DDL for all three tables: `tables\oracle_tables.sql`.

## 1. Install (Windows PowerShell, Python 3.11–3.13)

```powershell
cd C:\Users\Titeeksha\conversai-local
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt pytest
```

## 2. Create the data

```powershell
python tables\build_tables.py          # ~1 minute; warehouse\cards.duckdb + lake\cards\*.parquet
```

## 3. The semantic layer (20 fields)

**Two tables, one join.** `account` (ACCOUNT_DIM, primary entity `account`) and `account_cycle`
(ACCOUNT_CYCLE_FACT, foreign entity `account`). Any cycle metric can be grouped or filtered by any
account attribute; MetricFlow writes the join.

**Snapshot vs flow.** Balances, limits, scores, statuses and account counts are *snapshots*: each account
counts once, at its latest cycle in the period (or in each month when grouped by month), never summed
across months. Payments, fees, sales, cash advances and charge-offs are *flows*: summed over every cycle.

| Area | Metrics | Built from |
|---|---|---|
| Size | `accounts`, `active_accounts`, `total_balance`, `avg_balance` | ACCOUNT_KEY, CURR_MNTH_BALANCE_AMT |
| Credit lines | `total_credit_limit`, `avg_credit_limit`, `utilization_rate`, `total_open_to_buy`, `avg_open_to_buy`, `balance_over_80_util`, `high_util_balance_share` | CREDIT_LIMIT_AMT, OPEN_TO_BUY_AMT |
| Scores | `avg_bureau_score`, `avg_behavior_score` | BUREAU_SCORE, BEHAVIOR_SCORE |
| Delinquency | `dq30/60/90_accounts`, `dq30/60/90_rate`, `dq30_balance_rate`, `dq30_rate_mom_change`, `avg_days_past_due`, `total_past_due`, `total_min_payment_due` | NBR_DAYS_DELQ_AT_CYC, TOTAL_PAST_DUE_AMT, MIN_PYMT_AMT |
| Payments & fees | `total_payments`, `payment_rate`, `paid_minimum_rate`, `late_fee_revenue` | PYMT_AMT, MIN_PYMT_AMT (previous statement), LATE_CHARGE_ASSESSED |
| Losses | `total_chargeoffs`, `chargeoff_accounts`, `chargeoff_rate` | WRITEOFF_AMT |
| Pricing | `avg_purchase_apr`, `balance_weighted_apr` | LAST_MRCH_APR |
| Spend | `total_net_sales`, `total_cash_advances`, `total_spend`, `cash_advance_share` | NET_SALES, CASH_ADV_AMT |

Dimensions: status codes and their meanings (`account__internal_status` / `_desc`, `account__external_status` /
`_desc`, `account__bankruptcy_code` / `account__bankruptcy_status`, `account__ebill_ind`, `account__paperless`,
`account__charged_off`), `account__writeoff_date`, and per cycle `account_cycle__delinquency_bucket`,
`prior_delinquency_bucket` (roll rates), `bureau_score` / `bureau_score_band`, `behavior_score` /
`behavior_score_band`, `utilization_band`, `paid_minimum_due`, `book_status`, `days_past_due`;
time is `metric_time__month|quarter|year` (billing month). Code meanings come from the business
reference sheets; descriptions from the data dictionary. See `python cli.py catalog` for all of it.

Business rules worth knowing: delinquency rates exclude charged-off accounts; "paperless" = any e-bill code
(blank = paper); payment and charge-off rates divide by statement balances of the same cycles; filters on a
cycle attribute without a month match any cycle in the data (the plan shows a note; ask for a month for a
point-in-time answer).

## 4. Try it

```powershell
python cli.py catalog
python cli.py query   @examples\dq30_trend.json
python cli.py query   @examples\paid_minimum_high_risk.json
python cli.py compare @examples\apr_by_score_band.json --targets warehouse,lake
python cli.py sql     @examples\roll_rate_current_to_30.json --target oracle
python -m pytest -q tests
```
Semantic queries are small JSON files (see `examples\`); `@file` avoids shell quoting issues.
Each command takes a few seconds while MetricFlow starts.

**Evaluate** (about 4 minutes; every question's semantic answer vs an independent hand-written SQL):
```powershell
python eval\run_eval.py                         # gold mode, warehouse + lake
python eval\run_eval.py --only H1,H3 --show-sql
```

Natural-language questions (needs an Anthropic API key, never paste it in chat):
```powershell
$env:ANTHROPIC_API_KEY = Read-Host -MaskInput "API key"
python cli.py ask "What is the 30+ delinquency rate by external status?"
python eval\run_eval.py --llm                   # Claude answers all 27 questions, scored against gold
```

## 5. Optional: Oracle as a third engine

With the adb-free container running on the Docker laptop (plain TCP port 1521):
```powershell
$env:ORACLE_DSN = "localhost:1521/myatp_low.adb.oraclecloud.com"
$env:ORACLE_USER = "AI_AGENT_USER"          # any user with CREATE TABLE
$env:ORACLE_PASSWORD = Read-Host -MaskInput "Oracle password"
python tables\build_tables.py --oracle      # same seed -> same rows as the DuckDB build
python tables\verify_split.py --target oracle
python eval\run_eval.py --targets warehouse,lake,oracle
python -m pytest -q tests                   # now includes Oracle
```

## 6. Use it from VS Code Copilot (Agent mode)

`.vscode\mcp.json` is included. Open this folder in VS Code, run **MCP: List Servers**, start `conversai-semantic`, then ask in Agent mode: *"Using conversai-semantic,
what is the 30+ day delinquency rate by external status?"* Copilot builds the semantic query from the
governed names, you pick the engine and approve the SQL in dialogs, and the answer comes from the
returned rows.

## Changing the semantic layer

Edit the YAML under `semantic/models/`; the next command re-parses it automatically. To check it:
```powershell
cd semantic; $env:DBT_PROFILES_DIR = (Get-Location).Path
python -c "import sys; from dbt_metricflow.cli.main import cli; sys.argv[0]='mf'; sys.exit(cli())" validate-configs
cd ..
```
Adding an engine = a new entry in `targets.yaml` (no change to the definitions). Engines that
MetricFlow renders natively (BigQuery, Snowflake, Databricks, Postgres, Redshift, Trino) can be
rendered directly later; Oracle goes through translation plus fix-ups and Oracle's own EXPLAIN PLAN.

## Proposed definitions: when the layer does not have the concept yet

The agent never writes SQL. When no governed metric or dimension fits a question, it proposes a
*definition* in the layer's own terms (`propose_definition`): a dimension (a row-level expression on
`account` or `account_cycle`, e.g. a trend against the previous cycle with LAG) or a metric (simple /
ratio / derived). `engine/definitions.py` checks it before anyone sees it:

1. shape: a new snake_case name and a plain-English description (the thing the user approves)
2. whitelist: parsed with sqlglot; only governed columns of that table, only simple functions,
   LAG/LEAD only over the account's own cycles, no aggregates, subqueries, other tables or comments
3. compile: added to an in-memory copy of the semantic manifest; MetricFlow validates and compiles it
4. target: the preview SQL is checked by the engine (BigQuery dry run / DuckDB EXPLAIN)

The user approves it in a dialog; it becomes **provisional** for that conversation, and every answer that
uses it is labelled. Other conversations can reuse it (`use_definition`) after approving it again. The log
is `proposals/definitions.json` locally, or `definitions.json` next to the export registry on Cloud Storage.

A data owner promotes or rejects:

    python cli.py definitions list
    python cli.py definitions show <id>
    python cli.py definitions promote <id>      (writes semantic/models/*.yml, re-parses; then add a gold question, redeploy)
    python cli.py definitions reject <id> --reason "..."

Tests: `python -m pytest -q tests/test_definitions.py` (unsafe proposals refused, answers equal hand-written SQL,
BigQuery and exports, Cloud Storage log, promotion).
