@echo off
setlocal
cd /d "%~dp0"
title Texas County Regulatory Radar - Prepare ChatGPT Document Batches
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
set PYTHONUNBUFFERED=1
echo.
echo Preparing deterministic document-review batches from the cached SQLite text...
echo This does not call any API and does not redownload county records.
echo.
".venv\Scripts\python.exe" -m countywatch.chatgpt_backfill prepare-documents
set RESULT=%ERRORLEVEL%
echo.
if "%RESULT%"=="0" (
  echo Preparation finished. Follow CHATGPT-PRO-BACKFILL-README.txt.
) else (
  echo Preparation stopped safely. Read the message above.
)
pause
exit /b %RESULT%
