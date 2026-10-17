@echo off
REM ============================================================================================
REM  Public demo mode for evaluation: anyone can call the MCP endpoint without a Google login.
REM  Server-side guardrails stay on (semantic queries only, read-only SQL guard, per-query byte cap,
REM  approval dialog); CONVERSAI_PUBLIC_DEMO=1 also disables deleting extracts.
REM    set GCP_PROJECT=your-project-id
REM    deploy\make_public.cmd          (open)
REM    deploy\make_public.cmd close    (back to IAM-only access)
REM ============================================================================================
setlocal
if "%GCP_PROJECT%"=="" (
  echo Set GCP_PROJECT first, e.g.  set GCP_PROJECT=your-project-id
  exit /b 1
)
call gcloud config set project %GCP_PROJECT% >nul || exit /b 1
if /i "%1"=="close" (
  call gcloud run services remove-iam-policy-binding conversai-mcp --region us-central1 --member=allUsers --role=roles/run.invoker || exit /b 1
  echo Public access removed. Callers need roles/run.invoker again.
  exit /b 0
)
call gcloud run services update conversai-mcp --region us-central1 --update-env-vars CONVERSAI_PUBLIC_DEMO=1 || exit /b 1
call gcloud run services add-iam-policy-binding conversai-mcp --region us-central1 --member=allUsers --role=roles/run.invoker || exit /b 1
for /f "delims=" %%u in ('gcloud run services describe conversai-mcp --region us-central1 --format="value(status.url)"') do set URL=%%u
echo.
echo Public MCP endpoint: %URL%/mcp
echo Check it:  python deploy\check_cloud.py --url %URL%/mcp
endlocal
