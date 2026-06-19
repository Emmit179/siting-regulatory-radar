@echo off
setlocal
cd /d "%~dp0"
title Texas County Regulatory Radar - Import ChatGPT Document Results
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
set PYTHONUNBUFFERED=1
echo.
echo Importing Phase 1 result files and validating every quote against cached source text...
echo Model-supplied URLs are never trusted; source URLs come from the crawler database.
echo.
".venv\Scripts\python.exe" -m countywatch.chatgpt_backfill import-documents
set RESULT=%ERRORLEVEL%
echo.
if not "%RESULT%"=="0" echo Import stopped safely. Read the message above.
pause
exit /b %RESULT%
