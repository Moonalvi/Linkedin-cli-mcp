@echo off
call .venv\Scripts\activate.bat
echo Opening LinkedIn login browser...
echo Sign in, then close the browser and press Enter here.
socio-scanner login
pause