"""基础工具：日期/天气/命令执行。实时输出队列经参数注入（薄壳包装传 _shell_output_queue）。"""
import base64
import json
import queue
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime

# pwsh (PowerShell 7) 优先；没装则退回系统自带的 Windows PowerShell 5.1
PS_EXE = shutil.which("pwsh") or "powershell.exe"

from .control import interrupt_event
from .debuglog import log_error
from .file_tools import _resolve_path  # 复用已验证的路径沙箱（见 SECURITY_AUDIT_REPORT V4）

# 终端控制序列：CSI/OSC/DCS/简单 ESC 转义，以及其余 C0 控制字符（保留 \t \n \r）。
# 命令输出是攻击者可控的（网页/文件内容可进入命令输出），必须中和后才能上屏。
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"       # CSI ... 终止符
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL 或 ST
    r"|\x1b[P^_][^\x1b]*(?:\x1b\\)"  # DCS/SOS/PM/APC ... ST
    r"|\x1b[@-Z\\-_]"               # 两字符 ESC 序列
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"  # 其余 C0 控制字符 + DEL
)


def strip_terminal_escapes(text):
    """移除终端控制序列，防 UI 欺骗（SECURITY_AUDIT_REPORT V13）。非字符串原样返回。"""
    if not isinstance(text, str):
        return text
    return _ANSI_RE.sub("", text)


def get_date_mock():
    return datetime.now().strftime("%Y-%m-%d")


def get_weather_mock(location, date):
    return f"Weather in {location} on {date}: Cloudy 7~13°C"


def exec_shell_win_mock(shell_cmd, shell="cmd", q=None):
    """Execute shell command with real-time output pushed to q (TUI live shell queue).
    shell='cmd' (default) → cmd.exe; shell='powershell' → powershell.exe, UTF-8 output forced."""
    if q:
        q.put(("shell_start", shell_cmd))

    # 模型可能发 PowerShell/ps/pwsh（本地模型无视 schema enum），统一归一化，其余值走 cmd
    if (shell or "cmd").strip().lower() in ("powershell", "ps", "pwsh"):
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

    # 读输出放后台线程 → queue；主循环短轮询，同时检查 Esc 与 120s 截止时间。
    # 原实现 for line in proc.stdout 阻塞 readline，静默挂死的命令绕开超时与中断。
    lines_q = queue.Queue()

    def _reader():
        try:
            for line in proc.stdout:
                lines_q.put(line)
        except Exception as e:
            log_error("exec_shell reader", e)
        finally:
            lines_q.put(None)  # EOF 哨兵

    threading.Thread(target=_reader, daemon=True).start()

    all_lines = []
    interrupted = False
    timed_out = False
    deadline = time.monotonic() + 120
    while True:
        if interrupt_event.is_set():  # 用户按 Esc：杀掉子进程
            proc.kill()
            proc.wait()
            interrupted = True
            break
        if time.monotonic() >= deadline:
            proc.kill()
            proc.wait()
            timed_out = True
            break
        try:
            line = lines_q.get(timeout=0.2)
        except queue.Empty:
            continue
        if line is None:  # EOF，正常结束
            break
        line = strip_terminal_escapes(line)  # 中和控制序列后再入缓冲区/上屏（V13）
        all_lines.append(line)
        if q:
            q.put(("shell_line", line))

    # EOF 后进程可能还没完全退（管道先关），wait 收尸拿回 returncode
    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    stdout_text = "".join(all_lines)

    if timed_out and q:
        q.put(("shell_line", "\n--- TIMED OUT after 120s ---\n"))
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
        "timed_out": timed_out,
    }, ensure_ascii=False, indent=2)


def search_code_mock(pattern, path=".", glob="*.py", output_mode="content"):
    """Regex code search (Python-native, no ripgrep dependency).
    pattern:   Python regex to match against each line.
    path:      Directory or file to search. Relative paths resolve against WORK_DIR.
    glob:      File pattern filter (e.g. "*.py", "*.{py,js}", "*.md").
    output_mode: "content" (lines with line numbers), "files_with_matches" (paths only), "count" (match counts)."""
    from pathlib import Path as _Path

    # 复用统一路径沙箱：此前这里用 _Path.resolve() 完全绕过沙箱，
    # 可读任意绝对路径（已实测读到 .ssh 私钥 / .dsh 凭据库）。见 SECURITY_AUDIT_REPORT V4。
    try:
        resolved = _resolve_path(path)
    except Exception as e:
        return f"[Error] search_code: {e}"
    file_path = _Path(resolved)
    if not file_path.exists():
        return f"[Error] search_code: path not found: {path}"

    if file_path.is_file():
        files = [file_path]
        root = file_path.parent
    else:
        files = sorted(file_path.rglob(glob))
        root = file_path

    if not files:
        return f"[search_code] No files matching {glob!r} in {path}"

    try:
        compiled = re.compile(pattern)
    except re.error as e:
        return f"[Error] search_code: invalid regex: {e}"

    max_files = 80
    skip_dirs = {"__pycache__", ".git", ".claude", "headroom", "node_modules", "vendor", "tmp"}
    lines_out = []
    file_count = 0

    for fp in files:
        if any(d in fp.parts for d in skip_dirs):
            continue
        file_count += 1
        if file_count > max_files:
            break
        try:
            text_lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue

        rel = str(fp.relative_to(root))
        file_hits = [(i + 1, line) for i, line in enumerate(text_lines) if compiled.search(line)]

        if output_mode == "files_with_matches":
            if file_hits:
                lines_out.append(rel)
        elif output_mode == "count":
            if file_hits:
                lines_out.append(f"{rel}: {len(file_hits)} matches")
        else:  # content
            for lineno, line in file_hits:
                lines_out.append(f"{rel}:{lineno}: {line.strip()}")

    prefix = f"[search_code] pattern={pattern!r}  path={path}  glob={glob}\n"
    if file_count > max_files:
        prefix += f"(truncated to first {max_files} matching files)\n"
    body = "\n".join(lines_out[:250]) if lines_out else "(no matches)"
    return prefix + body


def code_index_mock(path=".", glob="*.py"):
    """Return a project-level structural index: for each file, list its imports/functions/classes
    with line numbers. Use BEFORE reading individual files — get the lay of the land in one call.
    Then use search_code for cross-file references and read_file(function=...) for implementation."""
    from pathlib import Path as _Path

    from .file_tools import build_outline

    # 与 search_code 同一处沙箱逃逸：改用统一路径沙箱（SECURITY_AUDIT_REPORT V4）。
    try:
        resolved = _resolve_path(path)
    except Exception as e:
        return f"[Error] code_index: {e}"
    file_path = _Path(resolved)
    if not file_path.exists():
        return f"[Error] code_index: path not found: {path}"

    skip_dirs = {"__pycache__", ".git", ".claude", "headroom", "node_modules", "vendor", "tmp"}

    if file_path.is_file():
        files = [file_path]
        root = file_path.parent
        skip_dirs = set()  # 显式指定文件时不跳过（包括 tmp 下的测试文件等）
    else:
        files = sorted(file_path.rglob(glob))
        root = file_path

    if not files:
        return f"[code_index] No files matching {glob!r} in {path}"

    max_files = 60
    lines_out = []
    file_count, total_funcs = 0, 0

    for fp in files:
        if any(d in fp.parts for d in skip_dirs):
            continue
        file_count += 1
        if file_count > max_files:
            rest = sum(1 for f in files if not any(d in f.parts for d in skip_dirs)) - max_files + 1
            lines_out.append(f"\n... (truncated, ~{rest} more files — narrow path or glob)")
            break
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        total_lines = len(content.splitlines())
        outline = build_outline(content)
        rel = str(fp.relative_to(root))
        lines_out.append(f"\n{rel} ({total_lines} lines)")
        if outline:
            for kind, name, ln in outline:
                lines_out.append(f"  {ln:>5}  {kind} {name}")
                total_funcs += 1
        else:
            lines_out.append("  (no functions/classes/imports detected)")

    header = f"[code_index] path={path}  glob={glob}  files={file_count}  symbols={total_funcs}\n"
    return header + "\n".join(lines_out)
