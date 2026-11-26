@echo off
rem Start the ML AI Code -> OpenAI bridge and keep it running.
setlocal enabledelayedexpansion
title mlai-bridge
cd /d "%~dp0"

set "PYEXE="
call "%~dp0find-python.bat"
if not defined PYEXE (
  echo.
  echo   Python not found. Install Python 3.9+ ^(python.org^) and run this again.
  echo.
  pause
  exit /b 1
)

echo Starting mlai-bridge on http://127.0.0.1:20129 ...
%PYEXE% -u bridge.py
echo.
echo Bridge exited. Press any key to close.
pause >nul
endlocal
