@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title ChiBaoBao - Preflight Check

set "PYEXE="
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PYEXE=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PYEXE goto NOPY

%PYEXE% preflight.py
echo.
pause
exit /b 0

:NOPY
echo [ERROR] No Python found.
pause
exit /b 1
