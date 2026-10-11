"""Self-check: 回合结束必须"看得见"（静默停止回归）. Run: python test_runner_visibility.py

背景（2026-10-11 20:52 真实事故）：本地/云端模型连续多轮只输出思考时，runner 会
注入 3 次 nudge、最后落一条 _STUCK_MSG —— 但这些**都只 return、不发事件**，而
tui._run_agent 又把 chat() 的返回值丢掉 → 面板上零输出、状态回 idle。
用户看到的就是"agent 静默停止工作"，4 次 API 调用完全不可见。

这组用例用假 client 驱动**真循环**，断言每一条终局路径都会发 notice 事件；
同时守住 edit_file 的锚点纠错提示（空行恒真的假候选）。
"""
import inspect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 导入期会构造 OpenAI client：给个占位 key，绝不出网（下面 client 全被替换）
os.environ.setdefault("DS_KEY", "sk-selftest-not-a-real-key")

import loop_agent_core.runner as runner  # noqa: E402
import loop_agent_v2 as la  # noqa: E402
from loop_agent_core.control import interrupt_event  # noqa: E402
from loop_agent_core.config import WORK_DIR  # noqa: E402

# 用例会真的走到"思考循环/乱码"分支：把诊断落盘改到 tmp/，别污染真实 debug_filtered.txt
_SELFTEST_LOG = os.path.join(WORK_DIR, "tmp", "_selftest_runner_visibility.log")
os.environ["FILTERED_LOG"] = _SELFTEST_LOG
if os.path.exists(_SELFTEST_LOG):  # 每次跑清空；跑完保留，失败时可直接看
    os.remove(_SELFTEST_LOG)


# ── 假流式 client ────────────────────────────────────────────────
class _Delta:
    def __init__(self, reasoning=None, content=None):
        self.reasoning_content = reasoning
        self.content = content
        self.tool_calls = None


class _Chunk:
    def __init__(self, delta=None):
        self.usage = None
        self.choices = [type("C", (), {"delta": delta})()] if delta is not None else []


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)

    def close(self):
        pass


class _FakeClient:
    """每次 create() 依次吐出预设的一批 chunk；用完后复用最后一批。"""

    def __init__(self, batches):
        self._batches = list(batches)
        self.calls = 0
        outer = self

        class _Completions:
            def create(self, **kwargs):
                idx = min(outer.calls, len(outer._batches) - 1)
                outer.calls += 1
                return _Stream(outer._batches[idx])

        self.chat = type("Chat", (), {"completions": _Completions()})()


def _conv():
    return {"messages": [{"role": "system", "content": "t"}], "size": None,
            "_archive": [], "_compact_state": {}}


def _run(batches, question="干活"):
    """在隔离 conv 上跑一次真循环（is_main=False：不注入记忆、不启摘要器）。"""
    conv = _conv()
    events = []
    saved = runner.client
    runner.client = _FakeClient(batches)
    try:
        out = la.chat(question, on_event=events.append, _conv=conv)
    finally:
        runner.client = saved
    return out, events, conv


def _notices(events, level=None):
    return [e["message"] for e in events
            if e.get("type") == "notice" and (level is None or e.get("level") == level)]


# ── 用例 ────────────────────────────────────────────────────────
def test_thinking_only_loop_is_visible():
    """只出思考 → 3 次 nudge 可见 + 终局 warn 可见 + 历史里留下 _STUCK_MSG。"""
    only_thinking = [_Chunk(_Delta(reasoning="我再想想，先不调用工具"))]
    out, events, conv = _run([only_thinking])

    assert out == runner._STUCK_MSG, out
    infos = _notices(events, "info")
    assert sum("只输出了思考过程" in m for m in infos) == 3, infos
    warns = _notices(events, "warn")
    assert any("连续多轮只输出思考" in m for m in warns), warns
    assert conv["messages"][-1]["content"] == runner._STUCK_MSG
    # 事件里必须同时有"回合结束原因"这条，而它以前只存在于 return 值里
    assert [e["type"] for e in events if e.get("type") == "notice"]


def test_interrupt_is_visible():
    """Esc 打断：不能只 return "[Interrupted by user]" 让面板空着。"""
    interrupt_event.set()
    try:
        out, events, conv = _run([[_Chunk(_Delta(reasoning="x"))]])
    finally:
        interrupt_event.clear()
    assert out == "[Interrupted by user]", out
    assert any("已中断" in m for m in _notices(events, "warn")), events


def test_empty_response_is_visible():
    """空流（既无正文也无思考）→ 乱码/过滤分支必须有 warn 提示。"""
    out, events, conv = _run([[_Chunk()]])
    assert "Response filtered" in out, out
    assert any("乱码" in m for m in _notices(events, "warn")), events


def _tool_call_chunk(name="read_file", args='{"path": "definitely_missing_file.txt"}'):
    d = _Delta()
    d.tool_calls = [type("T", (), {
        "index": 0, "id": "c1", "type": "function",
        "function": type("F", (), {"name": name, "arguments": args})()})()]
    return _Chunk(d)


def test_wrapup_nudge_is_visible():
    """工具轮次逼近上限时注入的收尾提示此前完全不可见（真实会话里模型收到过它）。"""
    # 同一个 batch 会被反复复用：循环一路调工具直到轮次上限，必然经过收尾提示那一轮
    out, events, conv = _run([[_tool_call_chunk()]])
    assert any("轮工具调用" in m for m in _notices(events)), _notices(events)


def test_tui_registers_notice_handler():
    """事件没有 handler 就等于没发：守住 tui 侧的接线。"""
    import tui
    assert hasattr(tui.AgentTUI, "_h_notice")
    src = inspect.getsource(tui.AgentTUI._handle_event)
    assert '"notice": self._h_notice' in src, src


def test_edit_hint_never_lists_blank_lines():
    """edit_file 找不到锚点时的候补行：空行不能占位（旧实现空行恒真）。"""
    os.makedirs("tmp", exist_ok=True)
    path = "tmp/_selftest_edit_hint.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("alpha\n\n\n    def target_function(self) -> None:\n        pass\n")
    try:
        msg = la.TOOL_CALL_MAP["edit_file"](
            path=path, old_string="def target_function(self) -> None:\n    return 1\n")
    finally:
        if os.path.exists(path):
            os.remove(path)
    assert "[Error]" in msg and "not found" in msg, msg
    assert "Possible close matches" in msg, msg
    candidate_lines = [ln for ln in msg.splitlines() if ln.strip().startswith("line ")]
    assert candidate_lines, msg
    for ln in candidate_lines:
        assert ln.split(":", 1)[1].strip(), f"候补行内容是空的：{ln!r}"
    assert any("target_function" in ln for ln in candidate_lines), msg


def _run_all():
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {name}: {e}")
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
