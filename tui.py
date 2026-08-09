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
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import RichLog, TextArea, Static, ProgressBar, Button
from textual.binding import Binding
from rich.markup import escape

from loop_agent_v2 import chat, messages as _agent_messages
from loop_agent_v2 import CURRENT_MODEL
import loop_agent_v2
import loop_agent_core.config as _cfg


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


class ConfirmDialog(Screen):
    """Modal: allow/deny a sensitive tool action (image read / file write / file edit)."""

    def __init__(self, kind: str, target: str, is_worker: bool = False) -> None:
        super().__init__()
        self._kind = kind
        self._target = target
        self._is_worker = is_worker

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
        title = "子任务请求" if self._is_worker else "确认操作"
        with Vertical(id="confirm-dialog"):
            yield Static(
                f"[bold]{title}[/bold]\n\n{self._kind}: [cyan]{escape(self._target)}[/cyan]",
                markup=True,
            )
            with Horizontal():
                yield Button("允许", id="allow", variant="primary")
                yield Button("拒绝", id="deny")
                yield Button("本会话放行", id="allow_session")

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


class PanelLog(RichLog):
    """RichLog with double-click detection for panel expansion + reflow on resize."""

    _MAX_HISTORY = 1000  # reflow 重播上限：防止长对话下 _history 无界增长拖垮 resize

    class ExpandRequest(Message):
        """Posted when panel is double-clicked."""
        def __init__(self, panel_id: str) -> None:
            self.panel_id = panel_id
            super().__init__()

    def __init__(self, *args, panel_id: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.panel_id = panel_id
        self._last_click: float = 0.0
        self._history: list = []  # 写入过的原始行，用于展开后按新宽度重排
        self._last_width: int = 0

    @staticmethod
    def _safe_strip(text):
        """尽力移除 Rich markup 标签，保留可见文本。"""
        return re.sub(r"\[/?[a-zA-Z#][^\]]*\]", "", str(text))

    def write(self, content, *args, **kwargs):
        self._history.append(content)
        if len(self._history) > self._MAX_HISTORY:
            del self._history[:-self._MAX_HISTORY]
        try:
            return super().write(content, *args, **kwargs)
        except Exception:
            # MarkupError 或任何渲染异常：剥离 markup 按纯文本写，绝不崩掉整个 TUI
            try:
                return super().write(self._safe_strip(content), *args, **kwargs)
            except Exception:
                return super().write("[dim](output omitted -- rendering error)[/dim]", *args, **kwargs)

    def clear(self):
        self._history.clear()
        return super().clear()

    def on_resize(self, event) -> None:
        w = event.size.width
        if w != self._last_width and w >= 2:
            self._last_width = w
            self.call_after_refresh(self.reflow)

    def reflow(self) -> None:
        """按当前面板宽度重排所有已上屏的行（RichLog 行一旦渲染宽度就固定，需整体重建）。"""
        y = self.scroll_offset.y
        super().clear()
        for line in self._history:
            try:
                super().write(line, scroll_end=False)
            except Exception:
                try:
                    super().write(self._safe_strip(line), scroll_end=False)
                except Exception:
                    super().write("[dim]...[/dim]", scroll_end=False)
        self.scroll_to(y=min(y, self.max_scroll_y), animate=False, immediate=True)

    def on_click(self, event) -> None:
        now = time.monotonic()
        if self._last_click and now - self._last_click < 0.4:
            self.post_message(self.ExpandRequest(self.panel_id))
        self._last_click = now


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
        Binding("`", "toggle_shell", "Shell"),
    ]

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

    def __init__(self):
        super().__init__()
        self._event_queue: queue.Queue = queue.Queue()
        self._shell_queue: queue.Queue = queue.Queue()
        self._confirm_queue: queue.Queue = queue.Queue()
        loop_agent_v2.confirm_mod._ask = self._confirm_ask
        self._agent_busy: bool = False
        self._token_total: int = 0
        self._status: str = "idle"
        self._stream_buffer: str = ""
        self._think_buffer: str = ""
        self._think_round = None
        self._shell_visible: bool = False
        self._expanded_panel: str = ""  # non-empty when a panel is expanded
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
                yield PanelLog(id="chat-panel", panel_id="chat-panel", wrap=True, markup=True, max_lines=5000)
                yield PanelLog(id="think-panel", panel_id="think-panel", wrap=True, markup=True, max_lines=5000)
                yield PanelLog(id="tool-panel", panel_id="tool-panel", wrap=True, markup=True, max_lines=5000)
            yield TextArea(id="input-area")
            with Horizontal(id="progress-row"):
                yield Static(id="progress-label")
                yield ProgressBar(id="progress-bar", total=100)
            yield Static(id="footer-bar")

    def on_mount(self) -> None:
        self.query_one("#chat-panel", RichLog).border_title = " Chat "
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
        if panel_id := panel_map.get(name):
            self.query_one(panel_id, RichLog).focus()

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

    def _toggle_expand(self, panel_id: str) -> None:
        if self._expanded_panel == panel_id:
            self._restore_panels()
        else:
            self._expand_panel(panel_id)

    def _expand_panel(self, panel_id: str) -> None:
        for pid in ("#chat-panel", "#think-panel", "#tool-panel"):
            p = self.query_one(pid, PanelLog)
            p.styles.width = "1fr" if pid == f"#{panel_id}" else "0"
        self._expanded_panel = panel_id

    def _restore_panels(self) -> None:
        for pid in ("#chat-panel", "#think-panel", "#tool-panel"):
            self.query_one(pid, PanelLog).styles.width = "1fr"
        self._expanded_panel = ""

    def action_collapse_panel(self) -> None:
        if self._agent_busy:
            # Esc 在 agent 运行时 = 请求中断（流式中断、长命令被杀）
            loop_agent_v2.interrupt_event.set()
            self.query_one("#chat-panel", RichLog).write("[bold red]🛑 已请求中断…[/bold red]")
            return
        if self._expanded_panel:
            self._restore_panels()

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

        # Clean up corrupted files from previous failed saves
        for f in os.listdir(save_dir):
            if f.startswith("chat_") and f.endswith(".json") and f != filename:
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
        chat_panel = self.query_one("#chat-panel", RichLog)
        # 加载历史前自动保存当前会话，避免被覆盖丢失
        if self._has_content():
            path = self._save_conversation()
            chat_panel.write(f"[dim]Auto-saved current → {escape(os.path.basename(path))}[/dim]")
        arg = question[5:].strip()
        os.makedirs(self._save_dir, exist_ok=True)

        all_files = sorted(
            [f for f in os.listdir(self._save_dir) if f.startswith("chat_") and f.endswith(".json")],
            reverse=True,
        )

        if not all_files:
            chat_panel.write("[dim]No saved conversations found.[/dim]")
            return

        # Separate valid from corrupted
        valid_files = []
        corrupted_files = []
        for f in all_files:
            filepath = os.path.join(self._save_dir, f)
            try:
                with open(filepath, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if not isinstance(data, list):
                    corrupted_files.append(f)
                else:
                    valid_files.append((f, data))
            except Exception:
                corrupted_files.append(f)

        # Number selection → /load N (only counts valid files)
        try:
            idx = int(arg)
            if 1 <= idx <= len(valid_files):
                self._load_file(os.path.join(self._save_dir, valid_files[idx - 1][0]))
                return
            chat_panel.write(f"[bold red]Invalid number: {idx} (1-{len(valid_files)})[/bold red]")
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
            chat_panel.write(f"[bold red]Not found:[/bold red] {escape(arg)}")
            return

        # No arg → list all with title preview
        chat_panel.write("[bold]Saved conversations:[/bold]\n")
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
            chat_panel.write(f"  [bold]{i}[/bold]  {escape(label)}")
            if title:
                chat_panel.write(f"       [dim]\"{escape(title)}\"[/dim]")
            else:
                chat_panel.write(f"       [dim](empty)[/dim]")

        if corrupted_files:
            chat_panel.write(f"\n[dim yellow]Corrupted files (will be skipped on save cleanup):[/dim yellow]")
            for f in corrupted_files:
                chat_panel.write(f"  [dim]✗ {escape(f)}[/dim]")

        if valid_files:
            chat_panel.write(f"\n[dim]Usage: /load <number> (1-{len(valid_files)})[/dim]")

    def _load_file(self, filepath: str) -> None:
        """Load a conversation file and rebuild UI."""
        chat_panel = self.query_one("#chat-panel", RichLog)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception as e:
            chat_panel.write(f"[bold red]Failed to load:[/bold red] {escape(str(e))}")
            return

        _agent_messages.clear()
        _agent_messages.extend(loaded)

        chat_panel.clear()
        chat_panel.write(f"[dim]--- Loaded {escape(os.path.basename(filepath))} ---[/dim]")
        for msg in loaded:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                chat_panel.write(f"[bold white]You:[/bold white] {escape(content)}")
            elif role == "assistant":
                if len(content) > 500:
                    content = escape(content[:500]) + f"\n[dim]... ({len(content)} chars)[/dim]"
                else:
                    content = escape(content)
                chat_panel.write(f"[bold]Assistant:[/bold]\n{content}")
            elif role == "tool":
                chat_panel.write(f"[dim]  tool result ({len(content)} chars)[/dim]")

        self.query_one("#think-panel", RichLog).clear()
        self.query_one("#tool-panel", RichLog).clear()
        self._token_total = 0
        self._update_footer()

    def _try_recover_autosave(self) -> None:
        """启动时检测 .autosave.json，有可恢复内容则弹恢复对话框。"""
        autosave_path = os.path.join(self._save_dir, ".autosave.json")
        if not os.path.exists(autosave_path):
            return
        try:
            with open(autosave_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list) or len(data) <= 1:
                return
        except Exception:
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
        chat_panel = self.query_one("#chat-panel", RichLog)
        if result == "restore":
            _agent_messages.clear()
            _agent_messages.extend(self._recovery_data)
            chat_panel.write(f"[dim]--- Recovered {len(self._recovery_data)} messages from autosave ---[/dim]")
            for m in self._recovery_data:
                role = m.get("role", "")
                content = m.get("content", "")
                if role == "user":
                    chat_panel.write(f"[bold white]You:[/bold white] {escape(content)}")
                elif role == "assistant" and content:
                    chat_panel.write(f"[bold]Assistant:[/bold]\n{escape(content[:300])}")
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
            for pid in ("#chat-panel", "#think-panel", "#tool-panel"):
                self.query_one(pid, RichLog).clear()
            self.query_one("#chat-panel", RichLog).write("[dim]--- Conversation cleared ---[/dim]")
            self._token_total = 0
            self._update_footer()
            return

        # /load [name] — load a saved conversation
        if question.startswith("/load"):
            self._cmd_load(question)
            return

        # /quit — safe quit with save dialog
        if question == "/quit":
            self.action_quit()
            return

        # /save — save current conversation now
        if question == "/save":
            if self._has_content():
                path = self._save_conversation()
                self.query_one("#chat-panel", RichLog).write(
                    f"[dim]Saved → {escape(os.path.basename(path))}[/dim]")
            else:
                self.query_one("#chat-panel", RichLog).write("[dim]Nothing to save.[/dim]")
            return

        if question == "/help":
            chat_panel = self.query_one("#chat-panel",RichLog)
            help_txt = """================================================================================
                         Local Agent TUI 帮助文档
================================================================================

当前模型：[cyan]{CURRENT_MODEL}[/cyan]

键盘快捷键
  1           聚焦聊天面板
  2           聚焦思考面板
  3           聚焦工具面板
  i           聚焦输入框
  Tab         切换焦点到下一个组件
  Ctrl+J      发送消息（Enter 换行）
  `           展开/收起 Shell 终端面板
  Esc         折叠展开的面板
  双击面板     展开/恢复该面板

命令
  /clear      清空对话历史，重置上下文
  /load       列出所有可加载的对话（加载前自动保存当前会话）
  /load N     加载指定编号的对话
  /save       立即保存当前对话到 conversations/
  /quit       安全退出（可保存对话）
  /help       显示此帮助信息

系统功能
  - 后端：local-3.5 / local-3.6 (Qwen 9B/35B) 或 DeepSeek
  - 跨会话记忆：recall/remember 工具
  - 11 个工具：9 个基础工具 + recall + remember
  - 纯实现位于 loop_agent_core/ 包，loop_agent_v2.py 为薄壳

================================================================================"""
            chat_panel.write(help_txt.format(CURRENT_MODEL=loop_agent_v2.CURRENT_MODEL))
            return
        textarea.disabled = True
        self._agent_busy = True

        chat_panel = self.query_one("#chat-panel", RichLog)
        chat_panel.write(f"[bold white]You:[/bold white] {escape(question)}")

        self._set_status("thinking")

        threading.Thread(target=self._run_agent, args=(question,), daemon=True).start()

    def _confirm_ask(self, kind, target, is_worker):
        """Runs in agent thread. Emits a confirm_request event, blocks for the TUI answer."""
        self._event_queue.put({"type": "confirm_request", "kind": kind, "target": target, "is_worker": is_worker})
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
        # Drain agent event queue
        while True:
            try:
                event = self._event_queue.get_nowait()
            except queue.Empty:
                break
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
                    shell_log.write(f"[dim]--- exit code: {escape(data)} ---[/dim]")

        # 推进进度条
        self._tick_progress()

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
        self._update_footer()

    def _h_thinking(self, e: dict) -> None:
        content = e.get("content", "")
        if not content:
            return
        r = e.get("round", "?")
        panel = self.query_one("#think-panel", RichLog)
        if len(content) > 300:
            panel.write(f"\n[dim]── Round {r} (enter to expand) ──[/dim]")
            panel.write(f"{escape(content[:300])}\n[dim]... ({len(content)} chars)[/dim]")
        else:
            panel.write(f"\n[dim]── Round {r} ──[/dim]")
            panel.write(escape(content))

    def _h_thinking_chunk(self, e: dict) -> None:
        content = e.get("content", "")
        if not content:
            return
        rnd = e.get("round", self._think_round)
        panel = self.query_one("#think-panel", RichLog)
        if rnd != self._think_round:
            # 新轮次：flush 上一轮残量 + 分隔线
            if self._think_buffer:
                panel.write(self._think_buffer)
                self._think_buffer = ""
            if self._think_round is not None:
                panel.write(f"\n[dim]── Round {rnd} ──[/dim]")
            self._think_round = rnd
        self._think_buffer += escape(content)
        # 完整行立即上屏；半行在句末标点或接近面板宽度时才 commit（同 chat 面板）
        while "\n" in self._think_buffer:
            line, self._think_buffer = self._think_buffer.split("\n", 1)
            panel.write(line)
        width = panel.content_region.width
        if (len(self._think_buffer) >= max(1, 2*width // 3)
                or self._think_buffer.rstrip().endswith(("。", "！", "？", "!", "?", "；", ";"))):
            panel.write(self._think_buffer)
            self._think_buffer = ""

    def _h_tool_calls(self, e: dict) -> None:
        self._set_status("calling_tool")
        chat_panel = self.query_one("#chat-panel", RichLog)
        tool_panel = self.query_one("#tool-panel", RichLog)
        for c in e.get("calls", []):
            name = c["name"]
            chat_panel.write(f"[dim]  calling {escape(name)}...[/dim]")
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
            self.query_one("#chat-panel", RichLog).write("\n[bold]Assistant:[/bold]")
        self._stream_buffer += escape(e.get("content", ""))
        chat = self.query_one("#chat-panel", RichLog)
        # 完整行（含换行）立即上屏
        while "\n" in self._stream_buffer:
            line, self._stream_buffer = self._stream_buffer.split("\n", 1)
            chat.write(line)
        # 半行只在句末标点或接近面板宽度时 commit，避免逐 token 写把文字切碎
        width = chat.content_region.width
        if (len(self._stream_buffer) >= max(1, width*2 // 3)
                or self._stream_buffer.rstrip().endswith(("。", "！", "？", "!", "?", "；", ";"))):
            chat.write(self._stream_buffer)
            self._stream_buffer = ""

    def _h_response_done(self, e: dict) -> None:
        content = e.get("content", "")
        self.query_one("#chat-panel", RichLog).write(f"\n[bold]Assistant:[/bold]\n{escape(content)}")

    def _h_confirm(self, e: dict) -> None:
        def done(result):
            self._confirm_queue.put(result)

        self.app.push_screen(
            ConfirmDialog(e.get("kind", "?"), e.get("target", "?"), bool(e.get("is_worker"))),
            done,
        )

    def _h_status(self, e: dict) -> None:
        self._set_status(e.get("state", "idle"))

    # -- helpers --

    def _flush_stream(self) -> None:
        if self._stream_buffer.strip():
            self.query_one("#chat-panel", RichLog).write(self._stream_buffer)
        self._stream_buffer = ""

    def _flush_think(self) -> None:
        if self._think_buffer:
            self.query_one("#think-panel", RichLog).write(self._think_buffer)
        self._think_buffer = ""

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
        self.query_one("#footer-bar", Static).update(
            f" {model_show} | {status} | {tok} tokens | Get-help: {get_help}"
        )

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
