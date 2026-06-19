@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m countywatch update --no-llm
set RESULT=%ERRORLEVEL%
echo.
if not "%RESULT%"=="0" echo Crawl ended with an error. Review the output above.
if "%RESULT%"=="0" echo Crawl complete. Run deep-rebuild-intelligence.bat to validate and publish new intelligence.
pause
exit /b %RESULT%
