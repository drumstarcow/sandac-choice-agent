@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title ChiBaoBao Agent - Auto Demo

python banner.py demo
if errorlevel 1 goto NOPY

echo [1/2] Checking local model service...
curl -s --max-time 4 http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo       Service not running, starting Ollama ...
    start "" "C:\Users\13601\AppData\Local\Programs\Ollama\ollama app.exe"
    timeout /t 8 >nul
) else (
    echo       [OK] Model service is running
)

echo.
echo [2/2] Starting auto demo ...
echo ----------------------------------------------------------------
echo.

python agent_chat.py --demo

echo.
echo ----------------------------------------------------------------
echo Demo finished. For interactive mode, run the 2nd .bat file.
echo.
pause
exit /b 0

:NOPY
echo.
echo [ERROR] Python not found in PATH.
echo Please install Python 3.10+ or add it to PATH.
echo.
pause
exit /b 1
