"""
TUI for loop_agent_v2.py — three-panel chat interface powered by Textual.
Run: python tui.py
"""

import queue
import threading
import json
import os
import re
import time
import atexit
from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import RichLog, TextArea, Static, ProgressBar, Button
from textual.widget import Widget
from textual.geometry import Size
from textual.strip import Strip
from textual.binding import Binding
from rich.cells import cell_len
from rich.markup import escape
from rich.markdown import Markdown as RichMarkdown
from rich.segment import Segment

from loop_agent_v2 import chat, messages as _agent_messages
from loop_agent_v2 import CURRENT_MODEL
import loop_agent_v2
import loop_agent_core.config as _cfg


# ─────────────────── 会话文件：识别 + 校验（BUG 31 的防线） ───────────────────

def is_conversation_file(name: str) -> bool:
    """是否是**会话**文件（排除折叠档案 sidecar 与 autosave）。

    坑：sidecar 名是 `<会话>.json.archive.json`，它**同样**满足
    `startswith("chat_") and endswith(".json")` —— 用这个口径列目录会把 sidecar
    当成会话（实测：用户 `/load 1` 加载了 sidecar，`messages` 被换成折叠档案条目，
    此后每次请求都 `422 messages[0]: missing field role`，保存出来的历史也是坏的）。
    """
    return (name.startswith("chat_") and name.endswith(".json")
            and not name.endswith(".archive.json"))


def read_conversation(path: str):
    """读取并**校验**会话文件，返回 `(messages, error)`；校验不过绝不返回半份数据。

    校验必须在动 `_agent_messages` **之前**做：加载失败时当前会话必须原封不动。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return None, f"读取失败：{e}"
    problems = loop_agent_v2.conversation_problems(data)
    if problems:
        return None, "不是有效会话（" + "；".join(problems) + "）"
    return data, ""


class QuitDialog(Screen):
    """Modal: save conversation before exit?"""

    BINDINGS = [
        Binding("y", "save_quit", "Save & Quit"),
        Binding("n", "discard_quit", "Discard"),
        Binding("escape", "cancel_quit", "Cancel"),
    ]

    CSS = """
    QuitDialog {
        align: center middle;
    }

    #quit-dialog {
        padding: 1 2;
        border: solid $primary;
        background: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(
            "Save conversation before exit?\n\n"
            "  [bold]y[/bold] — save to conversations/ and quit\n"
            "  [bold]n[/bold] — discard and quit\n"
            "  [bold]Esc[/bold] — cancel",
            id="quit-dialog",
        )

    def action_save_quit(self) -> None:
        self.dismiss("save")

    def action_discard_quit(self) -> None:
        self.dismiss("discard")

    def action_cancel_quit(self) -> None:
        self.dismiss("cancel")


class ConfirmDialog(Screen):
    """Modal: allow/deny a sensitive tool action.

    覆盖：命令执行 / 写文件 / 改文件 / 读图 / 上传文件。
    注意 target 会被完整显示 —— 命令执行的确认必须让用户看到**将执行的命令原文**，
    否则确认框只是形式（此前 exec_shell_win 甚至完全不经过这里，见 SECURITY_AUDIT_REPORT V2）。
    """

    _KIND_LABEL = {
        "exec_shell_win": "执行命令",
        "write_file": "写入文件",
        "edit_file": "修改文件",
        "analyze_image": "读取图片",
        "use_ds_from_web(upload)": "上传文件到 DeepSeek 网页版",
    }

    def __init__(self, kind: str, target: str, is_worker: bool = False, purpose: str = "") -> None:
        super().__init__()
        self._kind = kind
        self._target = target
        self._is_worker = is_worker
        self._purpose = (purpose or "").strip()

    CSS = """
    ConfirmDialog {
        align: center middle;
    }
    #confirm-dialog {
        padding: 1 2;
        border: solid $primary;
        background: $surface;
        max-width: 90;
        min-width: 40;
    }
    """

    def compose(self) -> ComposeResult:
        title = "⚠ 子任务请求" if self._is_worker else "⚠ 确认操作"
        label = self._KIND_LABEL.get(self._kind, self._kind)
        # 目的必须显示：审批的意义就在于"知道要干什么"，只有命令原文等于盲批。
        # 模型没给目的不当成可省略——显式标红"未说明"，本身就是给审批人的一个信号。
        purpose_line = (
            f"[bold]目的[/bold] {escape(self._purpose)}"
            if self._purpose
            else "[bold red]目的：未说明[/bold red] [dim]（模型未提供操作目的，请据命令自行判断风险）[/dim]"
        )
        with Vertical(id="confirm-dialog"):
            yield Static(
                f"[bold]{title}[/bold]\n\n[bold yellow]{escape(label)}[/bold yellow] · {purpose_line}\n{escape(self._target)}",
                markup=True,
            )
            with Horizontal():
                # 顺序即语义：首焦点是"拒绝"，回车=拒绝（fail-closed）。
                # 此前"允许"在首位，用户习惯性回车就会批准敏感操作（SECURITY_AUDIT_REPORT V11）。
                yield Button("拒绝", id="deny", variant="error")
                yield Button("允许", id="allow", variant="primary")
                yield Button("本会话放行此类", id="allow_session")
            yield Static(
                "[dim]回车 = 拒绝 · Esc = 拒绝 · 「本会话放行此类」仅对当前类别生效，可用 /revoke 撤销[/dim]",
                markup=True,
            )

    def on_button_pressed(self, event) -> None:
        self.dismiss(event.button.id)

    def action_cancel_quit(self) -> None:
        self.dismiss("cancel")


class RecoveryDialog(Screen):
    """Modal: autosave found on startup — restore?"""

    BINDINGS = [
        Binding("y", "restore", "Restore"),
        Binding("n", "discard", "Discard"),
        Binding("escape", "cancel", "Cancel"),
    ]

    CSS = """
    RecoveryDialog {
        align: center middle;
    }
    #recovery-dialog {
        padding: 1 2;
        border: solid $warning;
        background: $surface;
        max-width: 70;
    }
    """

    def __init__(self, msg_count: int, first_msg: str) -> None:
        super().__init__()
        self._msg_count = msg_count
        self._first_msg = first_msg

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold yellow]Found autosaved conversation[/bold yellow]\n\n"
            f"Messages: {self._msg_count}\n"
            f"First: \"{escape(self._first_msg)}\"\n\n"
            f"  [bold]y[/bold] — restore\n"
            f"  [bold]n[/bold] — discard\n"
            f"  [bold]Esc[/bold] — decide later",
            id="recovery-dialog",
        )

    def action_restore(self) -> None:
        self.dismiss("restore")

    def action_discard(self) -> None:
        self.dismiss("discard")

    def action_cancel(self) -> None:
        self.dismiss("cancel")


# 中文排版禁则。
#
# Rich 的 divide_line 按空白分词，一整句没有空格的中文会被当成一个"词"：放不进
# 当前行剩余空间就整句下移（那一行留白一大片），超过 width 就用 chop_cells 按
# cell 硬切——切点完全不管标点该不该留在行尾，于是 "，" "。" 经常掉到下一行行首。
# 面板宽度是奇数时最严重：一行 22 个中文字刚好 44 cell，剩 1 cell，而中文标点占
# 2 cell，几乎每个整行的第一个收尾标点都会被挤到下一行。
_CLOSERS = frozenset("，。、；：！？）】》」』〉·…—,.!?;:)]}")
_OPENERS = frozenset("（【《「『〈([{")


def _break_tokens(text: str) -> list:
    """把文本切成折行原子。

    每个 CJK 字符、每个收尾标点各自成原子（中文本来就可以在任意字之间换行）；
    ASCII 串（标识符、单词、URL）整体不可断，只在空白处断开。
    """
    out = []
    buf = ""
    for ch in text:
        if ch == "\n" or ch in " \t" or ord(ch) > 0x2E7F or ch in _CLOSERS:
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


def wrap_cjk(text: str, width: int) -> list:
    """面向中文的折行：按 cell 宽度贪心填行，并遵守中文禁则。

    刻意不用 Rich 的 divide_line：它把整句中文当一个词，中英混排实测填充率只有
    82%，本函数同样输入是 98%。行首禁则在这里是"选断点"的一部分（换行点往前挪），
    不是事后挪字，所以字符顺序永远不变——把标点拉回上一行行尾虽然也能消除行首
    标点，但连读时标点会跑到错误的位置（"…方式。嗯，…" 会变成 "…方式。，" + "嗯…"）。
    """
    if width < 2 or not text:
        return [text] if text else []
    lines: list = []
    cur = ""
    cur_cells = 0
    for tok in _break_tokens(text):
        if tok == "\n":
            lines.append(cur)
            cur, cur_cells = "", 0
            continue
        tw = cell_len(tok)
        if tw > width:
            # 超长 ASCII 串（URL / base64 / 长标识符）只能硬折
            if cur:
                lines.append(cur)
                cur, cur_cells = "", 0
            while cell_len(tok) > width:
                cut = 0
                acc = 0
                for ch in tok:
                    cw = cell_len(ch)
                    if acc + cw > width:
                        break
                    acc += cw
                    cut += 1
                cut = cut or 1
                lines.append(tok[:cut])
                tok = tok[cut:]
            cur, cur_cells = tok, cell_len(tok)
            continue
        if cur and cur_cells + tw > width:
            if tok in _CLOSERS:
                # 行首禁则：换行点往前挪，直到新行不以收尾标点开头、且不超宽。
                # 两个条件必须同时满足——只图消除行首标点而让行超宽的话，RichLog
                # 会再折一次，版面更乱。
                carry, rest, guard = "", cur, 0
                while rest and guard < 8:
                    guard += 1
                    carry = rest[-1] + carry
                    rest = rest[:-1]
                    if carry[0] not in _CLOSERS and cell_len(carry) + tw <= width:
                        break
                if (rest and carry and carry[0] not in _CLOSERS
                        and cell_len(carry) + tw <= width):
                    lines.append(rest)
                    cur, cur_cells = carry, cell_len(carry)
                else:
                    # 数学上挪不动了（比如整行都是标点）：只能让标点留在行首
                    lines.append(cur)
                    cur, cur_cells = "", 0
            else:
                lines.append(cur)
                cur, cur_cells = "", 0
        cur += tok
        cur_cells += tw
    if cur or not lines:
        lines.append(cur)

    # 行尾禁则：开引号 / 开括号不能留在行尾，推到下一行去跟它的内容待在一起。
    # 下一行装不下就保持原样，绝不制造超宽行。
    for i in range(len(lines) - 1):
        if (len(lines[i]) > 1 and lines[i][-1] in _OPENERS
                and cell_len(lines[i + 1]) + cell_len(lines[i][-1]) <= width):
            lines[i + 1] = lines[i][-1] + lines[i + 1]
            lines[i] = lines[i][:-1]
    return lines


class ThinkBlock:
    """思考面板里的一个块 = 一轮思考，可折叠。

    块并不另存正文：它只是 _history 里的一条 ("h", block) 标记，正文仍按原来的方式
    平铺在标记之后。这样流式折行、残尾缓冲、重排、裁尾（_tail_history）这些已经调好
    的逻辑一行都不用改，块只负责"标题 + 折叠状态 + 统计"。

    has_header=False 的块（会话/清空后的第一块）不打标题行，与旧版"第一轮不打
    分隔线"的表现一致。
    """

    __slots__ = ("key", "label", "has_header", "collapsed", "chars",
                 "tools", "tail", "started", "duration")

    _TAIL_KEEP = 400  # 折叠预览只看正文尾部这么多字符

    def __init__(self, key, label: str, has_header: bool = True) -> None:
        self.key = key                  # (问题序号, 轮次)：轮次重号时也不会串块
        self.label = label              # 标题里显示的轮次，如 "Round 3"
        self.has_header = has_header
        self.collapsed = False
        self.chars = 0
        self.tools: list = []
        self.tail = ""
        self.started = time.monotonic()
        self.duration = 0.0

    def add_text(self, raw: str) -> None:
        self.chars += len(raw)
        self.tail = (self.tail + raw)[-self._TAIL_KEEP:]

    def add_tool(self, name: str) -> None:
        if name not in self.tools:
            self.tools.append(name)

    def close(self) -> None:
        self.duration = time.monotonic() - self.started


class PanelLog(RichLog):
    """RichLog with double-click detection for panel expansion + reflow on resize."""

    _MAX_HISTORY = 1000  # reflow 重播上限：防止长对话下 _history 无界增长拖垮 resize
    _REFLOW_DEBOUNCE = 0.12  # resize 防抖秒数：合并拖拽窗口时的连续 resize
    _MIN_RENDER_WIDTH = 4  # 窄于这个宽度就当作不可见：不折行、不上屏，等变宽再重排
    _STASH_CHARS = 4096  # 不可见时攒够这么多字就并进重播记录（str += 是 O(n)，不能无限攒）

    # 控件级绑定：**只有本面板拿到焦点时才生效**（Textual 会沿 MRO 合并，RichLog 的
    # 滚动键位不受影响）。这正是"按块操作"该有的作用域——输入框里打字不会误触发。
    BINDINGS = [
        Binding("j", "cursor_next_block", "下一块", show=False),
        Binding("k", "cursor_prev_block", "上一块", show=False),
        Binding("enter", "toggle_cursor_block", "展开/折叠", show=False),
        Binding("space", "toggle_cursor_block", "", show=False),
    ]

    class ExpandRequest(Message):
        """Posted when panel is double-clicked."""
        def __init__(self, panel_id: str) -> None:
            self.panel_id = panel_id
            super().__init__()

    def __init__(self, *args, panel_id: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.panel_id = panel_id
        self._last_click: float = 0.0
        # 重播记录：("r", renderable) 原样重播；("t", [文本片…]) 是流式文本，
        # 重排时把同一块的碎片重新接成一整段再折行——否则展开后只能各自重排，
        # 跨旧断点的碎行永远合不回长行。
        self._history: list = []
        self._pending_text: str = ""       # 已到达但还凑不满一整行的残尾
        self._pending_since: float = 0.0   # 残尾开始等待的时刻（超时兜底用）
        self._block_emitted: bool = False  # 当前文本块是否已经上过屏
        self._last_width: int = 0
        self._reflow_timer = None
        # 块模型：块头以 ("h", ThinkBlock) 标记项混在 _history 里，正文照旧平铺其后。
        self._blocks: list = []
        self._cur_block = None  # 正在写入的块（流式文本都记在它身上）
        # 块光标：j/k 移动、Enter 展开/折叠；鼠标单击标题同效（见 on_click）。
        # 光标块标题行反白，滚动位置自动跟随（_scroll_to_cursor）。
        self._cursor: int = -1
        self._block_line: dict = {}  # 块 key → 在 self.lines 里的行号，reflow 时重建
        self._last_title_click: float = 0.0

    @staticmethod
    def _safe_strip(text):
        """尽力移除 Rich markup 标签，保留可见文本。"""
        return re.sub(r"\[/?[a-zA-Z#][^\]]*\]", "", str(text))

    # -- 流式纯文本 --

    @property
    def has_pending_text(self) -> bool:
        return bool(self._pending_text)

    @property
    def pending_age(self) -> float:
        """残尾已经等了多久（秒）。"""
        return time.monotonic() - self._pending_since

    @property
    def text_started(self) -> bool:
        """当前文本块是否已经上过屏。

        用来区分两种兜底：还没上过屏 → 首字延迟，阈值要短；已经上过屏 → 只是流
        暂时中断，阈值要长，否则慢速模型会被切成一堆填不满的碎行。
        """
        return self._block_emitted

    def _text_width(self) -> int:
        """当前可用于折行的宽度；面板被挤到 0 宽（不可渲染）时返回 0。

        展开别的面板时本面板宽度会被设成 "0"，content_region.width 也随之变成 0。
        旧实现此时回落到 max(2, size.width) = 2，于是按 2 cell 折行：2 万字直接
        折成 5000 行、重排耗时从 45ms 涨到 800ms，而且一行都看不见。0 宽就是没有
        可渲染宽度，调用方必须按"不可见"处理（见 _append_text / write / reflow）。
        """
        try:
            w = self.content_region.width
        except Exception:
            w = 0
        if w >= self._MIN_RENDER_WIDTH:
            return w
        try:
            w = self.size.width
        except Exception:
            w = 0
        return w if w >= self._MIN_RENDER_WIDTH else 0

    def stream_text(self, chunk: str) -> None:
        """流式写入纯文本（调用方需自行 escape）。

        逐片直接 write() 时每一片都会独立折行：片尾凑不满整行就留下空白，下一片
        又从新的一行开始，面板于是大片留白。改成自己折行、只把"已折满的整行"上屏，
        残尾留在缓冲区里等后续字符补齐，这样除了最后一个残尾，每一行都是满行。
        """
        if not chunk:
            return
        for i, part in enumerate(chunk.split("\n")):
            if i:
                self.flush_text()  # 硬换行：残尾也得收尾
            self._append_text(part)

    def _append_text(self, part: str) -> None:
        if not part:
            return
        if not (self._history and self._history[-1][0] == "t"):
            self._block_emitted = False  # 上一块已经结束，这是新的一块
        if not self._pending_text:
            self._pending_since = time.monotonic()
        self._pending_text += part
        width = self._text_width()
        if not width:
            # 不可见：折了也没人看，等重新变宽时整体重排。攒到一定长度就并进重播
            # 记录，避免 _pending_text 随隐藏时长无限增长（str += 是 O(n)）。
            if len(self._pending_text) >= self._STASH_CHARS:
                self._stash_pending()
            return
        lines = wrap_cjk(self._pending_text, width)
        if len(lines) < 2:
            return
        self._pending_text = lines[-1]
        self._emit_lines(lines[:-1])

    def _stash_pending(self) -> None:
        """把残尾并进重播记录但不上屏（面板不可见时用，重排后内容会回来）。"""
        text, self._pending_text = self._pending_text, ""
        self._pending_since = time.monotonic()
        self._record_text(text)

    def _record_text(self, raw: str) -> None:
        """把一段原文续进当前文本块的重播记录（存连续原文，重排时才能跨断点合并）。"""
        if not raw:
            return
        if self._history and self._history[-1][0] == "t":
            self._history[-1][1].append(raw)
        else:
            self._history.append(("t", [raw]))
            self._trim_history()
        if self._cur_block is not None:
            self._cur_block.add_text(raw)  # 块只统计字数/留一段尾部（折叠预览用）

    def flush_text(self) -> None:
        """把残尾也上屏（句末 / 硬换行 / 超时兜底 / 轮次收尾时调用）。"""
        if not self._pending_text:
            return
        if not self._text_width():
            self._stash_pending()
            return
        tail, self._pending_text = self._pending_text, ""
        self._pending_since = time.monotonic()
        self._emit_lines([tail])

    def _emit_lines(self, lines: list) -> None:
        """上屏若干完整行，并把原始文本续写到当前文本块的重播记录。

        记录里存的是**去掉换行**的连续原文：重排时重新折行才能跨旧断点合并。
        """
        if not lines:
            return
        width = self._text_width()
        if not width:
            self._stash_lines(lines)
            return
        body = "\n".join(lines)
        raw = "".join(lines)
        self._record_text(raw)
        self._block_emitted = True
        self._pending_since = time.monotonic()
        try:
            super().write(body, width=width)
        except Exception:
            try:
                super().write(self._safe_strip(body), width=width)
            except Exception:
                super().write("[dim](output omitted -- rendering error)[/dim]", width=width)

    def _stash_lines(self, lines: list) -> None:
        """本来要上屏的整行，因为面板不可见改为只记历史。"""
        self._record_text("".join(lines))
        self._block_emitted = True
        self._pending_since = time.monotonic()

    # -- 其他写入 --

    def write(self, content, *args, **kwargs):
        self.flush_text()  # 保证顺序：残尾先上屏，再写这一条
        self._history.append(("r", content))
        self._trim_history()
        if not self._text_width():
            return self  # 不可见：只记历史，重新可见时由 reflow 一次性补上
        try:
            return super().write(content, *args, **kwargs)
        except Exception:
            # MarkupError 或任何渲染异常：剥离 markup 按纯文本写，绝不崩掉整个 TUI
            try:
                return super().write(self._safe_strip(content), *args, **kwargs)
            except Exception:
                return super().write("[dim](output omitted -- rendering error)[/dim]", *args, **kwargs)

    def _trim_history(self) -> None:
        if len(self._history) > self._MAX_HISTORY:
            del self._history[:-self._MAX_HISTORY]

    def write_wrapped(self, content) -> None:
        """按面板内容宽度写入，绕开 RichLog.min_width=78 的钳制。

        直接 write() 时 RichLog 会把渲染宽度抬到 min_width（默认 78），于是文字
        永远按 78 列折行——面板只有 45 列时会溢出、展开变宽也不会重排。
        """
        width = self._text_width()
        if width:
            self.write(content, width=width)
        else:
            self.write(content)  # 不可见：write 内部只记历史，不上屏

    def clear(self):
        self._history.clear()
        self._blocks.clear()
        self._cur_block = None
        self._pending_text = ""
        self._pending_since = time.monotonic()
        self._block_emitted = False
        return super().clear()

    def on_resize(self, event) -> None:
        w = event.size.width
        if w == self._last_width:
            return
        self._last_width = w
        self._cancel_reflow()
        if w < self._MIN_RENDER_WIDTH:
            # 被别的面板挤成 0 宽：这时候重排纯属白干（还把内容按 2 cell 折成几千行）。
            # 重新变宽时还会有一次 resize，那次再排。
            return
        # 防抖：拖拽窗口会连续触发 resize，每次都重播 _history（1000 条约 132ms）。
        # 合并成一次重排，避免拖拽时卡成幻灯片。
        self._reflow_timer = self.set_timer(self._REFLOW_DEBOUNCE, self._run_reflow)

    def _cancel_reflow(self) -> None:
        timer = getattr(self, "_reflow_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
            self._reflow_timer = None

    def _run_reflow(self) -> None:
        self._reflow_timer = None
        self.reflow()

    def _tail_history(self, width: int) -> list:
        """只取"还能上屏"的那段历史（面板最多显示 max_lines 行）。

        _history 是会长到很大的：一个长会话几百轮思考能攒到上百万字，而 max_lines
        只有 5000 行，更早的内容折出来立刻就被裁掉。实测 150 万字历史收起面板时
        重排要 4035ms，只折尾部后与历史长度无关（57ms）。留 1.25 倍余量：折行只会
        让每行更短，所以 cells/width 就是行数的下界，1.25 倍保证裁到 max_lines 之前
        不会缺行。只有文本块（"t"）参与这个预算——"r" 条目各占一行，截掉它们反而
        会把本该看得见的行删掉。
        """
        max_lines = self.max_lines
        if not max_lines:
            return list(self._history)
        target = int(max_lines * width * 1.25)
        hist = self._history
        tail: list = []
        cells = 0
        start = 0
        for i in range(len(hist) - 1, -1, -1):
            kind, payload = hist[i]
            if kind == "t":
                keep: list = []
                for chunk in reversed(payload):
                    keep.append(chunk)
                    cells += cell_len(chunk)
                    if cells >= target:
                        break
                keep.reverse()
                tail.append((kind, keep))
            else:
                tail.append((kind, payload))
            if cells >= target:
                start = i
                break
        tail.reverse()
        # 若窗口从某个块体中间开始，往前补上它的块头标记：否则重排时不知道这个块是
        # 折叠的，会把已经折起的内容又整段铺出来。
        if tail and tail[0][0] != "h":
            j = start
            while j > 0 and hist[j][0] != "h":
                j -= 1
            if hist[j][0] == "h":
                tail.insert(0, hist[j])
        return tail

    def reflow(self) -> None:
        """按当前面板宽度重排所有已上屏的内容（RichLog 的行宽一旦渲染就固定，只能整体重建）。

        必须显式传 width：RichLog.write 会把渲染宽度钳制到 min_width（默认 78），
        不传的话文字永远按 78 列折行，面板变宽也不会重排。
        文本块会先接回一整段再折行，这样展开变宽后可以跨旧断点合并成长行。

        性能：旧实现是"每个折行结果各调一次 RichLog.write"。write 每次都要跑一遍
        Rich 渲染、建 Strip、写 virtual_size（reactive），2251 行要 42ms；同样这些行
        自己建 Strip 只要 6ms。所以折行结果在这里直接铺成 Strip，只有真正的 markup
        条目（轮次分隔线等）才走 RichLog.write。
        """
        self._cancel_reflow()
        self.flush_text()  # 残尾先并进文本块，否则重排后会短暂消失
        width = self._text_width()
        if not width:
            return  # 不可见：等重新变宽时再排
        y = self.scroll_offset.y
        super().clear()
        chunks: list = []
        pending: list = []

        def flush_pending() -> None:
            """把攒下的文本行直接铺成 Strip（等价于 RichLog.write 的结果）。"""
            if not pending:
                return
            self.lines.extend([Strip([Segment(line)]) if line else Strip([])
                               for line in pending])
            pending.clear()
            if self.max_lines is not None and len(self.lines) > self.max_lines:
                drop = len(self.lines) - self.max_lines
                self._start_line += drop
                self.lines = self.lines[-self.max_lines:]
                # 行号会整体上移：鼠标点击的落点映射要跟着修，否则点到隔壁块
                for k in list(self._block_line):
                    self._block_line[k] = max(0, self._block_line[k] - drop)

        def flush_block() -> None:
            if not chunks:
                return
            raw = "".join(chunks)
            chunks.clear()
            pending.extend(wrap_cjk(raw, width))

        def write_rich(content) -> None:
            try:
                super(PanelLog, self).write(content, width=width, scroll_end=False)
            except Exception:
                try:
                    super(PanelLog, self).write(self._safe_strip(content), width=width,
                                                scroll_end=False)
                except Exception:
                    super(PanelLog, self).write("[dim]...[/dim]", width=width, scroll_end=False)

        collapsed = False
        self._block_line = {}  # 每次重排重建：块 key → 标题行在 self.lines 里的下标
        for kind, payload in self._tail_history(width):
            if kind == "h":
                # 块头：先把上一块的正文铺完，再决定本块是整段铺开还是只留标题+预览
                flush_block()
                flush_pending()
                before = len(self.lines)
                if payload.has_header:
                    write_rich(self._block_title(payload))
                    # 标题渲染成 "\n── … ──"（首行空行），标题落在最后一行的位置
                    self._block_line[payload.key] = max(before, len(self.lines) - 1)
                else:
                    # 会话首个块没有标题行：块体从当前行开始
                    self._block_line[payload.key] = before
                collapsed = payload.collapsed
                if collapsed:
                    preview = self._preview_text(payload, width)
                    if preview:
                        write_rich(f"[dim]{escape(preview)}[/dim]")
                continue
            if collapsed:
                continue  # 折叠块：正文整段跳过
            if kind == "t":
                chunks.extend(payload)
                continue
            flush_block()
            flush_pending()
            write_rich(payload)
        flush_block()
        flush_pending()
        # 文本行不经过 RichLog.write，自己做收尾（wrap=True 下内容不会超过面板宽度，
        # 所以用面板宽度当上界即可，省掉一次逐字的 cell_len 扫描）。
        if self.lines:
            self._widest_line_width = max(self._widest_line_width, width)
        self.virtual_size = Size(self._widest_line_width, len(self.lines))
        self.refresh()
        self.scroll_to(y=min(y, self.max_scroll_y), animate=False, immediate=True)

    # -- 块（思考面板的折叠单元） --

    def begin_block(self, key, label: str, has_header: bool = True) -> ThinkBlock:
        """开启一个新块；上一个块自动收尾并折叠，当前块保持展开。

        has_header 由调用方给：会话/清空后的第一块不打标题行，与旧版"第一轮不打
        分隔线"一致。
        """
        self.flush_text()
        cur = self._cur_block
        if cur is not None:
            cur.close()
            cur.collapsed = True
        block = ThinkBlock(key, label, has_header)
        self._blocks.append(block)
        self._cur_block = block
        self._cursor = len(self._blocks) - 1  # 块光标跟着最新块走（打开就能看到高亮）
        self._history.append(("h", block))
        self._trim_history()
        if self._text_width():
            self.reflow()  # 上一块的正文要以"折叠"形态重铺，只能整体重建
        return block

    def note_tool(self, name: str) -> None:
        """记下当前块调用过的工具（折叠标题里显示）。

        这里刻意不重排：标题会在下一次 begin_block/resize 时自然更新，避免每次工具
        调用都把整块屏幕重建一遍。
        """
        if self._cur_block is not None:
            self._cur_block.add_tool(name)

    def _block_title(self, block: ThinkBlock) -> str:
        """块标题行，如 `── ▸ Round 3 · edit_file · 1.2k 字 · 4.1s ──`。

        光标块（j/k 选中的那一块）反白显示，让人一眼看到"选中的是哪一轮"。
        """
        arrow = "▸" if block.collapsed else "▾"
        bits = [f"{arrow} {block.label}"]
        if block.tools:
            bits.append(", ".join(block.tools))
        bits.append(self._fmt_chars(block.chars))
        if block.duration >= 0.1:
            bits.append(f"{block.duration:.1f}s")
        body = escape(" · ".join(bits))
        if self._cursor_block() is block:
            return f"\n[bold reverse]▶ {body} ◀[/bold reverse]"
        return f"\n[dim]── {body} ──[/dim]"

    @staticmethod
    def _fmt_chars(n: int) -> str:
        return f"{n / 1000:.1f}k 字" if n >= 1000 else f"{n} 字"

    def _preview_text(self, block: ThinkBlock, width: int) -> str:
        """折叠体默认显示的尾部预览：取正文末尾，截到一行放得下。"""
        text = " ".join(block.tail.split())
        if not text:
            return ""
        budget = max(8, width - 8)
        out: list = []
        cells = 0
        for ch in reversed(text):
            c = cell_len(ch)
            if cells + c > budget:
                break
            out.append(ch)
            cells += c
        body = "".join(reversed(out))
        return "…" + body if len(out) < len(text) else body

    def expand_all(self) -> None:
        """展开所有块（折叠块只显示标题 + 尾部预览，展开才看得到全文）。"""
        for b in self._blocks:
            b.collapsed = False
        if self._text_width():
            self.reflow()

    def collapse_all(self) -> None:
        """折叠所有已完成的块；正在写入的块保持展开。"""
        cur = self._cur_block
        for b in self._blocks:
            b.collapsed = b is not cur
        if self._text_width():
            self.reflow()

    def toggle_all(self) -> None:
        if any(b.collapsed for b in self._blocks):
            self.expand_all()
        else:
            self.collapse_all()

    # -- 块光标：选一轮、只展开那一轮 --

    def _cursor_block(self):
        if 0 <= self._cursor < len(self._blocks):
            return self._blocks[self._cursor]
        return None

    def focus_block(self, delta: int) -> None:
        """移动块光标（j/k）。还没选过时按方向落到最近的一端。"""
        if not self._blocks:
            return
        if self._cursor < 0:
            self._cursor = len(self._blocks) - 1 if delta < 0 else 0
        else:
            self._cursor = max(0, min(len(self._blocks) - 1, self._cursor + delta))
        if self._text_width():
            self.reflow()  # 反白高亮在标题行里，得重铺才知道选中了谁
        self._scroll_to_cursor()

    def _scroll_to_cursor(self) -> None:
        blk = self._cursor_block()
        line = self._block_line.get(blk.key) if blk is not None else None
        if line is None or line < 0:
            return
        try:
            self.scroll_to(y=max(0, line - 1), animate=False, immediate=True)
        except Exception:
            pass

    def toggle_block(self, block) -> None:
        """只展开/折叠这一块，其它块不受影响（Enter 与鼠标单击标题都走这里）。"""
        block.collapsed = not block.collapsed
        if self._text_width():
            self.reflow()
        self._scroll_to_cursor()

    def block_at_line(self, line_idx: int):
        """这一行属于哪一块（块按时间顺序排；标题行算它自己）。"""
        if line_idx is None or line_idx < 0:
            return None
        found = None
        for b in self._blocks:
            start = self._block_line.get(b.key)
            if start is None:
                continue
            if start <= line_idx:
                found = b
            else:
                break
        return found

    def _line_index_at(self, event) -> int:
        """鼠标 y → self.lines 下标（扣掉边框，加上滚动）。"""
        try:
            off = event.y - (self.content_region.y - self.region.y)
            return int(self.scroll_offset.y + off)
        except Exception:
            return -1

    def action_cursor_next_block(self) -> None:
        self.focus_block(1)

    def action_cursor_prev_block(self) -> None:
        self.focus_block(-1)

    def action_toggle_cursor_block(self) -> None:
        blk = self._cursor_block()
        if blk is None and self._blocks:
            self._cursor = len(self._blocks) - 1
            blk = self._cursor_block()
        if blk is not None:
            self.toggle_block(blk)

    def on_focus(self) -> None:
        """把块操作键位写在边框副标题上——不然没人知道能选块（用户就是这么反馈的）。"""
        if self.panel_id == "think-panel":
            self.border_subtitle = " j/k 选块 · Enter 展开 · 单击标题 · z 全折/全开 "

    def on_blur(self) -> None:
        if self.panel_id == "think-panel":
            self.border_subtitle = ""

    def is_title_click(self, block, line_idx: int) -> bool:
        """这一行是不是"该块的标题点击区"：标题行本身（折叠态再加上尾部预览行）。

        单击展开/折叠的落点判据。抽成方法是为了能被测试直接钉住——点正文不该折块
        （正文上的单击要留给"双击放大面板"）。
        """
        if block is None or not block.has_header or line_idx is None:
            return False
        title = self._block_line.get(block.key)
        if title is None:
            return False
        return title <= line_idx <= title + (1 if block.collapsed else 0)

    def on_click(self, event) -> None:
        # 单击块标题（折叠态的块含它的预览行）= 展开/折叠那一块。这是"选一轮来看"的
        # 鼠标入口；落点不在标题上时保持原有的双击放大面板手势。
        line_idx = self._line_index_at(event)
        blk = self.block_at_line(line_idx)
        if self.is_title_click(blk, line_idx):
            now = time.monotonic()
            if now - self._last_title_click > 0.35:  # 双击标题只折一次
                self._cursor = self._blocks.index(blk)
                self.toggle_block(blk)
            self._last_title_click = now
            return
        now = time.monotonic()
        if self._last_click and now - self._last_click < 0.4:
            self.post_message(self.ExpandRequest(self.panel_id))
        self._last_click = now


class ChatMessage(Static):
    """一条消息 = 一个控件。

    markdown=True 时内容是 RichMarkdown renderable：Rich 对未闭合围栏的容错
    使流式过程中永远不会出现裸露的 ``` （尾部被当作"正在增长的代码块"渲染）。
    markdown=False 时用 escape() 保护用户原始输入，Rich markup 不会被解释。
    """

    def __init__(self, text: str = "", *, markdown: bool = True, **kwargs):
        self.raw = text
        self._markdown_enabled = markdown
        super().__init__(self._rendered(text), **kwargs)

    def _rendered(self, text: str):
        """注意：不要命名为 _render —— 那是 Textual Widget 的内部方法。"""
        return RichMarkdown(text) if self._markdown_enabled else escape(text)

    def set_text(self, text: str, layout: bool = True) -> None:
        """替换内容。流式提交用默认 layout=True；定型时可传 False 省一次布局。"""
        self.raw = text
        self.update(self._rendered(text), layout=layout)

    def on_click(self, event) -> None:
        """把点击转发给 ChatTranscript。

        必需：Textual 不会把子控件的 Click 冒泡到父控件的 on_click，而聊天区几乎
        全被消息气泡覆盖，所以不在子控件上转发的话，双击展开就形同失效。
        """
        parent = self.parent
        if isinstance(parent, ChatTranscript):
            parent.on_click(event)


class ChatTranscript(VerticalScroll):
    """聊天面板：每条消息一个子控件，resize 由 Textual 自动重排（无需手动 reflow）。"""

    class ExpandRequest(Message):
        """Posted when the chat panel is double-clicked (与 PanelLog 行为一致)。"""
        def __init__(self, panel_id: str) -> None:
            self.panel_id = panel_id
            super().__init__()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._live: ChatMessage | None = None
        self._raw: str = ""  # 当前流式消息的完整原始文本
        self._last_click: float = 0.0
        self._expand_fired_at: float = 0.0  # 上次发出展开请求的时间（手势消抖）

    def on_mount(self) -> None:
        # 用 Textual 官方锚定机制：新内容自动跟随底部；用户一旦上翻即自动释放，
        # 回到底部后自动恢复。不要手写 scroll_y 比较——内容增长会让判断失效。
        self.anchor()

    def on_click(self, event) -> None:
        """双击展开/收起本面板。

        必须由 ChatMessage.on_click 转发：Textual 不会把子控件的 Click 冒泡到父控件
        的 on_click，而聊天区几乎全被消息气泡覆盖。
        同一次点击手势可能触发多次 on_click（子控件+父控件），所以用 _last_click
        判定双击后必须消抖，否则一次双击会 toggle 两次、互相抵消。
        """
        now = time.monotonic()
        if self._last_click and now - self._last_click < 0.4:
            self._last_click = now  # 推进时间戳，避免同一手势重复触发
            if now - self._expand_fired_at > 0.4:
                self._expand_fired_at = now
                self.post_message(self.ExpandRequest(self.id))
            return
        self._last_click = now

    # -- 消息 --

    def add_user(self, text: str) -> ChatMessage:
        msg = ChatMessage(text, markdown=False, classes="user")
        self.mount(msg)
        return msg

    def start_assistant(self) -> ChatMessage:
        """开一条新的助手消息并标记为流式目标。"""
        self._live = ChatMessage("", markdown=True, classes="assistant")
        self._raw = ""
        self.mount(self._live)
        return self._live

    def append_chunk(self, delta: str = "") -> None:
        """追加流式增量并重渲染整条消息。

        RichMarkdown 对未闭合围栏的容错保证流式过程中不会露出原始 ```。
        重复调用是幂等的（每次都拿完整文本重渲染），所以节流与 flush 都安全。
        """
        live = self._live
        if live is None:
            live = self.start_assistant()
        self._raw += delta
        live.set_text(self._raw)

    def set_live_text(self, text: str) -> None:
        """直接设定流式消息的完整内容（中断 / 兜底 flush 用）。"""
        live = self._live
        if live is None:
            live = self.start_assistant()
        self._raw = text
        live.set_text(text)

    def finish_message(self) -> None:
        """流式结束：定型当前消息。"""
        if self._live is not None:
            self._live.set_text(self._raw, layout=False)
            self._live = None
            self._raw = ""

    # -- 单行提示（命令输出 / 状态） --

    def note(self, text: str, classes: str = "note") -> ChatMessage:
        """一行暗色提示，等价于原来 chat_panel.write("[dim]...[/dim]")。"""
        msg = ChatMessage(text, markdown=False, classes=classes)
        self.mount(msg)
        return msg

    # -- 滚动 --

    # 跟随底部由 Textual 的 anchor 机制负责（见 on_mount），此处无需手动滚动。

    def clear_messages(self) -> None:
        self._live = None
        self._raw = ""
        for child in list(self.children):
            child.remove()

    def replay(self, messages) -> None:
        """回放一份对话 JSON（/load 与 autosave 恢复共用）。

        助手消息按 markdown 渲染；用户消息原样纯文本；工具结果只记一行摘要。
        """
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content") or ""
            if role == "user":
                self.mount(ChatMessage(content, markdown=False, classes="user"))
            elif role == "assistant":
                if content.strip():
                    self.mount(ChatMessage(content, markdown=True, classes="assistant"))
                elif msg.get("tool_calls"):
                    self.mount(ChatMessage(
                        f"  tool call x{len(msg['tool_calls'])}", markdown=False, classes="note"))
            elif role == "tool":
                self.mount(ChatMessage(
                    f"  tool result ({len(str(content))} chars)", markdown=False, classes="note"))


class AgentTUI(App):
    """Three-panel TUI: Chat | Thinking | Tools."""

    BINDINGS = [
        Binding("1", "focus_panel('chat')", "Chat"),
        Binding("2", "focus_panel('think')", "Think"),
        Binding("3", "focus_panel('tool')", "Tools"),
        Binding("i", "focus_input", "Input"),
        Binding("tab", "focus_next", "Next"),
        Binding("ctrl+c", "quit", "Save & Quit", show=False),
        Binding("escape", "collapse_panel", "", show=False),
        Binding("ctrl+j", "submit_input", "Send"),
        Binding("ctrl+t", "toggle_think_panel", "Fold Think", show=False),
        Binding("z", "toggle_think_blocks", "Fold Blocks", show=False),
        Binding("o", "expand_think_blocks", "Expand Blocks", show=False),
        Binding("`", "toggle_shell", "Shell"),
    ]

    # 新问题的"上一轮次"哨兵：不等于任何 round 值，也不是 None（None = 面板里还没轮次）
    _NEW_QUESTION = object()

    CSS = """
    Vertical {
        height: 100%;
    }

    #main-panels {
        height: 1fr;
    }

    #chat-panel {
        width: 1fr;
        border: solid $primary;
    }

    ChatTranscript {
        height: 100%;
    }

    #chat-panel ChatMessage {
        height: auto;
        width: 1fr;
    }

    #chat-panel ChatMessage.user {
        color: $text;
        padding: 0 0 1 0;
    }

    #chat-panel ChatMessage.assistant {
        padding: 0 0 1 0;
    }

    #chat-panel ChatMessage.note,
    #chat-panel ChatMessage.notice,
    #chat-panel ChatMessage.error {
        color: $text-muted;
    }

    #chat-panel ChatMessage.notice {
        color: $warning;
    }

    #chat-panel ChatMessage.error {
        color: $error;
    }

    #chat-panel ChatMessage.mono {
        color: $text-muted;
    }

    #think-panel {
        width: 1fr;
        border: solid $secondary;
    }

    #tool-panel {
        width: 1fr;
        border: solid $accent;
    }

    #input-area {
        height: auto;
        min-height: 3;
        max-height: 12;
        margin: 0 0 1 0;
    }

    #footer-bar {
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }

    #shell-output {
        height: 0;
        border: solid $warning;
        background: $surface;
        overflow-y: scroll;
    }

    #shell-output.visible {
        height: 10;
    }

    #progress-row {
        height: 1;
        background: $surface;
        padding: 0 1;
    }

    #progress-label {
        color: $text-muted;
        width: 12;
    }

    #progress-bar {
        width: 1fr;
        height: 1;
    }
    """

    # 各阶段进度条目标（百分比）与标签。bar 每帧向当前阶段目标推进，阶段切换时平滑过渡。
    _PROGRESS_TARGET = {"thinking": 15, "calling_tool": 70, "generating": 100, "idle": 0}
    _PROGRESS_LABEL = {"thinking": "思考中…", "calling_tool": "调用工具…", "generating": "生成中…", "idle": ""}

    _AUTOSAVE_INTERVAL = 5   # 每 N 次工具调用自动存一次
    _AUTOSAVE_MAX = 3        # 轮转保留 .autosave.json / .1 / .2 共 3 份
    _THINK_FLUSH_TIMEOUT = 0.15  # 首字上屏兜底：慢速模型下不让思考面板长时间空着
    _THINK_STALL_TIMEOUT = 0.8   # 流中断兜底：出过字之后残尾最多再等这么久

    def __init__(self):
        super().__init__()
        self._event_queue: queue.Queue = queue.Queue()
        self._shell_queue: queue.Queue = queue.Queue()
        self._confirm_queue: queue.Queue = queue.Queue()
        loop_agent_v2.confirm_mod._ask = self._confirm_ask
        self._agent_busy: bool = False
        self._token_total: int = 0
        self._cache_ratio = None           # 上一次 API 调用的前缀缓存命中率（None=provider 不报）
        self._status: str = "idle"
        self._stream_buffer: str = ""
        self._stream_width: int = 0        # 缓存当前流式消息的面板宽度
        self._think_round = None
        self._q_index: int = 0             # 问题序号：块 key 用它区分重号的轮次
        self._shell_visible: bool = False
        self._expanded_panel: str = ""  # non-empty when a panel is expanded
        self._collapsed: set = set()       # 被折叠（宽度置 0）的面板裸 id，Ctrl+T 切换
        self._think_round_count: int = 0   # 累计思考轮次（面板边框与底栏徽标共用）
        self._progress_current: float = 0.0
        self._progress_target: int = 0
        self._tool_call_count: int = 0    # 自上次 autosave 以来的工具调用数
        self._autosave_counter: int = 0   # autosave 轮转序号
        self._did_save: bool = False      # 正常保存过 → atexit 应急保存跳过
        self._discard_exit: bool = False  # 用户主动丢弃 → atexit 应急保存跳过
        self._llm_reachable = None        # None=未检；P2 健康检查缓存

    def compose(self) -> ComposeResult:
        with Vertical():
            yield RichLog(id="shell-output", wrap=True, markup=True, max_lines=2000)
            with Horizontal(id="main-panels"):
                yield ChatTranscript(id="chat-panel")
                yield PanelLog(id="think-panel", panel_id="think-panel", wrap=True, markup=True, max_lines=5000)
                yield PanelLog(id="tool-panel", panel_id="tool-panel", wrap=True, markup=True, max_lines=5000)
            yield TextArea(id="input-area")
            with Horizontal(id="progress-row"):
                yield Static(id="progress-label")
                yield ProgressBar(id="progress-bar", total=100)
            yield Static(id="footer-bar")

    def on_mount(self) -> None:
        self.query_one("#chat-panel", ChatTranscript).border_title = " Chat "
        self.query_one("#think-panel", RichLog).border_title = " Thinking "
        self.query_one("#tool-panel", RichLog).border_title = " Tools "
        atexit.register(self._emergency_save)
        self._update_footer()
        self.set_interval(1 / 30, self._poll_queue)
        self.query_one("#input-area", TextArea).focus()
        self._try_recover_autosave()
        if _cfg._is_local_backend():
            threading.Thread(target=self._check_llm_async, daemon=True).start()

    # -- key bindings --

    def action_focus_panel(self, name: str) -> None:
        panel_map = {
            "chat": "#chat-panel",
            "think": "#think-panel",
            "tool": "#tool-panel",
        }
        panel_id = panel_map.get(name)
        if not panel_id:
            return
        key = panel_id.lstrip("#")
        # 聚焦前必须保证这个面板不是 0 宽，否则焦点落在 0 宽控件上、按键如同消失。
        # 两种会被挡住的情况都要处理：自己处于折叠态；或者别的面板正占满整个宽度。
        changed = False
        if key in self._collapsed:
            self._collapsed.discard(key)
            changed = True
        if self._expanded_panel and self._expanded_panel.lstrip("#") != key:
            self._expanded_panel = ""
            changed = True
        if changed:
            self._apply_layout()
        self.query_one(panel_id, Widget).focus()

    def action_focus_input(self) -> None:
        self.query_one("#input-area", TextArea).focus()

    def action_toggle_shell(self) -> None:
        widget = self.query_one("#shell-output", RichLog)
        self._shell_visible = not self._shell_visible
        if self._shell_visible:
            widget.add_class("visible")
            widget.clear()
            widget.border_title = " Terminal "
        else:
            widget.remove_class("visible")
            widget.border_title = ""

    def action_quit(self) -> None:
        """All quit paths converge here — save dialog intercepts."""
        if self._has_content():
            self.push_screen(QuitDialog(), self._on_quit_dialog)
        else:
            self.exit()

    # -- panel expand / collapse --

    def on_panel_log_expand_request(self, event: PanelLog.ExpandRequest) -> None:
        self._toggle_expand(event.panel_id)

    def on_chat_transcript_expand_request(self, event: ChatTranscript.ExpandRequest) -> None:
        self._toggle_expand(event.panel_id)

    def _toggle_expand(self, panel_id: str) -> None:
        # panel_id 可能是 "chat-panel"（ExpandRequest 自发 id）或 "#chat-panel"，
        # 统一成裸 id 再比较，否则展开后双击永远无法收起。
        normalized = panel_id.lstrip("#")
        if self._expanded_panel.lstrip("#") == normalized:
            self._restore_panels()
        else:
            self._expand_panel(normalized)

    def _apply_layout(self) -> None:
        """按展开/折叠状态统一设置三个面板的宽度（唯一改宽度的地方）。

        展开态优先：有面板处于双击展开态时它独占 1fr，其余一律 0；没有展开态时，
        未折叠的面板等分 1fr，折叠的置 0。
        折叠只改宽度、不丢内容：折叠期间思考照常写进 PanelLog，重新展开时由
        PanelLog.reflow 原样铺回来（0 宽时 PanelLog 不折行不上屏，只记历史）。
        """
        expanded = self._expanded_panel.lstrip("#")
        for pid in ("#chat-panel", "#think-panel", "#tool-panel"):
            key = pid.lstrip("#")
            if expanded:
                width = "1fr" if key == expanded else "0"
            else:
                width = "0" if key in self._collapsed else "1fr"
            self.query_one(pid, Widget).styles.width = width

    def _expand_panel(self, panel_id: str) -> None:
        self._expanded_panel = panel_id
        self._apply_layout()

    def _restore_panels(self) -> None:
        self._expanded_panel = ""
        self._apply_layout()

    def action_collapse_panel(self) -> None:
        if self._agent_busy:
            # Esc 在 agent 运行时 = 请求中断（流式中断、长命令被杀）
            loop_agent_v2.interrupt_event.set()
            self.query_one("#chat-panel", ChatTranscript).note("🛑 已请求中断…", classes="notice")
            return
        if self._expanded_panel:
            self._restore_panels()

    def action_toggle_think_blocks(self) -> None:
        """z：思考块在"展开全部 / 折叠全部"之间切换（整体操作）。"""
        self.query_one("#think-panel", PanelLog).toggle_all()

    def action_expand_think_blocks(self) -> None:
        """o：展开全部块（幂等）。想看某一轮就用 2 聚焦思考面板后 j/k + Enter。"""
        self.query_one("#think-panel", PanelLog).expand_all()

    def action_toggle_think_panel(self) -> None:
        """Ctrl+T：折叠/展开思考面板（同一按键来回切换，再按一次即恢复）。

        只改布局、不动内容：折叠期间思考照常写入 PanelLog，展开时由 reflow 原样回来。
        折叠态与展开态互斥，两个方向都要收：折叠时若思考面板正占满宽度，要连同展开
        一起收起；展开时若有别的面板占满宽度，也要先收起它——否则"展开"之后思考面板
        仍然是 0 宽，按键看起来毫无反应。
        """
        if "think-panel" in self._collapsed:
            self._collapsed.discard("think-panel")
            if self._expanded_panel:
                self._expanded_panel = ""
        else:
            self._collapsed.add("think-panel")
            if self._expanded_panel.lstrip("#") == "think-panel":
                self._expanded_panel = ""
        try:
            focused = self.focused
        except Exception:
            focused = None
        if "think-panel" in self._collapsed and focused is self.query_one("#think-panel", Widget):
            self.query_one("#input-area", TextArea).focus()
        self._apply_layout()
        # 折叠状态变了就得重刷徽标：底栏那个 🧠 只在折叠时显示，不刷新的话
        # 展开之后会一直挂着旧计数。
        self._update_think_badge()

    def _has_content(self) -> bool:
        """Any real conversation beyond the system prompt?"""
        return len(_agent_messages) > 1

    def _on_quit_dialog(self, result: str) -> None:
        if result == "save":
            path = self._save_conversation()
            self.notify(f"Saved → {path}", timeout=3)
            self.exit()
        elif result == "discard":
            self._discard_exit = True
            self.exit()
        # "cancel": do nothing

    def _save_conversation(self) -> str:
        save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversations")
        os.makedirs(save_dir, exist_ok=True)
        filename = f"chat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(save_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(_agent_messages, f, ensure_ascii=False, indent=2)
        # 折叠档案是 sidecar：会话文件仍只有 messages 数组（replay/旧加载逻辑完全不受影响）
        try:
            loop_agent_v2.persist_archive_sidecar(filepath)
        except Exception:
            pass

        # Clean up corrupted files from previous failed saves
        for f in os.listdir(save_dir):
            if (f.startswith("chat_") and f.endswith(".json")
                    and not f.endswith(".archive.json") and f != filename):
                try:
                    with open(os.path.join(save_dir, f), "r", encoding="utf-8") as fh:
                        if not isinstance(json.load(fh), list):
                            os.remove(os.path.join(save_dir, f))
                except Exception:
                    os.remove(os.path.join(save_dir, f))

        self._did_save = True
        return filepath

    def _autosave(self) -> None:
        """每 N 次工具调用把当前对话存到 conversations/.autosave*.json（轮转）。失败静默。"""
        save_dir = self._save_dir
        os.makedirs(save_dir, exist_ok=True)
        idx = self._autosave_counter % self._AUTOSAVE_MAX
        filename = ".autosave.json" if idx == 0 else f".autosave.{idx}.json"
        try:
            with open(os.path.join(save_dir, filename), "w", encoding="utf-8") as f:
                json.dump(_agent_messages, f, ensure_ascii=False, indent=2)
            try:
                loop_agent_v2.persist_archive_sidecar(os.path.join(save_dir, filename))
            except Exception:
                pass
            self._autosave_counter += 1
        except Exception:
            pass  # autosave 失败不崩 TUI

    def _emergency_save(self) -> None:
        """atexit 应急保存：意外退出兜底。已保存过或用户主动丢弃则跳过。"""
        if self._did_save or self._discard_exit or not self._has_content():
            return
        try:
            self._save_conversation()
        except Exception:
            pass

    @property
    def _save_dir(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversations")

    def _cmd_load(self, question: str) -> None:
        chat_panel = self.query_one("#chat-panel", ChatTranscript)
        # 加载历史前自动保存当前会话，避免被覆盖丢失
        if self._has_content():
            path = self._save_conversation()
            chat_panel.note(f"Auto-saved current → {os.path.basename(path)}")
        arg = question[5:].strip()
        os.makedirs(self._save_dir, exist_ok=True)

        all_files = sorted(
            [f for f in os.listdir(self._save_dir) if is_conversation_file(f)],
            reverse=True,
        )

        if not all_files:
            chat_panel.note("No saved conversations found.")
            return

        # Separate valid from corrupted（sidecar / 半截文件都会落到 corrupted）
        valid_files = []
        corrupted_files = []
        for f in all_files:
            data, err = read_conversation(os.path.join(self._save_dir, f))
            if err:
                corrupted_files.append((f, err))
            else:
                valid_files.append((f, data))

        # Number selection → /load N (only counts valid files)
        try:
            idx = int(arg)
            if 1 <= idx <= len(valid_files):
                self._load_file(os.path.join(self._save_dir, valid_files[idx - 1][0]))
                return
            chat_panel.note(f"Invalid number: {idx} (1-{len(valid_files)})", classes="error")
            return
        except ValueError:
            pass

        # Filename → backward compat: /load chat_xxx.json
        if arg and (arg.endswith(".json") or arg.startswith("chat_")):
            target = arg if arg.endswith(".json") else arg + ".json"
            if not os.path.exists(target):
                target = os.path.join(self._save_dir, target)
            if os.path.exists(target):
                self._load_file(target)
                return
            chat_panel.note(f"Not found: {arg}", classes="error")
            return

        # No arg → list all with title preview
        chat_panel.note("Saved conversations:")
        for i, (f, data) in enumerate(valid_files, 1):
            stem = f[len("chat_"):-len(".json")]
            try:
                dt = datetime.strptime(stem, "%Y%m%d_%H%M%S")
                label = dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                label = stem
            # Extract first user message as title
            title = ""
            for msg in data:
                if msg.get("role") == "user":
                    title = msg.get("content", "")
                    break
            title = title[:60].replace("\n", " ") + ("..." if len(title) > 60 else "")
            chat_panel.note(f"  {i}  {label}")
            chat_panel.note(f"       \"{title}\"" if title else "       (empty)")

        if corrupted_files:
            chat_panel.note("Corrupted files (will be skipped on save cleanup):", classes="notice")
            for f, err in corrupted_files:
                chat_panel.note(f"  ✗ {f}  {err}")

        if valid_files:
            chat_panel.note(f"Usage: /load <number> (1-{len(valid_files)})")

    def _load_file(self, filepath: str) -> None:
        """Load a conversation file and rebuild UI.

        **先校验再动历史**：加载失败/文件不是会话（如把折叠档案 sidecar 当会话）时必须
        保持当前会话原封不动，否则一次误加载就会把内存里的对话换成垃圾，且此后每次请求
        都 422、每次保存都写出坏历史（实测踩到）。
        """
        chat_panel = self.query_one("#chat-panel", ChatTranscript)
        loaded, err = read_conversation(filepath)
        if err:
            chat_panel.note(f"Failed to load {os.path.basename(filepath)}：{err}", classes="error")
            return

        _agent_messages.clear()
        _agent_messages.extend(loaded)
        # 恢复该会话的折叠档案（sidecar），使 /expand 与 recall_archive 在 /load 后仍可用
        try:
            loop_agent_v2.adopt_archive(filepath)
        except Exception:
            pass

        chat_panel.clear_messages()
        chat_panel.note(f"--- Loaded {os.path.basename(filepath)} ---")
        chat_panel.replay(loaded)

        self.query_one("#think-panel", RichLog).clear()
        self.query_one("#tool-panel", RichLog).clear()
        self._reset_think_rounds()  # 面板已清空 → 轮次徽标一并归零
        self._token_total = 0
        self._cache_ratio = None
        self._update_footer()

    def _try_recover_autosave(self) -> None:
        """启动时检测 .autosave.json，有可恢复内容则弹恢复对话框。

        同样要**先校验**：坏 autosave（例如被误加载的折叠档案又被 autosave 写出来）
        弹给用户恢复，等于把损坏的历史请回来。
        """
        autosave_path = os.path.join(self._save_dir, ".autosave.json")
        if not os.path.exists(autosave_path):
            return
        data, err = read_conversation(autosave_path)
        if err or len(data) <= 1:
            return
        self._recovery_data = data
        self._recovery_path = autosave_path
        self.call_later(self._show_recovery_dialog)

    def _show_recovery_dialog(self) -> None:
        first_user = ""
        for m in self._recovery_data:
            if m.get("role") == "user":
                first_user = m.get("content", "")[:60]
                break
        self.push_screen(
            RecoveryDialog(len(self._recovery_data), first_user),
            self._on_recovery_dialog,
        )

    def _on_recovery_dialog(self, result: str) -> None:
        chat_panel = self.query_one("#chat-panel", ChatTranscript)
        if result == "restore":
            _agent_messages.clear()
            _agent_messages.extend(self._recovery_data)
            try:
                loop_agent_v2.adopt_archive(self._recovery_path)
            except Exception:
                pass
            chat_panel.note(f"--- Recovered {len(self._recovery_data)} messages from autosave ---")
            chat_panel.replay(self._recovery_data)
            try:
                os.remove(self._recovery_path)
            except Exception:
                pass
        elif result == "discard":
            try:
                os.remove(self._recovery_path)
            except Exception:
                pass
        # "cancel": 保留 autosave 文件，下次启动再提示
        self._recovery_data = None

    # -- input handling --

    def action_submit_input(self) -> None:
        textarea = self.query_one("#input-area", TextArea)
        question = textarea.text.strip()
        if not question or self._agent_busy:
            return
        textarea.text = ""
        textarea.focus()

        # /clear — reset conversation
        if question == "/clear":
            _agent_messages.clear()
            _agent_messages.append({"role": "system", "content": loop_agent_v2.system_prompt})
            # 折叠档案属于本次对话：不清掉会让 recall_archive / /expand 拿到上个会话的原文
            loop_agent_v2.clear_archive()
            self.query_one("#chat-panel", ChatTranscript).clear_messages()
            for pid in ("#think-panel", "#tool-panel"):
                self.query_one(pid, RichLog).clear()
            # 会话放行是进程级状态，清空对话时必须一并撤销，否则门禁被静默留在"开"的位置
            loop_agent_v2.confirm_mod.revoke()
            self.query_one("#chat-panel", ChatTranscript).note(
                "--- Conversation cleared（会话放行已一并撤销）---")
            self._token_total = 0
            self._cache_ratio = None
            self._reset_think_rounds()  # 思考面板已清空 → 轮次徽标一并归零
            self._update_footer()
            return

        # /revoke — 撤销"本会话放行"（安全审计 V8：此前无任何撤销路径）
        if question == "/revoke":
            chat_panel = self.query_one("#chat-panel", ChatTranscript)
            loop_agent_v2.confirm_mod.revoke()
            chat_panel.note("已撤销全部会话放行；后续敏感操作将重新征求确认。")
            return

        # /policy — 查看当前确认门策略
        if question == "/policy":
            chat_panel = self.query_one("#chat-panel", ChatTranscript)
            policy = loop_agent_v2.confirm_mod.get_policy()
            if policy == "allow_all":
                chat_panel.note("⚠ 确认门策略 = allow_all：所有敏感操作**不询问**直接放行（仅测试/受控脚本应使用）")
            elif policy == "deny_all":
                chat_panel.note("确认门策略 = deny_all：所有敏感操作一律拒绝")
            else:
                chat_panel.note("确认门策略 = ask：敏感操作会弹窗征求确认，无人可问时默认拒绝")
            return

        # /load [name] — load a saved conversation
        if question.startswith("/load"):
            self._cmd_load(question)
            return

        # /compact — 立即跑一次上下文压缩（越过排期闸门，仍受会话次数上限约束）
        if question == "/compact":
            chat_panel = self.query_one("#chat-panel", ChatTranscript)
            try:
                chat_panel.note(loop_agent_v2.compact_session())
            except Exception as e:
                chat_panel.note(f"压缩出错：{e}", classes="error")
            self._update_footer()
            return

        # /expand — 还原最近一次压缩（只还原会话历史，不重绘面板）
        if question == "/expand":
            chat_panel = self.query_one("#chat-panel", ChatTranscript)
            try:
                chat_panel.note(loop_agent_v2.expand_compaction())
            except Exception as e:
                chat_panel.note(f"还原出错：{e}", classes="error")
            self._update_footer()
            return

        # /quit — safe quit with save dialog
        if question == "/quit":
            self.action_quit()
            return

        # /save — save current conversation now
        if question == "/save":
            if self._has_content():
                path = self._save_conversation()
                self.query_one("#chat-panel", ChatTranscript).note(
                    f"Saved → {os.path.basename(path)}")
            else:
                self.query_one("#chat-panel", ChatTranscript).note("Nothing to save.")
            return

        if question == "/help":
            chat_panel = self.query_one("#chat-panel", ChatTranscript)
            help_txt = """================================================================================
                         Local Agent TUI 帮助文档
================================================================================

当前模型：{CURRENT_MODEL}

键盘快捷键
  1           聚焦聊天面板
  2           聚焦思考面板
  3           聚焦工具面板
  i           聚焦输入框
  Tab         切换焦点到下一个组件
  Ctrl+J      发送消息（Enter 换行）
  Ctrl+T      折叠/展开思考面板（再按一次即恢复）
  z / o       思考块整体折叠 / 展开全部
  2 → j / k   聚焦思考面板后上下选块（选中的标题反白）
  Enter       展开/折叠选中的那一块（鼠标单击标题同样可以）
  `           展开/收起 Shell 终端面板
  Esc         折叠展开的面板
  双击面板     展开/恢复该面板

命令
  /clear      清空对话历史，重置上下文
  /compact    立即压缩上下文（L0 去重 + L1 分块摘要；不受触发阈值/间隔/退避约束，仍计入会话次数上限）
  /expand     还原最近一次压缩的原始历史（注意：面板不重绘）
  /load       列出所有可加载的对话（加载前自动保存当前会话）
  /load N     加载指定编号的对话
  /save       立即保存当前对话到 conversations/
  /quit       安全退出（可保存对话）
  /help       显示此帮助信息

上下文管理
  - L0 去重：重复的工具结果只留最后一份，更早的换成占位符（零信息损失）
  - L1 压缩：达阈值时把旧历史分块滚动摘要成 [上下文摘要]（模型可用 recall_archive 回取原文）
  - L2 裁剪：压缩失败或仍超限时，删除旧消息并注入 [Context trimmed]（计数累加）

系统功能
  - 后端：local-3.5 / local-3.6 (Qwen 9B/35B) 或 DeepSeek
  - 跨会话记忆：recall/remember 工具
  - 工具：基础工具 + recall/remember + recall_archive（回取被折叠的原文）
  - 纯实现位于 loop_agent_core/ 包，loop_agent_v2.py 为薄壳

================================================================================"""
            chat_panel.note(help_txt.format(CURRENT_MODEL=loop_agent_v2.CURRENT_MODEL)
                            + "\n助手回复按 Markdown 渲染；用户输入原样显示，不解析 Markdown。",
                            classes="mono")
            return
        textarea.disabled = True
        self._agent_busy = True
        self._begin_question()  # 轮次号从 1 重新数（见 _begin_question）

        chat_panel = self.query_one("#chat-panel", ChatTranscript)
        chat_panel.add_user(question)

        self._set_status("thinking")

        threading.Thread(target=self._run_agent, args=(question,), daemon=True).start()

    def _confirm_ask(self, kind, target, is_worker, purpose=""):
        """Runs in agent thread. Shows the confirm modal, then blocks for the TUI answer.

        延迟优化：原先把事件塞进 _event_queue，要等下一帧 30fps 轮询，且排在整批流式
        token 事件之后——实测 backlog 300 事件时弹窗出现要 118ms、1000 事件要 546ms。
        这里用 call_from_thread 直接在主线程调度 _handle_event，绕开队列与 tick，弹窗
        立即出现；本线程随后阻塞等用户答案。call_from_thread 在 app 未运行/同线程调用
        时抛 RuntimeError，此时回落原队列路径（_poll_queue 会优先处理 confirm_request）。

        purpose 由模型在敏感工具参数里给出，透传给确认框显示——只有命令原文、没有目的时，
        审批人无从判断意图，确认框就退化成形式。
        """
        event = {"type": "confirm_request", "kind": kind, "target": target,
                 "is_worker": is_worker, "purpose": purpose}
        try:
            self.call_from_thread(self._handle_event, event)
        except Exception:
            self._event_queue.put(event)
        try:
            return self._confirm_queue.get()
        except Exception:
            return "deny"

    def _run_agent(self, question: str) -> None:
        """Runs in background thread. Pushes events to queue."""
        loop_agent_v2._shell_output_queue = self._shell_queue

        def on_event(event):
            self._event_queue.put(event)

        try:
            chat(question, on_event=on_event)
        except Exception as e:
            self._event_queue.put({"type": "error", "message": str(e)})
        self._event_queue.put({"type": "__agent_done__"})

    # -- event polling (main thread, ~30fps) --

    def _poll_queue(self) -> None:
        # Drain agent event queue（先整批取出，再分优先级处理）
        pending = []
        while True:
            try:
                pending.append(self._event_queue.get_nowait())
            except queue.Empty:
                break
        # 确认弹窗必须最先处理：实测排在流式 token 事件之后时，弹窗出现延迟随 backlog
        # 线性放大（300 事件→118ms，1000 事件→546ms，见 confirm 实测）。其余事件保持
        # 原顺序，避免影响流式渲染与状态机的既有行为。
        for event in pending:
            if event.get("type") == "confirm_request":
                self._handle_event(event)
        for event in pending:
            if event.get("type") != "confirm_request":
                self._handle_event(event)

        # Drain shell output queue (only if panel is visible)
        if self._shell_visible:
            shell_log = self.query_one("#shell-output", RichLog)
            while True:
                try:
                    item = self._shell_queue.get_nowait()
                except queue.Empty:
                    break
                kind, data = item[0], item[1]
                if kind == "shell_start":
                    shell_log.write(f"[bold yellow]$ {escape(data)}[/bold yellow]")
                elif kind == "shell_line":
                    shell_log.write(escape(data.rstrip("\n")))
                elif kind == "shell_done":
                    shell_log.write(f"[dim]--- exit code: {escape(str(data))} ---[/dim]")

        # 推进进度条
        self._tick_progress()

        # 思考面板残尾超时兜底（慢速模型下保证首次输出不被阈值压住）
        self._drain_think_tail()

    def _handle_event(self, event: dict) -> None:
        etype = event.get("type")

        if etype == "__agent_done__":
            self._flush_stream()
            self._flush_think()
            self._autosave()  # 每轮结束都存：恢复点始终是最近一轮
            self._tool_call_count = 0
            self._agent_busy = False
            loop_agent_v2.interrupt_event.clear()  # 中断标记已消费，下轮不会误触发
            self._set_status("idle")
            self.query_one("#input-area", TextArea).disabled = False
            self.query_one("#input-area", TextArea).focus()
            return

        if etype == "error":
            self.query_one("#tool-panel", RichLog).write(
                f"[bold red]ERROR:[/bold red] {escape(str(event.get('message')))}"
            )
            return

        handlers = {
            "token_usage": self._h_token,
            "stream_usage": self._h_token,
            "thinking": self._h_thinking,
            "thinking_chunk": self._h_thinking_chunk,
            "tool_calls": self._h_tool_calls,
            "tool_result": self._h_tool_result,
            "self_check": self._h_self_check,
            "worker_start": self._h_worker_start,
            "worker_done": self._h_worker_done,
            "confirm_request": self._h_confirm,
            "response_chunk": self._h_response_chunk,
            "response_done": self._h_response_done,
            "status": self._h_status,
            # 上下文管理三层（此前压缩对用户完全不可见）
            "deduped": self._h_deduped,
            "compacting": self._h_compacting,
            "compacted": self._h_compacted,
            "compact_failed": self._h_compact_failed,
            "compact_skipped": self._h_compact_skipped,
            # 回合怎么结束的（思考循环 / 乱码 / 中断 / 收尾催办）：以前这些只在
            # runner 里 return，返回值又被丢掉 → 面板零输出，看起来像"静默停止工作"
            "notice": self._h_notice,
        }
        if handler := handlers.get(etype):
            handler(event)

    # -- per-event-type handlers --

    def _h_token(self, e: dict) -> None:
        usage = e.get("usage")
        try:
            if hasattr(usage, "total_tokens"):
                self._token_total += usage.total_tokens
            elif isinstance(usage, dict):
                self._token_total += usage.get("total_tokens", 0)
        except Exception:
            pass
        # 命中率只有 provider 报了 cached_tokens 才有；拿不到就保持上一次的值不动
        ratio = (e.get("cache") or {}).get("hit_ratio")
        if isinstance(ratio, (int, float)):
            self._cache_ratio = ratio
        self._update_footer()

    def _note_think_round(self) -> None:
        """记一轮思考并刷新徽标（面板边框的 (n) 与折叠时的底栏 🧠 n）。

        轮次由 runner 给出（每轮工具调用一个 round，且每个问题从 1 重新开始），
        所以只认"轮次号变了"这件事，计数在整个会话里累加。
        """
        self._think_round_count += 1
        self._update_think_badge()

    def _begin_question(self) -> None:
        """新问题开始：把"当前轮次"置成一个不可能等于任何 round 的哨兵。

        runner 的 tool_round 每个问题都从 0 重新计（loop_agent_core/runner.py:304），
        所以每个问题的第 1 轮都是 round=1。若沿用上一问的轮次号，第二问的第 1 轮会被
        判成"还是上一轮"：既不打破折线，两问的思考也会连成一片，轮次计数还少一次。
        哨兵不能是 None——None 表示"面板里还没有过轮次"，那时不该打分隔线。
        """
        self._think_round = self._NEW_QUESTION
        self._q_index += 1

    def _reset_think_rounds(self) -> None:
        """思考面板被清空（/clear、/load）后把轮次计数一起归零，避免徽标留着旧计数。"""
        self._think_round = None
        self._think_round_count = 0
        self._update_think_badge()

    def _h_thinking(self, e: dict) -> None:
        content = e.get("content", "")
        if not content:
            return
        r = e.get("round", "?")
        panel = self.query_one("#think-panel", PanelLog)
        if r != self._think_round:
            panel.flush_text()
            panel.begin_block((self._q_index, r), f"Round {r}",
                              has_header=self._think_round is not None)
            self._think_round = r
            self._note_think_round()
        if len(content) > 300:
            panel.stream_text(escape(content[:300]))
            panel.flush_text()
            panel.write_wrapped(f"[dim]... ({len(content)} chars)[/dim]")
        else:
            panel.stream_text(escape(content))
            panel.flush_text()

    def _h_thinking_chunk(self, e: dict) -> None:
        content = e.get("content", "")
        if not content:
            return
        rnd = e.get("round", self._think_round)
        panel = self.query_one("#think-panel", PanelLog)
        if rnd != self._think_round:
            # 新轮次：开一个新块（旧块自动收尾并折叠），块标题兼作旧版的分隔线。
            # key 用 (问题序号, 轮次)：runner 每个问题都从 round=1 重数，只认轮次会串块。
            panel.begin_block((self._q_index, rnd), f"Round {rnd}",
                              has_header=self._think_round is not None)
            self._think_round = rnd
            self._note_think_round()
        # 折行、禁则、残尾缓冲都在 PanelLog 里做：只把折满的整行上屏，
        # 不在半行处硬断（硬断出来的短行既填不满面板，展开后也合不回去）。
        panel.stream_text(escape(content))

    def _drain_think_tail(self) -> None:
        """思考面板残尾兜底上屏。

        字符阈值只对"出字足够快"的模型有效；慢速模型下必须靠时间触发首次显示，
        否则面板会长时间空着。已经出过字之后改用更长的阈值，避免低速率下把
        思考内容切成一堆填不满的碎行。
        """
        try:
            panel = self.query_one("#think-panel", PanelLog)
        except Exception:
            return
        if not panel.has_pending_text:
            return
        timeout = self._THINK_STALL_TIMEOUT if panel.text_started else self._THINK_FLUSH_TIMEOUT
        if panel.pending_age >= timeout:
            panel.flush_text()

    def _h_tool_calls(self, e: dict) -> None:
        self._set_status("calling_tool")
        chat_panel = self.query_one("#chat-panel", ChatTranscript)
        tool_panel = self.query_one("#tool-panel", RichLog)
        think_panel = self.query_one("#think-panel", PanelLog)
        for c in e.get("calls", []):
            name = c["name"]
            think_panel.note_tool(name)  # 本轮调用的工具：折叠标题里要显示
            chat_panel.note(f"  calling {name}...")
            tool_panel.write(f"\n[bold yellow]→ {escape(name)}[/bold yellow]")
            try:
                args = json.loads(c.get("args", "{}"))
                tool_panel.write(f"  [dim]{escape(json.dumps(args, ensure_ascii=False)[:200])}[/dim]")
            except Exception:
                pass

    def _h_tool_result(self, e: dict) -> None:
        name = e.get("tool_name", "?")
        result = str(e.get("result", ""))
        panel = self.query_one("#tool-panel", RichLog)
        truncated = escape(result[:400])
        panel.write(f"  [bold green]<- {escape(name)}[/bold green] [dim]{truncated}[/dim]")
        if len(result) > 400:
            panel.write(f"  [dim]... ({len(result)} chars total)[/dim]")
        self._tool_call_count += 1
        if self._tool_call_count >= self._AUTOSAVE_INTERVAL:
            self._tool_call_count = 0
            self._autosave()

    def _h_self_check(self, e: dict) -> None:
        self.query_one("#tool-panel", RichLog).write(
            f"  [bold red]! {escape(e.get('tool_name', '?'))}:[/bold red] {escape(e.get('critique', ''))}"
        )

    def _h_worker_start(self, e: dict) -> None:
        self.query_one("#tool-panel", RichLog).write(
            f"\n[bold cyan]⏳ 子任务派发 → [/bold cyan][dim]{escape(e.get('task', ''))}[/dim]"
        )

    def _h_worker_done(self, e: dict) -> None:
        self.query_one("#tool-panel", RichLog).write(
            f"  [bold cyan]✅ 子任务完成:[/bold cyan] [dim]{escape(e.get('result', ''))}[/dim]"
        )

    def _h_response_chunk(self, e: dict) -> None:
        if self._status != "generating":
            self._set_status("generating")
            self.query_one("#chat-panel", ChatTranscript).start_assistant()
            self._stream_width = 0  # 新消息：重新量一次面板宽度
        transcript = self.query_one("#chat-panel", ChatTranscript)
        self._stream_buffer += e.get("content", "")
        # 完整行立即 commit
        while "\n" in self._stream_buffer:
            line, self._stream_buffer = self._stream_buffer.split("\n", 1)
            transcript.append_chunk(line + "\n")
        # 半行只在句末标点或接近面板宽度时 commit，避免逐 token 重渲染把文字切碎。
        # 面板宽度在一次流式期间不变，缓存以避免每个 token 都做布局查询（270 tok/s 下有意义）。
        if self._stream_width < 2:
            # 面板被别的面板挤成 0 宽时不能拿 2 cell 当阈值：那会退化成每个字符都
            # commit 一次、每次重渲染整段 Markdown（O(n²)），长回复直接把 UI 卡住。
            # 隐藏时用一个大的固定批量，重新可见后 Textual 会自己按新宽度重排。
            w = transcript.content_region.width
            self._stream_width = w if w >= 4 else 512
        if (len(self._stream_buffer) >= max(1, self._stream_width * 2 // 3)
                or self._stream_buffer.rstrip().endswith(("。", "！", "？", "!", "?", "；", ";"))):
            transcript.append_chunk(self._stream_buffer)
            self._stream_buffer = ""

    def _h_response_done(self, e: dict) -> None:
        # runner 当前不发此事件（见 loop_agent_core/runner.py:383），保留以兼容旧路径。
        content = e.get("content", "")
        transcript = self.query_one("#chat-panel", ChatTranscript)
        if content.strip():
            transcript.set_live_text(content)
        transcript.finish_message()

    def _h_confirm(self, e: dict) -> None:
        def done(result):
            self._confirm_queue.put(result)

        self.app.push_screen(
            ConfirmDialog(e.get("kind", "?"), e.get("target", "?"), bool(e.get("is_worker")),
                          e.get("purpose", "")),
            done,
        )

    def _h_status(self, e: dict) -> None:
        self._set_status(e.get("state", "idle"))

    # -- 上下文管理事件（压缩此前对用户完全不可见） --

    def _h_deduped(self, e: dict) -> None:
        """L0 去重：重复的工具结果被换成占位符（零信息损失的那一层）。"""
        self.query_one("#chat-panel", ChatTranscript).note(
            f"🧹 上下文去重：{e.get('replaced')} 处重复工具结果，省 ~{e.get('saved')} token")

    def _h_compacting(self, e: dict) -> None:
        """L1 摘要开始：每块一次非流式调用，会阻塞十几秒，必须先给反馈。"""
        self.query_one("#chat-panel", ChatTranscript).note(
            f"⏳ 正在压缩上下文（{e.get('blocks')} 块）…")

    def _h_compacted(self, e: dict) -> None:
        # 前缀稳定块数：摘要链里"逐字相接"的相邻块数（= 可复用前缀缓存的次数）。
        # 整块丢弃的改动若失效（例如又改回按字符偏移切），这个数会立刻掉到 0。
        stable = ""
        blocks = e.get("blocks") or 0
        if e.get("prefix_stable") is not None and blocks:
            stable = f"，前缀稳定 {e.get('prefix_stable')}/{max(0, blocks - 1)} 块"
            if e.get("prefix_dropped"):
                stable += f"（丢弃 {e.get('prefix_dropped')} 块）"
        self.query_one("#chat-panel", ChatTranscript).note(
            f"📦 上下文已压缩：{e.get('removed')} 条 → {e.get('after')} token"
            f"（原 {e.get('before')}，{blocks} 块）{stable}"
            + ("；收益偏低，已改用裁剪兜底" if e.get("low_reclaim") else ""))

    def _h_compact_failed(self, e: dict) -> None:
        """压缩失败：必须给出**具体**原因 + 下一步怎么做（只报"失败"等于让用户干瞪眼）。"""
        head = f"压缩失败（{e.get('reason')}）"
        if e.get("block") is not None:
            head += f" @第{e.get('block') + 1}块"
        if e.get("attempt"):
            head += f"，第 {e.get('attempt')}/{e.get('max_attempts')} 次"
        line = f"{head}：{e.get('hint') or '已回退到硬裁剪'}。"
        if e.get("disabled"):
            line += "（可用 /compact 手动重试）"
        elif e.get("retry_in_turns"):
            line += f"（{e['retry_in_turns']} 轮后再试）"
        self.query_one("#chat-panel", ChatTranscript).note(line, classes="notice")

    def _h_compact_skipped(self, e: dict) -> None:
        reason = loop_agent_v2.skip_label(e.get("reason"))
        if e.get("reason") == "failed" and e.get("fails") is not None:
            reason += f"（已连续失败 {e.get('fails')}/{e.get('max_fails')} 次）"
        elif e.get("reason") == "backoff" and e.get("retry_in_turns"):
            reason += f"（还需 {e['retry_in_turns']} 轮）"
        self.query_one("#chat-panel", ChatTranscript).note(f"跳过压缩（{reason}）", classes="note")

    def _h_notice(self, e: dict) -> None:
        """回合结束原因 / 催办提示（runner 的 notice 事件）。

        这是"静默停止"的正解：以前模型卡在只输出思考时，runner 的消息只 return、
        不上屏，面板看起来就是 agent 自己停了。warn 用 ⚠ 标出（失败的回合），
        info 用 ·（自愈动作，例如"已要求模型继续 1/3"）。
        """
        msg = str(e.get("message", "")).strip()
        if not msg:
            return
        icon = "⚠" if e.get("level") == "warn" else "·"
        self.query_one("#chat-panel", ChatTranscript).note(f"{icon} {msg}", classes="notice")

    # -- helpers --

    def _flush_stream(self) -> None:
        """定型流式消息：把半行残量补齐，然后收尾。"""
        transcript = self.query_one("#chat-panel", ChatTranscript)
        if self._stream_buffer:
            transcript.append_chunk(self._stream_buffer)
            self._stream_buffer = ""
        transcript.finish_message()

    def _flush_think(self) -> None:
        try:
            panel = self.query_one("#think-panel", PanelLog)
        except Exception:
            return
        panel.flush_text()

    def _set_status(self, state: str) -> None:
        self._status = state
        self._progress_target = self._PROGRESS_TARGET.get(state, 0)
        self.query_one("#progress-label", Static).update(self._PROGRESS_LABEL.get(state, ""))
        if state == "idle":
            self._progress_current = 0.0
            self.query_one("#progress-bar", ProgressBar).update(progress=0)
        self._update_footer()

    def _tick_progress(self) -> None:
        """每帧把进度条向当前阶段目标推进（30fps）；idle 时不动。"""
        if self._status == "idle":
            return
        self._progress_current += (self._progress_target - self._progress_current) * 0.07
        self.query_one("#progress-bar", ProgressBar).update(progress=self._progress_current)

    def _update_footer(self) -> None:
        icons = {
            "idle": "idle",
            "thinking": "thinking...",
            "calling_tool": "calling tool",
            "generating": "generating",
        }
        status = icons.get(self._status, self._status)
        tok = f"{self._token_total / 1000:.1f}k" if self._token_total else "0k"
        get_help = "/help"
        model_show = CURRENT_MODEL
        if _cfg._is_local_backend():
            if self._llm_reachable is None:
                model_show = f"{CURRENT_MODEL} (checking…)"
            elif not self._llm_reachable:
                model_show = f"{CURRENT_MODEL} (offline)"
        # 思考面板折叠后其边框不可见，徽标改在常驻底栏显示
        badge = ""
        if "think-panel" in self._collapsed and self._think_round_count:
            badge = f" | 🧠 {self._think_round_count}"
        # 前缀缓存命中率：provider 不报这个字段时**不显示**（不假装是 0%）
        cache_show = ""
        if isinstance(self._cache_ratio, (int, float)):
            cache_show = f" | 缓存 {self._cache_ratio * 100:.0f}%"
        self.query_one("#footer-bar", Static).update(
            f" {model_show} | {status} | {tok} tokens{cache_show}{badge} | Get-help: {get_help}"
        )

    def _update_think_badge(self) -> None:
        """刷新思考面板边框的计数徽标（累计思考轮次），并同步常驻底栏。

        面板边框在折叠时不可见，所以底栏另给一个紧凑指示；这里改完边框标题顺带刷
        一次底栏，保证两处计数始终一致。
        """
        n = self._think_round_count
        try:
            self.query_one("#think-panel", RichLog).border_title = (
                f" Thinking ({n}) " if n else " Thinking "
            )
        except Exception:
            pass
        try:
            self._update_footer()
        except Exception:
            pass

    def _check_llm_async(self) -> None:
        """后台检测本地 llama 服务，结果缓存到 _llm_reachable 后刷新底部栏。"""
        try:
            reachable, _detail = _cfg.check_local_llm()
        except Exception:
            reachable = False
        self._llm_reachable = reachable
        self.call_from_thread(self._update_footer)


if __name__ == "__main__":
    AgentTUI().run()
