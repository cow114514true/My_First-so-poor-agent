"""生产入口录制用例的子进程启动器（tests/test_prod_recording.py 专用）。

为什么必须开一个**新进程**：录制开关是进程级的事实（环境变量 + 模块级 writer），
同一个测试进程里连着跑「开着」和「关着」两种，前一次的 writer 会漏给后一次；而
「真实会话到底录不录得下来」这件事，只有跨过真实的进程边界才算验过。

假后端是**唯一**被替换的东西：`llm.set_transport()` 是项目本来就留的注入缝
（`loop_agent_core/llm.py`），入口函数、runner、trace 发射点全是生产代码。换它是因为
硬约束不许触网，不是因为要绕开某个入口——两个入口都是原样执行的：

    cli → runpy.run_path("loop_agent_v2.py", run_name="__main__")
    tui → AgentTUI().run(headless=True, auto_pilot=...)（就是 `python tui.py` 那一行）

用法：python tests/_prod_recording_launcher.py <entry> [mode]
    entry ∈ {cli, tui}
    mode  ∈ {ok, boom, kb}，默认 ok
        ok    正常一问一答（要一个工具调用，再给正文）
        boom  传输层 create() 直接抛异常 → runner 记 api_error → 失败现场
        kb    第二句 input() 抛 KeyboardInterrupt（**就是**用户按 Ctrl-C 的路径）
"""

import builtins
import json
import os
import runpy
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

QUESTION = "用 get_date 工具告诉我今天是哪天"


# ── 假后端（形状与 tests/fixtures/replay/_regenerate.py 一致） ──────
class _Delta:
    def __init__(self, reasoning=None, content=None, tool_calls=None):
        self.reasoning_content = reasoning
        self.content = content
        self.tool_calls = tool_calls


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, index, cid, name=None, arguments=None):
        self.index = index
        self.id = cid
        self.function = _Fn(name, arguments)


class _Choice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason
        self.message = delta


class _Usage:
    prompt_tokens = 100
    completion_tokens = 5
    total_tokens = 105
    prompt_cache_hit_tokens = 40
    prompt_cache_miss_tokens = 60


class _Chunk:
    def __init__(self, delta=None, finish_reason=None, usage=False, no_choices=False):
        self.choices = [] if no_choices else [_Choice(delta or _Delta(), finish_reason)]
        self.usage = _Usage() if usage else None


class _Stream:
    def __init__(self, chunks):
        self._it = iter(chunks)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close(self):
        pass


# 慢流：每个 chunk 之间停一下，用来在"真的还在流式生成"这一瞬间按 Esc。
_SLOW_STEP = 0.25


class _SlowStream:
    """把一批 chunk 拉长到 `chunks × _SLOW_STEP` 秒，好让用例在流中按 Esc。

    为什么需要它：runner 只在 `for chunk in response:` 的**循环顶**查中断标记
    （`runner.py:1050`）。一次性把 chunk 全吐完，等 Esc 到达时循环早结束了，
    查的是「下一轮请求前」那个检查点，验不到流式中断。
    """

    def __init__(self, chunks):
        self._it = iter(chunks)

    def __iter__(self):
        return self

    def __next__(self):
        time.sleep(_SLOW_STEP)
        return next(self._it)

    def close(self):
        pass


class SlowTransport:
    """只有一轮、慢慢吐正文的假后端——供 Esc 中断用例使用。"""

    def __init__(self, max_calls=3):
        self._max = max_calls
        self._batches = [
            [_Chunk(_Delta(content="前半句")),
             _Chunk(_Delta(content="，后半句"), finish_reason="stop"),
             _Chunk(usage=True, no_choices=True)],
        ]
        self.calls = 0

    def create(self, kind, cfg, kwargs):
        idx = min(self.calls, len(self._batches) - 1)
        self.calls += 1
        if self.calls > self._max:          # 兜底：中断没送达时也别无限要流
            raise RuntimeError(f"SlowTransport 已被调用 {self.calls} 次，超过上限 {self._max}")
        return _SlowStream(self._batches[idx])


class FakeTransport:
    """第 1 轮要一个 get_date 工具调用，第 2 轮给正文。"""

    def __init__(self):
        self._batches = [
            [_Chunk(_Delta(tool_calls=[_ToolCall(0, "c1", "get_date", "{}")])),
             _Chunk(_Delta(), finish_reason="tool_calls"),
             _Chunk(usage=True, no_choices=True)],
            [_Chunk(_Delta(content="今天是 2026-01-01（假后端剧本）。"), finish_reason="stop"),
             _Chunk(usage=True, no_choices=True)],
        ]
        self.calls = 0

    def create(self, kind, cfg, kwargs):
        idx = min(self.calls, len(self._batches) - 1)
        self.calls += 1
        return _Stream(self._batches[idx])


class BoomTransport:
    """传输层当场抛异常：验证失败会话也收尾、也留现场。"""

    def create(self, kind, cfg, kwargs):
        raise RuntimeError("假后端故意炸掉（验证失败收尾）")


def _install_transport(mode):
    from loop_agent_core import llm

    if mode == "boom":
        llm.set_transport(BoomTransport())
    elif mode == "slow":
        llm.set_transport(SlowTransport())
    else:
        llm.set_transport(FakeTransport())
    # 工具**不**替换：get_date 就用真实现，于是录制时的日期与回放时按 trace_epoch
    # 注入的日期是同一个，录下来的会话可以直接 extract-cassette → replay。替换工具
    # 会让回放对不上号（录制侧前缀/取值与回放侧不一致），那种"录得下来但喂不进回放"
    # 的现场是自欺欺人。


def _run_cli(mode):
    if mode == "kb":
        # 真实循环读的就是 builtins.input。第二句把它变成 KeyboardInterrupt，
        # 等价于用户在 `😎 You:` 上按 Ctrl-C——异常**穿出**录制壳，由 __exit__ 收尾。
        state = {"n": 0}

        def fake_input(prompt=""):
            state["n"] += 1
            if state["n"] == 1:
                return QUESTION
            raise KeyboardInterrupt()

        builtins.input = fake_input
        runpy.run_path(os.path.join(ROOT, "loop_agent_v2.py"), run_name="__main__")
        return

    os.environ.setdefault("_PRODREC_STDIN", "1")
    runpy.run_path(os.path.join(ROOT, "loop_agent_v2.py"), run_name="__main__")


def _run_tui(mode="ok", esc_probe="", max_calls=3):
    """跑一次真 TUI 入口。

    `mode == "esc"`：用慢流后端，等 agent 真的进入流式生成后按一次 Esc——走的是
    用户在真终端里按 Esc 的同一条路（`tui.py:1405 action_collapse_panel` →
    置 `interrupt_event` → runner 在流循环顶看见它 → `turn_end:reason=interrupted`）。
    `esc_probe` 非空时把「Esc 按键那一刻 agent 是否在忙、中断标记是否真的被置起」
    写进那个文件：这两件事在按键之后就被清掉了（`tui.py:1919-1920`），事后从 trace
    里读不出来。
    """
    import tui

    app = tui.AgentTUI()
    # atexit 应急保存会往 conversations/ 写一份会话文件；用例不验它，别留垃圾。
    app._did_save = True

    async def pilot(p):
        await p.pause(1.5)
        if mode == "slow":
            # `interrupt_event` 是**进程级**全局（`loop_agent_core/control.py:4`），
            # 同进程里前一次会话留下的置位会漏到这里——第一次实测就是被它坑的：
            # 回合在开跑前就撞上中断检查、直接结束，于是"按 Esc"根本没落在在飞的回合上。
            # 真 TUI 里由 `tui.py:1919-1920` 在每轮结束时清；用例里得自己保证起点干净。
            from loop_agent_v2 import interrupt_event
            interrupt_event.clear()
        app.query_one("#input-area").text = QUESTION
        await app.run_action("submit_input")
        if mode == "slow":
            seen_busy = False
            # 等 agent 真的进到"在跑"这个窗口再按。固定睡一会儿不可靠：提交动作与线程
            # 起来之间隔着一次事件循环调度，抢跑就会按在窗口之外。
            for _ in range(60):                     # 最多等 3 秒
                await p.pause(0.05)
                if getattr(app, "_agent_busy", False):
                    seen_busy = True
                    break
            busy_at_key = bool(getattr(app, "_agent_busy", False))
            app.query_one("#input-area").focus()
            await p.press("escape")
            key_path = bool(interrupt_event.is_set())
            # 关键：动作里置起的中断标记会被 **agent 线程消费掉**（`tui.py:1919-1920`）。
            # 所以判据必须在**同步**代码里读——`await` 一让出事件循环，agent 线程就可能
            # 把标记清掉，那时候再读只看到 False，会把一次成功的中断误判成没生效。
            app.action_collapse_panel()             # 与 Esc 绑定同一个动作
            set_now = bool(interrupt_event.is_set())
            if esc_probe:
                with open(esc_probe, "w", encoding="utf-8") as fp:
                    json.dump({"seen_busy": seen_busy,
                               "busy_at_key": busy_at_key,
                               "focus": str(getattr(app.focused, "id", "")) if app.focused else "",
                               "key_path": key_path,
                               "set_after_key": set_now}, fp)
        for _ in range(300):
            await p.pause(0.1)
            if not getattr(app, "_agent_busy", False):
                break
        await p.pause(0.5)
        app.exit()

    app.run(headless=True, auto_pilot=pilot)


def main():
    entry = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "ok"
    esc_probe = sys.argv[3] if len(sys.argv) > 3 else ""
    _install_transport(mode)
    if entry == "cli":
        _run_cli(mode)
    elif entry == "tui":
        _run_tui(mode, esc_probe)
    else:
        raise SystemExit(f"未知入口：{entry}")


if __name__ == "__main__":
    main()
