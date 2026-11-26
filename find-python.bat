@echo off
rem Locates a Python that actually runs and sets PYEXE.
rem PYEXE keeps its own quotes (paths here contain spaces), so callers must use
rem it bare:  %PYEXE% -u "script.py"
rem The caller must enable delayed expansion first (setlocal enabledelayedexpansion).

if defined PYEXE exit /b 0

for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"
  "%ProgramFiles%\Python313\python.exe"
  "%ProgramFiles%\Python312\python.exe"
  "%ProgramFiles%\Python311\python.exe"
) do (
  if not defined PYEXE if exist "%%~P" (
    "%%~P" -c "import sys" >nul 2>&1
    if !errorlevel! equ 0 set "PYEXE="%%~P""
  )
)

rem the py launcher and PATH python are probed too — a broken Microsoft Store
rem stub answers to `python` but cannot run anything, so it must fail the probe
if not defined PYEXE (
  where py >nul 2>&1
  if !errorlevel! equ 0 (
    py -3 -c "import sys" >nul 2>&1
    if !errorlevel! equ 0 set "PYEXE=py -3"
  )
)
if not defined PYEXE (
  where python >nul 2>&1
  if !errorlevel! equ 0 (
    python -c "import sys" >nul 2>&1
    if !errorlevel! equ 0 set "PYEXE=python"
  )
)
exit /b 0
