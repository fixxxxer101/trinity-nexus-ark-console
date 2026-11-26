@echo off
cd /d %~dp0
if not exist ".venv\Scripts\pythonw.exe" goto nosetup
start "" ".venv\Scripts\pythonw.exe" "arkmap\arkmap.py" --gui
exit /b
:nosetup
echo Please double-click SETUP.bat first, then run this again.
pause
