@echo off
setlocal
cd /d "%~dp0"
title Texas County Regulatory Radar - Verify, Consolidate, and Publish
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
set PYTHONUNBUFFERED=1
echo.
echo Importing Phase 2 results, enforcing local constraints, backing up the database,
echo and atomically publishing only when every checkpoint is complete...
echo.
".venv\Scripts\python.exe" -m countywatch.chatgpt_backfill import-counties-publish
set RESULT=%ERRORLEVEL%
echo.
if "%RESULT%"=="0" (
  echo The command finished safely. If all files were present, the rebuilt dashboard is published.
) else (
  echo The command stopped safely. The existing dashboard was not partially replaced.
)
pause
exit /b %RESULT%
