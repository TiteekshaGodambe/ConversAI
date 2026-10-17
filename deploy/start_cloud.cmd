@echo off
REM ============================================================================================
REM  Daily start: connect this laptop to the ConversAI server on Cloud Run.
REM    deploy\start_cloud.cmd            (from the project folder, with sl activated)
REM  1) checks you are logged in to gcloud   2) checks the Cloud Run service exists
REM  3) opens the authenticated proxy in its own window (port 8080)   4) runs deploy\check_cloud.py
REM  Then in VS Code: MCP: List Servers -> conversai-cloud -> Start (or Restart).
REM ============================================================================================
setlocal
set REGION=us-central1
set SERVICE=conversai-mcp
set PORT=8080

echo.
echo === 1/4  gcloud login
for /f "delims=" %%a in ('gcloud config get-value account 2^>nul') do set ME=%%a
if "%ME%"=="" (
  echo Not logged in. Running: gcloud auth login
  call gcloud auth login || exit /b 1
) else (
  echo     logged in as %ME%
)
call gcloud auth print-access-token >nul 2>&1
if errorlevel 1 (
  echo Your gcloud login has expired. Running: gcloud auth login
  call gcloud auth login || exit /b 1
)

echo.
echo === 2/4  Cloud Run service %SERVICE%
call gcloud run services describe %SERVICE% --region %REGION% --format="value(status.url)" > "%TEMP%\conversai_url.txt" 2>nul
set /p URL=<"%TEMP%\conversai_url.txt"
if "%URL%"=="" (
  echo The service is not deployed in this project. Deploy it first:  deploy\gcp_deploy.cmd
  exit /b 1
)
echo     %URL%   (scales to zero when idle; the first request wakes it, ~30 s)

echo.
echo === 3/4  Proxy on http://localhost:%PORT%
netstat -ano | findstr /R /C:"127.0.0.1:%PORT% .*LISTENING" >nul
if not errorlevel 1 (
  echo     something is already listening on port %PORT% - assuming it is the proxy from earlier
) else (
  start "ConversAI proxy (keep open)" cmd /k gcloud run services proxy %SERVICE% --region %REGION% --port %PORT%
  echo     opened the proxy in a new window; keep that window open
  timeout /t 8 /nobreak >nul
)

echo.
echo === 4/4  Check the server (first call may take ~30 s while Cloud Run wakes up)
python deploy\check_cloud.py --url http://localhost:%PORT%/mcp
if errorlevel 1 exit /b 1

echo.
echo Next in VS Code: MCP: List Servers -^> conversai-cloud -^> Start (or Restart), then a new chat with only
echo conversai-cloud ticked in the tools picker.
endlocal
