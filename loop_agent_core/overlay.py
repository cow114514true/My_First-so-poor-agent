"""L2 overlay（TRACE_REPLAY_BUILD.md §8.1 / §8.2）：让回放里的 B 类工具真执行，但不碰真实工作区。

三条规矩：

1. **隔离**：B 类工具的入参路径先重写到 `tmp/overlay/<sid>/`（§8.1 目录副本），
   真执行只写这份副本；真实 `WORK_DIR` 的 mtime / sha256 必须一字不动。
2. **映射**：正向重写只发生在**执行用的那份副本**上，写进 trace 与会话历史的仍是逻辑路径；
   工具出参里的 overlay 路径在入 conv 之前换回逻辑路径（§8.2.2）。漏网的由 `$WORKSPACE`
   归一化吸收（`replay.replay_roots`）。
3. **跨类污染**（§8.2.3）：A 类读路径时先查 overlay，命中且 `read_from_overlay` 打开则
   返回 overlay 内容，否则吐录制结果。

关于"被触碰的路径"：不看现场而看 **cassette 里录制的入参**。现场的工具调用是模型刚刚
生成的（可能因为回放本身跑偏而完全不同），照着它拷会漏掉真正需要重放的路径；录制入参
才是"当时动过哪些文件"的唯一权威记录。这也是"目录副本"不必是整棵 WORK_DIR 的原因——
只拷被触碰的那些，副本才小、才可审。

不变量：`overlay_root` 必须落在真实 `WORK_DIR` **之内**，否则 `_map` 会把路径映射到
WORK_DIR 之外的兄弟目录（`os.path.join(root, "..", "x")`），隔离就漏了。构建时显式校验。
"""
from __future__ import annotations

import os
import shutil
import time

from .config import WORK_DIR
from .toolclass import TOOL_CLASS, UnclassifiedToolError

# overlay 根：跟 trace 一样放在仓库根的 tmp/ 下（`.gitignore:27` 的 `tmp/` 已覆盖）
DEFAULT_OVERLAY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp", "overlay")

# 文件工具与递归扫描工具的分类是"路径参数"而非"路径串"：把目标整体换到 overlay 根。
# 项目里 grep / glob / shell_cmd 三件事由 search_code / code_index / exec_shell_win 承担。
RECURSIVE_TOOLS = ("search_code", "code_index", "exec_shell_win")

# A 类里"路径在参数里、可以去 overlay 重读"的工具
READ_PATH_TOOLS = ("read_file", "count_tokens")


def overlay_root(session_id: str, *, root: str | None = None) -> str:
    return os.path.join(root or DEFAULT_OVERLAY_DIR, str(session_id or "session"))


def _under(path: str, parent: str) -> bool:
    p = os.path.abspath(path)
    q = os.path.abspath(parent)
    if p == q:
        return True
    return p.startswith(q + os.sep)


def _rel_in_workspace(path: str, workspace: str) -> str | None:
    """逻辑路径 → 相对 WORK_DIR 的路径；越界或写不成相对路径 → None。"""
    if not path:
        return None
    raw = str(path).strip().strip('"').strip("'")
    if not raw:
        return None
    try:
        resolved = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(workspace, raw))
    except (ValueError, OSError):
        return None
    if not _under(resolved, workspace):
        return None
    return os.path.relpath(resolved, workspace)


def _path_variants(root: str) -> tuple:
    """一个根路径的字符串变体（两类分隔符都有）：命令串里四种写法都得覆盖。"""
    variants = [root, root.replace("\\", "/")]
    return tuple(v for v in variants if v)


# ── 一次回放的 overlay ──────────────────────────────────────────────
class Overlay:
    """一次回放的 overlay 目录 + 路径映射。

    生命周期：`ReplaySession.__enter__` 建（惰性：第一次真要写才拷），`__exit__` 删。
    """

    def __init__(self, session_id: str, *, workspace: str | None = None,
                 root: str | None = None, read_from_overlay: bool = False,
                 keep: bool = False, delete_on_exit: bool = True):
        self.session_id = str(session_id or "session")
        self.workspace = os.path.abspath(workspace or WORK_DIR)
        self.root = os.path.abspath(overlay_root(self.session_id, root=root))
        if not _under(self.root, self.workspace):
            # 别把这条降级成 warning：一旦成立，"写到 overlay 里"就变成了"写到工作区之外"，
            # 隔离承诺当场失效，而现场看起来一切正常。
            raise ValueError(
                f"overlay 根必须落在 WORK_DIR 之内：overlay={self.root!r} WORK_DIR={self.workspace!r}")
        self.read_from_overlay = bool(read_from_overlay)
        self.keep = bool(keep)
        self.delete_on_exit = bool(delete_on_exit)
        self.prepared = False
        self.touched: list = []        # 录制中被触碰的逻辑路径（绝对路径）
        self.copied: list = []         # 实际拷进 overlay 的路径
        self.missing: list = []        # 录制时触碰过、但现场 WORK_DIR 里已不存在
        self.written: list = []        # 回放里被 B 类工具写过的逻辑路径（跨类污染的判据）
        self.live_calls: list = []     # 真执行过的工具调用（call_id, name）
        self.reads_from_overlay: list = []

    # ── 路径映射 ────────────────────────────────────────────────
    def to_overlay(self, path: str) -> str:
        """逻辑路径 → overlay 内路径（相对结构原样保留）。越界路径原样返回。

        幂等：已经落在 overlay 里的路径原样返回——重写会在两个地方各跑一次（A 类递归
        工具的目标根、以及被上层调用方预改写过的入参），不幂等的话第二次会把
        `tmp/overlay/<sid>` 自己当成"WORK_DIR 内的一个相对路径"再套一层。
        """
        resolved = os.path.abspath(str(path))
        if _under(resolved, self.root):
            return resolved
        rel = _rel_in_workspace(path, self.workspace)
        if rel is None:
            return path
        return os.path.normpath(os.path.join(self.root, rel))

    def _strip_prefix(self, path: str):
        """把 overlay 根或 WORK_DIR 前缀换成逻辑相对路径；不在其下则原样返回。

        两条前缀都要认，因为回放里两种写法会同时出现：B 类工具是在 overlay 里跑的
        （出参里是 overlay 绝对路径），但工具也可能把"入参那条逻辑路径"拼成 WORK_DIR
        绝对路径之后再回显。只认 overlay 那条，第二种就会在会话历史里留下一串本机
        绝对路径，与录制期存的对不上（实测就是这条让真录 fixture 的 `NoMatchInWindow`
        卡在第二个请求上）。

        `_path_variants` 的 `/` 变体只做纯文本替换，所以切完先用 `_under` 复核：
        否则 `/` 分隔的"恰好同前缀的别的目录"会被误切，切出来的相对路径还可能带 `..`。
        """
        p = str(path)
        for variant in _path_variants(self.root):
            if variant in p:
                rest = p.split(variant, 1)[1].lstrip("\\/")
                cand = os.path.abspath(os.path.join(self.root, rest))
                if _under(cand, self.root):
                    return os.path.relpath(cand, self.root)
        for variant in _path_variants(self.workspace):
            if variant in p:
                rest = p.split(variant, 1)[1].lstrip("\\/")
                if rest:
                    cand = os.path.abspath(os.path.join(self.workspace, rest))
                    if _under(cand, self.workspace):
                        return os.path.relpath(cand, self.workspace)
        return p

    def to_logical(self, path: str) -> str:
        """overlay 内路径 → 逻辑相对路径（反向映射，§8.2.2）。"""
        rel = self._strip_prefix(path)
        return rel if rel != str(path) else str(path)

    def reverse_text(self, text: str) -> str:
        """把一段文本里的 overlay 根换回真实 WORK_DIR（工具出参用）。

        换回的是 **WORK_DIR 绝对路径**，不是相对路径：录制期工具出参里本就是"入参那条
        逻辑相对路径被 `_resolve_path` 拼成 WORK_DIR 绝对路径"的样子。会话历史两边必须
        是同一形态，否则真录 fixture 的指纹在第二个请求上就配不上（实测 `NoMatchInWindow`）。

        `_path_variants` 的两个变体只在分隔符上不同，而 `\\` 那条会把 `/` 写法的前缀
        顺带吃掉一部分，所以**先换 `/` 再换 `\\`**。换完把文本里剩下的 `/` 归一成 WORK_DIR
        的原生分隔符——这里处理的是一段**路径文本**（出参里那一行），归一让会话历史里
        不出现 `C:\\...\\agent/tmp/a/b.py` 这种混合写法。
        """
        if not isinstance(text, str) or not text:
            return text
        out = text
        for variant in reversed(_path_variants(self.root)):  # 先 "/" 后 "\\"
            out = out.replace(variant, self.workspace)
        if "\\" in self.workspace:
            out = out.replace("/", "\\")
        return out

    # ── 建立副本（§8.1）─────────────────────────────────────────
    def prepare(self, cassette=None) -> "Overlay":
        """按录制入参把被触碰的路径从 WORK_DIR 拷成目录副本。幂等。"""
        if self.prepared:
            return self
        self.prepared = True
        os.makedirs(self.root, exist_ok=True)
        for path in touched_paths(cassette):
            self.touched.append(path)
            self._copy_in(path)
        return self

    def _copy_in(self, logical: str) -> None:
        rel = _rel_in_workspace(logical, self.workspace)
        if rel is None:  # WORK_DIR 之外的触碰路径不参与 overlay（工具本身也会拒绝）
            return
        src = os.path.join(self.workspace, rel)
        dst = os.path.join(self.root, rel)
        if os.path.isdir(src):
            if os.path.exists(dst):
                return
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.copytree(src, dst, copy_function=shutil.copy2)
            self.copied.append(src)
            return
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)  # copy2 保留权限/时间戳（§8.1）
            self.copied.append(src)
            return
        if os.path.exists(dst):
            return
        self.missing.append(logical)  # 录制时存在、现场已删：不造空文件，让工具自己报错

    # ── 正向重写（§8.2.1）───────────────────────────────────────
    def rewrite_args(self, name: str, args: dict) -> tuple:
        """把入参里的逻辑路径重写为 overlay 路径。返回 (新入参, 说明)。

        `structured`：`path_args` 里点名的参数逐参数重写。
        `best_effort`：整串按 WORK_DIR 子串替换，并前置一个 `Set-Location` / `cd /d`——
        相对路径的命令只有在 overlay 里起跑，才真的落在副本上。
        """
        try:
            entry = TOOL_CLASS.get(name)
        except Exception:  # noqa: BLE001
            entry = None
        if not entry:
            raise UnclassifiedToolError(f"工具未登记在 TOOL_CLASS，无法重写路径：{name!r}")
        scope = entry.get("rewrite_scope")
        if scope == "structured":
            out = dict(args)
            changed = []
            for key in entry.get("path_args") or ():
                if key in out and isinstance(out[key], str):
                    before = out[key]
                    out[key] = self.to_overlay(before)
                    if out[key] != before:
                        changed.append(key)
            return out, ("structured:" + ",".join(changed) if changed else None)
        if scope == "best_effort":
            out = dict(args)
            changed = []
            for key, val in list(out.items()):
                if isinstance(val, str) and self._text_has_workspace(val):
                    out[key] = self.rewrite_text(val)
                    changed.append(key)
            return out, ("best_effort:" + ",".join(changed) if changed else None)
        return dict(args), None

    def _text_has_workspace(self, text: str) -> bool:
        return any(v in text for v in _path_variants(self.workspace))

    def rewrite_text(self, text: str, *, shell: str = "pwsh") -> str:
        """命令串级替换：WORK_DIR 子串 → overlay 根，并让命令在 overlay 里起跑。"""
        out = text
        for variant in _path_variants(self.workspace):
            if variant in out:
                out = out.replace(variant, self.root)
        lead = (f"Set-Location -LiteralPath '{self.root}'; "
                if str(shell).lower() in ("pwsh", "powershell", "ps")
                else f'cd /d "{self.root}" && ')
        return lead + out

    def note_live_call(self, call_id, name: str, args: dict) -> None:
        """记下这次真执行的调用。

        类别的副作用是工具自己的事（`exec_shell_win` 写哪儿只有命令串知道），这里**只记
        B 类且参数里带路径的调用**——把 A 类读、C 类网络调用也标成"写过"会让跨类污染
        检查凭空命中，A 类结果被 overlay 重读覆盖，回放反而偏离录制。
        """
        self.live_calls.append((call_id, name))
        entry = TOOL_CLASS.get(name) or {}
        if entry.get("class") != "B" or entry.get("rewrite_scope") != "structured":
            return
        for key in entry.get("path_args") or ():
            val = (args or {}).get(key)
            if isinstance(val, str) and val:
                self.note_written(val)

    def note_written(self, logical: str) -> None:
        """把"写过"的逻辑路径记下来（跨类污染检查的判据）。"""
        rel = _rel_in_workspace(logical, self.workspace)
        if rel is None:
            return
        dst = os.path.normpath(os.path.join(self.root, rel))
        if dst not in self.written:
            self.written.append(dst)

    # ── 跨类污染（§8.2.3）──────────────────────────────────────
    def modified(self, logical: str) -> str | None:
        """A 类读的逻辑路径若已被本次回放的 B 类工具写过 → 返回 overlay 内的真实文件路径。"""
        rel = _rel_in_workspace(logical, self.workspace)
        if rel is None:
            return None
        dst = os.path.join(self.root, rel)
        if dst in self.written and os.path.exists(dst):
            return dst
        # 写的可能是目录（write_file 到目录内），逐级往上找
        cur = dst
        while True:
            parent = os.path.dirname(cur)
            if parent in (cur, self.root, ""):
                break
            if parent in self.written and os.path.isfile(cur):
                return cur
            cur = parent
        return None

    # ── 与路径沙箱的接缝 ────────────────────────────────────────
    def permits(self, path: str) -> bool:
        """这个绝对路径是否在本次 overlay 之内（供 `file_tools._resolve_path` 放行）。

        overlay 根落在 `WORK_DIR` 之内，但 `_resolve_path` 的越界判据是 `commonpath`：
        `tmp/overlay/<sid>/a.py` 的 commonpath 是 `<WORK_DIR>/tmp`，不等于 `<WORK_DIR>`，
        于是「重写到 overlay 的路径」会被自己的沙箱拒绝——回放里 B 类工具真执行的第一步
        就报 Access denied。这里把 overlay 加成一个**额外的合法根**，不放松其它任何路径。
        """
        try:
            resolved = os.path.abspath(path)
        except (ValueError, OSError):
            return False
        return _under(resolved, self.root)

    # ── 收尾 ────────────────────────────────────────────────────
    def cleanup(self, *, force: bool = False) -> bool:
        """删掉本次 overlay。中途的 overlay 没有保留价值（下一次回放会重放同样的写入）。"""
        if self.keep and not force:
            return False
        if not os.path.exists(self.root):
            return False
        self.delete_on_exit = True
        for attempt in range(3):
            try:
                shutil.rmtree(self.root)
                return True
            except OSError:
                time.sleep(0.1 * (attempt + 1))
        return False

    def describe(self) -> str:
        return (f"overlay={self.root} 触碰={len(self.touched)} 拷贝={len(self.copied)} "
                f"缺失={len(self.missing)} 真执行={len(self.live_calls)} "
                f"overlay 重读={len(self.reads_from_overlay)}")


# ── 录制入参 → 被触碰的路径 ─────────────────────────────────────────
def touched_paths(cassette, *, workspace: str | None = None) -> list:
    """cassette 里录制的工具入参 → 被触碰的逻辑路径（去重，保持录制顺序）。

    只认"路径参数"（`TOOL_CLASS[*].path_args`），不做命令串里的路径猜测：猜测会带来
    两类错误——漏拷（写了但没识别出来，工具在副本里报"文件不存在"）与多拷（把无关目录
    整棵拷进副本）。命令串那一路交给 `best_effort` 在**执行时**替换，副本按别的调用
    已经拷好的部分使用。
    """
    ws = os.path.abspath(workspace or WORK_DIR)
    out: list = []
    for call in _recorded_calls(cassette):
        name = call.get("tool_name") or call.get("name") or ""
        entry = TOOL_CLASS.get(name)
        if not entry:
            continue
        args = call.get("arguments")
        if not isinstance(args, dict):
            continue
        for key in entry.get("path_args") or ():
            val = args.get(key)
            if not isinstance(val, str) or not val.strip():
                continue
            rel = _rel_in_workspace(val, ws)
            if rel is None:
                continue
            logical = os.path.normpath(os.path.join(ws, rel))
            if logical not in out:
                out.append(logical)
    return out


def _recorded_calls(cassette) -> list:
    if cassette is None:
        return []
    try:
        return list(cassette.tool_calls())
    except Exception:  # noqa: BLE001 —— 没有被录制调用不算致命
        return []


# ── runner / replay 的钩子 ──────────────────────────────────────────
def active_overlay():
    """当前活动回放的 overlay（没有 overlay 时返回 None）。"""
    try:
        from . import replay
    except Exception:  # noqa: BLE001
        return None
    session = replay.active_session()
    return getattr(session, "overlay", None) if session is not None else None


def rewrite_for_replay(name: str, args: dict) -> tuple:
    """回放中把入参重写到 overlay（非回放 / 无 overlay → 原样返回）。

    返回 (入参, 说明或 None)；说明会进 `tool_call` 事件的 `rewrite_detail` 字段——
    现场只看到"结果对不上"时，这一列是唯一能指出"是重写没覆盖到"的证据。
    """
    ov = active_overlay()
    if ov is None:
        return dict(args), None
    return ov.rewrite_args(name, args)


def reverse_for_replay(result, *, tool_name: str = "") -> str:
    """回放中把工具出参里的 overlay 路径换回逻辑路径（§8.2.2）。"""
    text = result if isinstance(result, str) else str(result)
    ov = active_overlay()
    if ov is None:
        return text
    return ov.reverse_text(text)


def overlay_result_for(name: str, args: dict):
    """跨类污染检查：A 类读的路径若被本次回放的 B 类工具写过 → 返回 overlay 内路径。"""
    ov = active_overlay()
    if ov is None or not ov.read_from_overlay or name not in READ_PATH_TOOLS:
        return None
    path = (args or {}).get("path")
    if not isinstance(path, str):
        return None
    hit = ov.modified(path)
    if hit:
        ov.reads_from_overlay.append((name, path, hit))
    return hit


def dump_json(cassette=None) -> dict:
    """overlay 的一次性摘要，供回放报告 / CLI 打印。"""
    ov = active_overlay()
    if ov is None:
        return {}
    return {
        "session_id": ov.session_id,
        "root": ov.root,
        "workspace": ov.workspace,
        "touched": list(ov.touched),
        "copied": list(ov.copied),
        "missing": list(ov.missing),
        "live_calls": [{"call_id": c, "name": n} for c, n in ov.live_calls],
        "reads_from_overlay": [{"name": n, "logical": p} for n, p, _ in ov.reads_from_overlay],
        "read_from_overlay": ov.read_from_overlay,
    }
