@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title WL Parser
setlocal EnableDelayedExpansion

rem IMPORTANT: keep this file pure ASCII, comments included.
rem cmd.exe reads .bat in the OEM codepage, not UTF-8, so Cyrillic bytes
rem corrupt the parser and every following line breaks.
rem All human-facing Russian text is printed by Python, which forces UTF-8.

where python >nul 2>&1
if errorlevel 1 goto nopython

rem Check a working import rather than the package alone: that also catches
rem the case where the package is present but a new dependency is missing.
python -c "import wlparser.app" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies, please wait...
    python -m pip install -e . --quiet --disable-pip-version-check
    if errorlevel 1 goto installfailed
    python -c "import wlparser.app" >nul 2>&1
    if errorlevel 1 goto installfailed
)

if not exist "config\config.yaml" (
    python -m wlparser setup
    if errorlevel 1 goto end
)

rem Same idea as restart: unless-stopped in docker-compose: the bot should
rem survive a dropped connection or a random crash instead of dying quietly.
set RETRIES=0

:runbot
python -m wlparser run
set CODE=!ERRORLEVEL!

if "!CODE!"=="0" goto stopped
if "!CODE!"=="3" goto alreadyrunning

set /a RETRIES+=1
if !RETRIES! GEQ 10 goto toomany
echo.
echo   Bot stopped unexpectedly (code !CODE!). Restart !RETRIES! of 10 in 10 seconds.
echo   Press Ctrl+C to quit.
timeout /t 10 /nobreak >nul
goto runbot

:stopped
echo.
echo   Stopped.
goto end

:alreadyrunning
rem Python already explained it in Russian. Restarting will not help.
goto end

:toomany
echo.
echo   Bot keeps crashing - restarting stopped. See the error above.
goto end

:nopython
echo.
echo   Python not found.
echo.
echo   1. Download Python 3.11 or newer: https://www.python.org/downloads/
echo   2. During install TICK the box "Add python.exe to PATH".
echo   3. Run this file again.
echo.
goto end

:installfailed
echo.
echo   Install failed. Check your internet connection and try again.
echo.

:end
echo.
pause
