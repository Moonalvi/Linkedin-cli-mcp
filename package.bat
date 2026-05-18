@echo off
setlocal enabledelayedexpansion
echo.
echo =============================================
echo   Socio Scanner - Package for Distribution
echo =============================================
echo.

echo [1/3] Checking for data leaks and hard-coded paths...

set LEAK=0

:: Check for data files in source dir
for %%f in (PostAnalytics_*.xlsx *.sqlite config.json) do (
    if exist "%%f" (
        echo   WARNING: Found %%f in source dir
        set LEAK=1
    )
)
if exist "browser-profile\" (
    echo   WARNING: Found browser-profile\ in source dir
    set LEAK=1
)

:: Check for hard-coded file paths in distributable files
findstr /s /i /m "C:\\Users" setup.bat login.bat scan.bat scanner\*.py tests\*.py >nul 2>&1
if %errorlevel% equ 0 (
    echo   WARNING: Hard-coded file paths ^(C:\Users\...^) found:
    findstr /s /i "C:\\Users" setup.bat login.bat scan.bat scanner\*.py tests\*.py 2>nul
    set LEAK=1
)

if %LEAK%==1 (
    echo.
    echo Packaging ABORTED. Fix the issues above, then retry.
    echo   - Data files: run "socio-scanner reset --force" to wipe
    echo   - Hard-coded paths: replace with auto-detection or relative paths
    pause
    exit /b 1
)
echo   Clean - no data leaks or hard-coded paths.

echo [2/3] Creating zip package...
call .venv\Scripts\python.exe -c "import zipfile, os, sys; from pathlib import Path; version = __import__('scanner').__version__; zip_name = f'socio-scanner-v{version}.zip'; dirs = ['scanner', 'tests']; files = ['login.bat', 'scan.bat', 'setup.bat', 'package.bat', 'requirements.txt', 'pyproject.toml', 'README.md', '.gitignore']; zf = zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED); [zf.write(str(f)) for d in dirs for f in sorted(Path(d).rglob('*')) if f.is_file() and '__pycache__' not in str(f)]; [zf.write(f) for f in files if Path(f).exists()]; zf.close(); size = Path(zip_name).stat().st_size; print(f'  Created {zip_name} ({size} bytes)'); zr = zipfile.ZipFile(zip_name, 'r'); names = zr.namelist(); print(f'  {len(names)} files'); bad = [n for n in names if (n.endswith('.xlsx') and 'test_fixture' not in n) or n.endswith('.sqlite') or n.endswith('config.json') or 'browser-profile' in n]; print('  PASS: Clean zip' if not bad else f'  FAIL: Data leaks: {bad}'); zr.close()"

echo [3/3] Final safety scan...
call .venv\Scripts\python.exe -c "import zipfile; zf = zipfile.ZipFile('socio-scanner-v' + __import__('scanner').__version__ + '.zip', 'r'); content = b''; [content := content + zf.read(n) for n in zf.namelist() if n.endswith(('.py', '.bat', '.toml', '.md'))]; text = content.decode('utf-8', errors='ignore'); hardcoded = [line.strip() for line in text.splitlines() if 'C:\\\\Users\\\\' in line or 'C:\\Users\\' in line]; print('  WARNING: hard-coded paths found in zip:' if hardcoded else '  PASS: No hard-coded paths in zip.'); [print(f'    {h[:120]}') for h in hardcoded[:5]]; zf.close()"

echo.
echo Ready. Send the zip file to your user.
echo They should: 1^) extract  2^) run setup.bat  3^) run login.bat  4^) run scan.bat
echo.
pause