@echo off
rem LabDesk for Windows, from a git clone: starts the app with its private Python folder (.venv).
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo LabDesk is not installed in this folder yet. Double-click install.bat first.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m labdesk_app %*
