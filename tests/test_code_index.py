"""Self-check for the code-index + context-pruning features in loop_agent_v2.py. Run: python test_code_index.py"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import loop_agent_v2 as la
import loop_agent_core.confirm as confirm

# 测试环境：显式声明"无人值守自动放行"。
# 修复前这里依赖 confirm 的隐式默认放行（_ask=None => 放行）；现在确认门是 fail-closed，
# 放行必须是一个有意识的决定，故显式声明（SECURITY_AUDIT_REPORT V1）。
confirm.set_policy("allow_all")

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
    # 占位必须是**可复原**的：长度 + 内容哈希，回放侧才能证明参数与录制一致
    assert len(comp) < 300
    d = json.loads(comp)
    assert d["path"] == "tui.py"
    assert d["content"]["$omitted"]["len"] == 5000
    import hashlib
    assert d["content"]["$omitted"]["sha256"] == hashlib.sha256(
        ("x" * 5000).encode("utf-8")).hexdigest()
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
    assert la._prune_cap() == 32000  # DeepSeek default
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
    # current question preserved at its new position; notice prepended (not separate msg)
    assert "CURRENT QUESTION" in la.messages[new_q]["content"]
    assert "Context trimmed" in la.messages[new_q]["content"]
    # system prompt kept first
    assert la.messages[0]["role"] == "system"
    # all tool chains gone before the question
    for m in la.messages[:new_q]:
        assert m.get("role") != "tool"
        assert not (m.get("role") == "assistant" and "tool_calls" in m)
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
    assert "CURRENT" in la.messages[new_q]["content"]
    assert "Context trimmed" in la.messages[new_q]["content"]
    # 旧提示回收后无独立 notice 消息（仅合并到问题文本的那条）
    stale = [m for m in la.messages[:new_q] if la._is_trim_notice(m)]
    assert len(stale) == 0, f"stale notices before question: {[n['content'][:30] for n in stale]}"
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

def test_file_not_found_suggests():
    # 文件名猜错时提示相似文件，帮模型纠错
    r = la.read_file_mock("HANDOFF")
    assert "File not found" in r and "HANDOFF_NEXT_SESSION.md" in r, r
    r2 = la.count_tokens_mock(path="HANDOFF")
    assert "HANDOFF_NEXT_SESSION.md" in r2, r2
    # 无相似文件时不附加
    assert "Similar files" not in la.read_file_mock("_definitely_missing_file_zzz.py")

def test_edit_backup_and_diff():
    """write/edit 前置备份、失败恢复、进程内 diff；不留 .bak 残留。

    核心回归：diff 对【未跟踪文件】也必须有效。旧实现走 `git diff -- <path>`，对未跟踪
    文件返回空串，而 write_file 的主用途（新建文件）和 edit_file 对刚建文件的编辑都在
    这个盲区里——等于这类编辑完全没有自检证据。"""
    probe = "_selftest_guard.txt"
    tmp = os.path.join(la.WORK_DIR, probe)
    for f in (tmp, tmp + ".bak"):
        if os.path.exists(f):
            os.remove(f)
    try:
        r1 = la.write_file_mock(probe, "alpha beta\nline2\n")
        assert "Written" in r1, r1
        # 新建文件：不做"空→全文"的伪 diff，明确说明没有基线
        assert "new file" in r1 and "nothing to diff against" in r1, r1

        # 未跟踪文件上的编辑：旧 git diff 实现此处返回空串（本测试就是它的反例）
        r2 = la.edit_file_mock(probe, "alpha beta", "ALPHA BETA")
        assert "Edited" in r2 and "1 occurrence(s) replaced" in r2, r2
        assert "[diff for self-check]" in r2, r2
        assert "-alpha beta" in r2 and "+ALPHA BETA" in r2, r2
        assert "+++ " not in r2, r2  # 同一文件，diff 头两行应被剥掉
        r3 = la.edit_file_mock(probe, "line2", "LINE2")
        assert "-line2" in r3 and "+LINE2" in r3, r3
        with open(tmp, encoding="utf-8") as f:
            content = f.read()
        assert "ALPHA BETA" in content and "LINE2" in content
        assert not os.path.exists(tmp + ".bak")  # 成功路径清理 .bak

        # 覆盖写入：diff 只含本次改动，不混入工作区里已有的其他改动
        r4 = la.write_file_mock(probe, "ALPHA BETA\nLINE2\nthird\n")
        assert "Overwritten" in r4 and "+third" in r4, r4
        assert "-alpha" not in r4 and "-line2" not in r4, r4
        # 内容完全一致：明确报"无改动"，而不是给一个空 diff 让人猜
        r5 = la.write_file_mock(probe, "ALPHA BETA\nLINE2\nthird\n")
        assert "no textual change" in r5, r5

        # 找不到 old_string：不写入、返回错误（可能带相近行提示）
        before = open(tmp, encoding="utf-8").read()
        r6 = la.edit_file_mock(probe, "zebra_does_not_exist", "x")
        assert r6.startswith("[Error]"), r6
        assert open(tmp, encoding="utf-8").read() == before  # 未写入

        # 超大改动：截断必须显式标注省略量，不静默丢内容
        r7 = la.write_file_mock(probe, "\n".join(f"line{i}" for i in range(200)) + "\n")
        assert "more diff line(s)" in r7, r7[:400]
    finally:
        for f in (tmp, tmp + ".bak"):
            if os.path.exists(f):
                os.remove(f)

def test_check_local_llm_cloud():
    """云端后端健康检查直接返回 cloud，不探测端口。"""
    from loop_agent_core.config import check_local_llm, _is_local_backend
    if not _is_local_backend():  # 默认 DeepSeek
        assert check_local_llm() == (True, "cloud")

def test_trim_notice_accumulates():
    """反复裁剪时 [Context trimmed] 的计数必须**累加**。

    改造前是重写：第二次裁剪会把"已删除 N 条"抹成新数字，模型看不到自己累计丢了多少历史。
    """
    content = "CURRENT QUESTION"
    once = la.render_prune_notice(content, 3)
    assert "已删除 3 条" in once and once.endswith(content)
    twice = la.render_prune_notice(once, 4)
    assert "已删除 7 条" in twice, twice
    assert twice.endswith(content), "问题原文必须保留在提示之后"
    assert once.count("已删除") == 1 and twice.count("已删除") == 1, "不能堆叠多条提示"

def test_compaction_contract():
    """上下文压缩的薄壳契约：tui / 其他测试 import 的符号必须都在。"""
    for name in ("dedup_tool_results", "should_compact", "compaction_span", "split_blocks",
                 "apply_compaction", "expand_last", "latest_compaction", "record_archive",
                 "persist_archive", "load_archive", "run_rolling_summary", "validate_summary",
                 "build_block_prompt", "compaction_cap", "placeholder_text", "_prep_context",
                 "persist_archive_sidecar", "adopt_archive", "clear_archive"):
        assert hasattr(la, name), f"薄壳缺少符号 {name}"
    assert "recall_archive" in la.TOOL_CALL_MAP
    # 工具数：原 11 + search_code/code_index/observe_browser/visual_locate/browser_act 等历史增量 + recall_archive
    assert any(t["function"]["name"] == "recall_archive" for t in la.tools)
    # 两层全关 → 行为与改造前一致（只走 L2）
    import loop_agent_core.config as cm
    os.environ["COMPACT_ENABLED"] = "0"
    os.environ["DEDUP_ENABLED"] = "0"
    cm._compact_k_cache = None
    try:
        msgs = [_mkmsg("system", "sys"), _mkmsg("user", "Q1"),
                _mkmsg("assistant", "", tool_calls=_tc("c1")),
                _mkmsg("tool", "res1" * 50, tool_call_id="c1"),
                _mkmsg("assistant", "A1"), _mkmsg("user", "CURRENT")]
        la.messages[:] = msgs
        la._msg_size = la.estimate_tokens(json.dumps(msgs, ensure_ascii=False))
        conv = {"messages": la.messages, "size": la._msg_size,
                "_compact_count": 0, "_compact_last_turn": None, "_user_turn": 1, "_dedup_saved": 0}
        events = []
        q_idx = len(msgs) - 1
        new_q, removed = la._prep_context(la._msg_size - 50, q_idx, conv, None, events.append)
        assert [e["type"] for e in events] == ["pruned"], events
        assert removed > 0
        assert la.messages[new_q]["role"] == "user"
    finally:
        os.environ.pop("COMPACT_ENABLED", None)
        os.environ.pop("DEDUP_ENABLED", None)
        cm._compact_k_cache = None
        la.messages[:] = [{"role": "system", "content": la.system_prompt}]
        la._msg_size = None

if __name__ == "__main__":
    for fn in [test_outline, test_extract, test_count, test_gate, test_noncode_preview, test_count_tool,
               test_strip_and_garbage, test_parse_xml_tool_calls, test_parse_qwen3_toolcalls, test_dsml_corrupted_tags,
               test_stream_strip_xml, test_store_tool_args, test_rollback_tool_round, test_file_not_found_suggests,
               test_run_tools, test_prune_cap, test_prune, test_prune_notice_recycle, test_no_prune_when_small,
               test_edit_backup_and_diff, test_check_local_llm_cloud,
               test_trim_notice_accumulates, test_compaction_contract]:
        fn()
        print(f"ok: {fn.__name__}")
    print("all code-index + pruning self-checks passed")
