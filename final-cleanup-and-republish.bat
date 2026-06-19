@echo off
setlocal
cd /d "%~dp0"
title Texas County Regulatory Radar - Final Cleanup and Republish
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
set PYTHONUNBUFFERED=1
echo.
echo Running deterministic jurisdiction, classification, date, URL, quote, and duplicate QA...
echo This does not crawl the web and does not call Groq, Gemini, OpenAI, or any other model.
echo The current dashboard stays untouched unless every check completes.
echo.
".venv\Scripts\python.exe" -m countywatch.final_cleanup republish
set RESULT=%ERRORLEVEL%
echo.
if "%RESULT%"=="0" (
  echo Cleanup and atomic republish completed successfully.
) else (
  echo The command stopped safely. The existing dashboard was not partially replaced.
)
pause
exit /b %RESULT%
