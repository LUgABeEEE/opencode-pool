@echo off
rem Launch the opencode-pool Windows client without a console window.
rem Prefer pythonw, then py -3w, then plain python (which keeps a console).
rem Keep this file pure ASCII: cmd.exe reads .cmd bytes as the OEM code page.
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "run_client.py" %*
    exit /b 0
)

where py >nul 2>nul
if %errorlevel%==0 (
    start "" py -3w "run_client.py" %*
    exit /b 0
)

echo pythonw / py not found; falling back to python (a console window stays open).
python "run_client.py" %*
