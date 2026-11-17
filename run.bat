@echo off
cd /d "%~dp0"
python -c "import PIL" 1>NUL 2>NUL
if errorlevel 1 (
  echo Installing Pillow ^(first run only^)...
  python -m pip install pillow
)
python axis_viewer_desktop.py
if errorlevel 1 (
  echo.
  echo If you saw "python is not recognized", install Python 3 from https://python.org
  echo and tick "Add python.exe to PATH" during setup, then run this again.
  echo.
  pause
)
