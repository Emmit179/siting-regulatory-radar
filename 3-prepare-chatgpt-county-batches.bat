@echo off
setlocal
cd /d "%~dp0"
title Texas County Regulatory Radar - Prepare County Consolidation Batches
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
set PYTHONUNBUFFERED=1
echo.
echo Preparing county-level deduplication batches from locally verified document events...
echo.
".venv\Scripts\python.exe" -m countywatch.chatgpt_backfill prepare-counties
set RESULT=%ERRORLEVEL%
echo.
if not "%RESULT%"=="0" echo Preparation stopped safely. Read the message above.
pause
exit /b %RESULT%
