@echo off
rem Optional: builds dist\PowerMeterLive.exe, a single file that runs on PCs
rem without Python. Run run.bat once first so the .venv exists.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run run.bat once first to set up the Python environment.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install pyinstaller
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed ^
    --name PowerMeterLive --collect-submodules pyvisa_py power_meter_live.py
if errorlevel 1 (
    echo Build failed - see the messages above.
    pause
    exit /b 1
)
echo.
echo Done: dist\PowerMeterLive.exe
pause
