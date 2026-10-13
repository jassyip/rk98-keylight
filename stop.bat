@echo off
rem Stop rk98-keylight (ends the tray instance recorded in tray.pid)
setlocal
cd /d "%~dp0"
if not exist tray.pid (
    echo [rk98-keylight] not running ^(no tray.pid^)
    exit /b 0
)
set /p PID=<tray.pid
echo [rk98-keylight] stopping PID %PID%...
taskkill /PID %PID% /F >nul 2>&1
del tray.pid >nul 2>&1
echo [rk98-keylight] stopped. Keyboard reverts to its own lighting.
exit /b 0