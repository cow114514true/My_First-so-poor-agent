@echo off
cd /d "%~dp0"

REM ============================================================
REM  Local LLM server (llama.cpp) - vision mode
REM  Start this BEFORE running test_vision.py
REM ============================================================

set LLAMA_DIR=%USERPROFILE%\llama.cpp
set MODEL_DIR=%USERPROFILE%\Downloads
set PORT=8080

REM 提示用户选择模型
echo ========================================
echo 请选择模型:
echo   [1] Qwen3.5-9B-Q4_K_M.gguf (推荐, 显存 ~6GB)
echo   [2] Qwen3.6-35B.gguf (需较大显存 ~20GB+)
echo ========================================
set /p choice="请输入 1 或 2: "

if "%choice%"=="1" (
    set MODEL=%MODEL_DIR%\Qwen3.5-9B-Q4_K_M.gguf
    set MMPROJ=%MODEL_DIR%\mmproj-F16.gguf
    set NGL=99
    set CTX_SIZE=16384
    set CACHE_TYPE=q8_0
) else if "%choice%"=="2" (
    set MODEL=%MODEL_DIR%\Qwen3.6-35B-Q4_K_M.gguf
    set MMPROJ=%MODEL_DIR%\mmproj-Qwen3.6-F16.gguf
    set NGL=30
    set CTX_SIZE=8192
    set CACHE_TYPE=q8_0
) else (
    echo 无效选择，退出。
    exit /b 1
)

REM 检查文件是否存在
if not exist "%LLAMA_DIR%\llama-server.exe" (
    echo [错误] 找不到 llama-server.exe
    echo 请确认路径: %LLAMA_DIR%
    pause
    exit /b 1
)

if not exist "%MODEL%" (
    echo [错误] 找不到模型文件: %MODEL%
    pause
    exit /b 1
)

if not exist "%MMPROJ%" (
    echo [警告] 找不到 mmproj 文件: %MMPROJ%
    echo 多模态功能可能不可用。
    set MMPROJ=
)

echo.
echo ========================================
echo  启动 LLM 服务器...
echo  模型: %MODEL%
echo  端口: %PORT%
echo  上下文: %CTX_SIZE%
echo ========================================
echo.
echo 按 Ctrl+C 停止服务器
echo.

REM 启动服务器
if "%choice%"=="1" (
    "%LLAMA_DIR%\llama-server.exe" -m "%MODEL%" --mmproj "%MMPROJ%" --host 127.0.0.1 --port %PORT% -ngl %NGL% --ctx-size %CTX_SIZE% --cache-type-k %CACHE_TYPE% --cache-type-v %CACHE_TYPE%
) else (
    "%LLAMA_DIR%\llama-server.exe" -m "%MODEL%" --mmproj "%MMPROJ%" --host 127.0.0.1 --port %PORT% -ngl %NGL% --ctx-size %CTX_SIZE% --cache-type-k %CACHE_TYPE% --cache-type-v %CACHE_TYPE%
)

pause