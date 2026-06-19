@echo off
setlocal
cd /d "%~dp0"
title Texas County Regulatory Radar - ChatGPT Backfill Status
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m countywatch.chatgpt_backfill status
pause
