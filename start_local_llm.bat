@echo off
setlocal enabledelayedexpansion
cd /d %~dp0

REM ============================================================
REM  Local LLM server (llama.cpp) - vision mode
REM  Start this BEFORE running test_vision.py
REM  All paths use %%USERPROFILE%% so this file is pure ASCII.
REM ============================================================

set LLAMA_DIR=%USERPROFILE%\llama.cpp
set MODEL_DIR=%USERPROFILE%\Downloads
REM set MODEL=%USERPROFILE%\Downloads\Qwen3.5-9B-Q4_K_M.gguf
set /p model="3.5 or 3.6: "

if "!model!" =="3.5" (
    set MODEL_FACT=%MODEL_DIR%\Qwen3.5-9B-Q4_K_M.gguf
    set MMPROJ=%USERPROFILE%\Downloads\mmproj-F16.gguf
    set NGL=99
    set CTX_SIZE=16384
) else if "!model!"=="3.6" (
    set MODEL_FACT=%MODEL_DIR%\Qwen3.6-35B.gguf
    set NGL=30
    set CTX_SIZE=8192
    set CTK=q8_0
    set CTV=q8_0
) else (
    echo Invalid choice. Please type 3.5 or 3.6.
)

set PORT=8080

if not exist "%LLAMA_DIR%\llama-server.exe" (
    echo [ERROR] llama-server.exe not found in %LLAMA_DIR%
    exit /b 1
)
if not exist "%MODEL_DIR%" (
    echo [ERROR] Model_DIR not found: %MODEL%
    exit /b 1
)
if "!model!"=="3.5" if not exist "%MMPROJ%" (
    echo [ERROR] mmproj not found: %MMPROJ%
    echo.
    echo Download mmproj-F16.gguf from:
    echo   https://huggingface.co/unsloth/Qwen3.5-9B-GGUF
    echo Save it as %MMPROJ%
    echo If you got mmproj-BF16.gguf instead, edit MMPROJ at the top.
    exit /b 1
)

echo Starting llama-server ...
echo   model : %MODEL_FACT%
echo   mmproj: %MMPROJ%
echo   listen: 127.0.0.1:%PORT%
echo   ctx   : %CTX_SIZE% (KV cache: q8_0)
echo.
echo Ctrl+C to stop.
echo.
REM NO --parallel here: the agent consumes each streaming response fully BEFORE running its
REM tool calls, so the analyze_image self-call always finds the single slot free - no deadlock.
REM --parallel 2 would HALVE each slot's context to ctx/2 (8k), causing context-overflow 400s.
REM --repeat-penalty 1.15 suppresses degenerate repetition (local 9B loops its thinking when stuck).
if "!model!"=="3.5" (
    "%LLAMA_DIR%\llama-server.exe" -m "%MODEL_FACT%" --mmproj "%MMPROJ%" --host 127.0.0.1 --port %PORT% -ngl %NGL% --ctx-size %CTX_SIZE% --cache-type-k q8_0 --cache-type-v q8_0 --temp 0.6 --repeat-penalty 1.15
) else if "!model!"=="3.6" (
    "%LLAMA_DIR%\llama-server.exe" -m "%MODEL_FACT%" --host 127.0.0.1 --port %PORT% -ngl %NGL% --ctx-size %CTX_SIZE% --cache-type-k %CTK% --cache-type-v %CTV% --temp 0.5 --repeat-penalty 1.15
)

