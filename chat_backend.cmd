@echo off
cd /d "%~dp0"
set SLEEPAGENT_DATA_SOURCE=backend
if not defined SLEEPAGENT_USER_ID set /p "SLEEPAGENT_USER_ID=Enter test user ID: "
if not defined SLEEPAGENT_USER_ID (
    echo [ERROR] SLEEPAGENT_USER_ID is required.
    pause
    exit /b 1
)
python chat.py
pause
