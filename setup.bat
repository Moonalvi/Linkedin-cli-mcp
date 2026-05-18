@echo off
setlocal
echo =============================================
echo   LinkedIn CLI - One-Time Setup
echo =============================================
echo.

:: Auto-detect Python (prefer python3, then python, then common paths)
set PYTHON=
for %%p in (python3 python) do (
    where %%p >nul 2>&1
    if %errorlevel% equ 0 (
        for /f "delims=" %%P in ('where %%p 2^>nul') do (
            set PYTHON=%%P
            goto :found_python
        )
    )
)

:: Fallback: check common install locations
for %%d in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "%ProgramFiles%\Python312\python.exe"
) do (
    if exist %%d (
        set PYTHON=%%d
        goto :found_python
    )
)

echo ERROR: Python 3.11+ not found.
echo Install from https://www.python.org/downloads/ and ensure it is in PATH.
pause
exit /b 1

:found_python
echo Using Python: %PYTHON%
"%PYTHON%" --version 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python executable failed to run
    pause
    exit /b 1
)

echo [1/4] Creating virtual environment...
"%PYTHON%" -m venv .venv
if %errorlevel% neq 0 (
    echo ERROR: Failed to create venv
    pause
    exit /b 1
)

echo [2/4] Installing dependencies...
call .venv\Scripts\activate.bat
pip install -e . --quiet
if %errorlevel% neq 0 (
    echo ERROR: Failed to install scanner
    pause
    exit /b 1
)

echo [3/4] Installing Chromium browser (this may take a minute)...
python -m playwright install chromium
if %errorlevel% neq 0 (
    echo ERROR: Failed to install Chromium
    pause
    exit /b 1
)

echo [4/4] Initializing scanner database...
linkedin-cli init
if %errorlevel% neq 0 (
    echo ERROR: Failed to initialize scanner
    pause
    exit /b 1
)

echo.
echo =============================================
echo   Setup complete!
echo   Next step: run login.bat to sign into LinkedIn
echo =============================================
pause