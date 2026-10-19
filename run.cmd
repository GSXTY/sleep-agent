@echo off
rem Sleep breathing product assistant - Windows launcher
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python not found. Install Python 3.10+ and tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)
python chat.py %*
if errorlevel 1 pause
