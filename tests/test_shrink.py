"""L3 最小化（ddmin）的用例 —— 施工文档 §7 / §9 #29#30#31#37。

骨架照 `tests/test_migrate.py`：自研 mini 运行器，`except BaseException` 而不是
`except Exception`——被测代码用 `SystemExit` 表达"输入不对"，那是一条用例的失败，
不该掀翻整个套件。

真跑输入来自入库 fixture：`positional_stuck/`（单轮；三条响应都是必需的，ddmin 删不动）
与 `multiturn_stuck/`（多用户轮；真会整轮删掉）。**入库 fixture 不许改**，收缩产物一律
写到 `tmp/` 下。合成输入只用来打边界：失败换了另一个失败、超时、预算耗尽。
"""
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DS_KEY", "sk-selftest-not-a-real-key")
os.environ.setdefault("FILTERED_LOG", os.path.join(ROOT, "tmp", "_selftest_shrink_filtered.log"))
os.environ.setdefault("CACHE_LOG", os.path.join(ROOT, "tmp", "_selftest_shrink_cache.log"))
os.environ["TRACE_ENABLED"] = "1"
_SELFTEST = os.path.join(ROOT, "tmp", "shrink-selftest")
_TRACE_ROOT = os.path.join(_SELFTEST, "traces")
os.environ["TRACE_DIR"] = _TRACE_ROOT

import loop_agent_core.cassette as cassette_mod  # noqa: E402
from tools import shrink as shrink_mod  # noqa: E402

FIXTURE_ROOT = os.path.join(ROOT, "tests", "fixtures", "replay")
STUCK = "turn_end:reason=stuck"


def _clean():
    shutil.rmtree(_SELFTEST, ignore_errors=True)
    os.makedirs(_TRACE_ROOT, exist_ok=True)


def _fixture(name):
    return os.path.join(FIXTURE_ROOT, name)


def _stuck_cassette():
    return cassette_mod.Cassette(_fixture("positional_stuck"), check_env=False)


def _positional_copy(fixture, name, expected, keep_types_only=None):
    """把入库 fixture 摊成 trace 根后按位重切一份（可只留第一组响应，造"没复现"）。"""
    src = _fixture(fixture)
    work = os.path.join(_SELFTEST, name)
    os.makedirs(work, exist_ok=True)
    root, cas = shrink_mod.prepare_root(src, work)
    if keep_types_only is not None:
        keep = {i for i, e in enumerate(cas.events)
                if e.get("type") not in shrink_mod.DELETABLE_TYPES}
        keep |= set(keep_types_only)
        shrink_mod._write_trace(root, [e for i, e in enumerate(cas.events) if i in keep])
    out = os.path.join(work, "cass")
    cassette_mod.slice_cassette(
        root, out, playback_mode=cassette_mod.PLAYBACK_POSITIONAL,
        derived_from="tests/test_shrink.py", expected_assertion=expected,
        workspace_root=cas.meta.get("workspace_root"), force=True)
    return out


# ── 单元树与依赖投影（不花回放预算的纯逻辑） ────────────────────────
def test_units_never_cover_control_flow():
    """轮单元只覆盖可删载荷；控制流事件不在任何单元里（§16.3 固定项）。"""
    cas = _stuck_cassette()
    units, notes = shrink_mod.build_units(list(cas.events), STUCK)
    assert notes == [], notes
    turns = [u for u in units if u.kind == "turn"]
    assert len(turns) == 1, f"这份 fixture 只有一个用户轮，实际 {len(turns)}"
    turn = turns[0]
    assert turn.pinned and "失败" in turn.why and "首轮" in turn.why, turn.why
    for i in turn.indices:
        assert cas.events[i].get("type") in shrink_mod.DELETABLE_TYPES, \
            f"#{i} {cas.events[i].get('type')} 是控制流，不该进删除单元"
    assert len(turn.children) == 3, [c.name for c in turn.children]
    # 3 条响应全部可删、且互不重叠
    seen = set()
    for kid in turn.children:
        assert not (set(kid.indices) & seen), "响应包互相重叠"
        seen |= set(kid.indices)


def test_projection_rejects_orphan_tool_result():
    """依赖保持投影：孤立的结果/调用在**回放之前**就被挡下（§16.3）。"""
    events = [
        {"type": "tool_result", "call_id": "c9", "seq": 1},
    ]
    problems = shrink_mod.check_projection(events)
    assert problems and "没有对应的 tool_call" in problems[0], problems
    events = [
        {"type": "tool_call", "call_id": "c1", "seq": 1},
        {"type": "tool_result", "call_id": "c1", "seq": 2},
    ]
    assert shrink_mod.check_projection(events) == []
    cas = _stuck_cassette()
    assert shrink_mod.check_projection(list(cas.events)) == []


class _StubTester:
    """假的候选评估器：按脚本回状态，用来单测 ddmin 自己的行为。"""

    def __init__(self, status, keep_units=()):
        self.status = status
        self.keep_units = set(keep_units)
        self.calls = []

    def run(self, cand):
        self.calls.append(set(cand))
        return self.status, {}


def test_ddmin_keeps_only_necessary_units():
    """ddmin 纯逻辑：8 个单元里只有 #3 是必需的 → 收缩后只剩 #3。"""
    events = [{"type": "llm_chunk", "seq": i} for i in range(8)]
    units = [shrink_mod.Unit(name=f"u{i}", kind="bundle", indices=(i,))
             for i in range(8)]

    class _Needs3:
        def run(self, cand):
            return (shrink_mod.FAIL if 3 in cand else shrink_mod.PASS), {}

    reducer = shrink_mod.Reducer(events, _Needs3())
    reducer.ddmin(units)
    assert reducer.best == {3}, sorted(reducer.best)


def test_unresolved_is_treated_as_pass():
    """UNRESOLVED 当 PASS：候选被保守拒绝，删除不成立（§7.2 / delta debugging 原义）。"""
    events = [{"type": "llm_chunk", "seq": i} for i in range(2)]
    units = [shrink_mod.Unit(name="u0", kind="bundle", indices=(0,))]
    reducer = shrink_mod.Reducer(events, _StubTester(shrink_mod.UNRESOLVED))
    reducer.ddmin(units)
    assert reducer.best == {0, 1}, "UNRESOLVED 不该被当成删除成功"
    reducer2 = shrink_mod.Reducer(events, _StubTester(shrink_mod.FAIL))
    reducer2.ddmin(units)
    assert reducer2.best == {1}, "FAIL 才允许删掉单元"


# ── §9 #29：失败变成另一个失败 ──────────────────────────────────────
def test_shrink_anchor():
    """#29：候选把失败换成了**另一个**失败 → 判 PASS，并记下换成了谁。"""
    src = cassette_mod.Cassette(_fixture("fingerprint_tool_round"), check_env=False)
    other = src.expected_assertion()
    assert other and other != STUCK, f"这条用例需要一个与 {STUCK} 不同的真断言，收到 {other}"
    cand = _positional_copy("fingerprint_tool_round", "anchor", other)
    probe = shrink_mod.probe_cassette(cand, runs=1, timeout=600)
    assert probe.get("kind") != "timeout", probe
    status, info = shrink_mod.classify(probe, STUCK)
    assert status == shrink_mod.PASS, f"换了失败必须判 PASS，实际 {status}（{probe}）"
    assert info.get("substituted") == other, info
    # 反过来：真锚定它自己那条断言时，它就是 FAIL——否则上面的 PASS 毫无信息量
    status2, _ = shrink_mod.classify(probe, other)
    assert status2 == shrink_mod.FAIL, f"锚定 {other} 应当判 FAIL"

    # 端到端：从"锚定另一个断言"的现场出发，收缩器必须拒绝在源现场上开局
    try:
        shrink_mod.shrink_cassette(cand, os.path.join(_SELFTEST, "anchor_out"),
                                   expected_assertion=STUCK, max_replays=4, runs=1,
                                   timeout=600)
    except shrink_mod.ShrinkError as exc:
        assert "不能复现" in str(exc), exc
    else:
        raise AssertionError("源现场不复现目标断言时，收缩不该继续")


# ── §9 #30：预算耗尽 ────────────────────────────────────────────────
def test_shrink_converged_flag():
    """#30：预算耗尽时输出 converged=false + 当前最优候选。"""
    out = os.path.join(_SELFTEST, "budget", "cass")
    report = shrink_mod.shrink_cassette(_fixture("positional_stuck"), out,
                                        max_replays=1, runs=1, timeout=600, force=True)
    assert report["converged"] is False, report
    assert report["replays"] >= 1, report
    assert report["result"] == "1-minimal"
    assert report["substituted_assertion"] is None
    assert any("预算耗尽" in s for s in report["skipped_units"]), report["skipped_units"]
    # "当前最优候选"必须是一份真能跑的 cassette，而不是空壳
    assert os.path.isfile(os.path.join(report["cassette"], cassette_mod.CASSETTE_EVENTS))
    assert report["verify"]["status"] == shrink_mod.FAIL, report["verify"]
    for key in ("cassette", "units", "replays", "converged", "result",
                "expected_assertion", "response_count_before", "response_count_after",
                "calls_before", "calls_after", "skipped_units",
                "unconsumed", "critical_elements", "substituted_assertion"):
        assert key in report, f"报告缺 §7.3 字段 {key}"


# ── §9 #31：阶段 2 守卫 ─────────────────────────────────────────────
def test_shrink_guard():
    """#31：响应新增或重排 → 候选无效；响应减少 → 记入报告。"""
    before = ("a", "b", "c")
    added = shrink_mod.spine_report(before, ("a", "b", "c", "d"))
    assert added["valid"] is False, added
    reordered = shrink_mod.spine_report(before, ("b", "a", "c"))
    assert reordered["valid"] is False, reordered
    reduced = shrink_mod.spine_report(before, ("a", "c"))
    assert reduced["valid"] is True, reduced
    assert reduced["reduced"] == ["b"], reduced
    assert (reduced["response_count_before"], reduced["response_count_after"]) == (3, 2)
    # 指纹全同也要报得出差值：集合差会认为"删了一个还剩三个，什么都没少"
    same = shrink_mod.spine_report(("a", "a", "a", "a"), ("a", "a", "a"))
    assert same["reduced"] == ["a"], same
    assert same["response_count_after"] == 3, same

    # 真 fixture 上的响应脊：3 条（预算 3 次 thinking-only 重试，不再有第 4 次请求）
    cas = _stuck_cassette()
    spine = shrink_mod.positional_spine(list(cas.events))
    assert len(spine) == 3, spine
    # 收缩报告里带着守卫这一栏（阶段 2 已启用，无历史组时也要说明为什么）
    out = os.path.join(_SELFTEST, "guard", "cass")
    report = shrink_mod.shrink_cassette(_fixture("positional_stuck"), out,
                                        max_replays=40, runs=1, timeout=600, force=True)
    guard = report["guard"]
    assert guard["enabled"] is True and guard["stage2_hook"] == "history_group_units()"
    # 失败轮是首轮（q_idx=1），_find_groups 扫 messages[1:1]，拿不到历史组。
    assert guard["groups"] == 0 and guard["groups_live"] == 0, guard
    assert guard["groups_deleted"] == [], guard
    assert "无可用历史组" in guard["why"], guard["why"]
    assert guard["response_count_before"] == 3, guard
    # 守卫报告必须与这次收缩**如实对应**：它记的减少量 = 真实减少量，
    # 且减少的条数要和 `deleted_units` 里的响应单元数对得上。
    assert guard["response_count_after"] == 3 - len(
        [u for u in report["deleted_units"] if u.startswith("response ")]), report["deleted_units"]
    assert len(guard["reduced"]) == guard["response_count_before"] - guard["response_count_after"], guard
    assert guard["valid"] is True, guard
    # 三次 thinking-only 重试的响应内容指纹完全相同（实测全是 2313f7b88d64d166），
    # 所以差值必须按位置+内容一起对齐算；用集合差会得出"删了一个还剩两个，什么都没少"。
    assert len(set(spine)) == 1, spine
    assert shrink_mod.history_group_units(list(cas.events),
                                          expected_assertion=STUCK) == []


# ── §7.2 阶段 2：历史组 converter ──────────────────────────────────
def _prior_turn_events():
    """两轮：turn0 正常答完，turn1 卡住；turn1 的首个请求带着 turn0 的对话态。"""
    return [
        {"type": "turn_start", "turn": 0, "seq": 0},
        {"type": "llm_request", "request_index": 1, "seq": 1, "messages": [
            {"role": "system", "content": "s"}, {"role": "user", "content": "q1"}]},
        {"type": "llm_chunk", "request_index": 1, "seq": 2, "content": "a1"},
        {"type": "llm_end", "request_index": 1, "seq": 3, "finish_reason": "stop"},
        {"type": "turn_end", "reason": "answer", "seq": 4},
        {"type": "turn_start", "turn": 0, "seq": 5},
        {"type": "llm_request", "request_index": 2, "seq": 6, "messages": [
            {"role": "system", "content": "s"}, {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"}]},
        {"type": "llm_chunk", "request_index": 2, "seq": 7, "content": "boom"},
        {"type": "llm_end", "request_index": 2, "seq": 8, "finish_reason": "stop"},
        {"type": "notice", "level": "warn", "code": "stuck", "seq": 9},
        {"type": "turn_end", "reason": "stuck", "seq": 10},
    ]


def test_history_groups_empty_when_failing_turn_is_first():
    """首轮失败没有"之前的历史"：三个入库 fixture 的历史组都为空。"""
    for name in ("positional_stuck", "multiturn_stuck", "l2_file_ops"):
        cas = cassette_mod.Cassette(_fixture(name), check_env=False)
        groups = shrink_mod.history_group_units(list(cas.events),
                                                 expected_assertion=STUCK)
        assert groups == [], (name, [(g.name, g.indices) for g in groups])


def test_history_groups_map_prior_turn_to_events():
    """turn1 失败时，turn0 的对话态被切成历史组，且指回产生它的事件。

    助手消息组能定位到 ri=1 的响应包络（llm_request/chunk/end → 索引 1,2,3）；
    用户提问这类非事件产生的消息组必须 pin 住，绝不参与删除。
    """
    groups = shrink_mod.history_group_units(_prior_turn_events(),
                                            expected_assertion=STUCK)
    got = {g.name: (g.pinned, g.indices) for g in groups}
    assert got == {"history[1:2]": (True, ()),
                   "history[2:3]": (False, (1, 2, 3))}, got
    for g in groups:
        if g.pinned:
            assert "非事件产生" in g.why, g.why


def test_history_group_deletion_rejected_by_spine_guard():
    """阶段 2 守卫：删历史组若动了 response 脊（这里把 ri=1 整条删掉），候选作废。"""
    events = _prior_turn_events()
    groups = shrink_mod.history_group_units(events, expected_assertion=STUCK)
    group = [g for g in groups if not g.pinned][0]
    # 阶段 1 收敛后的脊 = 剩下的响应序列；删掉 ri=1 后候选脊变短，仍在子序列内
    spine = shrink_mod.positional_spine([events[i] for i in (6, 7, 8, 9, 10)])
    kept = [events[i] for i in range(len(events)) if i not in set(group.indices)]
    ok, note = shrink_mod.spine_guard(shrink_mod.positional_spine(kept), spine)
    assert ok is True, note
    # 反过来：往候选里塞进一个源序列没有的响应 → 不是子序列，守卫必须拒绝。
    # （只加 llm_chunk 不够：没有配对的 llm_request，positional_spine 根本不认它。）
    bogus = kept + [{"type": "llm_request", "request_index": 9, "seq": 99},
                    {"type": "llm_chunk", "request_index": 9, "seq": 100, "content": "x"}]
    ok2, note2 = shrink_mod.spine_guard(shrink_mod.positional_spine(bogus), spine)
    assert ok2 is False, note2


# ── §9 #37：一份 fixture 只带一个断言 ───────────────────────────────
def test_l3_one_assertion_per_fixture():
    """#37：一个 fixture 只带一个 expected_assertion；多失败导出多个 fixture。"""
    cas = _stuck_cassette()
    assert isinstance(cas.meta.get("expected_assertion"), str), cas.meta.get("expected_assertion")
    plans = shrink_mod.plan_fixtures(cas)
    assert len(plans) == 1 and plans[0]["assertion"] == STUCK, plans
    plans = shrink_mod.plan_fixtures(cas, all_assertions=True)
    ids = [p["assertion"] for p in plans]
    assert len(ids) == len(set(ids)) >= 3, ids
    assert STUCK in ids and "notice:warn:stuck" in ids, ids
    names = [p["name"] for p in plans]
    assert len(names) == len(set(names)), names
    for plan in plans:
        assert isinstance(plan["assertion"], str)

    # 端到端：换一个断言锚，导出的 fixture 只带那一个 expected_assertion
    other = "notice:warn:stuck"
    out = os.path.join(_SELFTEST, "one", shrink_mod._slug(other))
    shrink_mod.shrink_cassette(_fixture("positional_stuck"), out,
                               expected_assertion=other, max_replays=1, runs=1,
                               timeout=600, force=True)
    meta = json.load(open(os.path.join(out, cassette_mod.CASSETTE_META), encoding="utf-8"))
    assert meta["expected_assertion"] == other, meta["expected_assertion"]
    assert isinstance(meta["expected_assertion"], str)
    assert meta["playback_mode"] == cassette_mod.PLAYBACK_POSITIONAL
    assert "shrink" in (meta.get("derived_from") or ""), meta.get("derived_from")


# ── 确定性护栏 ──────────────────────────────────────────────────────
def test_shrink_determinism_guard():
    """同一候选两次断言签名不一致 → UNRESOLVED（护栏是 UNRESOLVED 的第二个来源）。"""
    probe = {"signatures": [[STUCK, "notice:warn:stuck"], ["notice:warn:stuck"]],
             "assertion_ids": [STUCK, "notice:warn:stuck"], "consumed": [0]}
    status, info = shrink_mod.classify(probe, STUCK)
    assert status == shrink_mod.UNRESOLVED, (status, info)
    assert info["kind"] == "nondeterministic", info
    # 一致时不许冤枉它
    ok = {"signatures": [[STUCK], [STUCK]], "assertion_ids": [STUCK], "consumed": [0]}
    assert shrink_mod.classify(ok, STUCK)[0] == shrink_mod.FAIL
    # 真回放确实是确定性的：同一份 cassette 连跑两次，签名一致
    cas = _stuck_cassette()
    rec = shrink_mod.probe_cassette(cas.dir, runs=2, timeout=600)
    sigs = [json.dumps(s, ensure_ascii=False, sort_keys=True) for s in rec["signatures"]]
    assert len(set(sigs)) == 1, rec["signatures"]


# ── 超时是独立状态 ──────────────────────────────────────────────────
def test_shrink_timeout_is_its_own_state():
    """超时不是"没复现"：它走 UNRESOLVED/kind=timeout，另一条路径。"""
    cas = _stuck_cassette()
    timed = shrink_mod.probe_cassette(cas.dir, runs=1, timeout=0.001)
    assert timed["status"] == shrink_mod.UNRESOLVED, timed
    assert timed["kind"] == "timeout", timed
    assert "超时" in (timed.get("detail") or ""), timed
    status, info = shrink_mod.classify(timed, STUCK)
    assert status == shrink_mod.UNRESOLVED and info["kind"] == "timeout"

    # 另一条路径：候选真的没复现 → PASS（不是 UNRESOLVED），且带得回原样错误
    units, _ = shrink_mod.build_units(list(cas.events), STUCK)
    first_response = set(units[0].children[0].indices)
    thin = _positional_copy("positional_stuck", "thin", STUCK, keep_types_only=first_response)
    probe = shrink_mod.probe_cassette(thin, runs=1, timeout=600)
    status2, info2 = shrink_mod.classify(probe, STUCK)
    assert status2 == shrink_mod.PASS, (status2, info2, probe)
    assert info2.get("kind") != "timeout"
    assert STUCK not in (probe.get("assertion_ids") or [])
    assert probe.get("error"), "响应不够时回放应当明确报错，而不是静默跑完"


# ── 真跑第一条：入库 fixture 收缩到底 ───────────────────────────────
def test_shrink_positional_stuck_end_to_end():
    """真跑：`positional_stuck` 收缩到 1-最小，导出按位 cassette 并复核。"""
    out = os.path.join(_SELFTEST, "e2e", "cass")
    report = shrink_mod.shrink_cassette(_fixture("positional_stuck"), out,
                                        max_replays=120, runs=1, timeout=600, force=True)
    assert report["converged"] is True, report
    assert report["verify"]["status"] == shrink_mod.FAIL, report["verify"]
    assert report["expected_assertion"] == STUCK
    assert report["calls_before"] == report["response_count_before"] == 3
    assert report["calls_after"] == report["response_count_after"] <= 3
    assert report["result"] == "1-minimal"
    assert report["deleted_events"] >= 0
    meta = json.load(open(os.path.join(out, cassette_mod.CASSETTE_META), encoding="utf-8"))
    assert meta["playback_mode"] == cassette_mod.PLAYBACK_POSITIONAL
    assert meta["expected_assertion"] == STUCK
    assert "shrink" in (meta.get("derived_from") or "")
    assert STUCK in (meta.get("assertion_ids") or [])
    # 三栏都要在，且都能读
    assert isinstance(report["unconsumed"], list)
    assert any("永不删" in c for c in report["critical_elements"]), report["critical_elements"]
    assert report["substituted_assertion"] is None
    assert len(report["critical_elements"]) >= 2, report["critical_elements"]
    print("      ↳ 回放 {r} 次，响应 {b}→{a}，事件删掉 {d} 条，收敛={c}".format(
        r=report["replays"], b=report["response_count_before"],
        a=report["response_count_after"], d=report["deleted_events"],
        c=report["converged"]))


# ── L3「真删成功」：多用户轮现场 ─────────────────────────────────────
def test_shrink_multiturn_real_turn_deletion():
    """真删：多用户轮现场被整轮删除，checkpoint B 组重算，报告与 meta 口径一致。"""
    cas = cassette_mod.Cassette(_fixture("multiturn_stuck"), check_env=False)
    units, _ = shrink_mod.build_units(list(cas.events), STUCK)
    turns = [u for u in units if u.kind == "turn"]
    assert len(turns) == 3, [u.name for u in turns]
    pinned = [u for u in turns if u.pinned]
    assert len(pinned) == 1 and "失败" in pinned[0].why, [u.why for u in turns]

    out = os.path.join(_SELFTEST, "multiturn", "cass")
    report = shrink_mod.shrink_cassette(_fixture("multiturn_stuck"), out,
                                        max_replays=200, runs=1, timeout=600, force=True)
    assert report["converged"] is True, report
    assert report["verify"]["status"] == shrink_mod.FAIL, report["verify"]
    assert report["response_count_after"] < report["response_count_before"], report
    deleted_turns = [u for u in report["deleted_units"] if u.startswith("turn=")]
    assert deleted_turns, report["deleted_units"]
    # 删的必须是不含失败断言的那两轮，绝不能是首轮
    assert pinned[0].name not in deleted_turns, deleted_turns
    # 删掉整轮后 checkpoint 的 B 组必须按活值重算，否则下一轮请求的快照是旧世界的
    assert report["checkpoint_b_patched"] > 0, report["checkpoint_b_patched"]

    meta = json.load(open(os.path.join(out, cassette_mod.CASSETTE_META), encoding="utf-8"))
    assert meta["expected_assertion"] == STUCK
    assert meta["playback_mode"] == cassette_mod.PLAYBACK_POSITIONAL
    assert STUCK in (meta.get("assertion_ids") or [])
    # 欠账 #6：meta 里真有 request_count，且与导出后的请求条数一致
    assert meta.get("request_count") == report["response_count_after"], (
        meta.get("request_count"), report["response_count_after"])
    # 欠账 #5：derived_from 记的回放次数必须等于报告里的 replays（含导出后复核那次）
    m = re.search(r"replays=(\d+)", meta.get("derived_from") or "")
    assert m, meta.get("derived_from")
    assert int(m.group(1)) == report["replays"], (meta["derived_from"], report["replays"])
    print("      ↳ 多轮现场：响应 {b}→{a}，删掉 {t}，B 组重算 {p} 份".format(
        b=report["response_count_before"], a=report["response_count_after"],
        t=deleted_turns, p=report["checkpoint_b_patched"]))


def test_shrink_multiturn_budget_stops_at_baseline():
    """预算只够基线那一次探测：一轮都不删，但仍必须给出可核对、能复现的报告。

    全轮回放（all_turns）下，结果里保留下来的每个请求都会被重放消费，所以这里
    `unconsumed` 应当为空——"非空 unconsumed" 是回放层的删除可见性属性，由
    test_replay.py::test_signature_bucket_deletion_shows_unconsumed 覆盖。"""
    out = os.path.join(_SELFTEST, "multiturn-budget", "cass")
    report = shrink_mod.shrink_cassette(_fixture("multiturn_stuck"), out,
                                        max_replays=1, runs=1, timeout=600, force=True)
    assert report["converged"] is False, report
    assert report["deleted_units"] == [], report["deleted_units"]
    assert report["response_count_after"] == report["response_count_before"], report
    # 就算没来得及最小化，导出后复核也必须仍能复现失败，否则这份报告没有意义
    assert report["verify"]["status"] == shrink_mod.FAIL, report["verify"]
    assert report["unconsumed"] == [], report["unconsumed"]


# ── 欠账 #3：CLI 接在入库真 fixture 上 ───────────────────────────────
def test_shrink_cli_on_real_fixture():
    """CLI 入口（`python -m tools.shrink run`）直接吃入库真 cassette，不是合成品。"""
    out = os.path.join(_SELFTEST, "cli", "cass")
    rc = shrink_mod.main(["run", "--cassette", _fixture("positional_stuck"),
                          "--out", out, "--max-replays", "120",
                          "--runs", "1", "--timeout", "600", "--force"])
    assert rc == 0, rc
    meta = json.load(open(os.path.join(out, cassette_mod.CASSETTE_META), encoding="utf-8"))
    assert meta["expected_assertion"] == STUCK, meta["expected_assertion"]
    assert meta["playback_mode"] == cassette_mod.PLAYBACK_POSITIONAL
    assert "shrink" in (meta.get("derived_from") or ""), meta.get("derived_from")


def _run_all():
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = []
    for name, fn in tests:
        _clean()
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 —— SystemExit 也是一条用例的失败
            if isinstance(exc, (KeyboardInterrupt, GeneratorExit)):
                raise
            failed.append(name)
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"  ok   {name}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        return 1
    _clean()
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
