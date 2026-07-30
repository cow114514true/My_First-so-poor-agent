"""
TUI for loop_agent_v2.py — three-panel chat interface powered by Textual.
Run: python tui.py
"""

import queue
import threading
import json
import os
import time
from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import RichLog, TextArea, Static
from textual.binding import Binding

from loop_agent_v2 import chat, messages as _agent_messages
import loop_agent_v2


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


class PanelLog(RichLog):
    """RichLog with double-click detection for panel expansion."""

    class ExpandRequest(Message):
        """Posted when panel is double-clicked."""
        def __init__(self, panel_id: str) -> None:
            self.panel_id = panel_id
            super().__init__()

    def __init__(self, *args, panel_id: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.panel_id = panel_id
        self._last_click: float = 0.0

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
        Binding("ctrl+c", "noop", "", show=False),
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
    """

    def __init__(self):
        super().__init__()
        self._event_queue: queue.Queue = queue.Queue()
        self._shell_queue: queue.Queue = queue.Queue()
        self._agent_busy: bool = False
        self._token_total: int = 0
        self._status: str = "idle"
        self._stream_buffer: str = ""
        self._shell_visible: bool = False
        self._expanded_panel: str = ""  # non-empty when a panel is expanded

    def compose(self) -> ComposeResult:
        with Vertical():
            yield RichLog(id="shell-output", wrap=True, markup=True, max_lines=2000)
            with Horizontal(id="main-panels"):
                yield PanelLog(id="chat-panel", panel_id="chat-panel", wrap=True, markup=True, max_lines=5000)
                yield PanelLog(id="think-panel", panel_id="think-panel", wrap=True, markup=True, max_lines=5000)
                yield PanelLog(id="tool-panel", panel_id="tool-panel", wrap=True, markup=True, max_lines=5000)
            yield TextArea(id="input-area")
            yield Static(id="footer-bar")

    def on_mount(self) -> None:
        self.query_one("#chat-panel", RichLog).border_title = " Chat "
        self.query_one("#think-panel", RichLog).border_title = " Thinking "
        self.query_one("#tool-panel", RichLog).border_title = " Tools "
        self._update_footer()
        self.set_interval(1 / 30, self._poll_queue)
        self.query_one("#input-area", TextArea).focus()

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

    def action_noop(self) -> None:
        """Swallow Ctrl+C — use /quit to exit properly."""
        pass

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

        return filepath

    @property
    def _save_dir(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversations")

    def _cmd_load(self, question: str) -> None:
        chat_panel = self.query_one("#chat-panel", RichLog)
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
            chat_panel.write(f"[bold red]Not found:[/bold red] {arg}")
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
            chat_panel.write(f"  [bold]{i}[/bold]  {label}")
            if title:
                chat_panel.write(f"       [dim]\"{title}\"[/dim]")
            else:
                chat_panel.write(f"       [dim](empty)[/dim]")

        if corrupted_files:
            chat_panel.write(f"\n[dim yellow]Corrupted files (will be skipped on save cleanup):[/dim yellow]")
            for f in corrupted_files:
                chat_panel.write(f"  [dim]✗ {f}[/dim]")

        if valid_files:
            chat_panel.write(f"\n[dim]Usage: /load <number> (1-{len(valid_files)})[/dim]")

    def _load_file(self, filepath: str) -> None:
        """Load a conversation file and rebuild UI."""
        chat_panel = self.query_one("#chat-panel", RichLog)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception as e:
            chat_panel.write(f"[bold red]Failed to load:[/bold red] {e}")
            return

        _agent_messages.clear()
        _agent_messages.extend(loaded)

        chat_panel.clear()
        chat_panel.write(f"[dim]--- Loaded {os.path.basename(filepath)} ---[/dim]")
        for msg in loaded:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                chat_panel.write(f"[bold white]You:[/bold white] {content}")
            elif role == "assistant":
                if len(content) > 500:
                    content = content[:500] + f"\n[dim]... ({len(content)} chars)[/dim]"
                chat_panel.write(f"[bold]Assistant:[/bold]\n{content}")
            elif role == "tool":
                chat_panel.write(f"[dim]  tool result ({len(content)} chars)[/dim]")

        self.query_one("#think-panel", RichLog).clear()
        self.query_one("#tool-panel", RichLog).clear()
        self._token_total = 0
        self._update_footer()

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

        if question == "/help":
            chat_panel = self.query_one("#chat-panel",RichLog)
            help_txt = """
键盘快捷键:
║  1  — 聚焦聊天面板                                     
║  2  — 聚焦思考面板                                     
║  3  — 聚焦工具面板                                     
║  i  — 聚焦输入框                                          
║  Tab — 切换到下一个组件                                 
║  Ctrl+J — 发送消息   
║  `  — 切换终端输出面板                                 
║  Esc — 折叠展开的面板                                  
║  双击面板标题 — 展开/恢复该面板                        
╠════════════════════════════════════════════════════════╣
║ 命令：                                                 
║  /clear       — 清空对话历史                           
║  /load [编号] — 加载保存的对话                         
║  /load        — 列出所有可加载的对话                   
║  /quit        — 安全退出（可保存对话）
║  /help        — 显示此帮助信息     
            """
            chat_panel.write(help_txt)
            return
        textarea.disabled = True
        self._agent_busy = True

        chat_panel = self.query_one("#chat-panel", RichLog)
        chat_panel.write(f"[bold white]You:[/bold white] {question}")

        self._set_status("thinking")

        threading.Thread(target=self._run_agent, args=(question,), daemon=True).start()

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
                    shell_log.write(f"[bold yellow]$ {data}[/bold yellow]")
                elif kind == "shell_line":
                    shell_log.write(data.rstrip("\n"))
                elif kind == "shell_done":
                    shell_log.write(f"[dim]--- exit code: {data} ---[/dim]")

    def _handle_event(self, event: dict) -> None:
        etype = event.get("type")

        if etype == "__agent_done__":
            self._flush_stream()
            self._agent_busy = False
            self._set_status("idle")
            self.query_one("#input-area", TextArea).disabled = False
            self.query_one("#input-area", TextArea).focus()
            return

        if etype == "error":
            self.query_one("#tool-panel", RichLog).write(
                f"[bold red]ERROR:[/bold red] {event.get('message')}"
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
            panel.write(f"{content[:300]}\n[dim]... ({len(content)} chars)[/dim]")
        else:
            panel.write(f"\n[dim]── Round {r} ──[/dim]")
            panel.write(content)

    def _h_thinking_chunk(self, e: dict) -> None:
        content = e.get("content", "")
        if content:
            self.query_one("#think-panel", RichLog).write(content)

    def _h_tool_calls(self, e: dict) -> None:
        self._set_status("calling_tool")
        chat_panel = self.query_one("#chat-panel", RichLog)
        tool_panel = self.query_one("#tool-panel", RichLog)
        for c in e.get("calls", []):
            name = c["name"]
            chat_panel.write(f"[dim]  calling {name}...[/dim]")
            tool_panel.write(f"\n[bold yellow]→ {name}[/bold yellow]")
            try:
                args = json.loads(c.get("args", "{}"))
                tool_panel.write(f"  [dim]{json.dumps(args, ensure_ascii=False)[:200]}[/dim]")
            except Exception:
                pass

    def _h_tool_result(self, e: dict) -> None:
        name = e.get("tool_name", "?")
        result = str(e.get("result", ""))
        panel = self.query_one("#tool-panel", RichLog)
        truncated = result[:400]
        panel.write(f"  [bold green]<- {name}[/bold green] [dim]{truncated}[/dim]")
        if len(result) > 400:
            panel.write(f"  [dim]... ({len(result)} chars total)[/dim]")

    def _h_self_check(self, e: dict) -> None:
        self.query_one("#tool-panel", RichLog).write(
            f"  [bold red]! {e.get('tool_name', '?')}:[/bold red] {e.get('critique', '')}"
        )

    def _h_response_chunk(self, e: dict) -> None:
        if self._status != "generating":
            self._set_status("generating")
            self.query_one("#chat-panel", RichLog).write("\n[bold]Assistant:[/bold]")
        self._stream_buffer += e.get("content", "")
        while "\n" in self._stream_buffer:
            line, self._stream_buffer = self._stream_buffer.split("\n", 1)
            self.query_one("#chat-panel", RichLog).write(line)

    def _h_response_done(self, e: dict) -> None:
        content = e.get("content", "")
        self.query_one("#chat-panel", RichLog).write(f"\n[bold]Assistant:[/bold]\n{content}")

    def _h_status(self, e: dict) -> None:
        self._set_status(e.get("state", "idle"))

    # -- helpers --

    def _flush_stream(self) -> None:
        if self._stream_buffer.strip():
            self.query_one("#chat-panel", RichLog).write(self._stream_buffer)
        self._stream_buffer = ""

    def _set_status(self, state: str) -> None:
        self._status = state
        self._update_footer()

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
        self.query_one("#footer-bar", Static).update(
            f" deepseek-v4-flash | {status} | {tok} tokens | Get-help: {get_help}"
        )


if __name__ == "__main__":
    AgentTUI().run()
