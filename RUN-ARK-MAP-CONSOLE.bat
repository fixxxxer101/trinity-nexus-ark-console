@echo off
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" goto nosetup
".venv\Scripts\python.exe" "arkmap\arkmap.py"
pause
exit /b
:nosetup
echo Please double-click SETUP.bat first, then run this again.
pause
