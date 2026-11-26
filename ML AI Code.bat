@echo off
setlocal enabledelayedexpansion
title ML AI Code Console
cd /d "%~dp0"

rem --- find a python that exists and actually runs --------------------------
set "PYEXE="
call "%~dp0find-python.bat"

if not defined PYEXE (
  echo.
  echo   Python not found. Install Python 3.9+ ^(python.org^) and run this again.
  echo.
  pause
  exit /b 1
)

rem -u keeps output unbuffered so the console never looks frozen
%PYEXE% -u "%~dp0mlai_console.py" %*
if errorlevel 1 (
  echo.
  echo   Console exited with an error. Press any key to close.
  pause >nul
)
endlocal
