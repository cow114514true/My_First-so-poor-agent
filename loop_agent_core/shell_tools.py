"""基础工具：日期/天气/命令执行。实时输出队列经参数注入（薄壳包装传 _shell_output_queue）。"""
import json
import subprocess
from datetime import datetime

from .control import interrupt_event
from .debuglog import log_error


def get_date_mock():
    return datetime.now().strftime("%Y-%m-%d")


def get_weather_mock(location, date):
    return f"Weather in {location} on {date}: Cloudy 7~13°C"


def exec_shell_win_mock(shell_cmd, q=None):
    """Execute shell command with real-time output pushed to q (TUI live shell queue)."""
    if q:
        q.put(("shell_start", shell_cmd))

    proc = subprocess.Popen(
        shell_cmd, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
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
