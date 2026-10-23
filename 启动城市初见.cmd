@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Python environment not found. Please follow README.md to install dependencies.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" run.py --api-port 8001 --ui-port 8502 --open-browser %*
if errorlevel 1 (
    echo Startup failed. Check the message above. The app may already be running.
    pause
    exit /b 1
)
