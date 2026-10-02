@echo off
rem Power Meter Live launcher. The first run creates a private Python environment
rem (.venv) next to this file and installs the packages; later runs start at once.
rem Extra arguments are passed through, e.g.  run.bat --sim  (no hardware needed)
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto launch
echo Setting up Power Meter Live - first run only, this takes a minute...
py -3 -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if not exist ".venv\Scripts\python.exe" goto nopython
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto pipfailed

:launch
start "" ".venv\Scripts\pythonw.exe" power_meter_live.py %*
exit /b 0

:nopython
echo.
echo Python 3.10 or newer is required: https://www.python.org/downloads/
echo Tick "Add python.exe to PATH" during installation, then run this again.
pause
exit /b 1

:pipfailed
echo.
echo Installing packages failed - see the messages above.
pause
exit /b 1
