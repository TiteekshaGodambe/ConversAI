@echo off
REM ============================================================================================
REM  ConversAI on Google Cloud: one-time setup + deploy of the MCP server to Cloud Run.
REM  Run from the project folder in cmd, after the source data is loaded:
REM      set GCP_PROJECT=<your project id>
REM      python tables\load_bigquery.py --lake
REM      deploy\gcp_deploy.cmd
REM  Safe to re-run: existing resources are kept and the service is redeployed with the current code.
REM ============================================================================================
setlocal
if "%GCP_PROJECT%"=="" (
  echo Set GCP_PROJECT first, e.g.  set GCP_PROJECT=your-project-id
  exit /b 1
)
set REGION=us-central1
set SERVICE=conversai-mcp
set SA=conversai-mcp@%GCP_PROJECT%.iam.gserviceaccount.com
set BUCKET=%GCP_PROJECT%-conversai-lake

echo.
echo === 1/5  Project and APIs
call gcloud config set project %GCP_PROJECT% || exit /b 1
call gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com bigquery.googleapis.com storage.googleapis.com || exit /b 1

echo.
echo === 2/5  Service account the server runs as
call gcloud iam service-accounts describe %SA% >nul 2>&1
if errorlevel 1 call gcloud iam service-accounts create conversai-mcp --display-name "ConversAI MCP server" || exit /b 1
REM  run queries and dry runs
call gcloud projects add-iam-policy-binding %GCP_PROJECT% --member serviceAccount:%SA% --role roles/bigquery.jobUser --condition None >nul || exit /b 1
REM  read conversai_source / conversai_lake; create and delete export tables in conversai_exports
call gcloud projects add-iam-policy-binding %GCP_PROJECT% --member serviceAccount:%SA% --role roles/bigquery.dataEditor --condition None >nul || exit /b 1
REM  read the lake files (external tables) and read/write the export list, both in the lake bucket
call gcloud storage buckets add-iam-policy-binding gs://%BUCKET% --member serviceAccount:%SA% --role roles/storage.objectAdmin >nul || exit /b 1

echo.
echo === 2b/5 Let Cloud Build use the project's default compute service account (newer projects do not grant this)
for /f "delims=" %%n in ('gcloud projects describe %GCP_PROJECT% --format="value(projectNumber)"') do set PN=%%n
call gcloud projects add-iam-policy-binding %GCP_PROJECT% --member serviceAccount:%PN%-compute@developer.gserviceaccount.com --role roles/run.builder --condition None >nul || exit /b 1
echo     granted roles/run.builder to %PN%-compute@developer.gserviceaccount.com (allow 1-2 minutes to take effect)

echo.
echo === 3/5  Build the container with Cloud Build and deploy it to Cloud Run (private, no public access)
call gcloud run deploy %SERVICE% --source . --region %REGION% --service-account %SA% --no-allow-unauthenticated --max-instances 1 --session-affinity --timeout 3600 --memory 2Gi --cpu 1 --set-env-vars GCP_PROJECT=%GCP_PROJECT%,CONVERSAI_REGISTRY=gs://%BUCKET%/conversai/exports.json || exit /b 1

echo.
echo === 4/5  Allow your own account to call the service
for /f "delims=" %%a in ('gcloud config get-value account') do set ME=%%a
call gcloud run services add-iam-policy-binding %SERVICE% --region %REGION% --member user:%ME% --role roles/run.invoker >nul || exit /b 1

echo.
echo === 5/5  Deployed. To connect VS Code, open a second cmd window and keep this running:
echo        gcloud run services proxy %SERVICE% --region %REGION% --port 8080
echo     then use the "conversai-cloud" server in .vscode\mcp.json  (url http://localhost:8080/mcp)
endlocal
