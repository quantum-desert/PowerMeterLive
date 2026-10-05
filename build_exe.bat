@echo off
rem Builds PowerMeterLive.exe, a single file with the app's icon that runs on
rem PCs without Python, and copies it to the parent folder (..\PowerMeterLive.exe).
rem The .exe holds a frozen copy of the code: run this again after every change
rem to power_meter_live.py. Run run.bat once first so the .venv exists.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run run.bat once first to set up the Python environment.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install pyinstaller
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed ^
    --name PowerMeterLive --icon assets\power_meter_live.ico ^
    --add-data "assets;assets" ^
    --collect-submodules pyvisa_py power_meter_live.py
if errorlevel 1 (
    echo Build failed - see the messages above.
    pause
    exit /b 1
)
copy /Y "dist\PowerMeterLive.exe" "..\PowerMeterLive.exe" >nul
if errorlevel 1 (
    echo.
    echo Built dist\PowerMeterLive.exe, but could not replace ..\PowerMeterLive.exe.
    echo Close Power Meter Live if it is running, then run this again.
    pause
    exit /b 1
)
echo.
echo Done: updated %~dp0..\PowerMeterLive.exe
pause
