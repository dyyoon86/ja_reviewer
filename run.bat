@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Review Studio

if not exist ".venv" (
  echo [1/3] Creating virtual env .venv ...
  python -m venv .venv
  if errorlevel 1 (
    echo ERROR: Python not found. Install 64-bit Python from python.org, then retry.
    pause
    exit /b 1
  )
  call ".venv\Scripts\activate.bat"
  echo [2/3] Installing packages first run only, may take a few minutes ...
  python -m pip install --upgrade pip
  pip install -r "server\requirements.txt"
) else (
  call ".venv\Scripts\activate.bat"
)

echo [3/3] Starting server. Browser will open. Press Ctrl+C to stop.
python -m server.app
pause
endlocal
