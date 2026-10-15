"""旧会话迁移（§5）的行为。Run: python tests/test_migrate.py

这组用例守的是"迁移器说的话算不算数"：

1. **矩阵的每一格都是数出来的，不是估出来的**。§5.2 的六格里，`missing/*` 那一行
   承载了全部旧语料，而它左右两格分的是"参数还在不在"——这一栏一旦算错，
   "可回放比例"这个天花板就整个站不住。
2. **分母是用户轮**。旧会话的第一条消息是 system prompt；把它自己算作一轮，
   每份会话就凭空多一轮（实测 108 份会报出 494 轮，而真实 user 消息只有 417 条）。
   拿一个虚报的分母去谈比例，得到的结论是假的。
3. **降级必须写在脸上**。旧会话没有请求体、没有 limits，抽出来的 cassette 必然
   `llm: missing`；回放器必须据此拒绝，而不是静默 stub 出一段"复现"。
4. **已经不存在的工具名也要能落进 trace**。实测旧语料里有 `browse_web`——它不在
   今天的 `TOOL_CLASS` 里，而 `toolclass` 的查询函数对未登记名直接抛异常。
   整份会话因为"调用过一个现在没有的工具"而迁移失败，是丢掉了最该报出来的那条结论。
"""
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DS_KEY", "sk-selftest-not-a-real-key")
os.environ.setdefault("FILTERED_LOG", os.path.join(ROOT, "tmp", "_selftest_migrate_filtered.log"))
os.environ.setdefault("CACHE_LOG", os.path.join(ROOT, "tmp", "_selftest_migrate_cache.log"))

import loop_agent_core.cassette as cassette_mod  # noqa: E402
import loop_agent_core.replay as replay  # noqa: E402
from tools import trace_migrate as mig  # noqa: E402

_SELFTEST = os.path.join(ROOT, "tmp", "migrate-selftest")
_CONV = sorted(
    os.path.join(ROOT, "conversations", n)
    for n in os.listdir(os.path.join(ROOT, "conversations"))
    if mig.is_conversation_file(n)
)
_SCRATCH = os.path.join(_SELFTEST, "conversations")


def _clean():
    shutil.rmtree(_SELFTEST, ignore_errors=True)


def _trimmed_session(name: str, *, turns: int = 2) -> str:
    """从真实语料里截 n 个用户轮写成一份会话文件。

    用真实字节而不是手编 fixture：迁移器的输入是**别人写的历史**，手编的
    "旧格式"永远比真实语料干净，会正好绕开键缺失、工具消失这类真问题。

    源会话要**挑**够长的：语料里最短的一份只有 1 个 user 轮，拿它当模板会让
    "两轮会话"的用例静默退化成单轮（然后断言失败得莫名其妙）。
    """
    os.makedirs(_SCRATCH, exist_ok=True)
    for src_path in _CONV:
        src = json.load(open(src_path, encoding="utf-8"))
        msgs = [m for m in src if isinstance(m, dict)]
        if sum(1 for m in msgs if m.get("role") == "user") < turns:
            continue
        kept, seen = [], 0
        for m in msgs:
            kept.append(m)
            if m.get("role") == "user":
                seen += 1
                if seen >= turns:
                    break
        path = os.path.join(_SCRATCH, f"{name}.json")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(kept, f, ensure_ascii=False)
        return path
    raise AssertionError(f"语料里没有 >= {turns} 个用户轮的会话")


# ── §5.1 输入过滤 ───────────────────────────────────────────────
def test_filter_accepts_only_chat_sessions():
    """只收 `chat_*.json`，且判定与 tui 逐名一致（Bug 31 咬过"自抄一份"的写法）。"""
    import tui

    names = ["chat_1.json", "chat_20260101_000000.json",
             "chat_20260101_000000.json.archive.json",
             "chat_20260101_000000.json.bak", "chat_probe.autosave.json",
             "notes.json", "chat_.json"]
    for name in names:
        assert mig.is_conversation_file(name) == tui.is_conversation_file(name), name
    assert mig.is_conversation_file("chat_20260101_000000.json")


def test_autosave_is_interrupted_and_opt_in():
    """`.autosave*.json` 是中断现场：默认不迁，显式要求时才收，且标 interrupted。"""
    assert mig.is_autosave("chat_1.autosave.json")
    assert mig.is_autosave("chat_1.autosave.2.json")
    assert not mig.is_autosave("chat_1.json")
    os.makedirs(_SCRATCH, exist_ok=True)
    auto = os.path.join(_SCRATCH, "chat_probe.autosave.json")
    open(auto, "w", encoding="utf-8").write("[]")
    try:
        # 与真实会话混着给：默认只回真实那份（autosave 是中断现场，要显式要求）。
        default = mig.expand_inputs([_CONV[0], auto])
        assert [p for p, _ in default] == [os.path.abspath(_CONV[0])], default
        got = dict((os.path.abspath(p), i) for p, i in
                   mig.expand_inputs([_CONV[0], auto], include_autosave=True))
        assert got.get(os.path.abspath(auto)) is True, got
        assert got.get(os.path.abspath(_CONV[0])) is False, got
    finally:
        os.remove(auto)


def test_expand_inputs_dedupes_and_rejects_empty():
    """同一份输入被匹配两次只迁一次；一个文件都没匹配到要明确失败。"""
    got = mig.expand_inputs([_CONV[0], _CONV[0]])
    assert len(got) == 1, got
    assert [p for p, _ in got] == [os.path.abspath(_CONV[0])]
    # 通配符打错 → 立刻失败。放过去就会产出一张全 0 的矩阵，看上去像
    # "迁移成功，只是旧语料确实不可回放"，把打错字伪装成一个结论。
    for bad in ([], [os.path.join(_SCRATCH, "no_such_chat_*.json")]):
        try:
            mig.expand_inputs(bad)
        except SystemExit as exc:
            assert "没有匹配" in str(exc), exc
        else:
            raise AssertionError(f"空输入集必须报错：{bad}")


# ── §5.2 可回放比例矩阵 ─────────────────────────────────────────
def _cell(body: str, args: str) -> str:
    """矩阵键是 `'<body>/<args>'` 字符串（能进 JSON），标签表用元组做键。"""
    return f"{body}/{args}"


def test_migration_matrix():
    """108 份真实会话全迁不崩；矩阵每格计数 + 降级工具归因（验收 #27）。"""
    _clean()
    out = os.path.join(_SELFTEST, "traces")
    rc = mig.main(["migrate", os.path.join(ROOT, "conversations", "chat_*.json"),
                   "--out", out, "--quiet"])
    assert rc == 0, rc
    summary = mig.load_index(out)
    assert summary["n_sessions"] == len(_CONV) >= 100, summary["n_sessions"]
    assert summary["n_sessions_failed"] == 0, summary["sessions"]
    assert summary["units"] == "turn"

    # 分母：矩阵计数之和 == 总轮数 == 逐份 n_turns 之和。
    total = sum(summary["matrix"].values())
    assert total == summary["n_turns"], (total, summary["n_turns"])
    assert total == sum(s["n_turns"] for s in summary["sessions"])

    # 分母 == 真实 user 消息数（§5.2 的单位是用户轮，不是 assistant 条数）。
    real_turns = 0
    for path in _CONV:
        real_turns += sum(1 for m in json.load(open(path, encoding="utf-8"))
                          if isinstance(m, dict) and m.get("role") == "user")
    assert summary["n_turns"] == real_turns, (summary["n_turns"], real_turns)

    # 每格都能贴上中文结论（贴不上的格子是个没有解释的数字）。
    for key in summary["matrix"]:
        assert key in summary["cell_labels"], key
        assert summary["cell_labels"][key] in mig.CELL_LABEL.values(), key

    # 旧语料一条 finish_reason 都没有 → body 轴必然全 missing，`ok/*` 两格必须空着。
    for key, n in summary["matrix"].items():
        body, args = key.split("/")
        assert body in mig.BODY_STATES and args in mig.ARGS_STATES, key
        if body == mig.BODY_OK:
            assert n == 0, f"旧语料不该有可复现的 body：{key}={n}"

    # 降级工具归因：args 掉档的轮必须能指名道姓说出是哪个工具干的。
    degraded = summary.get("degraded_tools") or {}
    assert "browse_web" in degraded, degraded
    # 同一个工具的两面，不能一边有一边没有。
    assert "browse_web" in (summary.get("unregistered_tools") or {}), summary


def test_matrix_labels_every_cell():
    """六格各有中文结论，且键是 (body, args) 元组——矩阵字符串键由它派生。"""
    assert set(mig.CELL_LABEL) == {(b, a) for b in mig.BODY_STATES
                                   for a in mig.ARGS_STATES}
    for key, label in mig.CELL_LABEL.items():
        assert label.strip(), key


def test_per_turn_details_agree_with_matrix():
    """矩阵是 per_turn 的汇总——两者不一致时，报告里的每个结论都无据可查。"""
    _clean()
    out = os.path.join(_SELFTEST, "labels")
    path = _trimmed_session("chat_probe_turns")
    assert mig.main(["migrate", path, "--out", out, "--quiet"]) == 0
    rep = json.load(open(os.path.join(out, mig.session_id_for(path),
                                      "migration_report.json"), encoding="utf-8"))
    assert rep["units"] == "turn"
    assert len(rep["per_turn"]) == rep["n_turns"] == 2
    tally = {}
    for row in rep["per_turn"]:
        key = _cell(row["body"], row["args"])
        assert key in rep["matrix"], (key, rep["matrix"])
        tally[key] = tally.get(key, 0) + 1
    assert tally == {k: v for k, v in rep["matrix"].items() if v}, (tally, rep["matrix"])
    assert [r["turn"] for r in rep["per_turn"]] == [1, 2]


def test_split_turns_does_not_count_system_prompt_as_a_turn():
    """前导 system 归进第 1 轮，不单开一轮；索引连续无空洞。"""
    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"}]
    turns = mig.split_turns(msgs)
    assert [t["index"] for t in turns] == [1, 2], turns
    assert [len(t["messages"]) for t in turns] == [3, 2]
    assert mig.split_turns([{"role": "user", "content": "only"}])[0]["index"] == 1


# ── §5.3 迁移报告 / 降级 cassette ───────────────────────────────
def test_migrated_cassette_is_degraded_with_reasons():
    """抽出的 cassette 必须 `llm: missing`，且**降级原因写在 meta 里**。"""
    _clean()
    out = os.path.join(_SELFTEST, "cas")
    path = _trimmed_session("chat_probe_cas")
    assert mig.main(["migrate", path, "--out", out, "--quiet"]) == 0
    sid = mig.session_id_for(path)
    cas_dir = os.path.join(out, sid, "cassette")
    meta = json.load(open(os.path.join(cas_dir, "meta.json"), encoding="utf-8"))
    assert meta["completeness"]["llm"] == "missing", meta["completeness"]
    assert meta.get("migrated") is True, meta
    assert meta.get("source_file"), meta
    reasons = " ".join(meta["degraded_reasons"])
    assert "body_missing" in reasons, meta["degraded_reasons"]
    assert "limits_missing" in reasons, meta["degraded_reasons"]
    # tools 轴是"缺不缺东西"，不是"降级程度"：参数全是原文，就不该报 degraded。
    assert meta["completeness"]["tools"] in ("full", "partial"), meta["completeness"]

    # 旧会话没有 limits 记录 → tool_result 的截断**无法归因**，必须明写成字段，
    # 而不是留个今天算得出来的数字让人以为它可信。
    cas = cassette_mod.Cassette(cas_dir)
    ev = [e for e in cas.events if e.get("type") == "tool_result"]
    assert ev, "这份会话的 trace 里应该落了工具结果"
    for e in ev:
        assert e.get("truncation_unknown") is True, e
        assert e.get("truncation") is None, e


def test_migration_degraded_rejected():
    """`llm: missing` 的 cassette 进 LLM 回放路径必须抛错（验收 #28）。

    这条是迁移层的守门员：旧语料抽出来的东西**本来就不能回放**，
    挡不住它，整套回放的可信度就没了——静默 stub 出来的"复现"比不复现更坏。
    """
    _clean()
    out = os.path.join(_SELFTEST, "reject")
    path = _trimmed_session("chat_probe_reject")
    assert mig.main(["migrate", path, "--out", out, "--quiet"]) == 0
    cas_dir = os.path.join(out, mig.session_id_for(path), "cassette")

    try:
        cassette_mod.Cassette(cas_dir, require_llm=True)
    except cassette_mod.CassetteIncomplete:
        pass
    else:
        raise AssertionError("缺 LLM 记录的 cassette 必须在加载时就拒绝")

    try:
        replay.run_replay(cas_dir, question="随便", run_dir=None)
    except cassette_mod.CassetteIncomplete:
        pass
    else:
        raise AssertionError("回放必须拒绝 llm=missing 的 cassette")

    # 但 cassette 自身要能打开（语料仍可查询），只是不能拿去回放。
    cas = cassette_mod.Cassette(cas_dir)
    assert cas.meta["completeness"]["llm"] == "missing"
    assert cas.tool_calls(), "迁移出来的调用序列应该仍然可查"


def test_migration_report_is_json_round_trip():
    """报告要能原样读回来——矩阵结论是人要贴进别处的，序列化不该丢东西。"""
    _clean()
    out = os.path.join(_SELFTEST, "rt")
    path = _trimmed_session("chat_probe_rt")
    assert mig.main(["migrate", path, "--out", out, "--quiet"]) == 0
    sid = mig.session_id_for(path)
    raw = open(os.path.join(out, sid, "migration_report.json"), encoding="utf-8").read()
    rep = json.loads(raw)
    assert rep["session"] == sid
    assert rep["units"] == "turn"
    # 矩阵只记非零格（零格在报告里没有信息），但每个键都必须是已知的六格之一。
    for key in rep["matrix"]:
        assert key in {_cell(b, a) for b in mig.BODY_STATES for a in mig.ARGS_STATES}, key
    assert isinstance(rep["degraded_tools"], dict)
    # 这份会话必须真调过工具：调 0 个工具的会话会让"参数还在不在"这一轴无从检验，
    # 整条用例就退化成只跑了一遍 JSON 序列化。
    assert rep["n_tool_calls"] > 0, rep
    assert any(rep["matrix"].values()), rep
    # 逐轮那一栏是**列表**（"第 3 轮那个 write_file 参数丢了"才可行动），
    # 报告顶部那一栏才是 `工具名 → 次数` 的字典。
    for row in rep["per_turn"]:
        assert row["body"] in mig.BODY_STATES and row["args"] in mig.ARGS_STATES
        assert isinstance(row["degraded_tools"], list), row
        assert isinstance(row["n_tool_calls"], int), row


def test_duplicate_call_ids_are_disambiguated():
    """旧语料里同一个 call_id 会被复用，迁移必须消歧——否则 cassette 会把调用挤掉。

    实测全量：1107 次调用只有 971 个 key 落进 cassette，**136 次凭空消失**，
    原因是 `chat_20260803_234135` 那一轮 6 次 `read_file` 里 3 次的 id 就是字面量
    `xml0`。这是真实历史（录制侧当时拿到的 id 就是坏的），改不了，只能消歧。
    """
    msgs = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "xml0", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
            {"id": "xml0", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "b.py"}'}},
            {"id": "good1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "c.py"}'}},
        ]},
        {"role": "tool", "tool_call_id": "xml0", "content": "R1"},
        {"role": "tool", "tool_call_id": "good1", "content": "R3"},
    ]
    id_map = mig._stable_call_ids(msgs)
    assert len(set(id_map.values())) == 3, id_map  # 三个位置三个不同的 id
    assert list(id_map.values())[2] == "good1", id_map  # 本来就唯一的原样保留
    # 撞车的两个：一个保留原名，一个加后缀
    dup = [v for v in id_map.values() if v.startswith("xml0")]
    assert dup == ["xml0", "xml0#2"], id_map

    ev = mig.build_events("dup", msgs, {"trace_epoch": {}})
    calls = [e for e in ev if e["type"] == "tool_call"]
    assert [e["call_id"] for e in calls] == ["xml0", "xml0#2", "good1"]
    assert [e.get("call_id_disambiguated") for e in calls] == [False, True, False]
    assert any(e.get("code") == "migration_dup_call_id" for e in ev), \
        "消歧了就必须留下证人：这条 notice 是那份丢失的唯一记录"
    # 结果按"首次出现"派发——历史本来就分不清这两条结果谁是谁，不假装分得清。
    res = [e for e in ev if e["type"] == "tool_result"]
    assert [e["call_id"] for e in res] == ["xml0", "xml0#2", "good1"], res
    assert res[0]["result_full"] == res[1]["result_full"] == "R1", res


def test_zero_call_session_reports_full_not_missing():
    """没调用过工具的会话，tools 轴必须是 `full`——"没记到"不等于"结果丢了"。

    `cassette.completeness()` 原先把 tools 轴写成 `"full" if reqs else "missing"`：
    旧会话迁移出来的 trace 一条 `llm_request` 都没有（那时根本不写这个事件），
    于是**每一份**快照的 tools 轴都被报成 missing。实测 108 份里有 7 份是
    `system/user/assistant` 的空会话——它们从来没调用过工具，报 missing 会让人
    以为"工具面也丢了"。谁回答"有没有 LLM 面"是 llm 轴的事。
    """
    from loop_agent_core import cassette as cas_mod
    comp = cas_mod.completeness([
        {"type": "run_start"}, {"type": "notice", "code": "x"},
    ])
    assert comp["llm"] == "missing", comp
    assert comp["tools"] == "full", comp
    # 有调用、有结果 → full；有调用、没结果 → partial。这两格不能被上面那条改坏。
    with_call = cas_mod.completeness([
        {"type": "tool_call", "call_id": "a"}, {"type": "tool_result", "call_id": "a"},
    ])
    assert with_call["tools"] == "full", with_call
    missing_result = cas_mod.completeness([{"type": "tool_call", "call_id": "a"}])
    assert missing_result["tools"] == "partial", missing_result


def test_migration_report_tools_axis_agrees_with_cassette():
    """迁移报告与 `Cassette` 对 tools 轴必须给出同一个答案。

    两处口径曾经不一致：报告按"用户轮内的 tool 消息"数结果，而 `Cassette` 按全局
    call_id 认领。历史里 assistant 的调用与它的结果**可能跨过下一轮的 user 消息**
    （实测 `chat_20260808_173056` 第 3 轮的头一条结果属于第 2 轮的调用），于是报告说
    partial、cassette 说 full——一个查无实据的降级。全量曾出现 69 份这种不一致。
    """
    from loop_agent_core import trace as trace_mod, cassette as cas_mod
    src = _trimmed_session("agree", turns=2)
    rep = mig.migrate_one(src, out_root=_SELFTEST)
    assert rep.get("cassette_dir"), rep.get("cassette_error") or "没抽出 cassette"
    cas = cas_mod.Cassette(rep["cassette_dir"])
    events = trace_mod.read_events(os.path.join(_SELFTEST, rep["session"]))
    assert cas.completeness["tools"] == cas_mod.completeness(events)["tools"]
    assert rep["cassette_completeness"]["tools"] == cas.completeness["tools"], rep


def test_late_tool_result_belongs_to_its_own_turn():
    """结果落在下一轮里，也要算回发起它的那一轮。

    手写复现 `chat_20260808_173056` 的形状：第 2 轮的 assistant 发了 2 条调用，但
    第 2 条结果排在了下一条 user 消息之后。按轮切分时它落在第 3 轮，逐轮数会把
    第 2 轮误判成"结果丢了一条"。
    """
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "A", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
            {"id": "B", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "b.py"}'}},
        ]},
        {"role": "tool", "tool_call_id": "A", "content": "RA"},
        {"role": "user", "content": "u2"},
        {"role": "tool", "tool_call_id": "B", "content": "RB"},  # 迟到的结果
        {"role": "assistant", "content": "答"},
    ]
    result_ids = mig._result_ids_for(msgs)
    assert result_ids == {"A", "B"}, result_ids
    turns = mig.split_turns(msgs)
    assert len(turns) == 2, [t["index"] for t in turns]
    cls = mig.classify_turn(turns[0], result_ids)
    assert cls["n_tool_calls"] == 2, cls
    # 只看本轮 messages 会数出 1，从而误报 partial
    assert cls["n_results"] == 2, cls



# ── 运行器 ──────────────────────────────────────────────────────
def _run_all():
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = []
    for name, fn in tests:
        try:
            _clean()
            fn()
            print(f"  ok   {name}")
        except BaseException as exc:  # noqa: BLE001
            # `BaseException` 而不是 `Exception`：被测代码用 `SystemExit` 表达
            # "输入不对"，那是**一条用例的失败**，不该掀翻整个套件、让后面的
            # 用例一条都不跑（实测就是这么丢掉 8 个用例的结果的）。
            if isinstance(exc, (KeyboardInterrupt, GeneratorExit)):
                raise
            failed.append((name, exc))
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if not failed:
        _clean()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
