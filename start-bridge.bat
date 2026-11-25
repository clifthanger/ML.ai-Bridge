@echo off
rem Start the ML AI Code -> OpenAI bridge and keep it running.
title mlai-bridge
cd /d "%~dp0"
echo Starting mlai-bridge on http://127.0.0.1:20129 ...
python bridge.py
echo.
echo Bridge exited. Press any key to close.
pause >nul
