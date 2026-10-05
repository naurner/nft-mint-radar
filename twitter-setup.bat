@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title WL Parser - Twitter

where python >nul 2>&1
if errorlevel 1 (
    echo   Python not found. Run start.bat first.
    pause
    exit /b 1
)

python -m wlparser twitter-add
if errorlevel 1 goto end

echo.
python -m wlparser twitter-test

:end
echo.
pause
