@echo off
REM ---- Image Compressor: install runtime dependencies into .\libs ----
setlocal EnableExtensions
cd /d "%~dp0"

set "PYCMD="
py -3 -c "import tkinter" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"

if not defined PYCMD (
    python -c "import tkinter" >nul 2>nul
    if not errorlevel 1 set "PYCMD=python"
)

if not defined PYCMD (
    echo [ERROR] No Python 3 with tkinter found on PATH.
    echo         Install Python 3.10 or newer from https://www.python.org/downloads/
    echo         and keep the "tcl/tk and IDLE" option enabled.
    pause
    exit /b 1
)

echo Using interpreter: %PYCMD%
echo Installing into: "%~dp0libs"
echo.

%PYCMD% -m pip install --disable-pip-version-check --upgrade --target "%~dp0libs" -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo [ERROR] Dependency installation failed. Check your network or pip index.
    pause
    exit /b 1
)

echo.
echo [OK] Dependencies installed to .\libs
echo      Double-click the .vbs launcher in this folder to start the app.
pause
