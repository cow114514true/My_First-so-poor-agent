@echo off
setlocal enabledelayedexpansion

:: ============================================================
::  DeepSeek Agent — One-click setup & launch
::  Run from cmd.exe, not from PyCharm / IDE terminal
:: ============================================================

title DeepSeek Agent Setup

echo.
echo  +====================================================+
echo  ^|   DeepSeek Agent — One-click setup ^& launch       ^|
echo  +====================================================+
echo.
echo  ^> Run this script in cmd.exe:
echo     cd /d %~dp0
echo     setup.bat
echo.
echo  ^> Avoid IDE terminals (PyCharm, etc.)
echo    They may use their own Python, causing import errors
echo.
echo  ======================================================

:: --- step tracking ---
set STEP=0
set TOTAL=6

:: --- find Python ---
where python >nul 2>&1
if %errorlevel%==0 (
    set PY=python
) else (
    where py >nul 2>&1
    if %errorlevel%==0 (
        set PY=py
    ) else (
        call :fail "Python not found! Install Python 3.8+: https://www.python.org/downloads/"
    )
)

:: --- paths ---
set "VENV=%~dp0.venv"
set "ACTIVATE=%VENV%\Scripts\activate.bat"
set "REQ=%~dp0requirements.txt"

:: ============================================================
::  Step 1/6 — Show installed packages
:: ============================================================
call :next_step "Check global Python environment"
echo  Python: %PY%
echo  Path  : where %PY%
%PY% --version
echo.
echo  -- Global pip packages --
%PY% -m pip list 2>&1
echo  -------------------------

:: ============================================================
::  Step 2/6 — Create venv
:: ============================================================
call :next_step "Create virtual environment (.venv)"
if exist "%VENV%" (
    echo  venv already exists, skipping.
) else (
    %PY% -m venv "%VENV%" 2>&1
    if %errorlevel% neq 0 call :fail "Failed to create venv"
    echo  venv created.
)

:: ============================================================
::  Step 3/6 — Install Python deps
:: ============================================================
call :next_step "Install Python dependencies"
call "%ACTIVATE%"

echo.
echo  -- Packages in venv --
pip list 2>&1
echo  -----------------------
echo.

echo  Installing (already-installed packages will be skipped)...
echo.
pip install -r "%REQ%" 2>&1
if %errorlevel% neq 0 call :fail "pip install failed — check your network"

:: ============================================================
::  Step 4/6 — Install Chromium
:: ============================================================
call :next_step "Install Chromium (Playwright)"
call "%ACTIVATE%"
playwright install chromium 2>&1
if %errorlevel% neq 0 call :fail "Chromium install failed — check your network"

:: ============================================================
::  Step 5/6 — Check DS_KEY
:: ============================================================
call :next_step "Check API Key (DS_KEY)"

if defined DS_KEY (
    echo  DS_KEY is set.
) else (
    echo.
    echo  DS_KEY is not set. Enter your DeepSeek API Key:
    echo  (Get one at: https://platform.deepseek.com/api_keys)
    echo.
    set /p KEY_INPUT="  API Key: "

    if "!KEY_INPUT!"=="" (
        echo  Skipped — run `set DS_KEY=your-key` in cmd later.
    ) else (
        set DS_KEY=!KEY_INPUT!
        echo  DS_KEY set (this terminal session only)
        echo.
        echo  -------------------------------------------------
        echo  To set permanently:
        echo    Open cmd, run: setx DS_KEY "your-key"
        echo    Then reopen terminal
        echo  -------------------------------------------------
    )
)

:: ============================================================
::  Step 6/6 — Launch TUI
:: ============================================================
call :next_step "Launch TUI"
call "%ACTIVATE%"
echo.
echo  Loading...
echo.
%PY% tui.py
pause
exit /b 0

:: ============================================================
::  Subroutines
:: ============================================================

:next_step
set /a STEP+=1
set /a PCT=%STEP%*100/%TOTAL%
set /a BARS=%PCT%/10
set BAR=
for /l %%i in (1,1,10) do (
    if %%i leq !BARS! (set BAR=!BAR!#) else (set BAR=!BAR!.)
)
echo.
echo  [!BAR!] %PCT%%%  [%STEP%/%TOTAL%] %~1
exit /b

:fail
echo.
echo  [ERROR] %~1
pause
exit /b 1
