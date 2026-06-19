@echo off
setlocal
cd /d "%~dp0"
title Texas County Regulatory Radar - Unattended Deep Rebuild
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)

rem Local unattended mode: let provider-enforced quotas control throughput.
rem -1 means no artificial per-process call cap. HTTP 429 responses are waited out.
set PYTHONUNBUFFERED=1
set COUNTYWATCH_DEEP_AUTO_WAIT=true
set COUNTYWATCH_DEEP_KEEP_WINDOWS_AWAKE=true
set COUNTYWATCH_DEEP_MAX_GROQ_CALLS=-1
set COUNTYWATCH_DEEP_MAX_GEMINI_CALLS=-1
set COUNTYWATCH_DEEP_MAX_RATE_LIMIT_WAIT_SECONDS=0
set COUNTYWATCH_DEEP_RATE_LIMIT_BUFFER_SECONDS=5
set COUNTYWATCH_DEEP_WAIT_HEARTBEAT_SECONDS=300
set COUNTYWATCH_DEEP_MAX_TRANSIENT_RETRIES=50
set COUNTYWATCH_DEEP_MAX_RUN_MINUTES=0

echo.
echo Starting unattended checkpointed deep intelligence rebuild...
echo GPT-OSS-120B runs the primary read, skeptical second read, and final adjudication.
echo Gemini 3.1 Flash-Lite is used only as a diversity review on potential events.
echo Short rate limits, daily quotas, and transient provider errors are retried automatically.
echo Windows sleep is suppressed while this window remains open. The screen may turn off normally.
echo Press Ctrl+C only if you intentionally want to stop; completed phases remain checkpointed.
echo.
".venv\Scripts\python.exe" -m countywatch.deep_rebuild run
set RESULT=%ERRORLEVEL%
echo.
if not "%RESULT%"=="0" (
  echo Deep rebuild ended with a non-retryable error. Review the message above.
) else (
  echo Deep rebuild process finished safely.
  echo If all checkpoints completed, the dashboard was replaced atomically.
)
pause
exit /b %RESULT%
