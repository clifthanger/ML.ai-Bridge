@echo off
setlocal enabledelayedexpansion
title ML AI Code Console
cd /d "%~dp0"

rem --- find a python that exists -------------------------------------------
set "PYEXE="
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"
  "%ProgramFiles%\Python311\python.exe"
) do (
  if not defined PYEXE if exist "%%~P" set "PYEXE=%%~P"
)
if not defined PYEXE (
  where py >nul 2>&1
  if !errorlevel! equ 0 set "PYEXE=py -3"
)
if not defined PYEXE (
  where python >nul 2>&1
  if !errorlevel! equ 0 set "PYEXE=python"
)
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
