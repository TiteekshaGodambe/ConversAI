@echo off
REM ============================================================================================
REM  Look at what ConversAI has in BigQuery and Cloud Storage, from cmd (read-only).
REM    set GCP_PROJECT=your-project-id
REM    deploy\bq_check.cmd
REM  1) datasets  2) source tables + a 5-row peek  3) exports  4) the last 10 queries the server ran
REM  5) the export list and the proposed-definitions log on Cloud Storage
REM ============================================================================================
setlocal
if "%GCP_PROJECT%"=="" (
  echo Set GCP_PROJECT first, e.g.  set GCP_PROJECT=your-project-id
  exit /b 1
)
set P=%GCP_PROJECT%
set BUCKET=%GCP_PROJECT%-conversai-lake

echo.
echo === 1/5  Datasets in %P%
call bq ls --project_id=%P%

echo.
echo === 2/5  Source tables (conversai_source) and lake tables (conversai_lake)
call bq ls --project_id=%P% conversai_source
call bq ls --project_id=%P% conversai_lake
echo --- first 5 rows of ACCOUNT_DIM (a preview is free: it does not run a query)
call bq head --project_id=%P% -n 5 conversai_source.ACCOUNT_DIM

echo.
echo === 3/5  Exports (conversai_exports; each export = two tables that expire after 7 days)
call bq ls --project_id=%P% conversai_exports

echo.
echo === 4/5  Last 10 queries ConversAI ran (jobs labelled app=conversai)
call bq query --project_id=%P% --use_legacy_sql=false --format=pretty "SELECT FORMAT_TIMESTAMP('%%F %%T', creation_time) AS started, user_email, ROUND(total_bytes_processed / 1e6, 1) AS mb, SUBSTR(REGEXP_REPLACE(query, r'\s+', ' '), 1, 90) AS query_start FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY) AND EXISTS (SELECT 1 FROM UNNEST(labels) l WHERE l.key = 'app' AND l.value = 'conversai') ORDER BY creation_time DESC LIMIT 10"

echo.
echo === 5/5  Cloud Storage: export list and proposed definitions
call gcloud storage ls gs://%BUCKET%/conversai/
call gcloud storage cat gs://%BUCKET%/conversai/definitions.json 2>nul || echo     (no definitions proposed yet)
endlocal
