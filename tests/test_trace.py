"""trace / checkpoint（L1 层）的落盘事实与自洽性。Run: python tests/test_trace.py

这组用例守的不是"能跑"，而是 trace 里那些**回放判定离不开的不变量**：

1. request_index 全 trace 只增（由 writer 分配）。它一旦退化成"轮内序号"，
   每个工具轮的第一个请求都会拿到 0，checkpoint 全部撞在一起，seek 只能靠猜。
2. checkpoint 的 request_index 与 llm_request 的**同一个坐标系**，且
   seek 出来的 messages 必须与录制那次请求的 messages 逐字节一致。
3. 关键事件一个不少：turn_end（带闭集 reason）、token_usage、tool_call/tool_result、
   llm_request/llm_end、run_start/run_end。
4. 解析耗时有上限：seek 一次不应随着会话变长而把所有帧都解压一遍。

假后端挂在**网关底下**（llm.set_transport），而不是替换 runner.client：后者会绕过网关，
于是 llm_request / llm_end / llm_chunk 全部不产生——那种"绿"是没有意义的绿。
"""
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DS_KEY", "sk-selftest-not-a-real-key")
os.environ.setdefault("FILTERED_LOG", os.path.join(ROOT, "tmp", "_selftest_trace_filtered.log"))
os.environ.setdefault("CACHE_LOG", os.path.join(ROOT, "tmp", "_selftest_trace_cache.log"))
# trace 目录必须落在 tmp/ 下（CI 判"出现 tmp/traces/** 即失败"是仓库根的 tmp）
_TRACE_ROOT = os.path.join(ROOT, "tmp", "traces-selftest")
os.environ["TRACE_ENABLED"] = "1"
os.environ["TRACE_DIR"] = _TRACE_ROOT

import loop_agent_core.checkpoint as cp  # noqa: E402
import loop_agent_core.llm as llm  # noqa: E402
import loop_agent_core.trace as trace  # noqa: E402
import loop_agent_v2 as la  # noqa: E402


# ── 假后端（挂在网关下） ────────────────────────────────────────
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


class _FakeTransport:
    def __init__(self, batches):
        self._batches = list(batches)
        self.calls = 0

    def create(self, kind, cfg, kwargs):
        idx = min(self.calls, len(self._batches) - 1)
        self.calls += 1
        return _Stream(self._batches[idx])


_thinking_batch = [_Chunk(_Delta(reasoning="我再想想")),
                   _Chunk(_Delta(), finish_reason="stop"),
                   _Chunk(usage=True, no_choices=True)]
_tool_batch = [[_Chunk(_Delta(tool_calls=[_ToolCall(0, "c1", "get_date", "{}")])),
                _Chunk(_Delta(), finish_reason="tool_calls"),
                _Chunk(usage=True, no_choices=True)],
               [_Chunk(_Delta(content="今天是某天"), finish_reason="stop"),
                _Chunk(usage=True, no_choices=True)]]


def _fresh_session(name):
    """开一场新的 trace：独立目录，且把进程级缓存清掉。"""
    sid = f"{name}-{int(time.time() * 1000) % 1000000}"
    run_dir = os.path.join(_TRACE_ROOT, sid)
    shutil.rmtree(run_dir, ignore_errors=True)
    cp.close_store()
    cp.reset_counters()
    return sid, run_dir


def _run(batches, question="干活", name="t", level=None):
    sid, run_dir = _fresh_session(name)
    conv = {"messages": [{"role": "system", "content": "t"}], "size": None,
            "_archive": [], "_compact_state": {}, "_session_id": sid}
    events = []
    transport = _FakeTransport(batches)
    llm.set_transport(transport)
    w = trace.open_writer(sid, run_dir=run_dir, level=level,
                          manifest={"session_id": sid, "backend": "fake",
                                    "model": "deepseek-v4-flash"})
    try:
        out = la.chat(question, on_event=events.append, _conv=conv)
    finally:
        llm.reset()
        # 索引在关文件**之前**写：关掉之后 writer 就不知道自己写过的字节位置了
        w.write_index()
        trace.close_writer(w)
        cp.close_store()
    return {"out": out, "events": events, "conv": conv, "dir": run_dir,
            "stored": trace.read_events(run_dir), "transport": transport, "sid": sid}


def _types(evs):
    return [e["type"] for e in evs]


def _reqs(evs):
    return [e for e in evs if e["type"] == "llm_request"]


# ── 1. 事件流自洽 ───────────────────────────────────────────────
def test_event_sequence_is_self_consistent():
    """seq 连续、request_index 只增、首 run_start 尾 run_end、validate 干净。"""
    r = _run([_thinking_batch], name="selfconsistent")
    evs = r["stored"]
    assert evs, "trace 里一条事件都没有"
    assert evs[0]["type"] == "run_start", _types(evs)[:5]
    assert evs[-1]["type"] == "run_end", _types(evs)[-5:]
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1)), "seq 不连续"
    assert trace.validate_events(evs) == [], trace.validate_events(evs)


def test_request_index_is_monotonic_across_tool_rounds():
    """每个工具轮都发一次请求，序号必须继续往上走（不能每轮从 0 重来）。"""
    r = _run([_thinking_batch], name="monotonic")
    ri = [e["request_index"] for e in _reqs(r["stored"])]
    assert ri == sorted(set(ri)) and len(ri) == len(set(ri)), f"request_index 重复：{ri}"
    assert ri[0] >= 1, ri
    # 4 次只会思考的重试 → 4 个请求，序号 1..4
    assert ri == [1, 2, 3, 4], ri
    turn = [e["turn"] for e in r["stored"] if e["type"] == "turn_end"]
    assert turn == [0], turn


def test_turn_end_reason_in_vocab():
    """只出思考 → turn_end.reason=stuck；词表之外的 reason 必须当场暴露。"""
    r = _run([_thinking_batch], name="turnend")
    ends = [e for e in r["stored"] if e["type"] == "turn_end"]
    assert len(ends) == 1, f"应恰好一条 turn_end，实际 {len(ends)}"
    assert ends[0]["reason"] == "stuck", ends[0]
    assert ends[0]["reason"] in trace.TURN_END_REASONS


def test_answer_turn_end_reason():
    """正常作答 → turn_end.reason=answer（不是 stuck/garbage）。"""
    batch = [[_Chunk(_Delta(content="答案是 42"), finish_reason="stop"),
              _Chunk(usage=True, no_choices=True)]]
    r = _run(batch, name="answer")
    ends = [e for e in r["stored"] if e["type"] == "turn_end"]
    assert ends and ends[0]["reason"] == "answer", ends


def test_llm_request_and_response_pair_up():
    """每次 llm_request 都有 llm_end；chunk 归到正确的 request_index。"""
    r = _run([_thinking_batch], name="pair")
    evs = r["stored"]
    reqs, resps = _reqs(evs), [e for e in evs if e["type"] == "llm_end"]
    assert len(reqs) == len(resps) == 4, (len(reqs), len(resps))
    assert [e["request_index"] for e in reqs] == [e["request_index"] for e in resps]
    assert all(e.get("finish_reason") is not None for e in resps), resps
    usage = [e for e in evs if e["type"] == "token_usage"]
    assert len(usage) == 4, len(usage)
    assert usage[0]["usage"]["prompt_tokens"] == 100


def test_tool_call_and_result_are_recorded():
    """工具轮：tool_call 记 name/class/arguments，tool_result 记结果与错误码。"""
    r = _run(_tool_batch, question="今天几号", name="tool")
    evs = r["stored"]
    tcs = [e for e in evs if e["type"] == "tool_call"]
    trs = [e for e in evs if e["type"] == "tool_result"]
    assert len(tcs) == 1 and len(trs) == 1, (tcs, trs)
    assert tcs[0]["tool_name"] == "get_date", tcs[0]
    assert tcs[0]["call_id"] == "c1", tcs[0]
    assert tcs[0]["arguments_raw"] == "{}", tcs[0]
    assert tcs[0]["tool_class"] == "A", tcs[0]
    assert trs[0]["call_id"] == "c1", trs[0]
    assert trs[0]["result_full_chars"] > 0, trs[0]
    # 两通道：result_full 是原始结果，result_stored 是进上下文的那一份，truncation 记预算
    assert trs[0]["result_stored"] is not None, trs[0]
    assert trs[0]["truncation"]["cap"] > 0, trs[0]
    assert trs[0]["truncation"]["truncated"] is False, trs[0]
    assert trace.validate_events(evs) == [], trace.validate_events(evs)


def test_run_end_reason_normal():
    """正常返回答案 → run_end.reason=normal（回放靠它区分"跑完"和"崩了"）。"""
    r = _run([_thinking_batch], name="runend")
    end = [e for e in r["stored"] if e["type"] == "run_end"][-1]
    assert end["reason"] == "normal", end


# ── 2. checkpoint ───────────────────────────────────────────────
def test_checkpoint_index_matches_every_request():
    """每个 request_index 都能 seek 出**与录制 messages 完全相同**的那份快照。"""
    r = _run(_tool_batch, question="今天几号", name="ckpt-match")
    evs = r["stored"]
    reqs = _reqs(evs)
    assert reqs, "没有请求"
    store = cp.CheckpointStore(r["dir"])
    for e in reqs:
        snap = store.find_for_request(e["request_index"], e["messages"])
        assert snap["messages_hash"] == cp.messages_digest(e["messages"]), \
            f"ri={e['request_index']} 快照与录制 messages 不一致"
        assert snap["kind"] == "pre_request", snap["kind"]
        assert cp.verify_restore(snap, e["messages"]) == [], \
            cp.verify_restore(snap, e["messages"])


def test_checkpoint_pre_request_precedes_request():
    """pre_request 快照必须排在对应 llm_request **之前**（顺序即语义）。"""
    r = _run([_thinking_batch], name="ckpt-order")
    evs = r["stored"]
    for e in _reqs(evs):
        before = [x for x in evs if x["seq"] < e["seq"] and x["type"] == "checkpoint"
                  and x["request_index"] == e["request_index"]
                  and x.get("kind") == "pre_request"]
        assert before, f"ri={e['request_index']} 的 pre_request 不在请求之前"


def test_seek_cost_is_bounded():
    """seek 第 N 个请求的解压次数与 N 同阶，而不是与"前面所有请求之和"同阶。

    这一条守的是 §4.7 的核心承诺：seek 不能把整场会话从头解一遍。用真实的
    CheckpointStore 铺 12 份快照（GROUP_REQUESTS=5 → 跨 3 个帧组），逐份 seek：

    - 帧组存储：每份 seek 解压**恰好 1 次**（从 index 的 offset 直取），共 12 次；
    - 若退化成"一个会话一个 blob"：seek 第 k 份要重放前 k 份，共 78 次。

    所以用 `解压次数 <= 快照数` 就能把两者分开，且不依赖运行耗时。
    """
    r = _run([_thinking_batch], name="seek-cost")
    # 用一个**全新**的目录铺数据：复用上面的 trace 目录会让 by_request 里混进 runner
    # 录下的快照，同一 request_index 下多出一份候选，seek 成本就被取样噪声带偏了。
    scan_dir = r["dir"] + "-scan"
    shutil.rmtree(scan_dir, ignore_errors=True)
    store = cp.CheckpointStore(scan_dir)
    msgs = []
    for i in range(1, 13):
        m = [{"role": "system", "content": "t"},
             {"role": "user", "content": f"第 {i} 次请求的现场"}]
        msgs.append(m)
        store.append({
            "kind": "pre_request", "request_index": i, "_snapshot_seq": 100 + i,
            "turn": 0, "tool_round": 0, "messages_hash": cp.messages_digest(m),
            "messages_len": len(m), "messages_bytes": len(str(m)),
            "messages_inline": m, "_key": cp.checkpoint_key("pre_request", i, 0, 0, 100 + i),
        })
    store.close()
    frames = {store.entry(cp.checkpoint_key("pre_request", i, 0, 0, 100 + i))["frame"]
              for i in range(1, 13)}
    assert len(frames) >= 3, f"没铺出多个帧组，这一条测不到东西：{frames}"

    store = cp.CheckpointStore(scan_dir)
    # 先验一遍"写进去的和读回来的是同一份"（占用的解压次数不计入下面的成本断言）
    for i in range(1, 13):
        key = cp.checkpoint_key("pre_request", i, 0, 0, 100 + i)
        snap = store.load(key)
        assert snap["messages_inline"] == msgs[i - 1], (
            f"ri={i} 键 {key} 存进去的和读回来的不是同一份："
            f"{snap['messages_inline']!r} != {msgs[i - 1]!r}")
    store._decompresses = 0  # 只数 seek 的成本
    for i in range(1, 13):
        got = store.find_for_request(i, msgs[i - 1])
        assert got["messages_hash"] == cp.messages_digest(msgs[i - 1]), f"ri={i} 取错快照"
    full_scan_cost = sum(range(1, 13))  # "一个会话一个 blob"时要付的代价（78 次）
    assert store.decompress_count() == 12, (
        f"解压 {store.decompress_count()} 次；帧组存储应当每份快照恰好解一次（12），"
        f"一个 blob 的实现要 {full_scan_cost} 次")


def test_seek_rejects_unknown_request():
    """seek 一个没录过的 request_index 必须报错，而不是悄悄给一份近似的快照。"""
    r = _run([_thinking_batch], name="seek-unknown")
    store = cp.CheckpointStore(r["dir"])
    try:
        store.find_for_request(999, [{"role": "user", "content": "x"}])
    except cp.CheckpointStoreError:
        return
    raise AssertionError("不存在的 request_index 竟然 seek 成功了")


def test_seek_rejects_mismatched_messages():
    """messages 对不上时必须抛 CheckpointRestoreMismatch（fail-closed）。"""
    r = _run([_thinking_batch], name="seek-mismatch")
    e = _reqs(r["stored"])[0]
    store = cp.CheckpointStore(r["dir"])
    wrong = [{"role": "user", "content": "完全不同的内容"}]
    try:
        got = store.find_for_request(e["request_index"], wrong)
    except cp.CheckpointRestoreMismatch:
        return
    raise AssertionError(f"messages 对不上却返回了快照：{str(got)[:120]}")


def test_checkpoint_keys_do_not_collide():
    """同一请求下的多份快照必须各有各的键位（不能互相覆盖）。"""
    r = _run([_thinking_batch], name="ckpt-keys")
    store = cp.CheckpointStore(r["dir"])
    for req, keys in store.index["by_request"].items():
        assert len(keys) == len(set(keys)), f"ri={req} 键位重复：{keys}"
    # post_turn 与 pre_request 并存于同一请求时，两者都要能找回来
    post = [e for e in r["stored"] if e["type"] == "checkpoint" and e.get("kind") == "post_turn"]
    if post:
        ri = post[0]["request_index"]
        kinds = {cp.kind_of_key(k) for k in store.keys_for_request(ri)}
        assert "post_turn" in kinds, (ri, kinds)


def test_checkpoint_store_roundtrip():
    """写进帧组的快照能原样读回来（zstd 帧组 + 索引 offset 正确）。"""
    r = _run([_thinking_batch], name="roundtrip")
    e = _reqs(r["stored"])[0]
    store = cp.CheckpointStore(r["dir"])
    snap = store.find_for_request(e["request_index"], e["messages"])
    for field in ("turn", "tool_round", "messages_hash", "messages_len", "B", "C", "D", "E", "F"):
        assert field in snap, f"快照缺字段 {field}"
    assert snap["messages_inline"] == e["messages"], "inline messages 与录制不一致"


# ── 3. 事件分级 ─────────────────────────────────────────────────
def test_level_keeps_records_but_drops_stream_views():
    """档位只削"给人看的逐字视图"，绝不能削回放要用的 llm_chunk 记录（§4.4）。

    llm_chunk 是**录制记录**：回放要靠它把同一个 chunk 吐回去，缺一条响应就残缺；
    response_chunk / thinking_chunk 才是"是否值得逐字存"的问题，默认档丢弃。
    """
    default = _run([_thinking_batch], name="lvl-default")
    stream = _run([_thinking_batch], name="lvl-stream", level=trace.LEVEL_STREAM)
    chunks = [e for e in default["stored"] if e["type"] == "llm_chunk"]
    assert chunks, "assert 档也必须写 llm_chunk（回放要重放同一个 chunk）"
    assert all("choices_empty" in c for c in chunks), chunks[:2]
    assert not [e for e in default["stored"] if e["type"] == "thinking_chunk"], \
        "assert 档写了逐字视图 thinking_chunk"
    assert [e for e in stream["stored"] if e["type"] == "thinking_chunk"], \
        "stream 档没写 thinking_chunk"


def test_chunk_payload_carries_content():
    """chunk 事件必须带正文/思考/工具调用——回放要靠它把同样的 chunk 吐回去。"""
    strict = _run([_thinking_batch], name="chunk-payload", level=trace.LEVEL_DIAG)
    chunks = [e for e in strict["stored"] if e["type"] == "llm_chunk"]
    assert any(c.get("reasoning_content") == "我再想想" for c in chunks), chunks[:3]
    assert any(c.get("finish_reason") == "stop" for c in chunks), chunks


def test_trace_index_enables_offset_lookup():
    """index 里的 offset 能直接跳到那一行（O(1) 跳转，不用顺序解析）。"""
    r = _run([_thinking_batch], name="idx")
    idx_path = os.path.join(r["dir"], "trace.idx.json")
    assert os.path.exists(idx_path), "trace.idx.json 没写出来"
    with open(idx_path, encoding="utf-8") as f:
        idx = json.load(f)
    evs = r["stored"]
    assert len(idx["offsets"]) == len(evs), (len(idx["offsets"]), len(evs))
    with open(os.path.join(r["dir"], "trace.jsonl"), "rb") as f:
        for i, off in enumerate(idx["offsets"]):
            f.seek(off)
            line = f.readline().decode("utf-8")
            got = json.loads(line)
            assert got["seq"] == evs[i]["seq"], f"offset {off} 跳到的不是第 {i+1} 条"


# ── 4. 脱敏 ─────────────────────────────────────────────────────
def test_secrets_never_land_in_trace():
    """API key 形态的串不能落进 trace（事件里有 messages/参数，很容易带出来）。"""
    red = trace.redact("key=sk-abcdef0123456789abcdef " + "Bearer " + "x" * 30)
    assert "sk-abcdef0123456789abcdef" not in red, red
    assert "<image:base64-omitted>" in trace.redact("data:image/png;base64," + "A" * 200)


def test_manifest_is_written():
    """manifest 是回放判断"这份 trace 是什么环境录的"的唯一依据。"""
    r = _run([_thinking_batch], name="manifest")
    m = trace.load_manifest(r["dir"])
    assert m.get("session_id") == r["sid"], m
    assert m.get("backend") == "fake", m


def test_unclassified_tool_fails_closed():
    """没登记分类的工具必须抛错（回放要按 class 决定如何进行）。"""
    from loop_agent_core.toolclass import UnclassifiedToolError, class_of
    try:
        class_of("这是不存在的工具")
    except UnclassifiedToolError:
        return
    raise AssertionError("未登记工具竟然拿到了分类")


# ── 5. 确定性、词表与断言 id（§9 #1/#3/#4）─────────────────────
_VOLATILE = ("ts", "duration_ms", "latency_ms", "run_id", "session_id", "pid",
             "started_at", "ended_at", "iso", "extracted_at", "attempt", "seq",
             # 事件里的顶层字段叫 `session`（装的是会话 id，`session_id` 是别处的叫法）——
             # 两次运行必然不同，不剥掉它，"跑两次一致"永远不成立。
             "session",
             # `truncation.recorded_at` 就是 `trace_epoch()["iso"]`（秒级）——它**是**一个时间。
             # 漏在这一行里会让"跑两次逐字段一致"变成掷骰子：两次跑相隔几十毫秒，
             # 有约 6% 的概率跨过秒边界，于是确定性用例偶发地红。
             "recorded_at")


def _strip_volatile(obj):
    """去掉每次跑都会变、又不承载语义的字段，剩下的才该逐字相等。"""
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items() if k not in _VOLATILE}
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 6)
    return obj


def test_trace_determinism():
    """同一段历史跑两次：事件类型序列一致，逐字段（去时间）也一致（§9 #1）。

    比对前按 call_id 关联，避免"并发工具轮的结算顺序"这种与确定性无关的抖动。
    """
    a = _run(_tool_batch, question="今天几号", name="det-a")
    b = _run(_tool_batch, question="今天几号", name="det-b")
    ta = [e["type"] for e in a["stored"]]
    tb = [e["type"] for e in b["stored"]]
    assert ta == tb, f"两次跑的事件类型序列不同：\n{ta}\n{tb}"
    ca = {e.get("call_id"): _strip_volatile(e)
          for e in a["stored"] if e.get("call_id")}
    cb = {e.get("call_id"): _strip_volatile(e)
          for e in b["stored"] if e.get("call_id")}
    assert ca == cb, "同一次工具调用两次跑下来的事实不同（call_id 关联后应有同一份记录）"


def test_notice_codes_come_from_the_table():
    """每个 notice 都带 code，且 code/level 只能来自 NOTICE_CODES（§9 #3）。"""
    import loop_agent_core.runner as runner_mod
    r = _run([_thinking_batch], name="noticecode")
    notices = [e for e in r["stored"] if e["type"] == "notice"]
    assert notices, "只会思考的重试路径竟然没发 notice（L3 断言面就少了一大类）"
    allowed = {code: lvl for (lvl, code, _t) in runner_mod.NOTICE_CODES.values()}
    for e in notices:
        assert e.get("code"), f"notice 没带 code：{e}"
        assert e["code"] in allowed, f"notice.code={e['code']!r} 不在词表里"
        assert e.get("level") == allowed[e["code"]], \
            f"notice {e['code']} 的 level 与词表不一致：{e.get('level')}"


def test_notice_id_ignores_wording():
    """改文案不该改断言 id——否则每改一次提示语，旧 cassette 的断言面就全部作废。"""
    import loop_agent_core.runner as runner_mod
    r = _run([_thinking_batch], name="noticeword")
    before = trace.assertion_ids(r["stored"])
    orig = runner_mod.NOTICE_CODES["stuck"]
    runner_mod.NOTICE_CODES["stuck"] = (orig[0], orig[1], "完全换了一套说法的提示语")
    try:
        after = trace.assertion_ids(_run([_thinking_batch], name="noticeword2")["stored"])
    finally:
        runner_mod.NOTICE_CODES["stuck"] = orig
    assert before == after, f"改文案动了断言面：\n{before}\n{after}"
    assert "notice:warn:stuck" in before, before


def test_exception_id_uses_function_not_line():
    """断言 id 里的位置是**函数名**：同行号变（换个行号抛同一异常）不该换 id（§9 #4）。"""
    import traceback as tb_mod

    def _exc_with(frames):
        try:
            raise ValueError("boom")
        except ValueError as exc:
            fake = tb_mod.StackSummary.from_list(frames)
            orig = tb_mod.extract_tb
            tb_mod.extract_tb = lambda *a, **kw: fake
            try:
                return trace.exception_assertion_id(exc), trace.exception_site(exc)
            finally:
                tb_mod.extract_tb = orig

    proj = trace._PROJECT_DIR
    f_runner = os.path.join(proj, "runner.py")
    f_ctx = os.path.join(proj, "context.py")
    aid_a, site_a = _exc_with([(f_runner, 100, "_safe_create", "code")])
    aid_b, site_b = _exc_with([(f_runner, 999, "_safe_create", "code")])
    assert site_a == site_b == "_safe_create", (site_a, site_b)
    assert aid_a == aid_b, f"行号参与了断言 id：{aid_a} != {aid_b}"
    assert aid_a == "exception:ValueError@_safe_create", aid_a
    aid_c, site_c = _exc_with([(f_ctx, 100, "_prep_context", "code")])
    assert site_c == "_prep_context" and aid_c != aid_a, "函数名没有参与断言 id"
    # 项目外的帧不能当位置（测试文件、三方库都可能是第一个帧）
    aid_d, site_d = _exc_with([(os.path.join(ROOT, "tests", "test_trace.py"), 5, "helper", "x"),
                               (f_runner, 100, "_safe_create", "code")])
    assert site_d == "_safe_create", f"取了项目外的帧当位置：{site_d}"


def test_turn_end_covers_return_branches():
    """每条 return 路径都要发 turn_end，且 reason 落在闭集里（§9 #2）。

    没发 turn_end 的路径在 L3 里就是"无法声明失败轮次"——候选最小化会把它当成
    "这一轮什么都没发生"。所以分支覆盖是按**出口**数，不是按代码路径数。
    """
    from loop_agent_core.control import interrupt_event
    seen = {}

    def _reason(r):
        ends = [e for e in r["stored"] if e["type"] == "turn_end"]
        assert ends, f"没有 turn_end：{[e['type'] for e in r['stored']]}"
        assert len(ends) == 1, f"一个回合发了 {len(ends)} 次 turn_end"
        assert ends[0]["reason"] in trace.TURN_END_REASONS, ends[0]
        return ends[0]["reason"]

    seen["answer"] = _reason(_run(_tool_batch, question="今天几号", name="br-answer"))
    seen["stuck"] = _reason(_run([_thinking_batch], name="br-stuck"))

    # 中断：进循环前 interrupt_event 已置位
    interrupt_event.set()
    try:
        seen["interrupted"] = _reason(_run([_thinking_batch], name="br-int"))
    finally:
        interrupt_event.clear()

    # 后端报错：transport.create 抛异常 → api_error 分支
    class _Boom:
        def create(self, kind, cfg, kwargs):
            raise RuntimeError("后端炸了")

    sid, run_dir = _fresh_session("br-api")
    conv = {"messages": [{"role": "system", "content": "t"}], "size": None,
            "_archive": [], "_compact_state": {}, "_session_id": sid}
    llm.set_transport(_Boom())
    w = trace.open_writer(sid, run_dir=run_dir,
                          manifest={"session_id": sid, "backend": "fake", "model": "m"})
    try:
        la.chat("干活", on_event=[].append, _conv=conv)
    except Exception:  # noqa: BLE001 —— 后端错误必须往上抛（行为不变），但 turn_end 要先落地
        pass
    finally:
        llm.reset()
        w.write_index()
        trace.close_writer(w)
        cp.close_store()
    seen["api_error"] = _reason({"stored": trace.read_events(run_dir)})

    assert set(seen) == {"answer", "stuck", "interrupted", "api_error"}, seen


def test_second_run_in_same_process_records_the_same_trace():
    """run 起点的进程级状态必须复位：同一段历史录两次，trace 要逐事件一致。

    `request_index` 和 headless 的流式打印标志都是**进程级**的。不复位的话，第二次运行的
    run_start 会挂着上一轮末尾的请求序号，checkpoint 的 D 组（进程状态）会带上上一轮的
    打印状态——两处都不报错，但会让"重录一次"和"第一次录"产出不同的基线：入库 fixture
    的 --check 会假报漂移，回放看起来像"环境变了"。
    """
    import json as _json

    def _proj(evs):
        # 逐事件 JSON 化，但先去时间字段 —— 口径统一走 _strip_volatile，
        # 免得这里另写一份 volatile 清单又漏掉某个时钟字段（那正是本用例翻车的方式）。
        return [_json.dumps(_strip_volatile(e), sort_keys=True, ensure_ascii=False)
                for e in evs]

    a = _run(_tool_batch, question="复位", name="reseta")
    b = _run(_tool_batch, question="复位", name="resetb")

    first = a["stored"][0]
    assert (first["type"], first["request_index"], first["tool_round"]) == ("run_start", 0, 0), first
    pa, pb = _proj(a["stored"]), _proj(b["stored"])
    diff = next((i for i, (x, y) in enumerate(zip(pa, pb)) if x != y), None)
    assert len(pa) == len(pb) and diff is None, (
        f"同一进程内两次录制的 trace 不一致（第 {None if diff is None else diff + 1} 条起）\n"
        f"  第一次={pa[diff] if diff is not None else '<条数不同>'}\n"
        f"  第二次={pb[diff] if diff is not None else '<条数不同>'}")


def test_xml_toolcall_retry_emits_trace_event():
    """§4.3：XML 工具调用解析不出来时，除了 notice 还必须留一条结构化事件。

    这条路径只在本地后端生效（云端后端不解析 XML 工具调用），所以用例临时把
    MODEL_BACKEND 切成 local；断言两件事：事件带 round/count、且重试**没有掐断回合**——
    第二次重试后模型正常出答案，说明 continue 走的是"再给一次机会"而不是死循环。
    """
    xml_batch = [_Chunk(_Delta(reasoning="我调个工具 <tool_call>{坏掉的</tool_call>想完了")),
                 _Chunk(_Delta(), finish_reason="stop"),
                 _Chunk(usage=True, no_choices=True)]
    batches = [xml_batch, xml_batch, [_Chunk(_Delta(content="终答"), finish_reason="stop"),
                                     _Chunk(usage=True, no_choices=True)]]
    old = os.environ.get("MODEL_BACKEND")
    os.environ["MODEL_BACKEND"] = "local"
    try:
        r = _run(batches, name="xmlretry")
    finally:
        if old is None:
            os.environ.pop("MODEL_BACKEND", None)
        else:
            os.environ["MODEL_BACKEND"] = old

    evs = [e for e in r["stored"] if e["type"] == "xml_toolcall_retry"]
    assert [e["count"] for e in evs] == [1, 2], [e.get("count") for e in evs]
    assert all(isinstance(e["round"], int) for e in evs), evs
    assert r["out"] == "终答", r["out"]
    assert r["transport"].calls >= 3, r["transport"].calls


def test_clean_removes_only_old_trace_dirs():
    """clean 只删 trace 根下的、认得出来的、过期的会话目录（§4.9）。

    删除是不可逆操作：这条用例守的是"作用域"——根目录之外的东西、认不出的目录、
    以及没过期的会话，一律不许动。
    """
    import importlib.util
    import types

    spec = importlib.util.spec_from_file_location(
        "tools_trace_cli", os.path.join(ROOT, "tools", "trace.py"))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    root = os.path.join(_TRACE_ROOT, f"cleanroot-{int(time.time() * 1000) % 1000000}")
    old = os.path.join(root, "old-session")
    fresh = os.path.join(root, "fresh-session")
    alien = os.path.join(root, "not-a-trace")
    for d in (old, fresh, alien):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    for d in (old, fresh):
        with open(os.path.join(d, "trace.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"seq": 1, "type": "run_start"}\n')
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            f.write('{"session_id": "%s"}' % os.path.basename(d))
    with open(os.path.join(alien, "keep.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    # 把 old 的所有文件做成 40 天前
    past = time.time() - 40 * 86400
    for f in os.listdir(old):
        os.utime(os.path.join(old, f), (past, past))

    args = types.SimpleNamespace(root=root, older_than="30d", dry_run=False, quiet=True)
    cli.cmd_clean(args)
    assert not os.path.exists(old), "过期的 trace 目录没被清掉"
    assert os.path.exists(fresh), "没过期的 trace 目录被清掉了"
    assert os.path.exists(os.path.join(alien, "keep.txt")), "认不出的目录被清掉了"


def test_trace_output_is_not_tracked_by_git():
    """CI 闸：tmp/traces/** 不允许进版本库（§4.1/§4.9）。"""
    import subprocess
    try:
        out = subprocess.run(["git", "ls-files", "tmp/traces"],
                             cwd=ROOT, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return  # 不是 git 检出（或没有 git）：这条闸在 CI 里生效
    if out.returncode != 0:
        return
    assert out.stdout.strip() == "", f"tmp/traces 下有被跟踪的文件：{out.stdout[:200]}"


def test_prune_and_compaction_events_reach_trace():
    """L0/L1/L2 三层裁剪与压缩决策必须落在 trace 里（§4.3）。

    只走屏幕事件时，回放看到一个变小的上下文却判不出是哪一层丢的——三层可复现条件
    完全不同（L0 无成本、L1 要摘要器、L2 只依赖 cap），所以层号是回放的必需事实。
    """
    from loop_agent_core import context as ctx_mod
    sid, run_dir = _fresh_session("pruneev")
    w = trace.open_writer(sid, run_dir=run_dir,
                          manifest={"session_id": sid, "backend": "fake", "model": "m"})
    try:
        ctx_mod._trace_prune("L0", 2, reclaimed=120, cap=32000, before=9000, after=8880)
        ctx_mod._trace_compaction("skipped", reason="budget", size=9000)
        ctx_mod._trace_compaction("attempted", blocks=3)
        w.write_index()
    finally:
        trace.close_writer(w)
        cp.close_store()
    events = trace.read_events(run_dir)
    prunes = [e for e in events if e["type"] == "prune"]
    comps = [e for e in events if e["type"] == "compaction"]
    assert len(prunes) == 1 and prunes[0]["layer"] == "L0", prunes
    assert prunes[0]["removed"] == 2 and prunes[0]["cap"] == 32000, prunes[0]
    assert [c["decision"] for c in comps] == ["skipped", "attempted"], comps
    assert comps[0]["reason"] == "budget", comps[0]
    assert trace.validate_events(events) == [], trace.validate_events(events)
    assert "prune" in trace.EVENT_LEVEL and "compaction" in trace.EVENT_LEVEL


def test_thinking_only_retry_is_recorded():
    """thinking-only 重试要留痕（§4.3：round/count）。

    没有这条事件时，"模型只思考不产出"在 trace 里只剩 3 条 empty_nudge 提示，
    回放时无法区分"模型自己想接着说"和"被 nudge 顶回去的"。
    """
    r = _run([_thinking_batch], name="toret")
    retries = [e for e in r["stored"] if e["type"] == "thinking_only_retry"]
    assert retries, f"没有 thinking_only_retry：{[e['type'] for e in r['stored']]}"
    assert [e["count"] for e in retries] == [1, 2, 3], retries
    assert all(isinstance(e.get("round"), int) for e in retries), retries
    nudges = [e for e in r["stored"] if e["type"] == "nudge_injected"]
    assert len(nudges) == 3, nudges


def test_on_event_sees_the_same_filtering():
    """`on_event` 只转发通过档位过滤的事件（§4.4）。

    如果观察者能拿到被档位丢掉的事件，"默认档不写逐 chunk 文本"就只是一句口头约定：
    UI 照旧会收到上万条 response_chunk。
    """
    seen = []
    sid, run_dir = _fresh_session("onevt")
    w = trace.open_writer(sid, run_dir=run_dir, level="assert",
                          manifest={"session_id": sid, "backend": "fake", "model": "m"},
                          on_event=seen.append)
    try:
        trace.emit_record({"type": "llm_chunk", "content": "x"})          # 必须转发
        trace.emit_event({"type": "response_chunk", "chunk": "x"},
                         level_hint=trace.LEVEL_STREAM)                  # 默认档必须丢弃
        trace.emit_event({"type": "stream_summary", "round": 1},
                         level_hint=trace.LEVEL_SUMMARY)                 # 默认档要写，故转发
        w.write_index()
    finally:
        trace.close_writer(w)
        cp.close_store()
    types = [e["type"] for e in seen]
    assert "llm_chunk" in types, types
    assert "stream_summary" in types, types
    assert "response_chunk" not in types, types
    assert "run_end" in types, types  # 收尾事件也要经过同一条路
    on_disk = [e["type"] for e in trace.read_events(run_dir)]
    assert types == on_disk, (types, on_disk)


def test_headroom_layer_wraps_chat_transport():
    """headroom 必须套在**传输层**上，且只包 chat（§2 / §3.2）。

    包错位置的后果不是报错而是静默失效：audit 不记录、optimize 不压缩、
    headroom_transform 永不发射——外面看一切正常，回放时才发现整个请求侧没有事实。
    """
    from loop_agent_core import llm as llm_mod
    import types

    class _Inner:
        def __init__(self):
            self.calls = []

        def create(self, kind, cfg, kwargs):
            self.calls.append((kind, kwargs.get("stream")))
            return f"inner:{kind}"

    class _Layer:
        def __init__(self, boom=False):
            self.seen = []
            self.boom = boom
            outer = self

            class _Completions:
                def create(self, **kw):
                    outer.seen.append(kw)
                    if outer.boom:
                        raise RuntimeError("headroom 炸了")
                    return "layer:ok"

            self.chat = types.SimpleNamespace(completions=_Completions())

    # 只包 chat：summary/vision 走各自后端，套上主后端的上下文预算就是错的
    inner, layer = _Inner(), _Layer()
    t = llm_mod._HeadroomTransport(inner, layer)
    assert t.create("chat", {"model": "m"}, {"messages": [{"role": "user"}], "stream": True}) == "layer:ok"
    assert t.create("summary", {}, {"messages": []}) == "inner:summary"
    assert t.create("vision", {}, {"messages": []}) == "inner:vision"
    assert inner.calls == [("summary", None), ("vision", None)], inner.calls
    # model/messages 是 headroom 的**形参**，不能被塞进 kwargs（否则 TypeError）
    seen = layer.seen[0]
    assert seen["model"] == "m" and seen["messages"] == [{"role": "user"}], seen
    assert "model" not in seen.get("kwargs", {}), seen

    # 观测层故障必须退回内层：优化层挂掉不能让对话失败
    inner2 = _Inner()
    t2 = llm_mod._HeadroomTransport(inner2, _Layer(boom=True))
    assert t2.create("chat", {}, {"messages": [], "stream": False}) == "inner:chat"
    assert inner2.calls == [("chat", False)], inner2.calls

    # 真 headroom：整条链走通，且 headroom_transform 落进 trace
    from loop_agent_core import config as cfg_mod
    from loop_agent_core import trace as trace_mod

    class _Resp:
        usage = None

    class _Raw:
        def __init__(self):
            self.calls = []

            class _Completions:
                def __init__(self, outer):
                    self._outer = outer

                def create(self, **kw):
                    self._outer.calls.append(kw)
                    return _Resp()

            self.chat = types.SimpleNamespace(completions=_Completions(self))

    sid, run_dir = _fresh_session("hrlayer")
    old_store = os.environ.get("HEADROOM_STORE_URL")
    os.environ["HEADROOM_STORE_URL"] = "jsonl://" + os.path.join(
        run_dir, "headroom.jsonl").replace(os.sep, "/")
    raw = _Raw()
    w = trace.open_writer(sid, run_dir=run_dir,
                          manifest={"session_id": sid, "backend": "fake", "model": "m"})
    try:
        layer_real = cfg_mod._maybe_wrap_headroom(raw)
        try:
            layer_real._original = raw   # 换成假客户端：整条链只在本地跑
        except AttributeError:
            pass
        t3 = llm_mod._HeadroomTransport(_Inner(), layer_real)
        out = t3.create("chat", {"model": "m"},
                        {"model": "m", "messages": [{"role": "user", "content": "hi"}],
                         "stream": False, "temperature": 0.2})
        assert isinstance(out, _Resp), out
        assert raw.calls and raw.calls[0]["model"] == "m", raw.calls
        assert raw.calls[0]["messages"] == [{"role": "user", "content": "hi"}], raw.calls
        transforms = [e for e in trace.read_events(run_dir)
                      if e["type"] == "headroom_transform"]
        assert transforms, [e["type"] for e in trace.read_events(run_dir)]
        assert "messages_pre" in transforms[0] and "messages_post" in transforms[0], transforms[0]
        # audit（默认档）不改请求，所以两侧摘要必须相同——这条断言同时钉住两件事：
        # 事件在 audit 下**也**要发（否则默认档的回放看不到请求侧事实），
        # 以及 audit 真的没有偷偷改上下文。
        assert transforms[0]["changed"] is False, transforms[0]
        assert transforms[0]["messages_pre"] == transforms[0]["messages_post"], transforms[0]
        w.write_index()
    finally:
        if old_store is None:
            os.environ.pop("HEADROOM_STORE_URL", None)
        else:
            os.environ["HEADROOM_STORE_URL"] = old_store
        trace.close_writer(w)
        cp.close_store()


def test_headroom_transform_once_per_request():
    """每条请求只能有一条 headroom_transform，且 optimize 档下真的记到（§4.3）。

    optimize 档下 headroom 既发 input_compressed 又发 pre_send：两个钩子都记录就会
    每请求两条，回放按 request 取"最后一条"时行为随模式而变。这里钉住"一请求一条"。
    """
    from loop_agent_core import llm as llm_mod
    from loop_agent_core import config as cfg_mod
    import types

    class _Resp:
        usage = None

    class _Raw:
        def __init__(self):
            self.calls = []

            class _C:
                def __init__(self, outer):
                    self._o = outer

                def create(self, **kw):
                    self._o.calls.append(kw)
                    return _Resp()

            self.chat = types.SimpleNamespace(completions=_C(self))

    big = [{"role": "user", "content": "x" * 400} for _ in range(40)]
    sid, run_dir = _fresh_session("hropt")
    old_store = os.environ.get("HEADROOM_STORE_URL")
    old_mode = os.environ.get("HEADROOM_MODE")
    os.environ["HEADROOM_STORE_URL"] = "jsonl://" + os.path.join(
        run_dir, "headroom.jsonl").replace(os.sep, "/")
    os.environ["HEADROOM_MODE"] = "optimize"
    raw = _Raw()
    w = trace.open_writer(sid, run_dir=run_dir,
                          manifest={"session_id": sid, "backend": "fake", "model": "m"})
    try:
        layer = cfg_mod._maybe_wrap_headroom(raw)
        layer._original = raw
        t = llm_mod._HeadroomTransport(_Raw(), layer)
        for _ in range(3):
            t.create("chat", {"model": "m"},
                     {"model": "m", "messages": big, "stream": False, "temperature": 0.2})
        evs = [e for e in trace.read_events(run_dir) if e["type"] == "headroom_transform"]
        assert len(evs) == 3, [e.get("headroom_request_id") for e in evs]
        assert all("messages_pre" in e and "messages_post" in e for e in evs), evs
        w.write_index()
    finally:
        for key, old in (("HEADROOM_STORE_URL", old_store), ("HEADROOM_MODE", old_mode)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        trace.close_writer(w)
        cp.close_store()


def _run_all():
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok  {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    if passed == len(tests):
        # 全绿才清场：不清的话每跑一次都在 tmp/ 下留几十个会话目录，之后
        # `tools.trace list` 的输出就没法看了。失败时保留现场。
        shutil.rmtree(_TRACE_ROOT, ignore_errors=True)
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
