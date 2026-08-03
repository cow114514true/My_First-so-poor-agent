"""Self-check for the code-index + context-pruning features in loop_agent_v2.py. Run: python test_code_index.py"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import loop_agent_v2 as la

SAMPLE = (
    "import os\n"
    "from pathlib import Path\n"
    "\n"
    "CONFIG = 1\n"
    "\n"
    "def greet(name):\n"
    "    return f\"hi {name}\"\n"
    "\n"
    "class Foo:\n"
    "    def bar(self):\n"
    "        return 42\n"
)

def test_outline():
    out = la.build_outline(SAMPLE)
    kinds = [(k, n) for k, n, _ in out]
    assert ("import", "os") in kinds
    assert ("import", "pathlib") in kinds  # "from pathlib import" → import pathlib
    assert ("def", "greet") in kinds
    assert ("class", "Foo") in kinds
    assert ("def", "bar") in kinds
    lines = {ln for _, _, ln in out}
    assert lines == {1, 2, 6, 9, 10}

def test_extract():
    body, s, e = la.extract_function(SAMPLE, "greet")
    assert (s, e) == (6, 7)
    assert body == "def greet(name):\n    return f\"hi {name}\""
    body, s, e = la.extract_function(SAMPLE, "bar")
    assert (s, e) == (10, 11)
    assert "return 42" in body
    assert la.extract_function(SAMPLE, "nope") is None

def test_count():
    n = la.estimate_tokens("hello world " * 100)
    assert n > 0 and n < 10000
    assert la.estimate_tokens("") == 0

def test_gate():
    os.environ["READ_BUDGET_DS"] = "1000"
    os.environ["READ_BUDGET_LOCAL"] = "1000"
    big = "\n".join(f"def fn{i}():\n    pass" for i in range(500))
    tmp = os.path.join(la.WORK_DIR, "_selftest_big.py")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(big)
    try:
        res = la.read_file_mock("_selftest_big.py")
        assert res.startswith("[Large file]"), res[:80]
        assert "Outline" in res and "def fn0" in res
        got = la.read_file_mock("_selftest_big.py", function="fn5")
        assert "fn5():" in got and "pass" in got
        rng = la.read_file_mock("_selftest_big.py", start_line=1, end_line=2)
        assert "fn0" in rng
        big_rng = la.read_file_mock("_selftest_big.py", start_line=1, end_line=500)
        assert big_rng.startswith("[Large slice]"), big_rng[:80]
        assert "Showing first" in big_rng
        os.environ["READ_BUDGET_DS"] = "999999"
        os.environ["READ_BUDGET_LOCAL"] = "999999"
        full = la.read_file_mock("_selftest_big.py")
        assert full.startswith("def fn0"), full[:40]
    finally:
        os.remove(tmp)
        os.environ.pop("READ_BUDGET_DS", None)
        os.environ.pop("READ_BUDGET_LOCAL", None)

def test_noncode_preview():
    os.environ["READ_BUDGET_DS"] = "1000"
    os.environ["READ_BUDGET_LOCAL"] = "1000"
    tmp = os.path.join(la.WORK_DIR, "_selftest_big.txt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("line of text\n" * 2000)  # 24KB, no defs
    try:
        res = la.read_file_mock("_selftest_big.txt")
        assert res.startswith("[Large file]"), res[:80]
        assert "Non-code file" in res and "first ~2000 chars" in res
    finally:
        os.remove(tmp)
        os.environ.pop("READ_BUDGET_DS", None)
        os.environ.pop("READ_BUDGET_LOCAL", None)

def test_parse_xml_tool_calls():
    # reasoning 里常见的 Qwen XML 工具调用（换行 + <parameter=name> 风格）
    s = ("好的。\n\n<tool_call>\n<function=read_file>\n<parameter=path>\nloop_agent_v2.py\n"
         "</parameter>\n</function>\n</tool_call>")
    calls = la._parse_xml_tool_calls(s)
    assert len(calls) == 1
    assert calls[0]["name"] == "read_file"
    assert json.loads(calls[0]["arguments"]) == {"path": "loop_agent_v2.py"}
    # 多参数 + 非字符串值
    s2 = ("<tool_call><function=write_file><parameter=path>x.py</parameter>"
          "<parameter=lines>[1,2,3]</parameter></function></tool_call>")
    calls2 = la._parse_xml_tool_calls(s2)
    assert calls2[0]["name"] == "write_file"
    assert json.loads(calls2[0]["arguments"]) == {"path": "x.py", "lines": [1, 2, 3]}
    # 无 XML → 空列表
    assert la._parse_xml_tool_calls("just text") == []
    assert la._parse_xml_tool_calls("") == []

def test_count_tool():
    res = la.count_tokens_mock(text="你好 " * 50)
    assert res.endswith("(estimated)"), res
    assert la.count_tokens_mock().startswith("[Error]")
    assert la.count_tokens_mock(path="loop_agent_v2.py").startswith("loop_agent_v2.py")
    assert la.count_tokens_mock(path="missing_file_xyz.py").startswith("[Error]")

def _mkmsg(role, content=None, tool_calls=None, tool_call_id=None):
    d = {"role": role}
    if content is not None:
        d["content"] = content
    if tool_calls is not None:
        d["tool_calls"] = tool_calls
    if tool_call_id is not None:
        d["tool_call_id"] = tool_call_id
    return d

def _tc(tid):
    return [{"id": tid, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]

def test_prune_cap():
    assert la._prune_cap() == 8000  # DeepSeek default
    os.environ["PRUNE_CAP_DS"] = "1234"
    assert la._prune_cap() == 1234
    os.environ.pop("PRUNE_CAP_DS", None)

def test_prune():
    # two old turns (with tool chains), then a current question
    la.messages[:] = [
        _mkmsg("system", "sys"),
        _mkmsg("user", "Q1"),
        _mkmsg("assistant", "", tool_calls=_tc("c1")),
        _mkmsg("tool", "res1", tool_call_id="c1"),
        _mkmsg("user", "[Self-check] Tool 'read_file' result is questionable: x"),
        _mkmsg("assistant", "A1"),
        _mkmsg("user", "Q2"),
        _mkmsg("assistant", "", tool_calls=_tc("c2")),
        _mkmsg("tool", "res2", tool_call_id="c2"),
        _mkmsg("assistant", "A2"),
    ]
    la._msg_size = la.estimate_tokens(json.dumps(la.messages, ensure_ascii=False))
    la._append_msg(_mkmsg("user", "CURRENT QUESTION"))
    q_idx = len(la.messages) - 1

    new_q, removed = la._prune_messages(100, q_idx)  # small cap forces pruning
    assert removed > 0, "expected pruning to trigger"
    # current question preserved at its new position
    assert la.messages[new_q]["content"] == "CURRENT QUESTION"
    # system prompt kept first
    assert la.messages[0]["role"] == "system"
    # all tool chains gone before the question
    for m in la.messages[:new_q]:
        assert m.get("role") != "tool"
        assert not (m.get("role") == "assistant" and "tool_calls" in m)
    # notice injected right before the question
    assert la.messages[new_q - 1]["role"] == "user"
    assert "Context trimmed" in la.messages[new_q - 1]["content"]
    assert la._msg_size is not None and la._msg_size > 0

    la.messages[:] = [{"role": "system", "content": "sys"}]
    la._msg_size = None

def test_no_prune_when_small():
    la.messages[:] = [
        _mkmsg("system", "sys"),
        _mkmsg("user", "Q1"),
        _mkmsg("assistant", "", tool_calls=_tc("c1")),
        _mkmsg("tool", "res1", tool_call_id="c1"),
        _mkmsg("assistant", "A1"),
    ]
    la._msg_size = la.estimate_tokens(json.dumps(la.messages, ensure_ascii=False))
    la._append_msg(_mkmsg("user", "current"))
    q_idx = len(la.messages) - 1
    new_q, removed = la._prune_messages(999999, q_idx)
    assert removed == 0
    assert len(la.messages) == 6  # untouched
    la.messages[:] = [{"role": "system", "content": "sys"}]
    la._msg_size = None

if __name__ == "__main__":
    for fn in [test_outline, test_extract, test_count, test_gate, test_noncode_preview, test_count_tool,
               test_parse_xml_tool_calls, test_prune_cap, test_prune, test_no_prune_when_small]:
        fn()
        print(f"ok: {fn.__name__}")
    print("all code-index + pruning self-checks passed")
