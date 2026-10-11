"""基础工具：日期/天气/命令执行。实时输出队列经参数注入（薄壳包装传 _shell_output_queue）。"""
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime

# pwsh (PowerShell 7) 优先；没装则退回系统自带的 Windows PowerShell 5.1
PS_EXE = shutil.which("pwsh") or "powershell.exe"

from .control import interrupt_event
from .config import WORK_DIR
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


# 管道命令的两条提示（见 exec_shell_win_mock 末尾的注释：实测两次把 agent 卡住）
_PIPE_NOTE = ("[note] 这是管道命令：returncode/success 取自管道最后一个命令，不代表被测程序是否成功。")
_FINDSTR_NOTE = ("findstr 只显示匹配行——测试的汇总行（`N 项失败` / `全部通过`）若不在你的 /C: 模式里就会被滤掉，"
                 "于是「看不全 → 换模式再过滤 → 还是看不全」。要看结论就别过滤，或把汇总关键字一起写进模式，"
                 "或在 Python 侧过滤。")


def get_weather_mock(location, date):
    return f"Weather in {location} on {date}: Cloudy 7~13°C"


_PS_SHELL_ALIASES = ("powershell", "ps", "pwsh")

# cmd 独有语法 → (识别名, pwsh 里怎么写)。
# 为什么需要它：cmd 语法在 pwsh 下常常**不报错**而是静默跑错——`dir /b` 被 pwsh 当成
# `Get-ChildItem C:\b`（"找不到路径 C:\b"），`%VAR%` 原样当字符串，`2>nul` 会去建一个叫
# nul 的文件。模型看不出"这是 shell 语法错"，只会得出"pwsh 坏了"。命中就改用 cmd 跑，
# 并把对应写法回给对方 —— 兼容的同时把正确写法教会它。
_CMD_ONLY_SYNTAX = (
    (re.compile(r"%[A-Za-z_][A-Za-z0-9_]*%"), "%VAR% 环境变量",
     "`$env:VAR`"),
    (re.compile(r"(?i)(^|[\s|&(])dir\s+/[a-z]"), "dir /开关",
     "`Get-ChildItem -Name`（只要名字）/ `Get-ChildItem -Recurse -File`"),
    (re.compile(r"(?i)(^|[\s|&(])for\s+/[fd]"), "for /f、for /d",
     "`foreach ($x in $list) { … }` / `Get-ChildItem | ForEach-Object { … }`"),
    (re.compile(r"(?i)(^|[\s|&(])if\s+(not\s+)?(exist|errorlevel)\b"), "if exist / if errorlevel",
     "`if (Test-Path $p) { … }` / `if ($LASTEXITCODE -ne 0) { … }`"),
    (re.compile(r"(?i)\d?>\s*nul\b"), "2>nul",
     "`2>$null`"),
    (re.compile(r"(?i)(^|[\s|&(])(set\s+/[ap]|choice\s+/|ver|vol|assoc|mklink|fc)\b"),
     "cmd 内建命令",
     "`Get-Volume` / `New-Item -ItemType SymbolicLink` / `Get-FileHash` / `cmd /c ver`"),
    (re.compile(r"(^|[\s|&(])(xcopy|robocopy)\b", re.I), "xcopy / robocopy",
     "两者本身是 exe，pwsh 可直接调用；纯拷贝可用 `Copy-Item -Recurse`"),
    (re.compile(r"%\d|%~[a-zA-Z]"), "%~dp0 / %1 参数展开",
     "`$args[0]` / `$PSScriptRoot`"),
    (re.compile(r"(?i)(^|[\s|&(])@?echo\s+(off|on)\b"), "@echo off",
     "pwsh 不回显脚本，无需这条"),
)
# 出现这些就是 PowerShell 写法，绝不改道（防止 `Get-Date -Format "%Y%m%d"` 被 %…% 规则误判）
_PS_SYNTAX_MARKERS = re.compile(
    r"(\$|Get-[A-Za-z]|Set-[A-Za-z]|Select-|Where-|ForEach|Out-|Write-|Import-|Measure-|"
    r"Test-Path|New-Item|Remove-Item|Copy-Item|Get-ChildItem|\|\s*Where)", re.I)


def _is_ps_shell(shell):
    return (shell or "pwsh").strip().lower() in _PS_SHELL_ALIASES


# 结尾把"最后一条命令是否成功"反映到退出码（见 _ps_script_text）：pwsh 里 cmdlet 报错是
# **非终止**错误，脚本照样 exit 0（`Get-ChildItem /b` → rc=0 + 一条错误），模型看不出失败。
_PS_EPILOGUE_BODY = ("if (-not $?) { $global:__psbad = 1;"
                     " if ($LASTEXITCODE -and $LASTEXITCODE -ne 0)"
                     " { $global:__pscode = $LASTEXITCODE } }")
_PS_EXIT_BODY = ("if ($global:__psbad) {"
                 " if ($global:__pscode) { exit $global:__pscode } else { exit 1 } }")


def _ps_script_text(shell_cmd):
    """包一层 pwsh 脚本：UTF-8 输出 + **语法预检** + 用户命令 + 退出码回填。

    两处"不这样就会误导模型"的细节：
    1. 用户命令放在 here-string 里、再用 scriptblock 执行：否则一个未闭合的括号会把
       结尾的退出码回填一起吞掉，报错里露出来的是我的内部代码（实测
       `ParserError: …\\tmp\\_pwsh_xxx.ps1:3 … $LASTEXITCODE -ne 0 …`）。
    2. 语法预检用 `Parser::ParseInput`：模型拿到的是一句干净的
       「ParserError: “(” 后应为表达式。(第 1 行)」，且行号是它自己的行号。
    实测语义：`Get-ChildItem /b` → rc=1；`cmd /c exit 2` → rc=2；用户自己 `exit 7` → rc=7。
    """
    header = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
    if any(ln.lstrip().startswith("'@") for ln in shell_cmd.splitlines()):
        # 命令自带 here-string 终止符：无法嵌进 @'…'@，退回"直接执行 + 回填"（语法错略丑）
        return header + shell_cmd + "\n" + _PS_EPILOGUE_BODY + "\n" + _PS_EXIT_BODY + "\n"
    return (
        header
        + "$__src = @'\n" + shell_cmd + "\n'@\n"
        + "$__perr = $null\n"
        + "[void][System.Management.Automation.Language.Parser]::ParseInput("
          "$__src, [ref]$null, [ref]$__perr)\n"
        + "if ($__perr -and $__perr.Count) {\n"
        + "  $__e = $__perr[0]\n"
        + "  Write-Output ('ParserError: ' + $__e.Message"
          " + ' (第 ' + $__e.Extent.StartLineNumber + ' 行)')\n"
        + "  exit 1\n"
        + "}\n"
        + "$__full = $__src + \"`n\" + @'\n" + _PS_EPILOGUE_BODY + "\n'@\n"
        + "& ([scriptblock]::Create($__full))\n"
        + _PS_EXIT_BODY + "\n"
    )


def _ps_launch(shell_cmd):
    """启动 pwsh：(argv, 临时脚本路径或 None)。

    **不再用 `-EncodedCommand`**（原实现）：它会把错误流序列化成 CLIXML
    （`#< CLIXML <Objs…><S S="Error">_x001B_[31;1m…`），任何一次语法错/拼错参数
    都会让模型收到一坨 XML——它有真实会话为证：看到 CLIXML 后当场判定
    "PowerShell 不支持 <<EOF"，然后**显式改用 shell='cmd' 并此后再不碰 pwsh**
    （chat_20260812_175615.json 第 11→13 条、第 17→19 条）。

    改用 `-File`（脚本落 tmp/ 的临时 .ps1，UTF-8-BOM 让 5.1 也认中文）：报错是纯文本，
    语法错也照样可见（`ParserError: <file>:2`），引号/`$`/`%`/反斜杠路径全部原样，
    退出码正常传递。写不了临时文件时退回 `-Command <script>`（同样没有 CLIXML，
    代价是纯语法错只回 rc=1、不回文字）。
    """
    script = _ps_script_text(shell_cmd)
    try:
        tmp_dir = os.path.join(WORK_DIR, "tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="_pwsh_", suffix=".ps1", dir=tmp_dir)
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(script)
        return [PS_EXE, "-NoProfile", "-NonInteractive", "-File", path], path
    except Exception as e:  # 目录只读等：退回 -Command，功能不降级
        log_error("pwsh temp script", e)
        return [PS_EXE, "-NoProfile", "-NonInteractive", "-Command", script], None


def effective_shell(shell_cmd, shell="pwsh"):
    """(实际会用哪个 shell, cmd 语法提示或 None)。

    给 confirm 弹窗用：让用户在批准前就看到**真正**会跑的解释器。
    """
    if _is_ps_shell(shell) and not _PS_SYNTAX_MARKERS.search(shell_cmd):
        for pattern, label, hint in _CMD_ONLY_SYNTAX:
            if pattern.search(shell_cmd):
                return "cmd", (label, hint)
    return ("pwsh" if _is_ps_shell(shell) else "cmd"), None


def exec_shell_win_mock(shell_cmd, shell="pwsh", q=None):
    """Execute shell command with real-time output pushed to q (TUI live shell queue).

    shell='pwsh'（默认）→ PowerShell 7（没装则退回系统自带 5.1），UTF-8 输出；
    shell='cmd' → cmd.exe。默认之所以是 pwsh：cmd 是 1993 年的解析器（无对象管道、
    无 UTF-8、无 try/catch、%VAR% 展开），而模型写 cmd 语法时会被下面的兼容层接住并
    附上 pwsh 写法——反过来（默认 cmd、让模型自觉改用 pwsh）实测不会发生。
    """
    if q:
        q.put(("shell_start", shell_cmd))

    shell_used, cmd_hint = effective_shell(shell_cmd, shell)
    if cmd_hint:
        # 兼容层：用 cmd 跑，但把"这是什么语法 + pwsh 怎么写"回给模型
        payload = json.loads(exec_shell_win_mock(shell_cmd, shell="cmd", q=q))
        note = (f"[note] 命令被识别为 cmd 语法（{cmd_hint[0]}），已用 cmd.exe 执行；"
                f"pwsh 等价写法：{cmd_hint[1]}。若要固定用 cmd 请显式带 shell='cmd'。")
        payload["note"] = (note + " " + payload["note"]) if payload.get("note") else note
        payload["shell_used"] = "cmd"
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if _is_ps_shell(shell):
        argv, _tmp_script = _ps_launch(shell_cmd)
        # stderr 单独接管：与 stdout 合并时 PowerShell 会把错误流写成 CLIXML（见 _ps_launch）
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        if _tmp_script:
            _cleanup = lambda: os.path.exists(_tmp_script) and os.remove(_tmp_script)
        else:
            _cleanup = None
    else:
        _cleanup = None
        proc = subprocess.Popen(
            shell_cmd, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )

    # 读输出放后台线程 → queue；主循环短轮询，同时检查 Esc 与 120s 截止时间。
    # 原实现 for line in proc.stdout 阻塞 readline，静默挂死的命令绕开超时与中断。
    lines_q = queue.Queue()
    eof_needed = 0

    def _reader(stream):
        try:
            for line in stream:
                lines_q.put(line)
        except Exception as e:
            log_error("exec_shell reader", e)
        finally:
            lines_q.put(None)  # EOF 哨兵

    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        eof_needed += 1
        threading.Thread(target=_reader, args=(stream,), daemon=True).start()

    all_lines = []
    interrupted = False
    timed_out = False
    eofs = 0
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
        if line is None:  # 某条流 EOF；两条都收完才算结束
            eofs += 1
            if eofs >= eof_needed:
                break
            continue
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
    if _cleanup:  # 临时 .ps1 用完即删（不留垃圾在 tmp/）
        try:
            _cleanup()
        except Exception as e:
            log_error("pwsh temp script cleanup", e)

    stdout_text = "".join(all_lines)

    if timed_out and q:
        q.put(("shell_line", "\n--- TIMED OUT after 120s ---\n"))
    if interrupted and q:
        q.put(("shell_line", "\n--- [interrupted by user] ---\n"))
    if q:
        q.put(("shell_done", proc.returncode))

    payload = {
        "stdout": stdout_text,
        "stderr": "",
        "returncode": proc.returncode,
        "success": proc.returncode == 0,
        "interrupted": interrupted,
        "timed_out": timed_out,
        # 告诉模型"这次到底是谁跑的"：默认已是 pwsh，写 cmd 语法会被兼容层接住并注明
        "shell_used": "pwsh" if _is_ps_shell(shell) else "cmd",
    }
    # 管道陷阱（实测两次把 agent 卡在这上面）：
    # ① cmd.exe 的 returncode 取自管道**最后一个**命令，所以
    #    `python test_x.py 2>&1 | findstr /C:"FAIL"` 即使测试全失败也会返回 success=true
    #    （真实证据：三条 [FAIL] 配 "returncode": 0, "success": true）；
    # ② findstr 只显示匹配行 —— 汇总行("N 项失败"/"全部通过")不在 /C: 模式里就被滤掉，
    #    模型"看不全 → 再换模式过滤 → 还是看不全"，把工具轮次烧完还没拿到结论。
    if "|" in shell_cmd:
        notes = [_PIPE_NOTE]
        if "findstr" in shell_cmd.lower():
            notes.append(_FINDSTR_NOTE)
        payload["note"] = " ".join(notes)
    return json.dumps(payload, ensure_ascii=False, indent=2)


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
