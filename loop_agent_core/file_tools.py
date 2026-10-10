"""文件工具：路径沙箱、token 估算、代码大纲、函数体拉取、读写 mock。"""
import difflib
import os
import re
import shutil

from .config import WORK_DIR
from .tokens import _read_budget, estimate_tokens
from . import confirm


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


# 单次工具结果里 diff 段的字符上限：够看清一处改动的上下文，又不撑爆本地模型窗口。
_DIFF_BUDGET = 700


def _unified_diff(before, after, budget=_DIFF_BUDGET):
    """进程内 before→after 的 unified diff。

    相比 `git diff -- <path>` 的三个优势：
    - 未跟踪 / 刚新建 / 非 git 仓库的文件同样有 diff（git diff 对未跟踪文件返回空）；
    - 只含本次改动，不混入工作区里已有的其他未提交改动；
    - 截断点落在改动之后——git diff 的前缀截断会被无关 hunk 把真正的改动挤出窗口。
    超出 budget 时保留靠前的改动并注明省略了多少行/多少处改动，绝不静默丢内容。
    """
    if before == after:
        return ""
    body = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm=""))
    if len(body) >= 2 and body[0].startswith("---") and body[1].startswith("+++"):
        body = body[2:]  # 同一文件，这两行没有信息量
    if not body:
        return ""
    kept, used = [], 0
    for i, line in enumerate(body):
        cost = len(line) + 1
        if used + cost > budget and kept:
            rest = body[i:]
            changed = sum(1 for l in rest if l[:1] in "+-")
            kept.append(f"... [{len(rest)} more diff line(s), {changed} of them changed]")
            break
        kept.append(line)
        used += cost
    return "\n".join(kept)


def _diff_for(before, after):
    """构造 [diff for self-check] 段：同一文件写前→写后的对比（不依赖 git）。"""
    diff = _unified_diff(before, after)
    if not diff:
        return "\n[diff for self-check] no textual change (content identical to before)"
    return f"\n[diff for self-check]\n{diff}"


def _read_text_or_none(resolved):
    """尽力读为 UTF-8 文本；不可读（二进制/权限）返回 None，让调用方退化成"无 diff"而不是报错。"""
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _backup_before_write(resolved):
    """写前备份为 <file>.bak。返回 True 表示原本有文件（已备份）。"""
    if not os.path.exists(resolved):
        return False
    try:
        shutil.copy2(resolved, resolved + ".bak")
        return True
    except Exception:
        return False


def _cleanup_backup(resolved, restore=False):
    """restore=True（写失败）时从 .bak 恢复；随后无论成败删除 .bak。"""
    if restore:
        try:
            shutil.copy2(resolved + ".bak", resolved)
        except Exception:
            pass  # 恢复失败：主写操作已失败，.bak 保留供人工抢救，比覆盖成空文件好
    try:
        if os.path.exists(resolved + ".bak"):
            os.remove(resolved + ".bak")
    except Exception:
        pass  # 清理失败无害：.bak 留在原地，下次写会覆盖


def _suggest_files(path):
    """文件不存在时，列出同目录下名字相似的文件，帮模型纠错（如 HANDOFF → HANDOFF_NEXT_SESSION.md）。"""
    base = os.path.basename(path)
    stem = os.path.splitext(base)[0].lower()
    d = os.path.dirname(path) or "."
    if not os.path.isdir(d):
        return []
    out = []
    try:
        for name in os.listdir(d):
            low = name.lower()
            if low.startswith(stem) or stem in low:
                out.append(name)
    except OSError:
        return []
    return out[:8]


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
        sim = _suggest_files(path)
        msg = f"[Error] File not found: {resolved}"
        if sim:
            msg += f"\nSimilar files in the same directory: {sim}"
        return msg
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
        sim = _suggest_files(path)
        msg = f"[Error] File not found: {resolved}"
        if sim:
            msg += f"\nSimilar files in the same directory: {sim}"
        return msg
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return f"[Error] Cannot read '{resolved}' as UTF-8 text (binary file?)"
    n = len(content.splitlines())
    return f"{os.path.basename(resolved)}: {n} lines, ~{estimate_tokens(content)} tokens (estimated)"


def write_file_mock(path, content, purpose=""):
    """覆盖写入。写前备份（已有文件时），写失败自动恢复，返回附本次改动的 diff 供模型自检。"""
    resolved = _resolve_path(path)
    if not confirm.request("write_file", resolved, purpose):
        return "[Denied by user]"
    before_existed = os.path.exists(resolved)
    before = _read_text_or_none(resolved) if before_existed else None
    existed = _backup_before_write(resolved)
    os.makedirs(os.path.dirname(resolved), exist_ok=True)
    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        _cleanup_backup(resolved, restore=True)
        return f"[Error] write_file failed: {e}. Original file restored from backup."
    _cleanup_backup(resolved)  # 成功则只清理 .bak，不恢复
    size = os.path.getsize(resolved)
    msg = f"{'Written' if not existed else 'Overwritten'} {size} bytes to {resolved}"
    if not before_existed:
        msg += (f"\n[diff for self-check] new file: {len(content.splitlines())} line(s) "
                "written, nothing to diff against")
    elif before is None:
        msg += "\n[diff for self-check] previous content was not UTF-8 text — text diff unavailable"
    else:
        msg += _diff_for(before, content)
    return msg


def edit_file_mock(path, old_string, new_string="", replace_all=False, purpose=""):
    """精确替换 old_string。默认要求唯一（找不到/不唯一报错且不写入）；
    replace_all=True 时替换全部出现。写前备份，写失败自动恢复，返回附本次改动的 diff 供模型自检。"""
    resolved = _resolve_path(path)
    if not os.path.exists(resolved):
        sim = _suggest_files(path)
        msg = f"[Error] File not found: {resolved}"
        if sim:
            msg += f"\nSimilar files in the same directory: {sim}"
        return msg
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return f"[Error] Cannot read '{resolved}' as UTF-8 text (binary file?)"
    n = content.count(old_string)
    if n == 0:
        # 找不到：给出相近行候补，帮本地模型纠错（空白/缩进偏差常见）
        suggestions = []
        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if len(old_string) > 5 and (old_string[:5] in stripped or stripped[:5] in old_string):
                suggestions.append(f"  line {i}: {stripped[:60]}")
        hint = ""
        if suggestions:
            hint = "\nPossible close matches (check whitespace/indentation):\n" + "\n".join(suggestions[:5])
        return f"[Error] '{old_string[:40]}' not found in {os.path.basename(resolved)}.{hint}"
    if not confirm.request("edit_file", resolved, purpose):
        return "[Denied by user]"
    if not replace_all and n > 1:
        return (f"[Error] '{old_string[:40]}' appears {n} times in {os.path.basename(resolved)}. "
                "Set replace_all=True to replace every occurrence, or widen old_string to be unique.")
    backed_up = _backup_before_write(resolved)
    new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        _cleanup_backup(resolved, restore=True)
        return f"[Error] edit_file failed: {e}. Original file restored from backup."
    _cleanup_backup(resolved)
    msg = f"Edited {os.path.basename(resolved)}: {n} occurrence(s) replaced ({os.path.getsize(resolved)} bytes now)"
    msg += _diff_for(content, new_content)
    return msg
