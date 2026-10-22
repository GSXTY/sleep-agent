@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8100"

echo ============================================================
echo   Sleep Agent Demo
echo ============================================================

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo Please install Python and add it to PATH.
    echo.
    pause
    exit /b 1
)

if not exist "demo_server.py" (
    echo [ERROR] demo_server.py not found.
    echo Put this file in the project root directory.
    echo.
    pause
    exit /b 1
)

echo Port: %PORT%
echo Keep this window open. Press Ctrl+C to stop.
echo The server prints the local and phone URLs below.
echo ============================================================
echo.

python demo_server.py --host 0.0.0.0 --port %PORT%

echo.
echo Demo stopped.
pause
