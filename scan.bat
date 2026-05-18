@echo off
setlocal enabledelayedexpansion
call .venv\Scripts\activate.bat
echo.
echo =============================================
echo   Socio Scanner
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
socio-scanner status --url-only > "%TEMP%\socio_url.txt" 2>nul
set /p PROFILE_URL=<"%TEMP%\socio_url.txt"
del "%TEMP%\socio_url.txt" 2>nul
if not "%PROFILE_URL%"=="" (
    echo Profile URL: %PROFILE_URL%
    socio-scanner import --linkedin-profile-url "%PROFILE_URL%" --scan-now
) else (
    echo No profile URL stored yet. Run login.bat first to auto-detect it.
    echo.
    set /p url="Or enter LinkedIn profile URL now: "
    if not "!url!"=="" socio-scanner import --linkedin-profile-url "!url!" --scan-now
)
goto end

:scan
socio-scanner scan --force
goto end

:status
socio-scanner status
goto end

:pause_menu
echo.
echo   1. Pause
echo   2. Resume
set /p pchoice="Choose (1-2): "
if "%pchoice%"=="1" socio-scanner pause --paused
if "%pchoice%"=="2" socio-scanner resume
goto end

:end
echo.
pause