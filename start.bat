@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title ChiBaoBao - Campus Food Decision Agent

rem ---- find a usable python interpreter ----
set "PYEXE="

py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"

if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PYEXE=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

if not defined PYEXE goto NOPY

echo.
echo [1/2] Local model service (only needed when cloud key is empty) ...
curl -s --max-time 3 http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo       Not running. If you use the cloud model this is fine.
    if exist "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" (
        echo       Trying to start Ollama ...
        start "" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
        timeout /t 6 >nul
    )
) else (
    echo       [OK] Ollama is running
)

echo.
echo [2/2] Starting the app (interpreter: %PYEXE%)
echo       A desktop window will open. KEEP THIS WINDOW OPEN.
echo       Close this window to stop the server.
echo.
echo ----------------------------------------------------------------
echo.

%PYEXE% app.py

echo.
echo App stopped.
echo.
pause
exit /b 0

:NOPY
echo.
echo [ERROR] No Python found on this computer.
echo   Install Python from https://www.python.org/downloads/
echo.
pause
exit /b 1
