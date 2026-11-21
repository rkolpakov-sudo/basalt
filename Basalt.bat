@echo off
rem Basalt: one file for everything.
rem   Basalt.bat              start, or stop if it is already running
rem   Basalt.bat start        start (opens the web UI)
rem   Basalt.bat stop         stop
rem   Basalt.bat restart      stop, then start
rem   Basalt.bat status       say whether it runs
title Basalt
cd /d "%~dp0"

set "CTL=C:\basalt-v0.4.1-windows-x64\serve_ctl.ps1"
set "GIVEN=%~1"

if "%GIVEN%"=="" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%CTL%" -Action toggle -Open
    if errorlevel 1 pause
    exit /b 0
)
if /i "%GIVEN%"=="toggle" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%CTL%" -Action toggle -Open
    if errorlevel 1 pause
    exit /b 0
)
if /i "%GIVEN%"=="start" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%CTL%" -Action start -Open
    if errorlevel 1 pause
    exit /b 0
)
if /i "%GIVEN%"=="stop"    goto action
if /i "%GIVEN%"=="restart" goto action
if /i "%GIVEN%"=="status"  goto action
echo Usage: Basalt.bat [start^|stop^|restart^|status]
exit /b 2

:action
powershell -NoProfile -ExecutionPolicy Bypass -File "%CTL%" -Action %GIVEN%
exit /b %errorlevel%
