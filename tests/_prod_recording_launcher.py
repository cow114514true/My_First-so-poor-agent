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
import os
import runpy
import sys

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

    llm.set_transport(BoomTransport() if mode == "boom" else FakeTransport())
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


def _run_tui():
    import tui

    app = tui.AgentTUI()
    # atexit 应急保存会往 conversations/ 写一份会话文件；用例不验它，别留垃圾。
    app._did_save = True

    async def pilot(p):
        await p.pause(1.5)
        app.query_one("#input-area").text = QUESTION
        await app.run_action("submit_input")
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
    _install_transport(mode)
    if entry == "cli":
        _run_cli(mode)
    elif entry == "tui":
        _run_tui()
    else:
        raise SystemExit(f"未知入口：{entry}")


if __name__ == "__main__":
    main()
