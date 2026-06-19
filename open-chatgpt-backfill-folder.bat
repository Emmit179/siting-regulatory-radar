@echo off
setlocal
cd /d "%~dp0"
if not exist "var\chatgpt_backfill\current" (
  echo Run 1-prepare-chatgpt-document-batches.bat first.
  pause
  exit /b 1
)
explorer "var\chatgpt_backfill\current"
