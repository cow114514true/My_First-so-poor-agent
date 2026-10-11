"""上下文压缩自检：L0 去重 / L1 分块滚动摘要 / L2 兜底 / 档案回滚。纯逻辑，不触网。

Run: python test_compaction.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 纯逻辑测试不触网，但 config.py 在 import 时就要建 OpenAI client。
# 显式声明占位凭据（绝不用真实 key），避免测试依赖外部环境变量。
os.environ.setdefault("DS_KEY", "sk-selftest-not-a-real-key")

# 失败日志必须重定向到测试专用文件：否则本套件里刻意制造的失败会灌进真实诊断日志
# tmp/compaction_error.log，污染下次排查（实测灌了 34 行假失败）。
_SELFTEST_ERRLOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "tmp", "_selftest_compaction_error.log")
os.environ["COMPACT_ERROR_LOG"] = _SELFTEST_ERRLOG
if os.path.exists(_SELFTEST_ERRLOG):
    os.remove(_SELFTEST_ERRLOG)

import loop_agent_core.config as config_mod
import loop_agent_core.compaction as comp
import loop_agent_v2 as la


def _env(**kw):
    """设置/清理压缩相关环境变量并清空配置缓存（_compact_k 是惰性缓存）。"""
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    config_mod._compact_k_cache = None


def _mkmsg(role, content=None, tool_calls=None, tool_call_id=None):
    d = {"role": role}
    if content is not None:
        d["content"] = content
    if tool_calls is not None:
        d["tool_calls"] = tool_calls
    if tool_call_id is not None:
        d["tool_call_id"] = tool_call_id
    return d


def _tc(tid, name="read_file", args="{}"):
    return [{"id": tid, "type": "function",
             "function": {"name": name, "arguments": args}}]


def _conv(msgs):
    conv = {"messages": msgs, "size": la.estimate_tokens(json.dumps(msgs, ensure_ascii=False))}
    return conv


def _big(fill, n=400):
    return fill * n


# ─────────────────────────── L0 去重 ───────────────────────────

def test_disabled_matches_legacy_prune():
    """回归保护：两层都关掉时，行为必须与改造前完全一致（只走 L2 硬裁剪）。

    同时覆盖 `summarize_fn is None`（worker 会话的实际路径）。
    """
    from loop_agent_core.context import _prep_context
    _env(DEDUP_ENABLED=0, COMPACT_ENABLED=0)
    conv, msgs = _prep_conv()
    q_idx = len(msgs) - 1
    # 只删工具链就达标 → 不碰任何 user/assistant 主干
    chain_tokens = sum(g[2] for g in la._find_groups(q_idx, conv) if g[3] == 0)
    cap = conv["size"] - chain_tokens + 1
    events = []
    new_q, removed = _prep_context(cap, q_idx, conv, None, events.append)
    kinds = [e["type"] for e in events]
    assert kinds == ["pruned"], kinds
    # 删的是工具链（可能两条都删：cap 取的是"删一条刚好达标"的临界值）
    assert removed >= 2 and removed % 2 == 0, removed
    assert "Context trimmed" in str(msgs[new_q]["content"])
    for m in msgs[:new_q]:
        assert m.get("role") != "tool"
        assert not (m.get("role") == "assistant" and m.get("tool_calls"))
    # 计数累加：trim 提示里的数字必须等于实际删除量（不能被下次裁剪抹成"只删了 N 条"）
    assert f"已删除 {removed} 条" in str(msgs[new_q]["content"])
    _env(DEDUP_ENABLED=None, COMPACT_ENABLED=None)


def test_dedup_key_double_condition():
    a = comp.dedup_key("read_file", '{"path":"x.py"}', "SAME")
    b = comp.dedup_key("read_file", '{"path":"x.py"}', "SAME")
    c = comp.dedup_key("read_file", '{"path":"x.py"}', "DIFF")
    assert a == b, "参数同+内容同 → 必须同 key"
    assert a != c, "参数同+内容不同 → 必须不同 key（不能按参数去重）"


def test_dedup_key_range_reads_not_merged():
    """反例：不同区间/不同 function=/不同 path 即使内容相同也不能合并。"""
    base = comp.dedup_key("read_file", '{"path":"a.py"}', "SAME")
    rng = comp.dedup_key("read_file", '{"path":"a.py","start_line":1,"end_line":9}', "SAME")
    fn = comp.dedup_key("read_file", '{"path":"a.py","function":"foo"}', "SAME")
    other = comp.dedup_key("read_file", '{"path":"b.py"}', "SAME")
    assert len({base, rng, fn, other}) == 4
    # 键序不同但语义相同的参数必须归一化到同一 key
    k1 = comp.tool_call_key("x", '{"a":1,"b":2}')
    k2 = comp.tool_call_key("x", '{"b":2,"a":1}')
    assert k1 == k2


def test_dedup_keeps_last_only():
    _env(DEDUP_ENABLED=1, DEDUP_MIN_CHARS=10)
    body = _big("HELLO")
    msgs = [
        _mkmsg("system", "sys"),
        _mkmsg("user", "Q1"),
        _mkmsg("assistant", "", tool_calls=_tc("c1", args='{"path":"x.py"}')),
        _mkmsg("tool", body, tool_call_id="c1"),
        _mkmsg("assistant", "A1"),
        _mkmsg("user", "Q2"),
        _mkmsg("assistant", "", tool_calls=_tc("c2", args='{"path":"x.py"}')),
        _mkmsg("tool", body, tool_call_id="c2"),
        _mkmsg("assistant", "A2"),
        _mkmsg("user", "Q3"),
    ]
    conv = _conv(msgs)
    q_idx = len(msgs) - 1
    n = comp.dedup_tool_results(conv, q_idx)
    assert n == 1, f"应只替换更早的那一份，实际 {n}"
    assert msgs[3]["content"].startswith("[已省略]")
    assert msgs[7]["content"] == body, "最后一次必须是原文完整内容"
    assert len(msgs) == 10, "位置不能变（只改 content）"
    _env(DEDUP_ENABLED=None, DEDUP_MIN_CHARS=None)


def test_dedup_protocol_intact_and_accounting():
    _env(DEDUP_ENABLED=1, DEDUP_MIN_CHARS=10)
    body = _big("DATA")
    msgs = [
        _mkmsg("system", "sys"),
        _mkmsg("assistant", "", tool_calls=_tc("c1", args='{"path":"x.py"}')),
        _mkmsg("tool", body, tool_call_id="c1"),
        _mkmsg("assistant", "A1"),
        _mkmsg("assistant", "", tool_calls=_tc("c2", args='{"path":"x.py"}')),
        _mkmsg("tool", body, tool_call_id="c2"),
        _mkmsg("user", "CURRENT"),
    ]
    conv = _conv(msgs)
    q_idx = len(msgs) - 1
    comp.dedup_tool_results(conv, q_idx)

    # 配对完整性：每条 tool 的 tool_call_id 都能在其前的 assistant.tool_calls 找到
    seen = set()
    for m in msgs:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                seen.add(tc["id"])
        if m.get("role") == "tool":
            assert m["tool_call_id"] in seen, "去重后出现孤立 tool 消息"
    assert conv["_dedup_saved"] > 0
    # 记账与实际一致（estimate 是确定性的，差额扣减后应精确相等）
    assert conv["size"] == la.estimate_tokens(json.dumps(msgs, ensure_ascii=False)), \
        (conv["size"], la.estimate_tokens(json.dumps(msgs, ensure_ascii=False)))
    _env(DEDUP_ENABLED=None, DEDUP_MIN_CHARS=None)


def test_dedup_disabled_and_threshold():
    body = _big("DATA")
    msgs = [
        _mkmsg("system", "sys"),
        _mkmsg("assistant", "", tool_calls=_tc("c1", args='{"path":"x.py"}')),
        _mkmsg("tool", body, tool_call_id="c1"),
        _mkmsg("assistant", "", tool_calls=_tc("c2", args='{"path":"x.py"}')),
        _mkmsg("tool", body, tool_call_id="c2"),
        _mkmsg("user", "CURRENT"),
    ]
    # 关闭 → 完全无操作（回归保护）
    _env(DEDUP_ENABLED=0, DEDUP_MIN_CHARS=1)
    conv = _conv(msgs)
    assert comp.dedup_tool_results(conv, len(msgs) - 1) == 0
    assert msgs[2]["content"] == body

    # 门槛：内容短于 DEDUP_MIN_CHARS → 不参与
    _env(DEDUP_ENABLED=1, DEDUP_MIN_CHARS=100000)
    conv = _conv(msgs)
    assert comp.dedup_tool_results(conv, len(msgs) - 1) == 0
    assert msgs[2]["content"] == body
    _env(DEDUP_ENABLED=None, DEDUP_MIN_CHARS=None)


# ─────────────────────────── L1 分块滚动摘要 ───────────────────────────

def _long_conv(rounds=6, per_round_tokens=80):
    """构造一个含多轮工具链的长会话，每轮工具结果约 per_round_tokens token。

    循环体（工具结果）刻意取小：每轮真正的开销大头是 assistant(tool_calls) 的参数 JSON
    （fixture 里约 140 token/轮），所以整轮 ≈ 350-400 token。默认值配合测试里
    COMPACT_BLOCK_TOKENS=1000 可稳定得到"每块 2 轮、共多块"的形态。
    """
    msgs = [_mkmsg("system", "sys")]
    for i in range(rounds):
        msgs.append(_mkmsg("user", f"问题 {i} 请读文件 " + "描述" * 20))
        msgs.append(_mkmsg("assistant", "", tool_calls=_tc(f"c{i}", args=json.dumps({"path": f"f{i}.py"}))))
        msgs.append(_mkmsg("tool", f"FILECONTENT{i} " * per_round_tokens, tool_call_id=f"c{i}"))
        msgs.append(_mkmsg("assistant", f"答复 {i} " + "内容" * 40))
    msgs.append(_mkmsg("user", "当前问题"))
    return _conv(msgs)


def test_should_compact_threshold_and_budget():
    _env(COMPACT_ENABLED=1, COMPACT_TRIGGER_RATIO=0.5, COMPACT_MIN_ROUNDS=3,
         COMPACT_MAX_PER_SESSION=20, PRUNE_CAP_DS="32000")
    cap = comp.compaction_cap()
    assert cap == 16000, cap

    conv = {"messages": [_mkmsg("system", "s")], "size": cap - 1}
    assert comp.should_compact(conv) == (False, "below")
    conv["size"] = cap
    ok, why = comp.should_compact(conv)
    assert ok and why == "", (ok, why)

    # 频率预算：会话上限
    conv["_compact_count"] = 20
    assert comp.should_compact(conv) == (False, "budget")
    conv["_compact_count"] = 0
    # 最小间隔（按用户轮次）
    conv["_user_turn"] = 5
    conv["_compact_last_turn"] = 4
    assert comp.should_compact(conv) == (False, "interval")
    conv["_compact_last_turn"] = 1
    assert comp.should_compact(conv)[0] is True
    # 关闭
    _env(COMPACT_ENABLED=0)
    assert comp.should_compact(conv) == (False, "none")
    _env(COMPACT_ENABLED=None, COMPACT_TRIGGER_RATIO=None, COMPACT_MIN_ROUNDS=None,
         COMPACT_MAX_PER_SESSION=None, PRUNE_CAP_DS=None)


def test_span_never_cuts_tool_chain():
    _env(COMPACT_KEEP_ROUNDS=2)
    conv = _long_conv(rounds=6)
    q_idx = len(conv["messages"]) - 1
    span = comp.compaction_span(conv, q_idx)
    assert span is not None
    start, end = span
    msgs = conv["messages"]
    # 区间内每个 tool 都能在区间内找到发起它的 assistant.tool_calls
    ids = set()
    for m in msgs[start:end]:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids.update(tc["id"] for tc in m["tool_calls"])
        if m.get("role") == "tool":
            assert m["tool_call_id"] in ids, "span 起点把工具链切半了（孤立 tool）"
    # 区间末尾不能是 assistant(tool_calls)（它的 tool 结果落在区间外）
    for m in reversed(msgs[start:end]):
        if m.get("role") == "assistant":
            assert not m.get("tool_calls"), "span 末尾把工具链切半了"
            break
    # 保留最近 keep_rounds 个用户提问
    tail_users = [i for i in range(end, q_idx) if msgs[i].get("role") == "user"]
    assert len(tail_users) >= 2, tail_users
    _env(COMPACT_KEEP_ROUNDS=None)


def test_split_blocks_respects_group_boundaries_and_limit():
    _env(COMPACT_BLOCK_TOKENS=1000)
    conv = _long_conv(rounds=6)
    q_idx = len(conv["messages"]) - 1
    start, end = comp.compaction_span(conv, q_idx)
    groups = [g for g in la._find_groups(q_idx, conv) if start <= g[0] and g[1] <= end]
    span_msgs = [[conv["messages"][i] for i in range(g[0], g[1])] for g in groups]
    sizes = [g[2] for g in groups]
    blocks = comp.split_blocks(span_msgs, sizes, 1000)
    assert len(blocks) > 1, "超上限必须切多块"
    # 每个组必须完整地落在唯一一个块里（工具链不可切半）
    for group in span_msgs:
        containing = [b for b in blocks if all(any(m is gm for gm in b) for m in group)]
        assert len(containing) == 1, "工具链组被切半了或跨块"
    # 除"单组自身超限"外，每块累计不超过上限
    group_tokens = {id(group): sizes[i] for i, group in enumerate(span_msgs)}
    for b in blocks:
        ids_in_block = {id(g) for g in span_msgs if all(any(m is gm for gm in b) for m in g)}
        total = sum(group_tokens[i] for i in ids_in_block)
        if total > 1000:
            assert len(ids_in_block) == 1, "超限块必须只含一个组"
    _env(COMPACT_BLOCK_TOKENS=None)


def test_rolling_summary_calls_and_prefix_stability():
    _env(COMPACT_BLOCK_TOKENS=1000, COMPACT_MAX_TOKENS=1500)
    conv = _long_conv(rounds=6)
    q_idx = len(conv["messages"]) - 1
    start, end = comp.compaction_span(conv, q_idx)
    groups = [g for g in la._find_groups(q_idx, conv) if start <= g[0] and g[1] <= end]
    span_msgs = [[conv["messages"][i] for i in range(g[0], g[1])] for g in groups]
    sizes = [g[2] for g in groups]
    blocks = comp.split_blocks(span_msgs, sizes, 1000)
    assert len(blocks) > 1, "本用例需要多块才能验证滚动前缀"

    body = ("## 用户目标\nG\n## 已完成\nD\n## 关键决策与结论\nC\n"
            "## 改动的文件\nF\n## 未决问题与下一步\nO")
    calls = []

    def fake(prefix, block):
        calls.append({"prefix": prefix,
                      "prefix_tokens": la.estimate_tokens(prefix),
                      "block_tokens": la.estimate_tokens(json.dumps(block, ensure_ascii=False))})
        return {"summary": body, "digest": comp._digest(prefix + body)}

    res = comp.run_rolling_summary(blocks, fake, prefix_tokens=60)
    assert res is not None
    assert res["blocks"] == len(blocks)
    assert len(calls) == len(blocks), "调用次数必须等于块数"
    # 当前块输入不超上限；前缀也必须**有界**（无界前缀会把"输入上限"废掉）
    for c in calls:
        assert c["block_tokens"] <= 1000 + 400, ("块输入超限", c["block_tokens"])
        assert c["prefix_tokens"] <= 60 + 8, ("前缀无界", c["prefix_tokens"])
    assert calls[0]["prefix"] == ""
    assert len(blocks) >= 3, "本用例需要 ≥3 块才能验证前缀截断"
    assert calls[1]["prefix"] == body, "第 2 块的前缀应是第 1 块的摘要"
    # 前缀超预算后必须截断并留省略标记，且保留尾部（越近的工作对延续越重要）
    assert calls[2]["prefix"].startswith("…"), ("前缀未截断", calls[2]["prefix"][:40])
    assert calls[2]["prefix"].endswith(body), "应保留尾部"
    _env(COMPACT_BLOCK_TOKENS=None, COMPACT_MAX_TOKENS=None)


def test_rolling_summary_aborts_on_failure():
    _env(COMPACT_BLOCK_TOKENS=1000, COMPACT_MAX_TOKENS=1500)
    conv = _long_conv(rounds=6)
    before = json.dumps(conv["messages"], ensure_ascii=False)
    q_idx = len(conv["messages"]) - 1
    start, end = comp.compaction_span(conv, q_idx)
    groups = [g for g in la._find_groups(q_idx, conv) if start <= g[0] and g[1] <= end]
    span_msgs = [[conv["messages"][i] for i in range(g[0], g[1])] for g in groups]
    blocks = comp.split_blocks(span_msgs, [g[2] for g in groups], 1000)
    assert len(blocks) > 1

    state = {"n": 0}
    body = ("## 用户目标\nG\n## 已完成\nD\n## 关键决策与结论\nC\n"
            "## 改动的文件\nF\n## 未决问题与下一步\nO")

    def flaky(prefix, block):
        state["n"] += 1
        if state["n"] == 2:
            return None  # 第 2 块失败
        return {"summary": body, "digest": comp._digest(prefix + body)}

    assert comp.run_rolling_summary(blocks, flaky) is None
    # 整体放弃：会话未被改动（run_rolling_summary 本身不碰 conv）
    assert json.dumps(conv["messages"], ensure_ascii=False) == before
    _env(COMPACT_BLOCK_TOKENS=None, COMPACT_MAX_TOKENS=None)


def test_worker_session_never_compacts():
    """worker 会话本轮不启用 L0/L1（chat_impl 用 is_main_session 门控）。

    直接断言 runner 侧的门控表达式，避免为了跑一次 chat 而 monkeypatch 整个 API 层。
    """
    import inspect
    import loop_agent_core.runner as runner_mod
    src = inspect.getsource(runner_mod.chat_impl)
    assert "active_summarize = summarize_fn if is_main_session else None" in src, \
        "worker 会话必须拿到 None 摘要器（否则子 agent 也会触发压缩）"
    assert "summarize_fn=None" in src, "chat_impl 的 summarize_fn 必须有默认值（外部契约）"


def test_validate_summary():
    good = "## 用户目标\nG\n## 已完成\nD\n## 关键决策与结论\nC\n## 改动的文件\nF\n## 未决问题与下一步\nO"
    assert comp.validate_summary(good)
    assert not comp.validate_summary("")
    assert not comp.validate_summary("太短了")
    assert not comp.validate_summary("## 用户目标\n只有一个小标题")
    assert not comp.validate_summary(None)


def test_apply_and_expand_roundtrip():
    _env(COMPACT_MAX_TOKENS=1500)
    conv = _long_conv(rounds=4)
    before_msgs = json.dumps(conv["messages"], ensure_ascii=False)
    q_idx = len(conv["messages"]) - 1
    start, end = comp.compaction_span(conv, q_idx)
    assert start == 1, "首轮就从索引 1 开始（紧贴 system）"
    body = "## 用户目标\nG\n## 已完成\nD\n## 关键决策与结论\nC\n## 改动的文件\nF\n## 未决问题与下一步\nO"
    new_q, removed = comp.apply_compaction(conv, q_idx, start, end, body)
    msgs = conv["messages"]
    assert removed == end - start
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == "sys"
    # 摘要必须出现在"当前问题"之前（system 独立消息或合并进 user 前缀两种形态都算通过）
    assert msgs[new_q]["role"] == "user" and "当前问题" in str(msgs[new_q]["content"])
    summaries = [i for i, m in enumerate(msgs) if isinstance(m.get("content"), str)
                 and m["content"].startswith("[上下文摘要]")]
    assert len(summaries) == 1 and summaries[0] < new_q, (summaries, new_q)
    # 不产生连续 user
    for a, b in zip(msgs, msgs[1:]):
        assert not (a.get("role") == "user" and b.get("role") == "user"), "出现连续 user 消息"
    assert comp.latest_compaction(conv) is not None
    assert comp.expand_last(conv) is True
    assert json.dumps(conv["messages"], ensure_ascii=False) == before_msgs, "还原必须逐条相等"
    _env(COMPACT_MAX_TOKENS=None)


def test_apply_compaction_merged_shape_and_roundtrip():
    """span 起点 > 1 时（现实中最常见的中段压缩），摘要被并入 user 消息前缀，也必须能精确还原。

    直接针对 apply_compaction 构造确定的 span：不依赖 compaction_span 的推导，
    这样"合并分支"永远被覆盖（span 起点是否会 >1 由 span 自己的测试负责）。
    """
    _env(COMPACT_MAX_TOKENS=1500)
    msgs = [
        _mkmsg("system", "sys"),
        _mkmsg("user", "最初的问题"),
        _mkmsg("assistant", "最初的答复"),
        _mkmsg("user", "问题 A"),
        _mkmsg("assistant", "", tool_calls=_tc("cA", args='{"path":"a.py"}')),
        _mkmsg("tool", "TOOLRESULT " * 200, tool_call_id="cA"),
        _mkmsg("assistant", "答复 A"),
        _mkmsg("user", "当前问题"),
    ]
    conv = _conv(msgs)
    q_idx = len(msgs) - 1
    before_msgs = json.dumps(msgs, ensure_ascii=False)
    body = "## 用户目标\nG\n## 已完成\nD\n## 关键决策与结论\nC\n## 改动的文件\nF\n## 未决问题与下一步\nO"

    new_q, removed = comp.apply_compaction(conv, q_idx, 3, 7, body)
    assert removed == 4
    merged = [i for i, m in enumerate(msgs) if isinstance(m.get("content"), str)
              and m["content"].startswith("[上下文摘要]")]
    assert len(merged) == 1, "摘要必须只有一条"
    assert msgs[merged[0]]["role"] == "user", "中段压缩时摘要应并入 user 消息（不产生裸 user）"
    assert conv["_archive"][-1]["shape"] == "merged"
    assert msgs[new_q]["role"] == "user" and "当前问题" in str(msgs[new_q]["content"])
    # 不产生连续 user
    for a, b in zip(msgs, msgs[1:]):
        assert not (a.get("role") == "user" and b.get("role") == "user")
    assert comp.expand_last(conv) is True
    assert json.dumps(msgs, ensure_ascii=False) == before_msgs, "合并形态还原必须逐条相等"
    assert conv["size"] == la.estimate_tokens(json.dumps(msgs, ensure_ascii=False)), "还原后记账必须归位"
    _env(COMPACT_MAX_TOKENS=None)


def test_archive_persist_and_load():
    _env(COMPACT_ARCHIVE_MAX_CHARS=200000)
    conv = _long_conv(rounds=3)
    q_idx = len(conv["messages"]) - 1
    start, end = comp.compaction_span(conv, q_idx)
    comp.record_archive(conv, conv["messages"][start:end])
    path = os.path.join(la.WORK_DIR, "tmp", "_selftest_archive.json")
    try:
        assert comp.persist_archive(conv, path) is True
        fresh = {"messages": [{ "role": "system", "content": "sys"}], "size": 0}
        assert comp.load_archive(fresh, path) is True
        assert len(fresh["_archive"]) == 1
        hits = comp.find_archive(fresh, "FILECONTENT0", limit=3)
        assert hits, "档案里应能检索到原文"
    finally:
        if os.path.exists(path):
            os.remove(path)
    _env(COMPACT_ARCHIVE_MAX_CHARS=None)


def test_archive_char_cap_drops_oldest():
    _env(COMPACT_ARCHIVE_MAX_CHARS=500)
    conv = _long_conv(rounds=2)
    for _ in range(5):
        comp.record_archive(conv, conv["messages"][1:3])
    assert len(conv["_archive"]) < 5, "超上限必须丢最旧条目"
    _env(COMPACT_ARCHIVE_MAX_CHARS=None)


# ─────────────────────── 三层串联：_prep_context（L0 → L1 → L2） ───────────────────────

LARGE_CAP = 10 ** 9  # 测试用：把 cap 设大，避免 _prep_context 内部的 L2 抢先于 L1 触发


def _prep_conv():
    """含"重复工具结果 + 超阈值"的会话，用来验证 L0→L1→L2 的串联顺序。

    必须**以 user 开头**：compaction_span 的起点只能落在某轮的第一个 user 上，
    否则区间会被前移到空、返回 no_span。
    """
    body = "DUPLICATED-CONTENT " * 200
    msgs = [
        _mkmsg("system", "sys"),
        _mkmsg("user", "请读 a.py"),
        _mkmsg("assistant", "", tool_calls=_tc("c1", args='{"path":"a.py"}')),
        _mkmsg("tool", body, tool_call_id="c1"),
        _mkmsg("assistant", "答复 1 " + "填充" * 200),
        _mkmsg("user", "再读一次 a.py"),
        _mkmsg("assistant", "", tool_calls=_tc("c2", args='{"path":"a.py"}')),
        _mkmsg("tool", body, tool_call_id="c2"),
        _mkmsg("assistant", "答复 2 " + "填充" * 200),
        _mkmsg("user", "CURRENT"),
    ]
    conv = _conv(msgs)
    conv.update({"_compact_count": 0, "_compact_last_turn": None, "_user_turn": 1,
                 "_dedup_saved": 0, "_compact_token": None})
    return conv, msgs


def test_prep_context_chains_l0_then_l1():
    from loop_agent_core.context import _prep_context
    # trigger_ratio=0 → 压缩阈值降到 0，任何非空会话都必然达阈值（避免依赖 PruneCap 的绝对值）
    _env(DEDUP_ENABLED=1, DEDUP_MIN_CHARS=10, COMPACT_ENABLED=1, COMPACT_TRIGGER_RATIO=0,
         COMPACT_BLOCK_TOKENS=100000, COMPACT_MIN_RECLAIM=0.0, COMPACT_MAX_TOKENS=15000)
    conv, msgs = _prep_conv()
    q_idx = len(msgs) - 1
    events = []
    body = ("## 用户目标\nG\n## 已完成\nD\n## 关键决策与结论\nC\n"
            "## 改动的文件\nF\n## 未决问题与下一步\nO")

    def fake(prefix, block):
        return {"summary": body, "digest": comp._digest(prefix + body)}

    new_q, _ = _prep_context(LARGE_CAP, q_idx, conv, fake, events.append)
    kinds = [e["type"] for e in events]
    assert "deduped" in kinds, kinds
    assert "compacting" in kinds and "compacted" in kinds, kinds
    # 顺序：L0 必须先于 L1
    assert kinds.index("deduped") < kinds.index("compacting"), kinds
    # L0 生效：确实替换了 1 条（实测 payload，而不是去 messages 里找占位符 —— L1 之后那些消息
    # 本身已被摘要带走，在 messages 里找不到是正常现象）
    deduped = next(e for e in events if e["type"] == "deduped")
    assert deduped["replaced"] == 1, deduped
    assert deduped["saved"] > 0, deduped
    # L1 生效：出现且仅出现一条摘要，当前问题仍在
    assert sum(1 for m in msgs if isinstance(m.get("content"), str)
               and m["content"].startswith("[上下文摘要]")) == 1
    assert "CURRENT" in str(msgs[new_q].get("content"))
    assert conv["_compact_count"] == 1
    # 每次压缩只允许产生一条档案（曾出现 _prep_context 与 apply_compaction 各记一次 → 重复档案）
    assert len(conv.get("_archive") or []) == 1, conv.get("_archive")
    # 最终 size 必须严格小于压缩前（否则 L1 白做）
    assert conv["size"] < deduped["before"]
    _env(DEDUP_ENABLED=None, DEDUP_MIN_CHARS=None, COMPACT_ENABLED=None,
         COMPACT_TRIGGER_RATIO=None, COMPACT_BLOCK_TOKENS=None, COMPACT_MIN_RECLAIM=None,
         COMPACT_MAX_TOKENS=None)


def test_prep_context_falls_back_to_l2_but_budget_never_blocks_it():
    """L2 是兜底：L1 被频率预算跳过时，硬裁剪仍必须发生（绝不能被预算削弱）。"""
    from loop_agent_core.context import _prep_context
    # 模拟"距上次压缩不足最小间隔"：把上次压缩轮次设成与当前轮次相同
    _env(DEDUP_ENABLED=1, DEDUP_MIN_CHARS=10, COMPACT_ENABLED=1, COMPACT_TRIGGER_RATIO=0,
         COMPACT_MIN_ROUNDS=99, COMPACT_MAX_PER_SESSION=99, COMPACT_MAX_TOKENS=15000)
    conv, msgs = _prep_conv()
    conv["_user_turn"] = 5
    conv["_compact_last_turn"] = 5
    q_idx = len(msgs) - 1
    events = []
    body = ("## 用户目标\nG\n## 已完成\nD\n## 关键决策与结论\nC\n"
            "## 改动的文件\nF\n## 未决问题与下一步\nO")

    def fake(prefix, block):
        return {"summary": body, "digest": comp._digest(prefix + body)}

    _prep_context(100, q_idx, conv, fake, events.append)
    kinds = [e["type"] for e in events]
    assert "compact_skipped" in kinds, kinds
    assert "pruned" in kinds, ("预算跳过 L1 后必须仍走 L2 兜底", kinds)
    assert conv["_compact_count"] == 0
    _env(DEDUP_ENABLED=None, DEDUP_MIN_CHARS=None, COMPACT_ENABLED=None,
         COMPACT_TRIGGER_RATIO=None, COMPACT_MIN_ROUNDS=None, COMPACT_MAX_PER_SESSION=None,
         COMPACT_MAX_TOKENS=None)


def test_prep_context_summarizer_failure_falls_back():
    """摘要器失败 → compact_failed，且会话结构仍合法（走 L2，不留下半折叠状态）。"""
    from loop_agent_core.context import _prep_context
    _env(DEDUP_ENABLED=0, COMPACT_ENABLED=1, COMPACT_TRIGGER_RATIO=0, COMPACT_MAX_TOKENS=15000)
    conv, msgs = _prep_conv()
    q_idx = len(msgs) - 1
    events = []

    def fake(prefix, block):
        return None

    new_q, _ = _prep_context(LARGE_CAP, q_idx, conv, fake, events.append)
    kinds = [e["type"] for e in events]
    assert "compact_failed" in kinds, kinds
    # 失败后不得留下任何摘要痕迹
    assert not any(isinstance(m.get("content"), str)
                   and m["content"].startswith("[上下文摘要]") for m in msgs)
    # 当前问题仍在，且序列合法
    assert "CURRENT" in str(msgs[new_q].get("content"))
    seen = set()
    for m in msgs:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            seen.update(tc["id"] for tc in m["tool_calls"])
        if m.get("role") == "tool":
            assert m["tool_call_id"] in seen, "回退后出现孤立 tool"
    _env(DEDUP_ENABLED=None, COMPACT_ENABLED=None, COMPACT_TRIGGER_RATIO=None,
         COMPACT_MAX_TOKENS=None)


def test_thin_shell_reexports_contract():
    """薄壳再导出契约（tui/测试直接 import 的符号一个都不能少）。"""
    for name in ("dedup_tool_results", "should_compact", "compaction_span", "split_blocks",
                 "apply_compaction", "expand_last", "latest_compaction", "record_archive",
                 "persist_archive", "load_archive", "build_block_prompt", "validate_summary",
                 "summary_reject_reason", "failure_hint", "skip_label", "log_compaction_failure",
                 "cstate", "next_turn", "reset_failures", "clear_compact_state", "_compact_state",
                 "conversation_problems",
                 "run_rolling_summary", "compaction_cap", "placeholder_text",
                 "persist_archive_sidecar", "adopt_archive", "clear_archive",
                 "expand_compaction", "compact_session", "summarize_block", "_compact_k"):
        assert hasattr(la, name), f"薄壳缺少契约符号 {name}"
    assert "recall_archive" in la.TOOL_CALL_MAP
    # worker 内必须禁用（与 recall/remember 一致）
    from loop_agent_core.runner import _WORKER_FORBIDDEN
    assert "recall_archive" in _WORKER_FORBIDDEN


def test_archive_sidecar_adopt_and_persist():
    """sidecar 往返：/load 后 /expand 与 recall_archive 仍可用（档案是同一个 list 对象）。"""
    path = os.path.join(la.WORK_DIR, "tmp", "_selftest_sidecar.json")
    try:
        conv = _long_conv(rounds=3)
        q_idx = len(conv["messages"]) - 1
        start, end = comp.compaction_span(conv, q_idx)
        # record_archive 需要 conv 里的 _archive 与薄壳的会话档案是同一对象
        conv["_archive"] = la._archive()
        comp.record_archive(conv, conv["messages"][start:end])
        assert la._archive(), "薄壳会话档案应被就地填充"
        assert la.persist_archive_sidecar(path) is True

        la.clear_archive()
        assert la._archive() == []
        assert la.adopt_archive(path) is True
        assert len(la._archive()) == 1
        assert la._archive()[0]["messages"], "取回的档案必须含原始消息"
        # 取回后模型侧检索工具可用
        out = la.recall_archive_mock("FILECONTENT1")
        assert "FILECONTENT1" in out
    finally:
        la.clear_archive()
        for p in (path, path + ".archive.json"):
            if os.path.exists(p):
                os.remove(p)


# ────────────── 摘要失败：诊断 + 失败退避（BUG 27/28 回归） ──────────────

_SUMMARY_BODY = ("## 用户目标\nG\n## 已完成\nD\n## 关键决策与结论\nC\n"
                 "## 改动的文件\nF\n## 未决问题与下一步\nO")


def _resp(text, finish="stop"):
    """伪造一次 chat.completions 响应（只需 choices[0].message.content / finish_reason）。"""
    import types as _t
    return _t.SimpleNamespace(choices=[_t.SimpleNamespace(
        finish_reason=finish, message=_t.SimpleNamespace(content=text))])


class _FakeClient:
    """按脚本依次返回响应或抛异常，并记录每次调用的 kwargs。"""

    def __init__(self, script):
        import types as _t
        self.script = list(script)
        self.calls = []
        outer = self

        class _Completions:
            def create(self, **kw):
                outer.calls.append(kw)
                item = outer.script.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        self.chat = _t.SimpleNamespace(completions=_Completions())


def _with_fake_summary_client(script):
    """临时把薄壳摘要器换成假 client（返回 (fake, 还原函数)）。"""
    fake = _FakeClient(script)
    old = la._summary_client
    la._summary_client = lambda: fake
    return fake, lambda: setattr(la, "_summary_client", old)


def test_summary_reject_reason_is_specific():
    """校验必须给出**具体**原因：只报"失败"等于没法定位（实测吃过大亏）。"""
    assert comp.summary_reject_reason(_SUMMARY_BODY) is None
    assert comp.validate_summary(_SUMMARY_BODY) is True
    assert comp.summary_reject_reason("") == "empty"
    assert comp.summary_reject_reason(None) == "not_text"
    assert comp.summary_reject_reason("太短了") == "too_short"
    reason = comp.summary_reject_reason("## 用户目标\n" + "内容" * 40)
    assert reason.startswith("missing_headings:"), reason
    assert "已完成" in reason and "未决问题与下一步" in reason, reason


def test_rolling_summary_reports_error_reason():
    """on_error 必须回传具体原因与块号（面板/日志据此给出可执行提示）。"""
    blocks = [[_mkmsg("user", "a")], [_mkmsg("user", "b")]]

    seen = []
    def truncated(prefix, block):
        return {"error": "output_truncated(max_tokens=6000)"}
    assert comp.run_rolling_summary(blocks, truncated,
                                    on_error=lambda w, i: seen.append((w, i))) is None
    assert seen == [("output_truncated(max_tokens=6000)", 0)], seen

    seen2 = []
    def boom(prefix, block):
        raise RuntimeError("connection reset")
    assert comp.run_rolling_summary(blocks, boom, on_error=lambda w, i: seen2.append((w, i))) is None
    assert "api_error:RuntimeError" in seen2[0][0] and "connection reset" in seen2[0][0], seen2

    # 校验不过时也要点名缺了哪些小标题
    seen3 = []
    assert comp.run_rolling_summary(blocks, lambda p, b: {"summary": "## 用户目标\n" + "内容" * 40},
                                    on_error=lambda w, i: seen3.append(w)) is None
    assert str(seen3[0]).startswith("missing_headings:"), seen3


def test_failure_hint_and_log(tmp_path=None):
    """失败提示必须可执行；失败必须落盘（TUI 里 stdout 会被界面覆盖）。"""
    hint = comp.failure_hint("output_truncated(max_tokens=6000)")
    assert "截断" in hint and "COMPACT_MAX_TOKENS" in hint, hint
    assert "COMPACT_BLOCK_TOKENS" in comp.failure_hint("missing_headings:已完成"), hint
    assert "compaction_error.log" in comp.failure_hint("api_error:BadRequest:xx")
    hard = comp.failure_hint("api_error:BadRequest:xx", disabled=True)
    assert "/compact" in hard, hard

    path = os.path.join(la.WORK_DIR, "tmp", "_selftest_compaction_error.log")
    try:
        if os.path.exists(path):
            os.remove(path)
        comp.log_compaction_failure("output_truncated(max_tokens=6000)",
                                    {"turn": 3, "fails": 1, "block": 2}, path=path)
        with open(path, encoding="utf-8") as f:
            line = json.loads(f.read().strip())
        assert line["reason"].startswith("output_truncated")
        assert line["turn"] == 3 and line["fails"] == 1 and line["block"] == 2, line
        assert line.get("time"), line
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_summarize_block_escalates_on_truncation():
    """BUG 27 回归：输出被 finish_reason=length 截断时必须升级预算重试，而不是整次压缩作废。"""
    fake, restore = _with_fake_summary_client([_resp("## 用户目标\n没写完", "length"),
                                               _resp(_SUMMARY_BODY, "stop")])
    try:
        res = la.summarize_block("前序摘要", [_mkmsg("user", "x")])
    finally:
        restore()
    assert res.get("summary") == _SUMMARY_BODY, res
    assert len(fake.calls) == 2, fake.calls
    assert fake.calls[1]["max_tokens"] > fake.calls[0]["max_tokens"], "必须升级输出预算"
    # 云端摘要器必须关掉思考：deepseek-v4-flash 默认开思考，实测吃掉近一半输出预算
    assert fake.calls[0].get("extra_body") == {"thinking": {"type": "disabled"}}, fake.calls[0]

    # 升到上限仍被截断 → 明确报"被截断"（绝不伪装成功，也不留半份摘要）
    _env(COMPACT_MAX_TOKENS=100, COMPACT_MAX_TOKENS_HARD=200)
    fake2, restore2 = _with_fake_summary_client([_resp("## 用户目标\n半截", "length"),
                                                 _resp("## 用户目标\n还是半截", "length")])
    try:
        res2 = la.summarize_block("p", [_mkmsg("user", "x")])
    finally:
        restore2()
        _env(COMPACT_MAX_TOKENS=None, COMPACT_MAX_TOKENS_HARD=None)
    assert str(res2.get("error", "")).startswith("output_truncated"), res2
    assert len(fake2.calls) == 2, "升级只允许一次，不能无限重试"

    # 小标题已齐、只是末尾被切 → 直接采用，不白扔一次调用
    fake3, restore3 = _with_fake_summary_client([_resp(_SUMMARY_BODY + "\n（末尾被切）", "length")])
    try:
        res3 = la.summarize_block("p", [_mkmsg("user", "x")])
    finally:
        restore3()
    assert res3.get("summary"), res3
    assert len(fake3.calls) == 1, fake3.calls


def test_summarize_block_drops_unsupported_param():
    """后端不认 extra_body（本地/换模型）时必须摘掉参数重试，不能因此放弃压缩。"""
    class _BadRequest(Exception):
        status_code = 400

    fake, restore = _with_fake_summary_client([_BadRequest("unknown parameter: thinking"),
                                               _resp(_SUMMARY_BODY, "stop")])
    try:
        res = la.summarize_block("p", [_mkmsg("user", "x")])
    finally:
        restore()
    assert res.get("summary") == _SUMMARY_BODY, res
    assert len(fake.calls) == 2 and "extra_body" not in fake.calls[1], fake.calls


def test_compaction_failure_backoff_stops_retry_storm():
    """BUG 28 回归：一轮内 N 次 API 调用只允许尝试一次（否则面板被"压缩失败"刷满 + 白烧 token）。"""
    from loop_agent_core.context import _prep_context
    _env(DEDUP_ENABLED=0, COMPACT_ENABLED=1, COMPACT_TRIGGER_RATIO=0, COMPACT_MIN_ROUNDS=3,
         COMPACT_MAX_TOKENS=15000, COMPACT_MAX_FAILS=3)
    conv, msgs = _prep_conv()
    q_idx = len(msgs) - 1
    calls, events = [], []

    def fake(prefix, block):
        calls.append(1)
        return None

    for _ in range(5):  # 模拟同一轮里的 5 次工具调用 → 5 次 _prep_context
        _prep_context(LARGE_CAP, q_idx, conv, fake, events.append)

    assert len(calls) == 1, f"同一轮内不允许重复尝试（实际 {len(calls)} 次）"
    kinds = [e["type"] for e in events]
    assert kinds.count("compact_failed") == 1, kinds
    failed = next(e for e in events if e["type"] == "compact_failed")
    assert failed["attempt"] == 1 and failed["max_attempts"] == 3, failed
    assert failed["hint"], failed
    assert conv["_compact_fails"] == 1
    assert conv["_compact_retry_turn"] == conv["_user_turn"] + 3, conv["_compact_retry_turn"]
    _env(DEDUP_ENABLED=None, COMPACT_ENABLED=None, COMPACT_TRIGGER_RATIO=None,
         COMPACT_MIN_ROUNDS=None, COMPACT_MAX_TOKENS=None, COMPACT_MAX_FAILS=None)


def test_compaction_failure_hard_stop_then_manual_reset():
    """连续失败到上限即停用 L1（确定性失败重试无意义）；/compact 手动重试必须能解锁。"""
    _env(COMPACT_ENABLED=1, COMPACT_TRIGGER_RATIO=0, COMPACT_MIN_ROUNDS=3, COMPACT_MAX_FAILS=3)
    conv = _long_conv(rounds=3)
    conv["_compact_state"] = {}
    st = comp.cstate(conv)

    def fail_once():
        return None

    attempts = []
    for _ in range(3):
        turn = comp.next_turn(conv)
        st["_compact_retry_turn"] = None   # 模拟退避轮数已过
        ok, why = comp.should_compact(conv)
        assert ok, (why, turn)
        attempts.append(turn)
        # 直接走失败记账（等价于 _prep_context 的失败分支）
        st["_compact_fails"] = (st.get("_compact_fails") or 0) + 1
        backoff = min(3 * (2 ** (st["_compact_fails"] - 1)), 32)
        st["_compact_retry_turn"] = turn + backoff
    assert attempts == [1, 2, 3], attempts
    assert st["_compact_retry_turn"] == 3 + 12, st   # 3*2^(3-1) 封顶前
    assert comp.should_compact(conv) == (False, "failed"), "连续失败到上限必须停用 L1"
    # 退避轮数也必须是"跨轮"的：next_turn 递增后仍在退避窗口内
    comp.next_turn(conv)
    assert comp.should_compact(conv)[1] in ("failed", "backoff")
    comp.reset_failures(conv)
    st["_compact_retry_turn"] = None
    assert comp.should_compact(conv)[0] is True, "手动重试必须能解锁"
    _env(COMPACT_ENABLED=None, COMPACT_TRIGGER_RATIO=None, COMPACT_MIN_ROUNDS=None,
         COMPACT_MAX_FAILS=None)


def test_compact_state_survives_new_conv_wrapper():
    """BUG 28 根因回归：conv 每次 chat() 都是新 dict，计数器必须挂在共享 state 上。

    否则 MIN_ROUNDS / MAX_PER_SESSION 退化成"每轮"口径 —— interval 永不成立，
    达阈值时**每个用户轮**都会重试一次压缩。
    """
    _env(COMPACT_TRIGGER_RATIO=0, COMPACT_MAX_PER_SESSION=7)
    shared = {}
    conv1 = _long_conv(rounds=3)
    conv1["_compact_state"] = shared
    assert comp.next_turn(conv1) == 1
    comp.cstate(conv1)["_compact_count"] = 7
    comp.cstate(conv1)["_compact_last_turn"] = 1

    # 下一次 chat() 造出的新包装 dict：messages 相同、state 共享
    conv2 = {"messages": conv1["messages"], "size": conv1["size"], "_compact_state": shared}
    assert comp.next_turn(conv2) == 2, "轮次必须跨包装连续（否则 backoff/interval 全废）"
    assert comp.should_compact(conv2) == (False, "budget"), "次数预算必须跨轮生效"

    # 没有共享 state 时（对照）计数器确实会丢 —— 这正是线上"频繁压缩"的成因
    conv3 = {"messages": conv1["messages"], "size": conv1["size"]}
    assert comp.cstate(conv3) is conv3
    assert comp.should_compact(conv3)[0] is True
    _env(COMPACT_TRIGGER_RATIO=None, COMPACT_MAX_PER_SESSION=None)


def test_interrupted_compaction_is_not_a_failure():
    """用户按 Esc 不算摘要器失败：不能计入退避（否则按三次 Esc 就把 L1 停用了）。"""
    from loop_agent_core.context import _prep_context
    from loop_agent_core.control import interrupt_event
    _env(DEDUP_ENABLED=0, COMPACT_ENABLED=1, COMPACT_TRIGGER_RATIO=0, COMPACT_MAX_FAILS=2)
    conv, msgs = _prep_conv()
    q_idx = len(msgs) - 1
    events = []
    interrupt_event.set()
    try:
        _prep_context(LARGE_CAP, q_idx, conv, lambda p, b: _SUMMARY_BODY, events.append)
    finally:
        interrupt_event.clear()
    kinds = [e["type"] for e in events]
    assert "compact_failed" not in kinds, kinds
    assert any(e["type"] == "compact_skipped" and e["reason"] == "interrupted"
               for e in events), kinds
    assert (conv.get("_compact_fails") or 0) == 0, conv.get("_compact_fails")
    _env(DEDUP_ENABLED=None, COMPACT_ENABLED=None, COMPACT_TRIGGER_RATIO=None,
         COMPACT_MAX_FAILS=None)


def test_cross_turn_attempt_cadence():
    """E2E（假 API，走真实 chat_impl）：跨用户轮的尝试节奏必须受退避约束。

    这是唯一能抓住"conv 每个用户轮都是新建的 dict → 计数器归零 → 达阈值就每轮重试"这类
    **跨轮管线** bug 的测试：单元测试直接往 conv 上写计数器，永远看不到归零这件事。
    """
    import types as _t
    import loop_agent_core.runner as runner_mod

    def _fake_create(conv, **kw):
        usage = _t.SimpleNamespace(prompt_tokens=100, completion_tokens=5, total_tokens=105)
        return iter([_t.SimpleNamespace(
            choices=[_t.SimpleNamespace(delta=_t.SimpleNamespace(
                content="好的。", reasoning_content=None, tool_calls=None))], usage=None),
            _t.SimpleNamespace(choices=[], usage=usage)])

    saved_msgs = list(la.messages)
    saved_size = la._msg_size
    saved_state = dict(la._session_state)
    saved_create = runner_mod._safe_create
    saved_summary = la.summarize_block
    calls = []
    _env(COMPACT_ENABLED=1, COMPACT_TRIGGER_RATIO=0, COMPACT_MIN_ROUNDS=3,
         COMPACT_MAX_FAILS=2, DEDUP_ENABLED=0)
    try:
        la.messages.clear()
        la.messages.append(_mkmsg("system", "sys"))
        la._msg_size = None
        la.clear_archive()                      # 顺带清压缩记账
        runner_mod._safe_create = _fake_create
        la.summarize_block = lambda prefix, block: (calls.append(1), None)[1]

        seen = []
        for i in range(1, 6):                   # 5 个用户轮
            events = []
            la.chat(f"第 {i} 轮问题", on_event=events.append)
            st = la._compact_state()
            seen.append((st["_user_turn"],
                         [e.get("reason") for e in events
                          if e["type"] in ("compact_failed", "compact_skipped")]))
        # 第 1 轮没有可摘要区间；之后 3 轮退避，只有第 5 轮（turn 2 失败后 retry_at=5）再试一次
        assert [t for t, _ in seen] == [1, 2, 3, 4, 5], seen
        assert len(calls) == 2, f"退避失效：{len(calls)} 次尝试（无退避会是 4 次）"
        assert seen[1][1] and seen[1][1][0] == "no_result", seen[1]
        assert seen[2][1] == ["backoff"] and seen[3][1] == ["backoff"], seen[2:4]
        assert seen[4][1] == ["no_result"], seen[4]      # 第 5 轮允许再试（退避窗口已过）
        st = la._compact_state()
        assert st["_compact_fails"] == 2, st
        # 手动 /compact 是明确意图：必须能越过"连续失败硬停"，否则用户没有任何自救手段
        before = len(calls)
        la.compact_session()
        assert len(calls) == before + 1, "手动 /compact 被硬停挡掉了"
    finally:
        runner_mod._safe_create = saved_create
        la.summarize_block = saved_summary
        la.messages.clear()
        la.messages.extend(saved_msgs)
        la._msg_size = saved_size
        la._session_state.clear()
        la._session_state.update(saved_state)
        _env(COMPACT_ENABLED=None, COMPACT_TRIGGER_RATIO=None, COMPACT_MIN_ROUNDS=None,
             COMPACT_MAX_FAILS=None, DEDUP_ENABLED=None)


def test_compact_command_never_wipes_history():
    """BUG 30 回归：`/compact` 曾把 cap=0 传给 L2 → 低于阈值时直接把历史删到 system+当前问题。

    `/compact` 同时是失败提示里推荐的**自救路径**，所以它既不能毁数据，也不能被排期闸门挡住。
    """
    saved_msgs = list(la.messages)
    saved_size = la._msg_size
    saved_state = dict(la._session_state)
    saved_summary = la.summarize_block
    _env(COMPACT_ENABLED=1, COMPACT_TRIGGER_RATIO=0.5)   # 默认阈值：这个会话远低于它
    try:
        la.messages.clear()
        la.messages.append(_mkmsg("system", "sys"))
        for i in range(3):
            la.messages.append(_mkmsg("user", f"q{i} " + "x" * 200))
            la.messages.append(_mkmsg("assistant", f"a{i} " + "y" * 200))
        la.messages.append(_mkmsg("user", "CURRENT"))
        la._msg_size = la.estimate_tokens(json.dumps(la.messages, ensure_ascii=False))
        la.clear_archive()
        before = len(la.messages)

        # (a) 摘要器失败时，L2 兜底也只能按正常 cap：绝不能清空历史
        la.summarize_block = lambda prefix, block: None
        msg = la.compact_session()
        assert len(la.messages) == before, f"/compact 动了历史：{before} → {len(la.messages)}（{msg}）"
        assert "压缩失败" in msg and "摘要" in msg, msg   # 失败必须给出可执行的原因

        # (b) 低于触发阈值也必须真的尝试（/help 承诺"立即压缩"）
        calls = []
        la.summarize_block = lambda prefix, block: (calls.append(1), None)[1]
        la.compact_session()
        assert calls, "force 未生效：低于阈值时 /compact 根本没尝试压缩"
    finally:
        la.summarize_block = saved_summary
        la.messages.clear()
        la.messages.extend(saved_msgs)
        la._msg_size = saved_size
        la._session_state.clear()
        la._session_state.update(saved_state)
        _env(COMPACT_ENABLED=None, COMPACT_TRIGGER_RATIO=None)


# ────────────── 会话文件校验：sidecar 不能被当成会话加载（BUG 31） ──────────────

def test_conversation_problems_detects_bad_shapes():
    """坏结构必须能在本地被点名（API 端只说 `messages[0]: missing field role`）。"""
    assert la.conversation_problems([_mkmsg("system", "s"), _mkmsg("user", "q")]) == []
    assert la.conversation_problems([]) == ["不是非空的消息数组"]
    assert la.conversation_problems("不是 list") == ["不是非空的消息数组"]
    # 折叠档案条目：是 dict、是 list 元素，但没有 role —— 正是线上 422 的形态
    entry = {"ts": "2026-10-11 20:27:01", "messages": [_mkmsg("user", "q")],
             "inserted": "...", "shape": "system"}
    problems = la.conversation_problems([entry, entry])
    assert problems and "role" in problems[0], problems
    assert len(problems) <= 3, "问题清单要有界（样例会截断）"


def test_sidecar_is_never_listed_or_loaded_as_conversation():
    """BUG 31 回归：sidecar 名 `<会话>.json.archive.json` 也匹配 `chat_*.json`。

    直接测 TUI 用的两个函数 + 断言 `/load` 确实接了它们（否则改回旧口径测试也发现不了）。
    """
    import inspect
    import tui

    assert tui.is_conversation_file("chat_20261011_202701.json") is True
    assert tui.is_conversation_file("chat_20261011_202701.json.archive.json") is False, \
        "折叠档案 sidecar 不是会话"
    assert tui.is_conversation_file(".autosave.json") is False
    assert tui.is_conversation_file("chat_20261011_202701.json.bak") is False

    # 接线：列表、加载、autosave 恢复三处都必须走这套校验
    for fn in (tui.AgentTUI._cmd_load, tui.AgentTUI._load_file, tui.AgentTUI._try_recover_autosave):
        src = inspect.getsource(fn)
        assert "is_conversation_file" in src or "read_conversation" in src, fn.__name__

    tmpdir = os.path.join(la.WORK_DIR, "tmp")
    os.makedirs(tmpdir, exist_ok=True)
    good = os.path.join(tmpdir, "_selftest_conv_good.json")
    sidecar = os.path.join(tmpdir, "_selftest_conv_good.json.archive.json")
    try:
        msgs = [_mkmsg("system", "s"), _mkmsg("user", "你好"), _mkmsg("assistant", "在")]
        with open(good, "w", encoding="utf-8") as f:
            json.dump(msgs, f, ensure_ascii=False)
        with open(sidecar, "w", encoding="utf-8") as f:   # sidecar 内容是档案条目数组
            json.dump([{"ts": "x", "messages": msgs, "inserted": "y", "shape": "system"}],
                      f, ensure_ascii=False)

        loaded, err = tui.read_conversation(good)
        assert err == "" and loaded == msgs, (err, loaded)
        loaded2, err2 = tui.read_conversation(sidecar)
        assert loaded2 is None and "role" in err2, (loaded2, err2)
        _, err3 = tui.read_conversation(os.path.join(tmpdir, "_selftest_conv_missing.json"))
        assert err3, "读不存在的文件必须报错而不是返回空"
    finally:
        for p in (good, sidecar):
            if os.path.exists(p):
                os.remove(p)


def test_chat_refuses_corrupted_history():
    """E2E 护栏：历史结构坏了要在**发请求之前**拒绝，并给出可照做的提示（而不是 422 天书）。"""
    import types as _t
    import loop_agent_core.runner as runner_mod

    saved_msgs = list(la.messages)
    saved_size = la._msg_size
    saved_create = runner_mod._safe_create
    calls = []

    def _fake_create(conv, **kw):
        calls.append(kw)
        return iter([])

    try:
        # 模拟"把 sidecar 当会话加载"后的历史
        la.messages.clear()
        la.messages.append({"ts": "x", "messages": [_mkmsg("user", "q")], "shape": "system"})
        la._msg_size = None
        runner_mod._safe_create = _fake_create
        events = []
        out = la.chat("随便问一句", on_event=events.append)
        assert not calls, "结构坏了就不该发请求"
        assert "[Internal error]" in out and "role" in out, out
        errs = [e for e in events if e.get("type") == "error"]
        assert errs and "/clear" in errs[0]["message"], events
    finally:
        runner_mod._safe_create = saved_create
        la.messages.clear()
        la.messages.extend(saved_msgs)
        la._msg_size = saved_size


if __name__ == "__main__":
    tests = [
        test_dedup_key_double_condition,
        test_dedup_key_range_reads_not_merged,
        test_dedup_keeps_last_only,
        test_dedup_protocol_intact_and_accounting,
        test_dedup_disabled_and_threshold,
        test_disabled_matches_legacy_prune,
        test_should_compact_threshold_and_budget,
        test_span_never_cuts_tool_chain,
        test_split_blocks_respects_group_boundaries_and_limit,
        test_rolling_summary_calls_and_prefix_stability,
        test_rolling_summary_aborts_on_failure,
        test_worker_session_never_compacts,
        test_validate_summary,
        test_apply_and_expand_roundtrip,
        test_apply_compaction_merged_shape_and_roundtrip,
        test_archive_persist_and_load,
        test_archive_char_cap_drops_oldest,
        test_prep_context_chains_l0_then_l1,
        test_prep_context_falls_back_to_l2_but_budget_never_blocks_it,
        test_prep_context_summarizer_failure_falls_back,
        test_summary_reject_reason_is_specific,
        test_rolling_summary_reports_error_reason,
        test_failure_hint_and_log,
        test_summarize_block_escalates_on_truncation,
        test_summarize_block_drops_unsupported_param,
        test_compaction_failure_backoff_stops_retry_storm,
        test_compaction_failure_hard_stop_then_manual_reset,
        test_compact_state_survives_new_conv_wrapper,
        test_interrupted_compaction_is_not_a_failure,
        test_cross_turn_attempt_cadence,
        test_compact_command_never_wipes_history,
        test_conversation_problems_detects_bad_shapes,
        test_sidecar_is_never_listed_or_loaded_as_conversation,
        test_chat_refuses_corrupted_history,
        test_thin_shell_reexports_contract,
        test_archive_sidecar_adopt_and_persist,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL  {t.__name__}: {e!r}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    if os.path.exists(_SELFTEST_ERRLOG):
        os.remove(_SELFTEST_ERRLOG)   # 测试制造的失败日志不留痕
    sys.exit(1 if failed else 0)
