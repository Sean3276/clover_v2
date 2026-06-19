@echo off
REM ===========================================================================
REM  Clover v2 - one-click launcher
REM  Double-click this file. On the first run it installs everything Clover
REM  needs (Python packages + a small browser engine), then opens Clover in
REM  your web browser. Later runs skip the setup and start straight away.
REM ===========================================================================
setlocal EnableExtensions
chcp 65001 >nul
title Clover v2
cd /d "%~dp0"

REM ---- 1) Find a working Python --------------------------------------------
set "PY="
python -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul && set "PY=python"
if not defined PY ( py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul && set "PY=py -3" )
if not defined PY (
  echo.
  echo   Clover needs Python ^(version 3.11 or newer^), which isn't installed ^(or is too old^).
  echo.
  echo     1^) Download it from   https://www.python.org/downloads/
  echo     2^) IMPORTANT: on the first install screen, tick
  echo        "Add python.exe to PATH".
  echo     3^) When it finishes, double-click this file again.
  echo.
  start "" "https://www.python.org/downloads/"
  pause
  exit /b 1
)

REM ---- 2) Python packages (self-healing; quiet when already installed) ------
echo   Checking Clover's components ^(the first run can take a few minutes^)...
%PY% -m pip install --upgrade --disable-pip-version-check -q pip
%PY% -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo   Could not install the required Python packages.
  echo   Please check your internet connection and try again.
  echo.
  pause
  exit /b 1
)

REM ---- 3) Browser engine for fetching shared files ^(one-time, ~150 MB^) -----
set "MARKER=%~dp0.setup_done"
if not exist "%MARKER%" (
  echo   Installing the browser engine used to fetch shared files...
  %PY% -m playwright install chromium
  if errorlevel 1 (
    echo.
    echo   Could not install the browser engine ^(needed for fetching shared files^).
    echo   Check your internet connection and run this file again.
    echo.
    pause
    exit /b 1
  )
  echo done> "%MARKER%"
)

REM ---- 4) Prepare the runtime folder ^(creates .clover_v2, seeds config^) ----
%PY% bootstrap.py
if errorlevel 1 (
  echo.
  echo   Setup did not complete. See the message above.
  echo.
  pause
  exit /b 1
)

REM ---- 5) Launch -----------------------------------------------------------
echo.
echo   Starting Clover - your browser will open at http://127.0.0.1:8765
echo   Keep this window open while you use Clover. Close it to stop.
echo.
start "" "http://127.0.0.1:8765"
%PY% -m uvicorn app.main:app --host 127.0.0.1 --port 8765
echo.
echo   Clover has stopped. You can close this window now.
pause
