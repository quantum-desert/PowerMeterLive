@echo off
rem Power Meter Live launcher. The first run creates a private Python environment
rem (.venv) next to this file and installs the packages; later runs start at once.
rem Extra arguments are passed through, e.g.  run.bat --sim  (no hardware needed)
rem
rem It also creates "Power Meter Live" shortcuts, with the app's icon, in this
rem folder and on the desktop. Use those to start the app or pin it to the
rem taskbar / Start. Delete "Power Meter Live.lnk" here to recreate both.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto shortcut
echo Setting up Power Meter Live - first run only, this takes a minute...
py -3 -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if not exist ".venv\Scripts\python.exe" goto nopython
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto pipfailed

:shortcut
if exist "Power Meter Live.lnk" goto launch
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d='%~dp0'.TrimEnd('\'); $w=New-Object -ComObject WScript.Shell; foreach ($dir in @($d, [Environment]::GetFolderPath('Desktop'))) { $s=$w.CreateShortcut((Join-Path $dir 'Power Meter Live.lnk')); $s.TargetPath=(Join-Path $d '.venv\Scripts\pythonw.exe'); $s.Arguments='power_meter_live.py'; $s.WorkingDirectory=$d; $s.IconLocation=(Join-Path $d 'assets\power_meter_live.ico') + ',0'; $s.Description='Live optical power plot for the Agilent 8163A'; $s.Save() }" >nul 2>&1
if exist "Power Meter Live.lnk" echo Created "Power Meter Live" shortcuts here and on the desktop.

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
