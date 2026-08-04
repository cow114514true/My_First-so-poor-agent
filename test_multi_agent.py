"""Self-check for the multi-agent (orchestrator-worker) feature in loop_agent_v2.py. Run: python test_multi_agent.py"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import loop_agent_v2 as la


def test_tool_definition():
    names = {t["function"]["name"] for t in la.tools}
    assert "delegate_task" in names
    # delegate_task 不在 TOOL_CALL_MAP 中 —— 由 _run_tools 特判执行，worker 无法通过普通分发调用
    assert "delegate_task" not in la.TOOL_CALL_MAP


def test_worker_system_prompt():
    assert "Sub-agent mode" in la.WORKER_SYSTEM_PROMPT
    assert "Do not call delegate_task" in la.WORKER_SYSTEM_PROMPT


def test_recursion_guard():
    la._in_worker = True
    try:
        res = la.delegate_task_mock("nested task")
        assert "[Error]" in res and "not allowed" in res
    finally:
        la._in_worker = False
    assert la._in_worker is False  # 守卫已复位


def test_delegate_dispatch_and_events():
    """_run_tools 特判 delegate_task：发 worker_start/worker_done + tool_result，结果入 conv。"""
    orig = la.delegate_task_mock
    la.delegate_task_mock = lambda task: "WORKER_RESULT_DONE"
    la.messages[:] = [{"role": "system", "content": "sys"}]
    la._msg_size = None
    emitted = []
    conv = {"messages": la.messages, "size": la._msg_size}
    try:
        calls = la._xml_to_tool_calls([{"name": "delegate_task", "arguments": json.dumps({"task": "Do X"})}])
        la._run_tools(calls, emitted.append, conv)
        la._msg_size = conv["size"]
    finally:
        la.delegate_task_mock = orig
    types = [e["type"] for e in emitted]
    assert types[:3] == ["worker_start", "worker_done", "tool_result"], types
    assert emitted[0]["task"] == "Do X"
    assert emitted[1]["result"] == "WORKER_RESULT_DONE"
    assert emitted[2]["tool_name"] == "delegate_task"
    assert emitted[2]["result"] == "WORKER_RESULT_DONE"
    assert conv["messages"][-1]["role"] == "tool"  # 结果落库进 conv
    assert "self_check" not in types  # worker 短结果不触发质疑
    la.messages[:] = [{"role": "system", "content": "sys"}]
    la._msg_size = None


def test_worker_isolated_context():
    """delegate_task_mock 用全新 messages 调 chat（隔离），不碰全局，完成后复位守卫。"""
    captured = {}

    def fake_chat(question, on_event=None, _conv=None):
        captured["question"] = question
        captured["conv"] = _conv
        captured["on_event"] = on_event
        return "ISOLATED_RESULT"

    orig_chat = la.chat
    la.chat = fake_chat
    la._in_worker = False
    try:
        res = la.delegate_task_mock("isolated task")
    finally:
        la.chat = orig_chat
    assert res == "ISOLATED_RESULT"
    assert captured["question"] == "isolated task"
    conv = captured["conv"]
    assert conv is not None and conv["messages"] is not la.messages  # 隔离列表
    assert [m["role"] for m in conv["messages"]] == ["system"]
    assert conv["messages"][0]["content"] == la.WORKER_SYSTEM_PROMPT
    assert captured["on_event"] is not None  # worker 内部事件被抑制（黑盒）
    assert la._in_worker is False


def test_validate():
    ok, crit = la.validate_tool_result("delegate_task", "Done.")
    assert ok and crit == ""  # 短结果豁免
    ok, crit = la.validate_tool_result("delegate_task", "[Error] sub-agent failed")
    assert not ok and "Sub-agent failed" in crit
    ok, _ = la.validate_tool_result("delegate_task", "")
    assert not ok


def test_chat_impl_passes_conv_messages():
    """chat_impl 的 API 调用必须传 conv['messages']（worker 隔离 bug 回归）。
    worker 用独立列表，主会话用全局 messages，两者不可混传。"""
    import loop_agent_core.runner as runner_mod

    class FakeChunk:
        def __init__(self):
            self.choices = [types.SimpleNamespace(delta=types.SimpleNamespace(
                reasoning_content=None, content="answer", tool_calls=[]))]
            self.usage = None

    captured = {}
    orig = runner_mod._safe_create
    def fake_safe_create(conv, **kwargs):
        captured["messages"] = kwargs["messages"]
        return [FakeChunk()]
    runner_mod._safe_create = fake_safe_create
    try:
        # worker 场景：conv 是独立列表，与全局 messages 不同对象
        la.messages[:] = [{"role": "system", "content": "MAIN"},
                          {"role": "user", "content": "main q"}]
        worker_conv = {"messages": [{"role": "system", "content": la.WORKER_SYSTEM_PROMPT}], "size": None}
        res = la.chat("worker q", on_event=lambda e: None, _conv=worker_conv)
        assert res == "answer"
        assert captured["messages"] is worker_conv["messages"]  # 传的是 worker 自己的列表
        assert captured["messages"] is not la.messages  # 不是全局
        # 主会话场景：conv['messages'] 就是全局
        main_conv = {"messages": la.messages, "size": None}
        la.chat("main q", on_event=lambda e: None, _conv=main_conv)
        assert captured["messages"] is la.messages
    finally:
        runner_mod._safe_create = orig
        la.messages[:] = [{"role": "system", "content": "sys"}]
        la._msg_size = None


if __name__ == "__main__":
    for fn in [test_tool_definition, test_worker_system_prompt, test_recursion_guard,
               test_delegate_dispatch_and_events, test_worker_isolated_context, test_validate,
               test_chat_impl_passes_conv_messages]:
        fn()
        print(f"ok: {fn.__name__}")
    print("all multi-agent self-checks passed")
