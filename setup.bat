@echo off
echo ====================================
echo   Teams Watcher — First-Time Setup
echo ====================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed or not in PATH.
    echo Download it from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Creating virtual environment...
python -m venv .venv
call .venv\Scripts\activate.bat

echo Installing dependencies...
pip install -r requirements.txt

echo.
if not exist .env (
    copy .env.example .env
    echo Created .env file — open it and fill in your values!
) else (
    echo .env already exists — skipping.
)

echo.
echo ====================================
echo   Setup complete!
echo   1. Edit .env with your credentials
echo   2. Run  run.bat  to start
echo ====================================
pause
