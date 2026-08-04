"""基础工具：日期/天气/命令执行。实时输出队列经参数注入（薄壳包装传 _shell_output_queue）。"""
import json
import subprocess
from datetime import datetime


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
    try:
        for line in proc.stdout:
            all_lines.append(line)
            if q:
                q.put(("shell_line", line))
    except Exception:
        pass

    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        if q:
            q.put(("shell_line", "\n--- TIMED OUT after 120s ---\n"))

    stdout_text = "".join(all_lines)

    if q:
        q.put(("shell_done", proc.returncode))

    return json.dumps({
        "stdout": stdout_text,
        "stderr": "",
        "returncode": proc.returncode,
        "success": proc.returncode == 0,
    }, ensure_ascii=False, indent=2)
