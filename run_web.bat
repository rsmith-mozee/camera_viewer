@echo off
rem Browser-based version (no Pillow needed). The native desktop app is run.bat.
cd /d "%~dp0"
python axis_viewer.py
if errorlevel 1 (
  echo.
  echo If you saw "python is not recognized", install Python 3 from https://python.org
  echo and tick "Add python.exe to PATH" during setup, then run this again.
  echo.
  pause
)
