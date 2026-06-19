@echo off
setlocal
cd /d "%~dp0"
title Texas County Regulatory Radar - Update and Unattended Deep Review
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
set PYTHONUNBUFFERED=1
echo Updating cached county records without spending calls on the legacy classifier...
".venv\Scripts\python.exe" -m countywatch update --no-llm
if errorlevel 1 (
  echo.
  echo County crawl ended with an error. Deep review was not started.
  pause
  exit /b 1
)

echo.
echo Crawling is complete. Starting unattended checkpointed deep review...
set COUNTYWATCH_DEEP_AUTO_WAIT=true
set COUNTYWATCH_DEEP_KEEP_WINDOWS_AWAKE=true
set COUNTYWATCH_DEEP_MAX_GROQ_CALLS=-1
set COUNTYWATCH_DEEP_MAX_GEMINI_CALLS=-1
set COUNTYWATCH_DEEP_MAX_RATE_LIMIT_WAIT_SECONDS=0
set COUNTYWATCH_DEEP_RATE_LIMIT_BUFFER_SECONDS=5
set COUNTYWATCH_DEEP_WAIT_HEARTBEAT_SECONDS=300
set COUNTYWATCH_DEEP_MAX_TRANSIENT_RETRIES=50
set COUNTYWATCH_DEEP_MAX_RUN_MINUTES=0
".venv\Scripts\python.exe" -m countywatch.deep_rebuild run
set RESULT=%ERRORLEVEL%
echo.
if not "%RESULT%"=="0" echo Deep review ended with a non-retryable error. Review the output above.
if "%RESULT%"=="0" echo Update and deep-review process finished safely.
pause
exit /b %RESULT%
