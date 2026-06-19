@echo off
setlocal
cd /d "%~dp0"
echo.
echo ============================================================
echo   Texas County Regulatory Radar - Windows setup
echo ============================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
if errorlevel 1 (
  echo.
  echo Setup did not finish successfully. Read the error above.
  pause
  exit /b 1
)
echo.
echo Setup complete. Edit .env, then run update-now.bat.
pause
