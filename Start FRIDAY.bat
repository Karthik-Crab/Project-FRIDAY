@echo off
rem Opens FRIDAY's chat app in the browser (Windows). Double-click this file.
rem Keep the window that opens; closing it stops FRIDAY.
title FRIDAY
setlocal
set "HERE=%~dp0"
if "%FRIDAY_PORT%"=="" set "FRIDAY_PORT=8765"
set "URL=http://127.0.0.1:%FRIDAY_PORT%"
set "FRIDAY=%HERE%backend\.venv\Scripts\friday.exe"

curl.exe -s -o NUL -m 2 "%URL%/api/status" >NUL 2>&1
if not errorlevel 1 (
    echo FRIDAY is already running. Opening it in the browser.
    start "" "%URL%"
    timeout /t 2 >NUL
    exit /b 0
)

if not exist "%FRIDAY%" (
    echo FRIDAY is not installed in %HERE%backend\.venv yet.
    pause
    exit /b 1
)

echo Starting FRIDAY. The browser opens by itself when it is ready.
echo Keep this window open while you use FRIDAY. Close it to stop FRIDAY.
echo.
cd /d "%HERE%backend"
"%FRIDAY%" app
if errorlevel 1 (
    echo.
    echo FRIDAY stopped with a problem, see the lines above.
    pause
)
