@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m countywatch.deep_rebuild retry-errors
set RESULT=%ERRORLEVEL%
echo.
if "%RESULT%"=="0" echo Failed checkpoints were reset. Run deep-rebuild-intelligence.bat again.
pause
exit /b %RESULT%
