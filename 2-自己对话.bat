@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title ChiBaoBao Agent - Chat

rem ---- find a usable python interpreter ----
set "PYEXE="

py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"

if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PYEXE=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PYEXE if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" set "PYEXE=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"

if not defined PYEXE goto NOPY

%PYEXE% banner.py chat
if errorlevel 1 goto RUNFAIL

echo [1/2] Checking local model service...
curl -s --max-time 4 http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo       Service not running, starting Ollama ...
    start "" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
    timeout /t 8 >nul
) else (
    echo       [OK] Model service is running
)

echo.
echo [2/2] Entering chat  (interpreter: %PYEXE%)
echo.

%PYEXE% agent_chat.py

echo.
echo Bye.
echo.
pause
exit /b 0

:NOPY
echo.
echo [ERROR] No Python found on this computer.
echo.
echo   Tried: py -3 / Python314 / Python313 / Python312 / bundled runtime
echo   Install Python from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" during setup.
echo.
pause
exit /b 1

:RUNFAIL
echo.
echo [ERROR] The banner script failed to run.
echo   Interpreter: %PYEXE%
echo   Try running this in cmd to see the real error:
echo     %PYEXE% banner.py chat
echo.
pause
exit /b 1
