@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Capsule paper desk. Localhost only. live_order stays false.
if "%CAPSULE_DASH_PORT%"=="" set "CAPSULE_DASH_PORT=8791"

echo Capsule dashboard (dry-run) on 127.0.0.1:%CAPSULE_DASH_PORT%

if exist "%~dp0.venv\Scripts\python.exe" goto :venv
where py >nul 2>&1 && goto :py
where python >nul 2>&1 && goto :python

echo Capsule: Python not found. Expected .venv\Scripts\python.exe, py, or python.
exit /b 1

:venv
"%~dp0.venv\Scripts\python.exe" "%~dp0dashboard\server.py"
exit /b %ERRORLEVEL%

:py
py -3 "%~dp0dashboard\server.py"
exit /b %ERRORLEVEL%

:python
python "%~dp0dashboard\server.py"
exit /b %ERRORLEVEL%
