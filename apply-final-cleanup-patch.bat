@echo off
setlocal
cd /d "%~dp0"
title Texas County Regulatory Radar - Install Final Cleanup
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
echo.
echo Installing the deterministic final-cleanup hook...
echo.
".venv\Scripts\python.exe" apply-final-cleanup-patch.py
set RESULT=%ERRORLEVEL%
echo.
if "%RESULT%"=="0" (
  echo Installation check completed successfully.
) else (
  echo Nothing was published. Read the error above.
)
pause
exit /b %RESULT%
