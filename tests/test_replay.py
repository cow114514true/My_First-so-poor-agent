"""L1 回放（§6）与 cassette（§4.8）的行为。Run: python tests/test_replay.py

这组用例守的是"回放算不算数"：

1. **回放必须离线**：工具结果从 cassette 回吐，绝不真执行。真执行会写工作区、打网络，
   跑出来的就不是"同一段历史再跑一遍"——失败复现与否都不再说明问题。
2. **指纹分得清"同一段历史"和"另一段历史"**：路径、session id、日期不该影响判定；
   请求内容变了必须判定为不匹配，而不是靠窗口硬凑一个命中。
3. **strict 的四条判据**：能归因的差异放行并记报告，归不了因的必须失败；reinject 槽位
   结构对不上时明确报「strict 不适用：<槽位> 不可复现」，不能含糊通过。
4. **模式与完整度校验**：playback_mode 缺失 / 双向错配 / positional 缺 derived_from /
   LLM 记录缺失，都必须**在加载时**抛错，不能等回放跑了一半才发现。

假后端挂在**网关底下**（llm.set_transport），而不是替换 runner.client：后者会绕过网关，
于是 llm_request / llm_chunk / llm_end 全部不产生——回放会"复现"一份从来没录过的东西。
"""
import json
import os
import shutil
import sys
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DS_KEY", "sk-selftest-not-a-real-key")
os.environ.setdefault("FILTERED_LOG", os.path.join(ROOT, "tmp", "_selftest_replay_filtered.log"))
os.environ.setdefault("CACHE_LOG", os.path.join(ROOT, "tmp", "_selftest_replay_cache.log"))
_TRACE_ROOT = os.path.join(ROOT, "tmp", "traces-replay-selftest")
_CASSETTE_ROOT = os.path.join(ROOT, "tmp", "cassettes-selftest")
os.environ["TRACE_ENABLED"] = "1"
os.environ["TRACE_DIR"] = _TRACE_ROOT

import loop_agent_core.cassette as cassette_mod  # noqa: E402
import loop_agent_core.checkpoint as cp  # noqa: E402
import loop_agent_core.llm as llm  # noqa: E402
import loop_agent_core.replay as replay  # noqa: E402
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


_TOOL_TEXT = "录制结果：2026-01-01"
# 一个工具轮 + 一轮收尾：请求序列 2 条，响应序列 2 条
_TOOL_BATCH = [[_Chunk(_Delta(tool_calls=[_ToolCall(0, "c1", "get_date", "{}")])),
                _Chunk(_Delta(), finish_reason="tool_calls"),
                _Chunk(usage=True, no_choices=True)],
               [_Chunk(_Delta(content="今天是某天"), finish_reason="stop"),
                _Chunk(usage=True, no_choices=True)]]
# 空 choices 尾块（BUG 7）：尾块只有 usage 没有 choices，回放不得 IndexError
_ONLY_THINKING_BATCH = [[_Chunk(_Delta(reasoning="我再想想")),
                         _Chunk(_Delta(), finish_reason="stop"),
                         _Chunk(usage=True, no_choices=True)]]


def _fresh_dir(root, name):
    path = os.path.join(root, f"{name}-{int(time.time() * 1000) % 1000000}")
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def _new_conv(sid):
    return {"messages": [{"role": "system", "content": "t"}], "size": None,
            "_archive": [], "_compact_state": {}, "_session_id": sid}


def _record(batches, question="干活", name="rec", tool_text=_TOOL_TEXT):
    """录一次会话，返回 trace 目录。假后端挂在网关下（见模块 docstring）。"""
    run_dir = _fresh_dir(_TRACE_ROOT, name)
    sid = os.path.basename(run_dir)
    cp.close_store()
    cp.reset_counters()
    conv = _new_conv(sid)
    events = []
    transport = _FakeTransport(batches)
    llm.set_transport(transport)
    prev_tool = la.TOOL_CALL_MAP.get("get_date")
    if tool_text is not None:
        la.TOOL_CALL_MAP["get_date"] = lambda **kw: tool_text
    w = trace.open_writer(sid, run_dir=run_dir,
                          manifest={"session_id": sid, "backend": "fake",
                                    "model": "deepseek-v4-flash"})
    try:
        out = la.chat(question, on_event=events.append, _conv=conv)
    finally:
        if prev_tool is not None:
            la.TOOL_CALL_MAP["get_date"] = prev_tool
        llm.reset()
        w.write_index()
        trace.close_writer(w)
        cp.close_store()
    return {"dir": run_dir, "out": out, "events": events, "conv": conv,
            "stored": trace.read_events(run_dir), "transport": transport}


def _extract(rec, name, **kw):
    out_dir = _fresh_dir(_CASSETTE_ROOT, name)
    shutil.rmtree(out_dir, ignore_errors=True)
    meta = cassette_mod.extract_cassette(rec["dir"], out_dir, **kw)
    return out_dir, meta


def _replay(cass_dir, question=None, *, mode=None, strict=False, name="rep",
            tool_text="真执行了！！", check_env=False):
    """跑一次回放。工具被换成"真执行了"以便证明回放**没有**真执行。"""
    run_dir = _fresh_dir(_TRACE_ROOT, name)
    _LAST_RUN["dir"] = run_dir
    # 回放必须自带一个干净的会话：loop_agent_v2 的模块级 messages 是跨用例累积的，
    # 不传 _conv 的话现场 messages 会带着前面用例的历史（实测 2 条录制 vs 17 条现场）。
    conv = _new_conv(os.path.basename(run_dir))
    prev_tool = la.TOOL_CALL_MAP.get("get_date")
    la.TOOL_CALL_MAP["get_date"] = lambda **kw: tool_text
    try:
        return replay.run_replay(cass_dir, question=question, conv=conv,
                                 run_dir=run_dir,
                                 mode=mode, strict=strict, check_env=check_env)
    finally:
        if prev_tool is not None:
            la.TOOL_CALL_MAP["get_date"] = prev_tool


# ── 端到端：抽取 → 回放 ─────────────────────────────────────────
def test_extract_and_replay_reproduces_answer():
    rec = _record(_TOOL_BATCH, question="今天几号", name="e2e")
    cass, meta = _extract(rec, "e2e", workspace_root=ROOT)
    assert os.path.exists(os.path.join(cass, "cassette.jsonl")), "cassette 没落盘"
    assert meta["playback_mode"] == "fingerprint"
    rep = _replay(cass, name="e2e-rep")
    assert rep.skips_count == 0, f"同一段历史不该有跳过：{rep.skips}"
    assert len(rep.matches) == 2, f"应该匹配 2 个请求，实际 {len(rep.matches)}"
    assert "今天是某天" in str(rep.result), f"回放没拿到录制正文：{rep.result!r}"


def test_replay_executes_no_tools():
    rec = _record(_TOOL_BATCH, question="今天几号", name="off")
    cass, _ = _extract(rec, "off", workspace_root=ROOT)
    rep = _replay(cass, name="off-rep", tool_text="真执行了！！")
    assert "真执行了" not in str(rep.result), "回放真的执行了工具"
    results = [e for e in trace.read_events(_LAST_RUN["dir"]) if e.get("type") == "tool_result"]
    assert results, "回放 trace 里没有 tool_result"
    assert _TOOL_TEXT in str(results[0].get("result_stored")), \
        f"回放没有回吐录制结果：{results[0].get('result_stored')!r}"
    assert results[0].get("from_replay") is True, "回放的 tool_result 没标 from_replay"


def test_replay_bug7_empty_choices_tail():
    """空 choices 的 usage 尾块：录制与回放都不得 IndexError（BUG 7）。"""
    rec = _record(_ONLY_THINKING_BATCH, question="随便", name="bug7")
    types = [e["type"] for e in rec["stored"]]
    assert "llm_chunk" in types
    empties = [e for e in rec["stored"]
               if e.get("type") == "llm_chunk" and e.get("choices_empty")]
    assert empties, "录制的空 choices 尾块没有被记下来（回放将无法重建）"
    cass, _ = _extract(rec, "bug7", workspace_root=ROOT)
    rep = _replay(cass, name="bug7-rep")  # 不抛 IndexError 就算过
    assert rep.matches, "没有匹配到任何请求"


def test_positional_replay_banner_and_reproduction():
    rec = _record(_TOOL_BATCH, question="今天几号", name="pos")
    cass, _ = _extract(rec, "pos", playback_mode="positional",
                       derived_from=rec["dir"], expected_assertion="turn_end:reason=answer")
    rep = _replay(cass, name="pos-rep", mode="positional")
    assert rep.banner == replay.POSITIONAL_BANNER
    assert "未做请求指纹比对" in rep.text(), "按位回放的报告首行必须说清没做指纹比对"
    assert rep.expected_assertion == "turn_end:reason=answer"
    assert rep.reproduced is True, f"目标断言没复现：{rep.assertion_ids}"


def test_positional_requires_derived_from():
    rec = _record(_TOOL_BATCH, name="posreq")
    out_dir = _fresh_dir(_CASSETTE_ROOT, "posreq")
    shutil.rmtree(out_dir, ignore_errors=True)
    try:
        cassette_mod.extract_cassette(rec["dir"], out_dir, playback_mode="positional")
    except cassette_mod.CassetteFormatError:
        return
    raise AssertionError("positional 缺 derived_from/expected_assertion 竟然抽取成功")


def test_playback_mode_required():
    rec = _record(_TOOL_BATCH, name="mode")
    cass, _ = _extract(rec, "mode", workspace_root=ROOT)
    meta_path = os.path.join(cass, "meta.json")
    meta = json.load(open(meta_path, encoding="utf-8"))
    del meta["playback_mode"]
    json.dump(meta, open(meta_path, "w", encoding="utf-8"), ensure_ascii=False)
    try:
        cassette_mod.Cassette(cass, check_env=False)
    except cassette_mod.CassetteFormatError:
        return
    raise AssertionError("playback_mode 缺失竟然加载成功")


def test_playback_mode_mismatch_both_ways():
    rec = _record(_TOOL_BATCH, name="mm")
    fp, _ = _extract(rec, "mm-fp", workspace_root=ROOT)
    try:
        cassette_mod.Cassette(fp, mode="positional", check_env=False)
        raise AssertionError("fingerprint cassette 接受按位回放")
    except cassette_mod.PlaybackModeMismatch:
        pass
    po, _ = _extract(rec, "mm-po", playback_mode="positional", derived_from=rec["dir"],
                     expected_assertion="turn_end:reason=answer")
    try:
        cassette_mod.Cassette(po, mode="fingerprint", check_env=False)
        raise AssertionError("positional cassette 接受指纹回放")
    except cassette_mod.PlaybackModeMismatch:
        return


def test_incomplete_llm_rejected():
    rec = _record(_TOOL_BATCH, name="inc")
    cass, _ = _extract(rec, "inc", workspace_root=ROOT)
    meta_path = os.path.join(cass, "meta.json")
    meta = json.load(open(meta_path, encoding="utf-8"))
    meta["completeness"] = {"llm": "missing", "tools": "full"}
    json.dump(meta, open(meta_path, "w", encoding="utf-8"), ensure_ascii=False)
    try:
        cassette_mod.Cassette(cass, require_llm=True, check_env=False)
    except cassette_mod.CassetteIncomplete:
        return
    raise AssertionError("completeness.llm=missing 竟然进入 LLM 回放路径")


def test_headroom_drift_expires_cassette():
    rec = _record(_TOOL_BATCH, name="hr")
    cass, _ = _extract(rec, "hr", workspace_root=ROOT)
    meta_path = os.path.join(cass, "meta.json")
    meta = json.load(open(meta_path, encoding="utf-8"))
    meta["headroom"] = {"version": "0.0.1-not-installed", "mode": "audit"}
    json.dump(meta, open(meta_path, "w", encoding="utf-8"), ensure_ascii=False)
    try:
        cassette_mod.Cassette(cass, check_env=True)
    except cassette_mod.CassetteExpired:
        return
    raise AssertionError("headroom 版本不一致竟然没让 cassette 过期")


def test_assertion_ids_come_from_events_only():
    rec = _record(_ONLY_THINKING_BATCH, question="随便", name="aid")
    ids = trace.assertion_ids(rec["stored"])
    assert "turn_end:reason=stuck" in ids, f"断言面里没有 stuck：{ids}"
    cass, meta = _extract(rec, "aid", workspace_root=ROOT)
    assert "turn_end:reason=stuck" in meta["assertion_ids"]
    # 测试代码自己写的 assert 不参与 id：id 只能由 assert 级事件派生
    assert all("assert" not in i for i in meta["assertion_ids"])


def test_replay_bug27_summary_budget_upgrade():
    """BUG 27：摘要被 finish_reason=length 截断 → 升级预算重试；全程 < 2s（按位 cassette）。"""
    valid = "\n".join(["## 用户目标", "把历史压缩成一段可继续的摘要。",
                       "## 已完成", "读完了相关文件。",
                       "## 关键决策与结论", "按 target-at-end 布局拼块。",
                       "## 改动的文件", "无。",
                       "## 未决问题与下一步", "继续观察预算。"])
    entries = [
        {"type": "llm_request", "request_index": 0, "kind": "summary", "model": "deepseek-v4-flash",
         "params": {"max_tokens": 3000, "temperature": 0.2}, "messages": [{"role": "user", "content": "x"}],
         "stream": False},
        {"type": "llm_end", "request_index": 0, "kind": "summary", "finish_reason": "length",
         "message": {"content": "太短"}, "usage": {"completion_tokens": 3}},
        {"type": "llm_request", "request_index": 1, "kind": "summary", "model": "deepseek-v4-flash",
         "params": {"max_tokens": 6000, "temperature": 0.2}, "messages": [{"role": "user", "content": "x"}],
         "stream": False},
        {"type": "llm_end", "request_index": 1, "kind": "summary", "finish_reason": "stop",
         "message": {"content": valid}, "usage": {"completion_tokens": 90}},
        {"type": "turn_end", "reason": "answer", "request_index": 1},
    ]
    cass = _synth_cassette("bug27", entries, playback_mode="positional",
                           derived_from="synthetic:bug27",
                           expected_assertion="turn_end:reason=answer")
    t0 = time.time()
    with replay.ReplaySession(cass, mode="positional", check_env=False):
        out = la.summarize_block("digest", [{"role": "user", "content": "块内容"}])
    elapsed = time.time() - t0
    assert "error" not in out, f"摘要在回放里失败了：{out}"
    assert "## 未决问题与下一步" in out["summary"]
    assert elapsed < 2.0, f"回放太慢：{elapsed:.2f}s"


# ── 指纹与路径 ───────────────────────────────────────────────────
def test_path_normalization_cross_checkout():
    msgs = [{"role": "user", "content": "看 C:\\builds\\a\\agent\\tmp\\x.py 和 C:\\builds\\a\\agent\\y.py"}]
    other = [{"role": "user", "content": "看 C:\\builds\\b\\agent\\tmp\\x.py 和 C:\\builds\\b\\agent\\y.py"}]
    a = replay.loose_fingerprint("chat", "m", {}, msgs,
                                roots=replay.record_roots("C:\\builds\\a\\agent"))
    b = replay.loose_fingerprint("chat", "m", {}, other,
                                roots=replay.record_roots("C:\\builds\\b\\agent"))
    assert a == b, "跨检出的同一段历史指纹不同"


def test_path_normalization_longest_prefix():
    roots = replay.record_roots("C:\\w")
    roots.add("C:\\w\\tmp\\overlay\\s1", "$OVERLAY")
    out = replay.normalize_text("C:\\w\\tmp\\overlay\\s1\\a.py", {"workspace_path"}, roots)
    assert out == "$OVERLAY\\a.py", f"没有按最长前缀归一：{out!r}"


def test_tools_sha256_shared_between_record_and_replay():
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    assert trace.tools_sha256(tools) == replay._tools_sha256(tools), \
        "录制侧与回放侧的 tools 摘要算法分叉了"
    rec = replay.canon_params({"tools_sha256": trace.tools_sha256(tools)}, tools)
    live = replay.canon_params({}, tools)
    assert "tools_sha256" in live, "现场侧没有把 tools 折进指纹"


def test_canon_ignores_session_ids():
    a = [{"role": "user", "content": "session_id: abcdef request_id: 12345 干活"}]
    b = [{"role": "user", "content": "session_id: zzzzzz request_id: 99999 干活"}]
    fa = replay.loose_fingerprint("chat", "m", {}, a)
    fb = replay.loose_fingerprint("chat", "m", {}, b)
    assert fa == fb, "session/request id 影响了指纹（它们每跑一次都不同）"


# ── strict 判据 ─────────────────────────────────────────────────
def test_strict_precheck_skips_diff():
    """§6.5 Step 1：strict_hash 相等就直接放行，结构化 diff 只在不等的那些请求上跑。

    这个用例断言的是**预检确实省掉了工作**：相等数 ≥ 1（走通了跳过路径），且
    explain_diff 的调用次数恰好等于 strict 指纹不等的请求数——多调一次就说明
    "相等也去跑 diff 了"，预检名存实亡。
    """
    rec = _record(_TOOL_BATCH, question="今天几号", name="sp")
    cass, _ = _extract(rec, "sp", workspace_root=ROOT)
    called = []
    orig = replay.explain_diff

    def _spy(*a, **kw):
        called.append(1)
        return orig(*a, **kw)

    replay.explain_diff = _spy
    try:
        rep = _replay(cass, name="sp-rep", strict=True)
    finally:
        replay.explain_diff = orig
    assert rep.matches, "strict 回放没有匹配上任何请求"
    assert rep.strict_equal >= 1, "strict 预检从未命中（等于没有预检这条路径）"
    assert len(called) == rep.strict_diffed, \
        f"explain_diff 调用 {len(called)} 次，但只有 {rep.strict_diffed} 个请求指纹不等"


def test_strict_rejects_unclassified_diff():
    """现场多了一条录制里没有的消息 → 未归类差异，必须失败。"""
    ex = replay.explain_diff([{"role": "user", "content": "甲"}],
                             [{"role": "user", "content": "甲"},
                              {"role": "user", "content": "多出来的一条"}])
    assert ex.unclassified, "未归类差异没有被识别"
    assert not ex.ok


def test_strict_tolerates_timing_but_flags_slot_structure():
    # 只有耗时不同 → 记入报告，仍然通过
    ex = replay.explain_diff([{"role": "tool", "content": "耗时 12 ms 完成"}],
                             [{"role": "tool", "content": "耗时 848 ms 完成"}])
    assert ex.ok and ex.tolerated.get("timing"), f"耗时差异没被容忍：{ex.summary()}"
    # 录制里有 nudge、现场没有 → nudge 槽位结构对不上
    import loop_agent_core.runner as runner_mod
    nudge = runner_mod._EMPTY_NUDGE
    ex2 = replay.explain_diff([{"role": "user", "content": "干活"},
                               {"role": "user", "content": nudge}],
                              [{"role": "user", "content": "干活"}])
    assert not ex2.ok, "nudge 槽位结构对不上却判通过"
    assert "不可复现" in (ex2.unclassified or ""), ex2.summary()


# ── 截断归因（§4.5）──────────────────────────────────────────────
def _rec_trunc(cap=6000, raw_len=9000):
    return {"call_id": "c9", "truncation": {"cap": cap, "raw_len": raw_len}}


def test_limits_knob_changed_names_knob():
    full = "x" * 9000
    stored = full[:6000] + "\n...(stored truncated, 9000 chars total; re-read via read_file if needed)"
    limits_rec = {"knobs": {"result_cap": 6000}}
    limits_now = {"knobs": {"result_cap": 8000}}
    try:
        replay.attribute_truncation(full, stored, _rec_trunc(6000), limits_now, limits_rec)
    except replay.TruncationBudgetChanged as exc:
        assert "result_cap" in str(exc), f"没点名旋钮：{exc}"
        assert "6000" in str(exc) and "8000" in str(exc), f"没给出新旧值：{exc}"
        return
    raise AssertionError("预算从 6000 改到 8000 竟然没抛 TruncationBudgetChanged")


def test_truncation_logic_drift_only_reports():
    full = "y" * 9000
    stored = "完全不同的截断结果"
    rep = replay.ReplayReport(None, "fingerprint")
    verdict = replay.attribute_truncation(full, stored, _rec_trunc(6000),
                                          {"knobs": {"result_cap": 6000}},
                                          {"knobs": {"result_cap": 6000}}, report=rep)
    assert verdict == "drift", f"预算没变却对不上应判 drift：{verdict}"
    assert rep.drifts, "漂移没有记进报告"


def test_unattributable_without_limits():
    try:
        replay.attribute_truncation("z" * 9000, "对不上", _rec_trunc(6000),
                                    {"knobs": {"result_cap": 6000}}, None)
    except replay.Unattributable:
        return
    raise AssertionError("录制里没有 limits 时不得猜，应抛 Unattributable")


# ── 工具供给与目录 ───────────────────────────────────────────────
def test_delegate_task_stub():
    entries = [
        {"type": "llm_request", "request_index": 0, "kind": "chat", "model": "m",
         "params": {}, "messages": [{"role": "user", "content": "派活"}], "stream": True},
        {"type": "tool_call", "call_id": "t9", "tool_name": "delegate_task", "class": "C",
         "arguments_raw": "{}", "request_index": 0},
        {"type": "tool_result", "call_id": "t9", "tool_name": "delegate_task", "class": "C",
         "result_full": "子 agent 的录制结论", "result_stored": "子 agent 的录制结论"},
        {"type": "turn_end", "reason": "answer", "request_index": 0},
    ]
    cass = _synth_cassette("deleg", entries)
    cas = cassette_mod.Cassette(cass, check_env=False)
    box = replay.ReplayToolBox(cas)
    rec = box.result_for("delegate_task", "t9")
    assert rec and rec["result_full"] == "子 agent 的录制结论", "delegate_task 没有整体 stub"


def test_get_date_returns_injected_epoch_date():
    """§8.3 特例：回放时 `get_date` 吐 `trace_epoch.injected_date_str`，而不是录制那天的日期。"""
    epoch = trace.trace_epoch()
    entries = [
        {"type": "llm_request", "request_index": 0, "kind": "chat", "model": "m",
         "params": {}, "messages": [{"role": "user", "content": "今天几号"}], "stream": True},
        {"type": "tool_call", "call_id": "d1", "tool_name": "get_date", "class": "A",
         "arguments_raw": "{}", "request_index": 0},
        {"type": "tool_result", "call_id": "d1", "tool_name": "get_date", "class": "A",
         "result_full": "1999-01-01", "result_stored": "1999-01-01"},
        {"type": "turn_end", "reason": "answer", "request_index": 0},
    ]
    cass = _synth_cassette("epochdate", entries)
    cas = cassette_mod.Cassette(cass, check_env=False)
    box = replay.ReplayToolBox(cas, exec_date=True)          # ① CLI 默认打开注入
    d = box.decision("get_date", "d1")
    assert isinstance(d, str) and d == epoch["injected_date_str"], \
        f"get_date 没有注入 trace_epoch 的日期：{d!r}"
    assert box.injected_date() == epoch["injected_date_str"], box.injected_date()
    # 关掉注入 → 退回录制结果（仍是吐文本，不真跑工具）
    box2 = replay.ReplayToolBox(cas, policy="class", exec_date=False)
    d2 = box2.decision("get_date", "d1")
    assert d2 == "1999-01-01", d2



    rec = _record(_TOOL_BATCH, name="under")
    assert os.path.commonpath([os.path.abspath(rec["dir"]), os.path.abspath(ROOT)]) == os.path.abspath(ROOT)
    rel = os.path.relpath(rec["dir"], ROOT).replace("\\", "/")
    assert rel.startswith("tmp/"), f"trace 目录必须在 tmp/ 下：{rel}"
    for f in ("trace.jsonl", "trace.idx.json", "meta.json"):
        assert os.path.exists(os.path.join(rec["dir"], f)), f"缺 {f}"


def test_assertion_vocab_rename_is_detected():
    """改 notice code / turn_end reason 后，旧 cassette 必须**报错**而不是静默读下去（§9 #19）。

    静默读下去的后果是：回放照跑，导出一个当前词表里根本不存在的断言 id，于是报
    "目标失败没复现"——错的是词表，指向的却是回放。这条用例守的就是这个歧义。
    """
    rec = _record(_ONLY_THINKING_BATCH, question="随便", name="vocab")
    cass, _ = _extract(rec, "vocab", workspace_root=ROOT)
    path = os.path.join(cass, "cassette.jsonl")
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    hit = 0
    for e in rows:
        if e.get("type") == "notice" and e.get("code"):
            e["code"] = "retired_code_v0"
            hit += 1
    assert hit, "这次录制里没有 notice 事件，用例失去意义"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for e in rows:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    try:
        cassette_mod.Cassette(cass, check_env=False)
    except cassette_mod.CassetteFormatError as exc:
        assert "词表" in str(exc), exc
    else:
        raise AssertionError("用了退役 code 的 cassette 竟然加载成功")

    # converter 路径：注册一个改名映射后必须能读进来，且 id 跟着迁移。
    def _rename(meta, events):
        for e in events:
            if e.get("type") == "notice" and e.get("code") == "retired_code_v0":
                e["code"] = "stuck"
        return meta, events

    cassette_mod.register_converter(cassette_mod.CASSETTE_FORMAT - 1, _rename)
    try:
        meta = json.load(open(os.path.join(cass, "meta.json"), encoding="utf-8"))
        meta["cassette_format"] = cassette_mod.CASSETTE_FORMAT - 1
        json.dump(meta, open(os.path.join(cass, "meta.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
        cas = cassette_mod.Cassette(cass, check_env=False)
        assert "notice:warn:stuck" in cas.assert_ids(), cas.assert_ids()
    finally:
        cassette_mod._VOCAB_CONVERTERS.pop(cassette_mod.CASSETTE_FORMAT - 1, None)


def test_canon_version_independent():
    """只改规范化口径、不重录 → 旧 cassette 仍可回放（§9 #23）。

    指纹是回放时**现场重算**的：录制里存的是原始 messages，不是指纹值。所以换一版
    归一化规格不需要重录。canon_version 只作为显式诊断（check_canon_version）存在，
    不能变成加载时的硬闸——那会把"口径改了"误判成"证据过期了"。
    """
    rec = _record(_TOOL_BATCH, question="今天几号", name="canonver")
    cass, _ = _extract(rec, "canonver", workspace_root=ROOT)
    meta_path = os.path.join(cass, "meta.json")
    meta = json.load(open(meta_path, encoding="utf-8"))
    meta["canon_version"] = 0
    json.dump(meta, open(meta_path, "w", encoding="utf-8"), ensure_ascii=False)
    rep = _replay(cass, name="canonver-rep")
    assert len(rep.matches) == 2, f"换了 canon 口径后旧 cassette 回放失败：{rep.matches}"
    cas = cassette_mod.Cassette(cass, check_env=False)
    try:
        cas.check_canon_version()
    except cassette_mod.CassetteFormatError:
        return
    raise AssertionError("显式调用时竟然没报 canon_version 不一致")


def test_skip_report_explains_each_skip():
    """permissive 回放的每个 skip 都能解释（首个分叉 message）；钉死用例 skips == 0（§9 #10）。"""
    # 钉死：同一段历史重跑，一次都不该跳。
    rec = _record(_TOOL_BATCH, question="今天几号", name="skip0")
    cass, _ = _extract(rec, "skip0", workspace_root=ROOT)
    rep0 = _replay(cass, name="skip0-rep")
    assert rep0.skips_count == 0, f"同一段历史出现了跳过：{rep0.skips}"

    # 现场第一跳就直接跳到录制的第二个请求 → 录制的第一个请求被跳过，必须给解释。
    entries = [
        {"type": "llm_request", "request_index": 1, "kind": "chat", "model": "m",
         "params": {"max_tokens": 10}, "stream": True,
         "messages": [{"role": "user", "content": "第一轮"},
                      {"role": "assistant", "content": "答"},
                      {"role": "user", "content": "第二轮"}]},
        {"type": "llm_chunk", "request_index": 1, "seq_content": None, "content": "第二轮答",
         "finish_reason": None},
        {"type": "llm_end", "request_index": 1, "finish_reason": "stop",
         "usage": {"total_tokens": 3}},
        {"type": "turn_end", "reason": "answer", "request_index": 1},
    ]
    path = _synth_cassette("skip", entries, playback_mode="fingerprint")
    cas = cassette_mod.Cassette(path, check_env=False)
    report = replay.ReplayReport(cas, "fingerprint")
    tr = replay.ReplayTransport(path, cassette=cas, report=report)
    try:
        tr.create("chat", {}, {"model": "m", "max_tokens": 10, "stream": True,
                               "messages": [{"role": "user", "content": "完全不同的历史"}]})
    except replay.NoMatchInWindow:
        pass
    else:
        raise AssertionError("窗口内没有匹配却成功回放")
    # 现场这次必须能命中录制的第 2 条（按位消费不算，这里用真指纹路径）
    tr.create("chat", {}, {"model": "m", "max_tokens": 10, "stream": True,
                           "messages": entries[0]["messages"]})
    assert len(report.matches) == 1, report.matches
    assert report.skips_count == 0, "精确命中不该记 skip"


def test_skip_report_note_has_first_divergence():
    """真的发生前移时，skip 的 note 必须指出首个分叉（不是"前移了"这种空话）。"""
    entries = [
        {"type": "llm_request", "request_index": 1, "kind": "chat", "model": "m",
         "params": {}, "stream": True,
         "messages": [{"role": "user", "content": "第一轮"}]},
        {"type": "llm_chunk", "request_index": 1, "content": "一", "finish_reason": None},
        {"type": "llm_end", "request_index": 1, "finish_reason": "stop"},
        {"type": "llm_request", "request_index": 2, "kind": "chat", "model": "m",
         "params": {}, "stream": True,
         "messages": [{"role": "user", "content": "第一轮"},
                      {"role": "assistant", "content": "一"},
                      {"role": "user", "content": "第二轮"}]},
        {"type": "llm_chunk", "request_index": 2, "content": "二", "finish_reason": None},
        {"type": "llm_end", "request_index": 2, "finish_reason": "stop"},
        {"type": "turn_end", "reason": "answer", "request_index": 2},
    ]
    path = _synth_cassette("skipnote", entries, playback_mode="fingerprint")
    cas = cassette_mod.Cassette(path, check_env=False)
    report = replay.ReplayReport(cas, "fingerprint")
    tr = replay.ReplayTransport(path, cassette=cas, report=report)
    tr.create("chat", {}, {"model": "m", "stream": True,
                           "messages": entries[3]["messages"]})
    assert report.skips_count == 1, report.skips
    note = report.skips[0]["note"]
    assert note and note != "窗口内前移到指纹命中的位置", f"skip 没有给出分叉解释：{note!r}"
    assert "$[" in note, f"skip note 没点名第几条 message：{note!r}"
    assert ("第一轮" in note or "第二轮" in note or '"一"' in note), \
        f"skip note 没带 message 内容：{note!r}"
    assert "skip" in "\n".join(report.lines()), report.lines()


def test_replay_does_not_touch_workspace():
    """回放不改工作区：仓库文件 mtime/sha256 不变，也不新增文件（§9 #26 的 L1 子集）。"""
    import hashlib

    def _snap():
        out = {}
        for name in sorted(os.listdir(ROOT)):
            p = os.path.join(ROOT, name)
            if os.path.isfile(p) and name.endswith((".md", ".py", ".txt")):
                st = os.stat(p)
                with open(p, "rb") as f:
                    out[name] = (st.st_mtime_ns, hashlib.sha256(f.read()).hexdigest())
        return out

    rec = _record(_TOOL_BATCH, question="今天几号", name="nowrite")
    cass, _ = _extract(rec, "nowrite", workspace_root=ROOT)
    before = _snap()
    _replay(cass, name="nowrite-rep", tool_text="真执行了！！")
    after = _snap()
    assert before == after, \
        f"回放动了工作区：{sorted(set(before.items()) ^ set(after.items()))[:3]}"


# ── L2 overlay 与工具策略（§8）─────────────────────────────────────
# 注意：`Overlay.root` 是 `overlay_root/<session_id>`（见 `overlay.overlay_root`），
# 不是 `overlay_root` 本身。断言里要判"落没落在 overlay 里"，一律取
# `rep.overlay.root`，别拿传进去的那个父目录去拼文件路径。
_L2_SEEN = {}


class _L2Fn:
    """带标记的 B 类工具：标记证明"真的被执行了"，返回值与录制期同形。"""

    def __init__(self, marker):
        self.marker = marker

    def __call__(self, path, content, purpose="", **kw):
        from loop_agent_core import confirm
        from loop_agent_core.file_tools import write_file_mock
        resolved = os.path.abspath(path if os.path.isabs(path) else os.path.join(ROOT, path))
        _L2_SEEN.setdefault("calls", []).append(resolved)
        if not confirm.request("write_file", resolved, purpose):
            return "[Denied by user]"
        res = write_file_mock(resolved, content, purpose)
        size = len(content.encode("utf-8"))
        out = f"{self.marker} {size} bytes to {resolved}"
        if os.environ.get("L2_DEBUG"):
            print(f"[L2_DEBUG] marker path={path!r} resolved={resolved!r} "
                  f"written={os.path.exists(resolved)} real={res!r}")
        return out


def _write_batch(rel, content):
    """一个 write_file 工具轮 + 一轮收尾。"""
    args = json.dumps({"path": rel, "content": content}, ensure_ascii=False)
    return [[_Chunk(_Delta(tool_calls=[_ToolCall(0, "w1", "write_file", args)])),
             _Chunk(_Delta(), finish_reason="tool_calls"),
             _Chunk(usage=True, no_choices=True)],
            [_Chunk(_Delta(content="写好了"), finish_reason="stop"),
             _Chunk(usage=True, no_choices=True)]]


def _record_write(name, rel, content, marker="[LIVE]"):
    from loop_agent_core import confirm
    _L2_SEEN.clear()
    prev_policy = confirm.get_policy()
    prev_tool = la.TOOL_CALL_MAP.get("write_file")
    confirm.set_policy("allow_all")
    la.TOOL_CALL_MAP["write_file"] = _L2Fn(marker)
    try:
        rec = _record(_write_batch(rel, content), question="写个文件", name=name)
    finally:
        confirm.set_policy(prev_policy)
        if prev_tool is not None:
            la.TOOL_CALL_MAP["write_file"] = prev_tool
    rec["calls"] = list(_L2_SEEN.get("calls") or [])
    return rec


def _replay_class(cass_dir, name="l2-rep", *, overlay_read=False, keep=False):
    """L2 回放：`tool_policy="class"`，B 类入 overlay 真执行。

    `keep=True` 让 overlay 在这次回放后**留一会儿**（`keep_overlay`），用完自己删：
    overlay 按设计是"本次回放的副作用现场"，跑完就清（§8.1），要在里面取样就得先留住它。
    """
    from loop_agent_core import confirm
    run_dir = _fresh_dir(_TRACE_ROOT, name)
    _LAST_RUN["dir"] = run_dir
    run_dir_clean = os.path.abspath(cass_dir.rstrip("\\/")) + ".check"
    shutil.rmtree(run_dir_clean, ignore_errors=True)
    _L2_SEEN.clear()
    prev_policy = confirm.get_policy()
    prev_tool = la.TOOL_CALL_MAP.get("write_file")
    ov_root = os.path.join(ROOT, "tmp", "overlay", f"l2-{name}")
    shutil.rmtree(ov_root, ignore_errors=True)
    confirm.set_policy("allow_all")
    la.TOOL_CALL_MAP["write_file"] = _L2Fn("[LIVE]")
    try:
        rep = replay.run_replay(cass_dir, question=None, conv=_new_conv(os.path.basename(run_dir)),
                                run_dir=run_dir, tool_policy="class", check_env=False,
                                overlay_root=ov_root, overlay_read=overlay_read,
                                keep_overlay=keep)
    finally:
        confirm.set_policy(prev_policy)
        if prev_tool is not None:
            la.TOOL_CALL_MAP["write_file"] = prev_tool
    _L2_SEEN["overlay_root"] = ov_root
    if os.environ.get("L2_DEBUG"):
        ov_ = rep.overlay
        print("[L2_DEBUG] root=", ov_root)
        print("[L2_DEBUG] overlay.root=", getattr(ov_, "root", None))
        print("[L2_DEBUG] touched=", getattr(ov_, "touched", None))
        print("[L2_DEBUG] copied=", getattr(ov_, "copied", None))
        print("[L2_DEBUG] missing=", getattr(ov_, "missing", None))
        print("[L2_DEBUG] live=", getattr(ov_, "live_calls", None))
        print("[L2_DEBUG] seen=", _L2_SEEN.get("calls"))
        print("[L2_DEBUG] tree=", [os.path.join(dp, f) for dp, _d, fn in os.walk(ov_root)
                                    for f in fn])
    return rep


def test_overlay_does_not_touch_repo():
    """§9 #26：L2 的 B 类真执行只写 overlay——真实 WORK_DIR 文件 mtime/sha256 一字不动。"""
    import hashlib

    def _dig(p):
        st = os.stat(p)
        with open(p, "rb") as f:
            return st.st_mtime_ns, hashlib.sha256(f.read()).hexdigest()

    content = "第一次写入\n"
    real_path = os.path.join(ROOT, "tmp", "_l2_real.txt")
    os.makedirs(os.path.dirname(real_path), exist_ok=True)
    with open(real_path, "w", encoding="utf-8") as f:
        f.write(content)

    rel = "tmp/_l2_real.txt"
    rec = _record_write("l2rec", rel, content)
    assert rec["calls"], "录制期没有真的调用 write_file（用例失去意义）"
    before = _dig(real_path)

    cass, meta = _extract(rec, "l2rec", workspace_root=ROOT)
    ov_root = os.path.join(ROOT, "tmp", "overlay", "l2-iso")
    shutil.rmtree(ov_root, ignore_errors=True)
    from loop_agent_core import confirm
    prev_policy = confirm.get_policy()
    prev_tool = la.TOOL_CALL_MAP.get("write_file")
    confirm.set_policy("allow_all")
    la.TOOL_CALL_MAP["write_file"] = _L2Fn("[LIVE]")
    _L2_SEEN.clear()
    try:
        rep = replay.run_replay(cass, conv=_new_conv("l2iso"),
                                run_dir=_fresh_dir(_TRACE_ROOT, "l2iso-rep"),
                                tool_policy="class", check_env=False, overlay_root=ov_root,
                                keep_overlay=True)
    finally:
        confirm.set_policy(prev_policy)
        if prev_tool is not None:
            la.TOOL_CALL_MAP["write_file"] = prev_tool

    after = _dig(real_path)
    assert before == after, f"L2 回放动了真实工作区文件：{real_path}"
    live = _L2_SEEN.get("calls") or []
    assert live, "L2 回放没有真执行 B 类工具（§8.3 的 B 类必须真跑）"
    assert all(p.startswith(rep.overlay.root) for p in live), \
        f"B 类真执行没有落在 overlay 里：{live}"
    assert rep.overlay is not None and rep.overlay.live_calls, "报告里没有真执行记录"
    # 真执行确实在副本上落了盘：overlay 里那份是"这次回放写出来的"
    copied = os.path.join(rep.overlay.root, rel)
    assert os.path.exists(copied), f"overlay 里没有写入落点：{copied}"
    text = rep.text()
    assert "overlay" in text and "真执行" in text, f"报告没有 overlay 段：\n{text}"
    # 清掉本次用的整个 overlay 根：`rep.overlay.root` 只是它下面的一个会话目录
    # （`overlay_root/<sid>`），只删那一层会在 tmp/overlay/ 里留下一串空目录。
    shutil.rmtree(ov_root, ignore_errors=True)
    os.remove(real_path)


def test_l2_isolation_records_but_does_not_apply():
    """L2 真执行的写入确实落在 overlay 副本上：overlay 里是新内容，工作区还是旧的。"""
    real_path = os.path.join(ROOT, "tmp", "_l2_iso.txt")
    os.makedirs(os.path.dirname(real_path), exist_ok=True)
    with open(real_path, "w", encoding="utf-8") as f:
        f.write("原始内容\n")
    rec = _record_write("l2iso2", "tmp/_l2_iso.txt", "原始内容\n")
    cass, _ = _extract(rec, "l2iso2", workspace_root=ROOT)

    rep = _replay_class(cass, name="iso2", keep=True)
    assert rep.overlay is not None, "policy=class 没有建 overlay"
    try:
        # 报告里写明"真执行"，且执行时看到的是 overlay 路径而不是工作区路径
        assert [n for _cid, n in rep.overlay.live_calls] == ["write_file"], rep.overlay.live_calls
        seen = _L2_SEEN.get("calls") or []
        assert seen, "回放没有真执行 B 类工具"
        assert all(p.startswith(rep.overlay.root) for p in seen), seen
        copied = os.path.join(rep.overlay.root, "tmp", "_l2_iso.txt")
        assert os.path.exists(copied), f"overlay 里没有这个被触碰的文件：{copied}"
        with open(copied, encoding="utf-8") as f:
            assert f.read() == "原始内容\n", "overlay 副本内容与工作区不一致"
        with open(real_path, encoding="utf-8") as f:
            assert f.read() == "原始内容\n", "真实工作区文件被回放改写了"
    finally:
        # 连同 overlay 根一起清（只删 `rep.overlay.root` 会留下 tmp/overlay/l2-iso2/ 空壳）
        shutil.rmtree(os.path.dirname(rep.overlay.root), ignore_errors=True)
        os.remove(real_path)


def test_overlay_recursive_rewrite():
    """§9 #35：search_code / code_index 的目标被重写成 overlay 根，且真扫的是副本。"""
    from loop_agent_core import overlay as ov_mod

    ov = ov_mod.Overlay("rewrite", root=os.path.join(ROOT, "tmp", "overlay"))
    try:
        assert ov.root.startswith(os.path.abspath(os.path.join(ROOT, "tmp"))), ov.root
        for name in ("search_code", "code_index"):
            new, note = ov.rewrite_args(name, {"path": ".", "pattern": "x", "glob": "*.py"})
            assert new["path"] == ov.root, f"{name} 没有把目标重写成 overlay 根：{new['path']}"
            assert note and note.startswith("structured:"), f"{name} 没有给出重写说明：{note}"
        # 已经指向 overlay 的路径不该被重复改写
        again, _ = ov.rewrite_args("search_code", {"path": ov.root})
        assert again["path"] == ov.root, "overlay 内的路径被二次改写"
        # 命令串走 best_effort：WORK_DIR 子串替换 + 在 overlay 里起跑
        cmd = f"Get-ChildItem {ROOT}\\tmp"
        rewritten, note = ov.rewrite_args("exec_shell_win", {"shell_cmd": cmd})
        assert ov.root in rewritten["shell_cmd"], rewritten["shell_cmd"]
        assert rewritten["shell_cmd"].startswith("Set-Location"), rewritten["shell_cmd"]
        assert note == "best_effort:shell_cmd", note
    finally:
        ov.cleanup()


def test_overlay_path_mapping_round_trip():
    """§8.2.2：overlay 路径 ↔ 逻辑路径双向映射，以及工具出参里的反向替换。"""
    from loop_agent_core import overlay as ov_mod

    ov = ov_mod.Overlay("map", root=os.path.join(ROOT, "tmp", "overlay"))
    try:
        logical = os.path.join(ROOT, "tmp", "a", "b.py")
        inside = ov.to_overlay(logical)
        assert inside.startswith(ov.root), inside
        assert ov.to_logical(inside) == logical, ov.to_logical(inside)
        # 出参里的 overlay 路径必须换回逻辑路径（写进会话历史前）
        text = f"Written 12 bytes to {inside}\n"
        assert ov.reverse_text(text) == f"Written 12 bytes to {logical}\n"
        # 斜杠写法也要换回去
        assert ov.reverse_text(inside.replace("\\", "/")) == logical
        # WORK_DIR 之外的路径不参与映射，原样返回
        outside = os.path.abspath(os.path.join(ROOT, "..", "outside.py"))
        assert ov.to_overlay(outside) == outside
        assert ov.to_logical(outside) == outside
    finally:
        ov.cleanup()


def test_overlay_permits_only_its_own_root():
    """overlay 是路径沙箱里**额外**的一个合法根，不放松任何其它路径（§8.2 的接缝）。"""
    from loop_agent_core import overlay as ov_mod

    ov = ov_mod.Overlay("permit", root=os.path.join(ROOT, "tmp", "overlay"))
    try:
        assert ov.permits(os.path.join(ov.root, "tmp", "x.py"))
        assert not ov.permits(os.path.join(ROOT, "loop_agent_v2.py"))
        assert not ov.permits(os.path.abspath(os.path.join(ROOT, "..")))
    finally:
        ov.cleanup()
    try:
        ov_mod.Overlay("escape", root=os.path.abspath(os.path.join(ROOT, "..", "overlay")))
    except ValueError as exc:
        assert "WORK_DIR" in str(exc), exc
        return
    raise AssertionError("overlay 根落在 WORK_DIR 之外竟然被接受了")


def test_overlay_read_prefers_overlay_after_b_write():
    """§8.2.3 跨类污染：B 类写过之后，A 类在该路径上读到的是 overlay 里那一份。

    这条用例用的是 overlay 自己的判据（`read_from_overlay` 开关 + 记账），不经过模型——
    "读到新内容"这件事不需要一次 LLM 往返来证明，而它正是判定回放有没有走偏的关键。
    """
    from loop_agent_core import overlay as ov_mod

    rel = "tmp/_l2_xread.txt"
    path = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("旧内容\n")
    ov = ov_mod.Overlay("xread", root=os.path.join(ROOT, "tmp", "overlay"),
                        read_from_overlay=True)
    try:
        # 建副本（§8.1）→ B 类在副本上写下新内容 → A 类要读到的是新的那一份
        ov.prepare()
        inner = os.path.normpath(os.path.join(ov.root, rel))
        os.makedirs(os.path.dirname(inner), exist_ok=True)
        with open(inner, "w", encoding="utf-8") as f:
            f.write("新内容\n")
        ov.note_written(rel)
        assert ov.modified(rel) == inner, ov.modified(rel)
        assert ov.read_from_overlay
        prev_active = replay.ReplaySession._active
        replay.ReplaySession._active = SimpleNamespace(overlay=ov)
        try:
            hit = ov_mod.overlay_result_for("read_file", {"path": rel})
            assert hit and hit.startswith(ov.root), f"没有重读 overlay：{hit!r}"
            # 写目录、读目录里的文件 → 同样指到 overlay 里那一份
            ov.note_written("tmp")
            assert ov.modified(rel) == inner, "写了目录之后没有向上找到副本"
            # 关掉开关 → 不重读，交给录制结果
            ov.read_from_overlay = False
            assert ov_mod.overlay_result_for("read_file", {"path": rel}) is None
        finally:
            replay.ReplaySession._active = prev_active
    finally:
        ov.cleanup()
        os.remove(path)


def test_runner_keeps_logical_args_in_history():
    """§8.2.1：重写只发生在执行用的副本上——会话历史里那条 tool_call 仍是逻辑路径。"""
    real_path = os.path.join(ROOT, "tmp", "_l2_hist.txt")
    os.makedirs(os.path.dirname(real_path), exist_ok=True)
    with open(real_path, "w", encoding="utf-8") as f:
        f.write("历史\n")
    rec = _record_write("l2hist", "tmp/_l2_hist.txt", "历史\n")
    cass, _ = _extract(rec, "l2hist", workspace_root=ROOT)
    try:
        _replay_class(cass, name="hist")
        ov_root = _L2_SEEN["overlay_root"]
        calls = [e for e in trace.read_events(_LAST_RUN["dir"]) if e.get("type") == "tool_call"]
        written = [e for e in calls if e.get("overlay")]
        if os.environ.get("L2_DEBUG"):
            print("[L2_DEBUG] run_dir=", _LAST_RUN["dir"])
            print("[L2_DEBUG] tool_calls=", json.dumps(calls, ensure_ascii=False)[:1500])
        assert written, "回放 trace 里没有带 overlay 标记的 tool_call"
        ev = written[0]
        assert ev["arguments"]["path"] == "tmp/_l2_hist.txt", \
            f"trace 里的逻辑入参被改写了：{ev['arguments']}"
        assert ev["overlay"]["written"]["path"].startswith(ov_root), ev["overlay"]
    finally:
        # 回放自身把 overlay 的会话目录清掉了，但它的**父目录**（本次 overlay 根）是这次
        # 调用建的：不删的话 tmp/overlay/ 下会攒下一串空目录。
        _ov_root = _L2_SEEN.get("overlay_root")
        if _ov_root:
            shutil.rmtree(_ov_root, ignore_errors=True)
        os.remove(real_path)


def test_cli_replay_overlay_flags():
    """CLI 是 L2 的入口：`--tool-policy class --overlay-root … --keep-overlay` 真的接上了。"""
    from tools import trace as trace_cli

    real_path = os.path.join(ROOT, "tmp", "_l2_cli.txt")
    os.makedirs(os.path.dirname(real_path), exist_ok=True)
    with open(real_path, "w", encoding="utf-8") as f:
        f.write("CLI 前的旧内容\n")
    rec = _record_write("l2cli", "tmp/_l2_cli.txt", "CLI 前的旧内容\n")
    # 这条 cassette 不是 positional（`expected_assertion` 留空），CLI 会把"未声明目标断言"判成
    # 返回码 2——那是判分口径，不是回放失败。所以这里只认"回放没崩 + overlay 行为对"。
    cass, _ = _extract(rec, "l2cli", workspace_root=ROOT)

    from loop_agent_core import confirm
    ov_root = os.path.join(ROOT, "tmp", "overlay", "l2-cli")
    shutil.rmtree(ov_root, ignore_errors=True)
    prev_policy = confirm.get_policy()
    prev_tool = la.TOOL_CALL_MAP.get("write_file")
    confirm.set_policy("allow_all")
    _L2_SEEN.clear()
    la.TOOL_CALL_MAP["write_file"] = _L2Fn("[CLI]")
    try:
        rc = trace_cli.main(["replay", cass, "--tool-policy", "class",
                             "--overlay-root", ov_root, "--keep-overlay",
                             "--overlay-read"])
    finally:
        confirm.set_policy(prev_policy)
        if prev_tool is not None:
            la.TOOL_CALL_MAP["write_file"] = prev_tool

    assert rc in (0, 2), f"CLI 回放异常退出：{rc}"
    live = _L2_SEEN.get("calls") or []
    assert live, "CLI 的 tool_policy=class 没有真执行 B 类工具"
    assert all(p.startswith(ov_root) for p in live), f"CLI 真执行没落在 overlay 里：{live}"
    with open(real_path, encoding="utf-8") as f:
        assert f.read() == "CLI 前的旧内容\n", "CLI 回放改写了真实工作区文件"
    assert os.path.isdir(ov_root), f"--keep-overlay 没有留下 overlay：{ov_root}"
    shutil.rmtree(ov_root, ignore_errors=True)
    os.remove(real_path)


def test_headroom_store_isolation():
    """回放的 headroom 记录只进本次 trace 目录，真实遥测文件不变（§9 #32）。"""
    import loop_agent_core.config as config_mod
    real = getattr(config_mod, "_HEADROOM_DB_PATH", None) or os.path.join(ROOT, "tmp", "headroom.jsonl")
    import hashlib

    def _dig(path):
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    before = _dig(real)
    rec = _record(_TOOL_BATCH, question="今天几号", name="hriso")
    cass, _ = _extract(rec, "hriso", workspace_root=ROOT)
    run_dir = _fresh_dir(_TRACE_ROOT, "hriso-rep")   # 回放自己的 trace 目录
    _replay(cass, name=os.path.basename(run_dir) if False else "hriso-rep2")
    assert _dig(real) == before, f"回放写了真实 headroom 遥测文件：{real}"
    # 本次回放的 headroom 记录（如果有）必须落在 trace 根之下
    for _, _, files in os.walk(_TRACE_ROOT):
        for fn in files:
            if fn == "headroom.jsonl":
                p = os.path.join(_, fn)
                assert os.path.commonpath([p, _TRACE_ROOT]) == os.path.abspath(_TRACE_ROOT), p


def test_replay_offline():
    """回放套件在**无网**下通过：任何 socket 连接都直接失败（§9 #33）。"""
    import socket
    rec = _record(_TOOL_BATCH, question="今天几号", name="offline")
    cass, _ = _extract(rec, "offline", workspace_root=ROOT)

    class _NoNet(socket.socket):
        def connect(self, *a, **kw):
            raise AssertionError(f"回放尝试联网：{a[:1]}")

    orig_connect = socket.socket.connect
    orig_create = socket.create_connection

    def _refuse(*a, **kw):
        raise AssertionError(f"回放尝试联网：{a[:1]}")

    socket.socket.connect = _NoNet.connect
    socket.create_connection = _refuse
    try:
        rep = _replay(cass, name="offline-rep")
    finally:
        socket.socket.connect = orig_connect
        socket.create_connection = orig_create
    assert rep.matches, "离线回放没有匹配到请求"


def test_checkpoint_restore_selfcheck():
    """seek → restore 的自检：messages hash 与 B 组记账都要与录制侧一致（§9 #11）。"""
    rec = _record(_TOOL_BATCH, question="今天几号", name="restore")
    reqs = [e for e in rec["stored"] if e.get("type") == "llm_request"]
    assert reqs, "没有 llm_request"
    limits_rec = next((e for e in rec["stored"] if e.get("type") == "limits"), None)
    conv = _new_conv("restored")
    r = cp.seek(rec["dir"], reqs[0]["request_index"], reqs[0]["messages"],
                conv=conv, apply=True)
    assert r["problems"] == [], f"restore 自检不干净：{r['problems']}"
    assert "interrupt_event" in r["applied"], r["applied"]
    assert r["snap"]["F"], "F 组（stable_prefix_hash）没有被恢复记录"
    # B 组记账写回了 conv（若录制里有的话）
    if (limits_rec or {}).get("accounting"):
        assert r["applied"].get("B") is not None, r["applied"]

    # messages 对不上 → find_for_request 就 fail-closed
    try:
        cp.seek(rec["dir"], reqs[0]["request_index"], [{"role": "user", "content": "改过了"}])
    except cp.CheckpointRestoreMismatch:
        pass
    else:
        raise AssertionError("messages 对不上竟然还能 seek 进去")

    # B 组记账被篡改 → 自检必须报出来（对账是 restore 的一部分，不能只看 messages）
    store = cp.CheckpointStore(rec["dir"])
    snap = store.find_for_request(reqs[0]["request_index"], reqs[0]["messages"])
    tampered = dict(snap)
    tampered["B"] = dict(snap.get("B") or {})
    tampered["B"]["_compact_count"] = 999
    out = cp.restore(tampered, _new_conv("t"),
                     messages=reqs[0]["messages"],
                     limits_recorded={"accounting": {"compact_count": 0}}, apply=False)
    assert any("B 组记账不一致" in p for p in out["problems"]), out


def test_restore_precheck_does_not_apply():
    """apply=False 只预检：不能改进程状态（否则"看一眼"就改变了被观察对象）。"""
    from loop_agent_core.control import interrupt_event
    rec = _record(_TOOL_BATCH, name="precheck")
    reqs = [e for e in rec["stored"] if e.get("type") == "llm_request"]
    interrupt_event.set()
    try:
        r = cp.seek(rec["dir"], reqs[0]["request_index"], reqs[0]["messages"], apply=False)
        assert r["problems"] == [], r["problems"]
        assert interrupt_event.is_set(), "apply=False 竟然清了中断标志"
    finally:
        interrupt_event.clear()


def test_checkpoint_matches_llm_request_messages():
    rec = _record(_TOOL_BATCH, name="ck")
    store = cp.CheckpointStore(rec["dir"])
    reqs = [e for e in rec["stored"] if e.get("type") == "llm_request"]
    assert reqs, "没有 llm_request"
    for req in reqs:
        snap = store.find_for_request(req["request_index"], req["messages"], kind="pre_request")
        assert snap["messages_hash"] == cp.messages_digest(req["messages"])
    # 篡改 messages 必须抛错，而不是拿"差不多"的快照继续跑
    try:
        store.find_for_request(reqs[0]["request_index"], [{"role": "user", "content": "改过了"}],
                               kind="pre_request")
    except cp.CheckpointRestoreMismatch:
        return
    raise AssertionError("messages 对不上竟然还能取到 checkpoint")


# ── 入库 fixture（§1 交付物 11）：跨时间的格式与指纹基线 ──────────
_FIXTURE_ROOT = os.path.join(ROOT, "tests", "fixtures", "replay")


def _fixture(name):
    path = os.path.join(_FIXTURE_ROOT, name)
    assert os.path.isdir(path), f"缺入库 fixture：{path}（跑 tests/fixtures/replay/_regenerate.py 生成）"
    return path


def test_fixture_fingerprint_replays():
    """指纹模式 fixture：改了指纹算法就必须在这里红，而不是悄悄放行。"""
    cass = _fixture("fingerprint_tool_round")
    cas = cassette_mod.Cassette(cass, check_env=False)
    assert cas.meta["playback_mode"] == "fingerprint"
    assert cas.meta["completeness"]["llm"] == "full", cas.meta["completeness"]
    assert cas.expected_assertion() == "turn_end:reason=answer"
    rep = _replay(cass, name="fixture-fp", check_env=False)
    assert rep.reproduced, rep.text()
    assert rep.skips_count == 0, rep.text()
    assert len(rep.matches) == cas.request_count(), (rep.matches, cas.request_count())


def test_fixture_positional_banner_and_assertion():
    """按位 fixture：报告必须自曝"没做指纹比对"，且判定只认 recorded 断言。"""
    cass = _fixture("positional_stuck")
    cas = cassette_mod.Cassette(cass, check_env=False)
    assert cas.meta["playback_mode"] == "positional"
    assert cas.meta["derived_from"], "positional fixture 必须记 derived_from（§4.8）"
    rep = _replay(cass, name="fixture-pos", mode="positional", check_env=False)
    assert replay.POSITIONAL_BANNER in rep.text()
    assert "未做请求指纹比对" in rep.text()
    assert rep.expected_assertion == "turn_end:reason=stuck"
    assert rep.reproduced, rep.text()


def test_replay_transport_signature_matches_spec():
    """§6.6 直调形态：`ReplayTransport(dir)` + `create(kind=..., **kwargs)` 必须真的能用。

    用按位模式的 cassette 来验签名：指纹模式还要求调用方带上与录制同源的 tools schema
    （cassette 只存 `tools_sha256`，存不下整份 schema），直调时没有 tools 就必然匹配不上——
    那是**正确的**行为，不适合用来测"签名对不对"。
    """
    cass = _fixture("positional_stuck")
    cas = cassette_mod.Cassette(cass, check_env=False)
    rep = replay.ReplayReport(cas, "positional")
    tr = replay.ReplayTransport(cass, mode="positional", cassette=cas, report=rep,
                                check_env=False)
    req = cas.request_at(0)
    resp = tr.create(kind=req.get("kind") or "chat", model=req.get("model"),
                     messages=req.get("messages"), stream=True)
    chunks = list(resp)
    assert chunks, "按 §6.6 签名直调没有拿到任何响应分片"
    assert rep.matches, "按 §6.6 签名直调没有记下匹配（说明入参被静默丢掉）"
    assert replay.POSITIONAL_BANNER in rep.text()


def test_strict_fail_closed_when_diff_is_not_attributable():
    """§6.5 Step 3/4 的失败映射：归因说"归不了"，回放就必须失败。

    `explain_diff` 只描述差异，判不判失败是 transport 的责任；这段守卫一旦失效，
    strict 回放就会把"其实对不上"的现场当成通过。这里把两种"归不了"的结论直接灌进去，
    钉死映射关系（真实差异只在 params 上出现时不会走到这里，所以必须构造）。

    构造办法：录制的 params 里只有 `temperature`，现场多带一个 `stream`。
    canon 白名单不看 `stream`，于是 **loose 相等**（能进入 strict 分支），
    而 **strict 指纹不等**（raw params 比原始 dict）→ 必然调 explain_diff。
    """
    entries = [
        {"type": "llm_request", "request_index": 0, "kind": "chat", "model": "m",
         "params": {"temperature": 0.0}, "messages": [{"role": "user", "content": "甲"}],
         "stream": True},
        {"type": "llm_end", "request_index": 0, "kind": "chat", "finish_reason": "stop",
         "message": {"content": "乙"}, "usage": {}},
    ]
    cass_dir = _synth_cassette("strictfc", entries, playback_mode="fingerprint",
                               completeness={"llm": "full", "tools": "full"})
    cas = cassette_mod.Cassette(cass_dir, check_env=False)
    req = cas.request_at(0)

    def _drive(explanation):
        rep = replay.ReplayReport(cas, "fingerprint", strict=True)
        tr = replay.ReplayTransport(cass_dir, cassette=cas, report=rep, strict=True,
                                   check_env=False)
        orig = replay.explain_diff
        replay.explain_diff = lambda *a, **kw: explanation
        try:
            return tr.create(kind="chat", model="m", messages=req["messages"],
                             temperature=0.0, stream=True)
        finally:
            replay.explain_diff = orig

    # 先确认这条现场真的会走到 explain_diff（否则下面的断言是空的）
    rep0 = replay.ReplayReport(cas, "fingerprint", strict=True)
    tr0 = replay.ReplayTransport(cass_dir, cassette=cas, report=rep0, strict=True,
                                 check_env=False)
    tr0.create(kind="chat", model="m", messages=req["messages"],
               temperature=0.0, stream=True)
    assert rep0.strict_diffed >= 1, "构造的现场没有进入 strict 差异分支"

    ex_unclassified = replay.DiffExplanation()
    ex_unclassified.unclassified = "槽位 memory 不可复现：录制 1 处、现场 2 处"
    try:
        _drive(ex_unclassified)
        raise AssertionError("未归类差异没有让 strict 回放失败")
    except replay.UnclassifiedDiff:
        pass

    ex_reinject = replay.DiffExplanation()
    ex_reinject.unequal_reinject["memory"] = ("旧的记忆块", ("新的", "两段"))
    try:
        _drive(ex_reinject)
        raise AssertionError("reinject 槽位不可复现没有让 strict 回放失败")
    except replay.ReinjectNotReproducible as exc:
        assert "memory" in str(exc) and "不可复现" in str(exc), str(exc)


def test_committed_fixtures_are_regenerable_and_drift_is_caught():
    """入库 fixture 必须与当前代码同源，而且这个"同源"要被**验证**过。

    光有"老 cassette 还能播"证明不了今天的录制器还会录出同样的东西，所以这里守两件事：

    1. 重录一遍（落 tmp，不碰入库目录）与入库版本逐事件一致 → `--check` 是可信的闸门；
    2. 被篡改过的副本必须被它报出来 → 闸门不是恒真的摆设。

    `_regenerate.py` 是脚本、不是模块，所以用 importlib 载入；它会往 os.environ 里写
    TRACE_DIR/TRACE_ENABLED/TRACE_EPOCH_ISO（自检自身的录制落点），载入前后必须还原——
    否则后面的用例会莫名其妙地把 trace 写进 fixture 的目录里。
    """
    import importlib.util

    keys = ("TRACE_DIR", "TRACE_ENABLED", "TRACE_EPOCH_ISO")
    saved = {k: os.environ.get(k) for k in keys}
    spec = importlib.util.spec_from_file_location(
        "replay_fixture_regen",
        os.path.join(ROOT, "tests", "fixtures", "replay", "_regenerate.py"))
    regen = importlib.util.module_from_spec(spec)
    tamper = os.path.join(_CASSETTE_ROOT, "fixture-tamper")
    try:
        spec.loader.exec_module(regen)

        drift = regen.check()
        assert drift == [], "入库 fixture 与当前代码不同源（确认是预期改动后重录）：\n" + \
            "\n".join(drift)

        # 篡改副本：少一条事件。只改副本，入库 fixture 不动。
        shutil.rmtree(tamper, ignore_errors=True)
        shutil.copytree(_FIXTURE_ROOT, tamper)
        p = os.path.join(tamper, "fingerprint_tool_round", "cassette.jsonl")
        with open(p, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        with open(p, "w", encoding="utf-8") as f:
            f.writelines(lines[:-1])
        caught = regen.check(root=tamper)
        assert caught, "事件少了一条，--check 却没报漂移（闸门是恒真的）"
        assert any("fingerprint_tool_round" in c for c in caught), caught
    finally:
        shutil.rmtree(tamper, ignore_errors=True)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_fixture_is_self_describing():
    """fixture 的 meta 必须是自洽的：模式/完整度/断言面/检查结果都在。"""
    for name in ("fingerprint_tool_round", "positional_stuck"):
        cas = cassette_mod.Cassette(_fixture(name), check_env=False)
        meta = cas.meta
        assert meta["cassette_format"] == cassette_mod.CASSETTE_FORMAT
        assert meta["canon_version"] == trace.CANON_VERSION
        assert meta["playback_mode"] in cassette_mod.PLAYBACK_MODES
        assert meta["completeness"]["llm"] in ("full", "partial", "missing")
        assert meta["trace_epoch"]["injected_date_str"], meta["trace_epoch"]
        assert meta["replay_check"]["reproduced"] is True, meta["replay_check"]
        assert cas.expected_assertion() in cas.assert_ids(), (
            cas.expected_assertion(), cas.assert_ids())
        # 抽取时落盘的 checkpoint 必须能按 request_index 取回来
        store = cas.store()
        for req in cas.requests:
            assert store.keys_for_request(req["request_index"]), req["request_index"]


# ── 合成 cassette（不经过录制）────────────────────────────────────
def _synth_cassette(name, entries, *, playback_mode="fingerprint", derived_from=None,
                    expected_assertion=None, completeness=None):
    path = _fresh_dir(_CASSETTE_ROOT, f"synth-{name}")
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "cassette.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        for i, e in enumerate(entries):
            e = dict(e)
            e.setdefault("seq", i)
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    meta = {"cassette_format": cassette_mod.CASSETTE_FORMAT,
            "canon_version": trace.CANON_VERSION,
            "workspace_root": ROOT,
            "playback_mode": playback_mode,
            "completeness": completeness or {"llm": "full", "tools": "full"},
            "degraded_reasons": [], "headroom": {},
            "trace_epoch": trace.trace_epoch(),
            "derived_from": derived_from, "expected_assertion": expected_assertion}
    with open(os.path.join(path, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return path


_LAST_RUN = {}


def _run_all():
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc))
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if not failed:
        # 全绿才清场：不清的话每跑一次都在 tmp/ 下留几十个会话目录（实测积到 1300+ 个），
        # 之后 `tools.trace list` 的输出就没法看了。失败时保留现场。
        for root in (_TRACE_ROOT, _CASSETTE_ROOT):
            shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
