@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment not found.
  echo Put check-progress.bat and check-progress.py beside update-now.bat.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -u "%~dp0check-progress.py"
echo.
pause
