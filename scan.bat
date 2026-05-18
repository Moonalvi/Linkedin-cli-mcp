@echo off
setlocal enabledelayedexpansion
call .venv\Scripts\activate.bat
echo.
echo =============================================
echo   LinkedIn CLI
echo =============================================
echo.
echo   1. Discover + Scan (fresh import)
echo   2. Scan pending (process due queue)
echo   3. Status
echo   4. Pause / Resume
echo.
set /p choice="Choose (1-4): "

if "%choice%"=="1" goto import
if "%choice%"=="2" goto scan
if "%choice%"=="3" goto status
if "%choice%"=="4" goto pause_menu
goto end

:import
linkedin-cli status --url-only > "%TEMP%\linkedin_cli_url.txt" 2>nul
set /p PROFILE_URL=<"%TEMP%\linkedin_cli_url.txt"
del "%TEMP%\linkedin_cli_url.txt" 2>nul
if not "%PROFILE_URL%"=="" (
    echo Profile URL: %PROFILE_URL%
    linkedin-cli import --linkedin-profile-url "%PROFILE_URL%" --scan-now
) else (
    echo No profile URL stored yet. Run login.bat first to auto-detect it.
    echo.
    set /p url="Or enter LinkedIn profile URL now: "
    if not "!url!"=="" linkedin-cli import --linkedin-profile-url "!url!" --scan-now
)
goto end

:scan
linkedin-cli scan --force
goto end

:status
linkedin-cli status
goto end

:pause_menu
echo.
echo   1. Pause
echo   2. Resume
set /p pchoice="Choose (1-2): "
if "%pchoice%"=="1" linkedin-cli pause --paused
if "%pchoice%"=="2" linkedin-cli resume
goto end

:end
echo.
pause