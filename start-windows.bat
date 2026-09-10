@echo off
REM ---------------------------------------------------------------------------
REM  TransformerProcure Intelligence - one-click start (Windows)
REM  Double-click this file. First run takes a few minutes to install.
REM ---------------------------------------------------------------------------
cd /d "%~dp0"

echo.
echo   TransformerProcure Intelligence
echo   ===============================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo   Python was not found.
  echo   Install Python 3.10+ from https://www.python.org/downloads/
  echo   Tick "Add Python to PATH" during installation, then run this again.
  echo.
  pause
  exit /b 1
)

if not exist venv (
  echo   First run - setting up ^(this takes 2-3 minutes^)...
  python -m venv venv
  venv\Scripts\pip install --quiet --upgrade pip
  venv\Scripts\pip install --quiet -r requirements.txt
  echo   Setup complete.
)

echo.
echo   Starting. The dashboard will open at http://127.0.0.1:8000
echo   Leave this window open while you use it. Press Ctrl+C to stop.
echo.

start "" /b cmd /c "timeout /t 5 >nul & start http://127.0.0.1:8000"
venv\Scripts\python backend\main.py
pause
