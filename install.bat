@echo off
rem LabDesk for Windows, from a git clone: creates the private Python folder (.venv) and installs the packages.
rem Run it once after "git clone", and again after "git pull".  Then start LabDesk with labdesk.bat.
setlocal
cd /d "%~dp0"

rem Find a Python 3.11+: LABDESK_PYTHON if set, else the "py" launcher, else "python" on the PATH.
rem (Each candidate is really run: on a PC without Python, "python" is only a Microsoft Store shortcut.)
set "PY="
set "CHECK=import sys; sys.exit(0 if sys.version_info[:2] in [(3, n) for n in range(11, 40)] else 1)"
if defined LABDESK_PYTHON set "PY=%LABDESK_PYTHON%"
if not defined PY py -3 -c "%CHECK%" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "%CHECK%" >nul 2>nul && set "PY=python"
if not defined PY goto :nopython
%PY% -c "%CHECK%" >nul 2>nul
if errorlevel 1 goto :nopython

echo == Creating the private Python folder (.venv)
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
if not exist ".venv\Scripts\python.exe" goto :failed

echo == Installing LabDesk's Python packages (the first time takes a few minutes)
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :failed

echo.
echo Done. Start LabDesk by double-clicking labdesk.bat in this folder.
if not defined LABDESK_NO_PAUSE pause
exit /b 0

:nopython
echo.
echo Python 3.11 or newer was not found.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo or run:  winget install --id Python.Python.3.13 -e
echo Then close this window, open a new one and run install.bat again.
if not defined LABDESK_NO_PAUSE pause
exit /b 1

:failed
echo.
echo The installation did not finish. Check your internet connection and run install.bat again.
if not defined LABDESK_NO_PAUSE pause
exit /b 1
