"""文件工具：路径沙箱、token 估算、代码大纲、函数体拉取、读写 mock。"""
import os
import re

from .config import WORK_DIR
from .tokens import _read_budget, estimate_tokens


def _resolve_path(path):
    """解析路径，限定在 WORK_DIR 内。越界则抛异常。"""
    if os.path.isabs(path):
        resolved = os.path.abspath(path)
    else:
        resolved = os.path.abspath(os.path.join(WORK_DIR, path))
    # 规范化后检查是否在 WORK_DIR 内
    if os.path.commonpath([resolved, WORK_DIR]) != os.path.abspath(WORK_DIR):
        raise ValueError(f"Access denied: '{path}' resolves outside working directory '{WORK_DIR}'")
    return resolved


_CODE_OUTLINE_RE = re.compile(
    r"^[ \t]*(?:(async\s+def|def|class)\s+([A-Za-z_]\w*)|(import|from)\s+([\w.]+))",
    re.M,
)


def build_outline(content):
    """Structure outline: list of (kind, name, 1-based line) for defs/classes/imports."""
    out = []
    for m in _CODE_OUTLINE_RE.finditer(content):
        kind = (m.group(1) or m.group(3)).replace("async ", "").replace("from", "import")
        name = m.group(2) or m.group(4)
        line = content.count("\n", 0, m.start()) + 1
        out.append((kind, name, line))
    return out


def extract_function(content, name):
    """Return (body, start_line, end_line) for the first def/class named `name`.
    Body ends at the next same-or-less-indented statement (decorators skipped); trailing blanks trimmed."""
    m = re.search(r"^([ \t]*)(async\s+def|def|class)\s+" + re.escape(name) + r"\b", content, re.M)
    if not m:
        return None
    start_indent = len(m.group(1))
    lines = content.splitlines()
    start_lineno = content.count("\n", 0, m.start())  # 0-based
    end_lineno = len(lines)
    for i in range(start_lineno + 1, len(lines)):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        if indent <= start_indent and not stripped.startswith("@"):
            end_lineno = i
            break
    while end_lineno > start_lineno:  # trim trailing blank/comment lines
        s = lines[end_lineno - 1].strip()
        if s and not s.startswith("#"):
            break
        end_lineno -= 1
    return "\n".join(lines[start_lineno:end_lineno]), start_lineno + 1, end_lineno


_read_cache = {}


def _cached_read_info(resolved, content):
    key = (resolved, os.path.getmtime(resolved))
    info = _read_cache.get(key)
    if info is None:
        info = {"tokens": estimate_tokens(content), "outline": build_outline(content)}
        if len(_read_cache) > 200:
            _read_cache.clear()
        _read_cache[key] = info
    return info


def _cap_to_budget(slice_lines, budget, label):
    """Return the slice in full if it fits the token budget, else a head truncated to within budget."""
    body = "\n".join(slice_lines)
    if estimate_tokens(body) <= budget:
        return f"--- {label} ---\n{body}"
    lo, hi = 0, len(slice_lines)  # largest prefix within budget (binary search)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens("\n".join(slice_lines[:mid])) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return (f"[Large slice] {label} exceeds ~{budget}-token budget ({len(slice_lines)} lines total). "
            f"Showing first {lo} lines.\n---\n{chr(10).join(slice_lines[:lo])}\n---\n"
            f"Narrow the range, or read in chunks with start_line/end_line.")


def read_file_mock(path, function=None, start_line=None, end_line=None):
    """Read a file. Large files auto-return an outline; fetch parts via function= or a line range."""
    resolved = _resolve_path(path)
    if not os.path.exists(resolved):
        return f"[Error] File not found: {resolved}"
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return f"[Error] Cannot read '{resolved}' as UTF-8 text (binary file?)"

    total_lines = len(content.splitlines())

    # 1. pull a named function/class body (token-capped like ranges)
    if function:
        got = extract_function(content, function)
        if got is None:
            return (f"[Error] No function/class named '{function}' found in {resolved}. "
                    "Call read_file (no params) to see the outline, or use start_line/end_line.")
        body, s, e = got
        return _cap_to_budget(body.splitlines(), _read_budget(),
                              f"{os.path.basename(resolved)}:{function} (lines {s}-{e})")

    # 2. explicit line range (token-capped; start_line=1, end_line=total = whole file)
    if start_line is not None or end_line is not None:
        s = start_line if start_line is not None else 1
        e = end_line if end_line is not None else total_lines
        s, e = max(1, s), min(total_lines, e)
        if s > total_lines:
            return f"[Error] start_line {s} beyond file length ({total_lines} lines)"
        slice_lines = content.splitlines()[s - 1:e]
        return _cap_to_budget(slice_lines, _read_budget(),
                              f"{os.path.basename(resolved)} (lines {s}-{e})")

    # 3. full read, gated by the token budget
    info = _cached_read_info(resolved, content)
    budget = _read_budget()
    if info["tokens"] <= budget:
        return content

    header = (f"[Large file] {os.path.basename(resolved)}: {total_lines} lines, ~{info['tokens']} tokens "
              f"(budget {budget}).\n")
    if info["outline"]:
        lines = [header, "Outline (read_file with function=<name> pulls a body; start_line/end_line reads a range):"]
        for kind, name, ln in info["outline"]:
            lines.append(f"  {ln:>5}  {kind} {name}")
        lines.append("Whole file: read_file(path, start_line=1, end_line=<total>).")
        return "\n".join(lines)
    # non-code file: head preview
    preview = content[:2000]
    return (f"{header}Non-code file — first ~2000 chars (1-{total_lines} lines available):\n---\n"
            f"{preview}\n---\nRead further with read_file(path, start_line=N, end_line=M).")


def count_tokens_mock(path=None, text=None):
    if (path is None) == (text is None):
        return "[Error] Provide exactly one of 'path' or 'text'."
    if text is not None:
        return f"~{estimate_tokens(text)} tokens (estimated)"
    resolved = _resolve_path(path)
    if not os.path.exists(resolved):
        return f"[Error] File not found: {resolved}"
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return f"[Error] Cannot read '{resolved}' as UTF-8 text (binary file?)"
    n = len(content.splitlines())
    return f"{os.path.basename(resolved)}: {n} lines, ~{estimate_tokens(content)} tokens (estimated)"


def write_file_mock(path, content):
    resolved = _resolve_path(path)
    os.makedirs(os.path.dirname(resolved), exist_ok=True)
    with open(resolved, "w", encoding="utf-8") as f:
        f.write(content)
    size = os.path.getsize(resolved)
    return f"Written {size} bytes to {resolved}"


def edit_file_mock(path, old_string, new_string="", replace_all=False):
    """精确替换 old_string。默认要求唯一（找不到/不唯一报错且不写入）；
    replace_all=True 时替换全部出现。"""
    resolved = _resolve_path(path)
    if not os.path.exists(resolved):
        return f"[Error] File not found: {resolved}"
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return f"[Error] Cannot read '{resolved}' as UTF-8 text (binary file?)"
    n = content.count(old_string)
    if n == 0:
        return f"[Error] '{old_string[:40]}' not found in {os.path.basename(resolved)}"
    if not replace_all and n > 1:
        return (f"[Error] '{old_string[:40]}' appears {n} times in {os.path.basename(resolved)}. "
                "Set replace_all=True to replace every occurrence, or widen old_string to be unique.")
    new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
    with open(resolved, "w", encoding="utf-8") as f:
        f.write(new_content)
    return f"Edited {os.path.basename(resolved)}: {n} occurrence(s) replaced ({os.path.getsize(resolved)} bytes now)"
