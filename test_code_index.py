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

def test_strip_and_garbage():
    xml = ("好的。\n\n<tool_call>\n<function=read_file>\n<parameter=path>\nloop_agent_v2.py\n"
           "</parameter>\n</function>\n</tool_call>")
    # 剥 XML 后保留正文，且不再被当垃圾
    cleaned = la._strip_toolcall_xml(xml)
    assert "<tool_call>" not in cleaned and "<function" not in cleaned
    assert "好的。" in cleaned
    assert la._is_garbage_content(xml) is False
    # 纯工具调用 XML（无正文）→ 不可作为回复显示
    assert la._is_garbage_content(xml.replace("好的。\n\n", "")) is True
    # 未闭合（被截断）的 <tool_call → 残留整体丢弃，判为垃圾
    trunc = "<tool_call>\n<function=read_file>\n<parameter=path>\nloop_agent_v2.py"
    assert la._strip_toolcall_xml(trunc) == ""
    assert la._is_garbage_content(trunc) is True
    # 真正的 DSML 垃圾仍被检出
    assert la._is_garbage_content("前文 |DSML| 夹杂 |DSML| 后文") is True
    # 正常中文回复不受影响
    assert la._is_garbage_content("这是一个正常的回复，<b>加粗</b>都没问题。") is False

def test_stream_strip_xml():
    # 完整 <tool_call> 块跨 chunk 拆分也能剥掉
    emitted = []
    buf = ""
    for c in ["好的", "，我来读\n<tool_call><function=read_file><parameter=path>", "a.py</parameter></function>", "</tool_call>\n继续"]:
        buf = la._stream_strip_xml(buf + c, emitted.append)
    assert "".join(emitted) == "好的，我来读\n\n继续"
    # 未闭合块：其前正文上屏，XML 残留不上屏
    emitted2 = []
    buf2 = ""
    for c in ["前言\n<tool_call><function=read_file><parameter=path>\n", "a.py"]:
        buf2 = la._stream_strip_xml(buf2 + c, emitted2.append)
    assert "".join(emitted2) == "前言\n"

def test_parse_qwen3_toolcalls():
    # Qwen3 的 <tool_calls><invoke name=..><parameter name=..> 格式（本会话实际出现的泄漏源）
    s = ("先确认哪些文件引用了 `loop_agent_v2`。\n\n<tool_calls>\n<invoke name=\"exec_shell_win\">\n"
         "<parameter name=\"shell_cmd\" string=\"true\">findstr /s /n /c:\"loop_agent_v2\" *.py 2>nul</parameter>\n"
         "</invoke>\n</tool_calls>")
    calls = la._parse_xml_tool_calls(s)
    assert len(calls) == 1, calls
    assert calls[0]["name"] == "exec_shell_win"
    args = json.loads(calls[0]["arguments"])
    assert args["shell_cmd"] == "findstr /s /n /c:\"loop_agent_v2\" *.py 2>nul"
    # 剥 XML 后只留正文，不再泄漏
    cleaned = la._strip_toolcall_xml(s)
    assert "<tool_calls>" not in cleaned and "<invoke" not in cleaned
    assert "先确认哪些文件引用了" in cleaned
    # 多参数 + 无引号 name
    s2 = ("<tool_calls><invoke name=read_file><parameter name=path>x.py</parameter>"
          "<parameter name=start_line>3</parameter></invoke></tool_calls>")
    calls2 = la._parse_xml_tool_calls(s2)
    assert calls2[0]["name"] == "read_file"
    assert json.loads(calls2[0]["arguments"]) == {"path": "x.py", "start_line": 3}
    # 旧格式不受影响
    assert la._parse_xml_tool_calls("<tool_call><function=get_date></function></tool_call>")[0]["name"] == "get_date"
    # 无 <invoke> 的残块整体丢弃
    assert la._parse_xml_tool_calls("<tool_calls>\n<parameter name=x>1</parameter></tool_calls>") == []

def test_store_tool_args():
    big = json.dumps({"path": "tui.py", "content": "x" * 5000}, ensure_ascii=False)
    comp = la._store_tool_args("write_file", big)
    assert len(comp) < 300 and "omitted 5000 chars" in comp
    small = json.dumps({"path": "a", "content": "hi"}, ensure_ascii=False)
    assert la._store_tool_args("write_file", small) == small  # 小参数不动
    assert la._store_tool_args("read_file", big) == big  # 非 write_file 不动

def test_run_tools():
    la.messages[:] = [{"role": "system", "content": "sys"}]
    la._msg_size = None
    calls = la._xml_to_tool_calls([{"name": "get_date", "arguments": "{}"}])
    assert calls[0].id == "xml0" and calls[0].function.name == "get_date"
    emitted = []
    conv = {"messages": la.messages, "size": la._msg_size}
    la._run_tools(calls, emitted.append, conv)
    la._msg_size = conv["size"]
    assert any(e["type"] == "tool_result" for e in emitted)
    assert la.messages[-1]["role"] == "tool" and len(la.messages) == 2
    la.messages[:] = [{"role": "system", "content": "sys"}]
    la._msg_size = None

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
    conv = {"messages": la.messages, "size": la._msg_size}
    la._append_msg(_mkmsg("user", "CURRENT QUESTION"), conv)
    q_idx = len(la.messages) - 1

    new_q, removed = la._prune_messages(100, q_idx, conv)  # small cap forces pruning
    la._msg_size = conv["size"]
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

def test_prune_notice_recycle():
    # 工具链(prio 0)优先删除且删除后即达标 → 旧提示(prio 1)存活 → 回收后只剩一条新提示
    la.messages[:] = [
        _mkmsg("system", "sys"),
        _mkmsg("user", "[Context trimmed] 旧提示1"),
        _mkmsg("user", "[Context trimmed] 旧提示2"),
        _mkmsg("user", "Q1"),
        _mkmsg("assistant", "", tool_calls=_tc("c1")),
        _mkmsg("tool", "res1" * 50, tool_call_id="c1"),
        _mkmsg("assistant", "A1 " + "y" * 300),
        _mkmsg("user", "CURRENT"),
    ]
    la._msg_size = la.estimate_tokens(json.dumps(la.messages, ensure_ascii=False))
    conv = {"messages": la.messages, "size": la._msg_size}
    q_idx = len(la.messages) - 1
    tc = sum(g[2] for g in la._find_groups(q_idx, conv) if g[3] == 0)  # 工具链 token 数
    cap = la._msg_size - tc + 1  # 只删工具链就达标，不碰旧提示
    new_q, removed = la._prune_messages(cap, q_idx, conv)
    la._msg_size = conv["size"]
    assert removed == 2, removed  # assistant(tool_calls) + tool
    assert la.messages[new_q]["content"] == "CURRENT"
    notices = [m for m in la.messages if la._is_trim_notice(m)]
    assert len(notices) == 1, f"提示堆叠 {len(notices)} 条: {[n['content'][:30] for n in notices]}"
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
    conv = {"messages": la.messages, "size": la._msg_size}
    la._append_msg(_mkmsg("user", "current"), conv)
    q_idx = len(la.messages) - 1
    new_q, removed = la._prune_messages(999999, q_idx, conv)
    la._msg_size = conv["size"]
    assert removed == 0
    assert len(la.messages) == 6  # untouched
    la.messages[:] = [{"role": "system", "content": "sys"}]
    la._msg_size = None

def test_dsml_corrupted_tags():
    # DSML 污染：标签 `<` 后插入全角竖线(U+FF5C=chr(65372))包裹的 DSML，如 `<｜｜DSML｜｜tool_calls>`。
    # 用 chr() 动态拼接，避免源文件里出现污染的 XML 字面量。
    bar = chr(65372)
    lt = chr(60)
    def pollute(tag):
        return lt + bar * 2 + "DSML" + bar * 2 + tag
    # Qwen3 格式被污染后仍能解析
    s = ("好的。\n\n" + pollute("tool_calls>\n") +
         pollute('invoke name="exec_shell_win">\n') +
         pollute('parameter name="shell_cmd" string="true">') + "echo hi" +
         pollute("/parameter>\n") + pollute("/invoke>\n") + pollute("/tool_calls>"))
    calls = la._parse_xml_tool_calls(s)
    assert len(calls) == 1, calls
    assert calls[0]["name"] == "exec_shell_win"
    assert json.loads(calls[0]["arguments"]) == {"shell_cmd": "echo hi"}
    # 剥离后只留正文，DSML 标记消失
    cleaned = la._strip_toolcall_xml(s)
    assert "DSML" not in cleaned
    assert "好的。" in cleaned
    # 入口原始 text 带 DSML → 判为垃圾（不再原样落库）
    assert la._is_garbage_content(s) is True
    # 旧格式污染同样能解析
    s2 = pollute("tool_call>") + pollute("function=get_date>") + pollute("/function>") + pollute("/tool_call>")
    assert la._parse_xml_tool_calls(s2)[0]["name"] == "get_date"
    # 流式路径：污染块跨 chunk 也能剥掉，DSML 不上屏
    emitted = []
    buf = ""
    for c in ["好的", "，来\n" + pollute("tool_call><function=read_file><parameter=path>"),
              "a.py" + pollute("/parameter></function></tool_call>") + "\n继续"]:
        buf = la._stream_strip_xml(buf + c, emitted.append)
    assert "".join(emitted) == "好的，来\n\n继续"
    assert "DSML" not in "".join(emitted)

def test_rollback_tool_round():
    # 400 后回滚最后一轮工具调用（assistant+TC + tool 消息），会话回到可用状态
    conv = {"messages": [{"role": "system", "content": "sys"},
                         {"role": "user", "content": "q"},
                         {"role": "assistant", "content": "", "tool_calls": [{"id": "xml0", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
                         {"role": "tool", "tool_call_id": "xml0", "content": "result"}],
            "size": None}
    conv["size"] = la.estimate_tokens(json.dumps(conv["messages"], ensure_ascii=False))
    size_before = conv["size"]
    assert la._rollback_tool_round(conv) is True
    assert len(conv["messages"]) == 2
    assert conv["size"] < size_before
    # 无可回滚轮次 → False，不改变
    conv2 = {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}], "size": None}
    assert la._rollback_tool_round(conv2) is False
    assert len(conv2["messages"]) == 2

if __name__ == "__main__":
    for fn in [test_outline, test_extract, test_count, test_gate, test_noncode_preview, test_count_tool,
               test_strip_and_garbage, test_parse_xml_tool_calls, test_parse_qwen3_toolcalls, test_dsml_corrupted_tags,
               test_stream_strip_xml, test_store_tool_args, test_rollback_tool_round,
               test_run_tools, test_prune_cap, test_prune, test_prune_notice_recycle, test_no_prune_when_small]:
        fn()
        print(f"ok: {fn.__name__}")
    print("all code-index + pruning self-checks passed")
