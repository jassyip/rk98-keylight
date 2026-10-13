@echo off
rem ============================================================
rem  rk98-keylight one-click launcher  (double-click me)
rem
rem  - already running : open the settings window (no duplicate instance)
rem  - not running     : start the tray app
rem
rem  Stop it:  stop.bat   |   or tray icon -> Quit
rem
rem  NOTE: a Python 3.8+ with tkinter is required for the tray UI.
rem        If the tray fails to start, run:  python src\keylight.py run
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PORT=18990"

rem Find a python that has tkinter (needed for the settings window)
set "PYW="
for %%P in (
    "%LocalAppData%\Microsoft\WindowsApps\pythonw.exe"
    "pythonw.exe"
) do (
    if not defined PYW (
        %%~sP -c "import tkinter,hid,pystray,PIL" >nul 2>&1
        if !errorlevel! equ 0 set "PYW=%%~sP"
    )
)
if not defined PYW (
    echo [rk98-keylight] No Python with tkinter+hidapi+pystray+pillow found.
    echo   Install:  pip install hidapi pystray pillow
    echo   Then run:  python src\keylight.py run     (console mode, no tray UI)
    pause
    exit /b 1
)

rem Is it already running? -> just open the settings window
curl -s --noproxy * -m 2 -X POST http://127.0.0.1:%PORT%/test -d "{\"state\":\"UI\"}" >nul 2>&1
if %errorlevel%==0 (
    echo [rk98-keylight] already running - settings window opened
    exit /b 0
)

echo [rk98-keylight] starting tray app with: %PYW%
start "" /min "%PYW%" "%~dp0src\tray.py"
exit /b 0