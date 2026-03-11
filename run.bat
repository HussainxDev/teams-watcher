@echo off
echo Starting Teams Watcher...
echo Press Ctrl+C to stop.
echo.

if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
)

python teams_watcher.py
pause
