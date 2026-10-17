@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run python setup.py first.
  pause
  exit /b 2
)
if "%~1"=="" (
  start "" ".venv\Scripts\pythonw.exe" main.py
  exit /b 0
)
".venv\Scripts\python.exe" main.py %*
set "CHECKIN_EXIT=%ERRORLEVEL%"
exit /b %CHECKIN_EXIT%
