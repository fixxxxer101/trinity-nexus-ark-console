@echo off
cd /d %~dp0
title Trinity Nexus ARK Console
rem ================================================================
rem  Trinity Nexus ARK Console - the only file you ever click.
rem  First run on a PC: builds the app environment, then walks
rem  through the one-time token setup in the console window.
rem  Every later run: skips straight to starting the console.
rem  Modes: the window by default. Terminal mode or an update
rem  can be passed as an argument instead, e.g.
rem    RUN-ARK-MAP.bat console
rem    RUN-ARK-MAP.bat update
rem ================================================================

rem --- find Python: the py launcher first, because it only
rem --- exists alongside a proper python.org install
set PY=
py -3 --version >nul 2>&1
if not errorlevel 1 set PY=py -3
if defined PY goto havpy
python --version >nul 2>&1
if not errorlevel 1 set PY=python
if not defined PY goto nopython
:havpy

rem --- build the app environment once; skip it once it exists
if exist ".venv\Scripts\pythonw.exe" goto venvok
echo.
echo First run on this PC - building the app environment.
echo This happens once. Later runs skip straight to the console.
echo.
%PY% setup.py
if errorlevel 1 goto setupfail
goto aftervenv
:venvok
rem --- an update can flag that the environment needs a refresh
if not exist ".needs-setup" goto aftervenv
%PY% setup.py
if errorlevel 1 goto setupfail
del ".needs-setup"
:aftervenv

rem --- the one-time token setup needs a console to type in, so
rem --- a first-ever start always goes there
if not exist "%USERPROFILE%\.arkmap\config.json" goto firstconsole

rem --- arguments beat the prompt
if /i "%~1"=="console" goto console
if /i "%~1"=="update" goto update

rem --- window mode (updating lives on the window's Update button)
start "" ".venv\Scripts\pythonw.exe" "arkmap\arkmap.py" --gui
exit /b

:console
".venv\Scripts\python.exe" "arkmap\arkmap.py"
pause
exit /b

:firstconsole
echo.
echo One-time setup: the app will ask for your Nitrado API token
echo and service ID in the console window. Answer once - the
echo console then starts right there. From the next run on, this
echo file opens the app window by itself.
echo.
".venv\Scripts\python.exe" "arkmap\arkmap.py"
pause
exit /b

:update
rem --- the updater runs in its own window and this launcher
rem --- exits, so the update can replace this very file safely
start "ARK Console Update" ".venv\Scripts\python.exe" "update.py"
exit /b

:nopython
echo.
echo Python 3.10 or newer was not found on this PC.
echo Install it from https://www.python.org/downloads/ and tick
echo the "Add python.exe to PATH" box during the install.
echo Then double-click this file again - setup finishes by itself.
echo.
pause
exit /b

:setupfail
echo.
echo Setup did not finish - the message above says why.
echo Fix that, then double-click this file again.
echo.
pause
exit /b
