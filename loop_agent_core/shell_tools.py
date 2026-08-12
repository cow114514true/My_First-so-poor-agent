"""基础工具：日期/天气/命令执行。实时输出队列经参数注入（薄壳包装传 _shell_output_queue）。"""
import base64
import json
import shutil
import subprocess
from datetime import datetime

# pwsh (PowerShell 7) 优先；没装则退回系统自带的 Windows PowerShell 5.1
PS_EXE = shutil.which("pwsh") or "powershell.exe"

from .control import interrupt_event
from .debuglog import log_error


def get_date_mock():
    return datetime.now().strftime("%Y-%m-%d")


def get_weather_mock(location, date):
    return f"Weather in {location} on {date}: Cloudy 7~13°C"


def exec_shell_win_mock(shell_cmd, shell="cmd", q=None):
    """Execute shell command with real-time output pushed to q (TUI live shell queue).
    shell='cmd' (default) → cmd.exe; shell='powershell' → powershell.exe, UTF-8 output forced."""
    if q:
        q.put(("shell_start", shell_cmd))

    if shell == "powershell":
        # -EncodedCommand = base64(UTF-16LE)：整脚本做单参数传，绕开 PowerShell 引号解析。
        # 前置一行强制管道输出 UTF-8（pwsh 本就 UTF-8；仅 5.1 兜底分支必需）。
        script = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n" + shell_cmd
        argv = [PS_EXE, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                base64.b64encode(script.encode("utf-16-le")).decode("ascii")]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
    else:
        proc = subprocess.Popen(
            shell_cmd, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )

    all_lines = []
    interrupted = False
    try:
        for line in proc.stdout:
            if interrupt_event.is_set():  # 用户按 Esc：杀掉子进程
                proc.kill()
                interrupted = True
                break
            all_lines.append(line)
            if q:
                q.put(("shell_line", line))
    except Exception as e:
        log_error("exec_shell read stdout", e)

    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        if q:
            q.put(("shell_line", "\n--- TIMED OUT after 120s ---\n"))

    stdout_text = "".join(all_lines)

    if interrupted and q:
        q.put(("shell_line", "\n--- [interrupted by user] ---\n"))
    if q:
        q.put(("shell_done", proc.returncode))

    return json.dumps({
        "stdout": stdout_text,
        "stderr": "",
        "returncode": proc.returncode,
        "success": proc.returncode == 0,
        "interrupted": interrupted,
    }, ensure_ascii=False, indent=2)
