@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

:: ============================================================
::  DeepSeek Agent — 一键安装 & 启动
::  推荐在命令行 (cmd) 中运行，不建议从 PyCharm / IDE 启动
:: ============================================================

title DeepSeek Agent Setup

echo.
echo  ╔══════════════════════════════════════════════════════╗
echo  ║   DeepSeek Agent — 一键安装 ^& 启动                  ║
echo  ╚══════════════════════════════════════════════════════╝
echo.
echo  ^> 请在命令行 (cmd) 中使用以下命令运行本脚本：
echo     cd /d %~dp0
echo     setup.bat
echo.
echo  ^> 不建议从 PyCharm 或其他 IDE 内置终端运行
echo    （IDE 可能使用自己的 Python 环境，造成包找不到）
echo.
echo  ═══════════════════════════════════════════════════════

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
        call :fail "未找到 Python！请先安装 Python 3.8+：https://www.python.org/downloads/"
    )
)

:: --- paths ---
set VENV=%~dp0.venv
set ACTIVATE=%VENV%\Scripts\activate.bat
set REQ=%~dp0requirements.txt

:: ============================================================
::  Step 1/6 — 检测当前环境已安装的包
:: ============================================================
call :next_step "检测全局 Python 环境"
echo  Python: %PY%
echo  路径 : where %PY%
%PY% --version
echo.
echo  ── 全局环境已安装的包 ──
%PY% -m pip list 2>&1
echo  ──────────────────────────

:: ============================================================
::  Step 2/6 — 创建虚拟环境
:: ============================================================
call :next_step "创建虚拟环境 (.venv)"
if exist "%VENV%" (
    echo  虚拟环境已存在，跳过创建。
) else (
    %PY% -m venv "%VENV%" 2>&1
    if %errorlevel% neq 0 call :fail "创建虚拟环境失败"
    echo  虚拟环境创建完成。
)

:: ============================================================
::  Step 3/6 — 安装 Python 依赖
:: ============================================================
call :next_step "安装 Python 依赖"
call "%ACTIVATE%"

echo.
echo  ── 虚拟环境中已有的包 ──
pip list 2>&1
echo  ──────────────────────────
echo.

echo  正在安装（已装过的会自动跳过）...
echo.
pip install -r "%REQ%" 2>&1
if %errorlevel% neq 0 call :fail "依赖安装失败，请检查网络连接"

:: ============================================================
::  Step 4/6 — 安装 Chromium
:: ============================================================
call :next_step "安装 Chromium 浏览器（Playwright）"
call "%ACTIVATE%"
playwright install chromium 2>&1
if %errorlevel% neq 0 call :fail "Chromium 安装失败，请检查网络连接"

:: ============================================================
::  Step 5/6 — 检查 DS_KEY
:: ============================================================
call :next_step "检查 API Key (DS_KEY)"

if defined DS_KEY (
    echo  DS_KEY 已设置。
) else (
    echo.
    echo  DS_KEY 未设置。请输入你的 DeepSeek API Key：
    echo  （获取地址：https://platform.deepseek.com/api_keys）
    echo.
    set /p KEY_INPUT="  API Key: "

    if "!KEY_INPUT!"=="" (
        echo  跳过 — 可稍后在 cmd 中运行 set DS_KEY=你的key 设置。
    ) else (
        set DS_KEY=!KEY_INPUT!
        echo  已临时设置 DS_KEY（仅本次终端会话有效）
        echo.
        echo  ─────────────────────────────────────────
        echo  永久设置（以后不用每次输入）：
        echo    打开 CMD，运行: setx DS_KEY "你的key"
        echo    关闭并重新打开终端生效
        echo  ─────────────────────────────────────────
    )
)

:: ============================================================
::  Step 6/6 — 启动 TUI
:: ============================================================
call :next_step "启动 TUI"
call "%ACTIVATE%"
echo.
echo  加载中...
echo.
%PY% tui.py
pause
exit /b 0

:: ============================================================
::  子程序
:: ============================================================

:next_step
set /a STEP+=1
set /a PCT=%STEP%*100/%TOTAL%
set /a BARS=%PCT%/10
set BAR=
for /l %%i in (1,1,10) do (
    if %%i leq !BARS! (set BAR=!BAR!█) else (set BAR=!BAR!░)
)
echo.
echo  [!BAR!] %PCT%%  [%STEP%/%TOTAL%] %~1
exit /b

:fail
echo.
echo  [ERROR] %~1
pause
exit /b 1
