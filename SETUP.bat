@echo off
cd /d %~dp0
python --version >nul 2>&1
if %errorlevel%==0 (python setup.py) else (py -3 setup.py)
pause
