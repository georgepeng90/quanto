@echo off
setlocal
cd /d "%~dp0"
title Quanto Research Workbench

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Project Python environment not found.
    echo Run: uv sync --locked --extra dev --extra web --extra data
    pause
    exit /b 1
)

echo Workbench: http://127.0.0.1:8767/
echo Keep this window open. Press Ctrl+C to stop the service.
echo.

".venv\Scripts\python.exe" -m quanto.cli serve --host 127.0.0.1 --port 8767 --workspace "data\research-20261001" --frontend-dir "web\dist"
set "result=%errorlevel%"
if not "%result%"=="0" (
    echo.
    echo [ERROR] Service exited with code %result%. See the message above.
    pause
)
exit /b %result%
