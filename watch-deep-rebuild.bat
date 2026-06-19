@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
:watch
cls
echo Deep rebuild progress - refreshes every 15 seconds. Press Ctrl+C to close this watcher.
echo.
".venv\Scripts\python.exe" -m countywatch.deep_rebuild status
timeout /t 15 /nobreak >nul
goto watch
