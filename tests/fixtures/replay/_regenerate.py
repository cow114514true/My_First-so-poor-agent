"""生成 tests/fixtures/replay/ 下入库的 cassette（§1 交付物 11 / §9 #11#23）。

用法：
    python tests/fixtures/replay/_regenerate.py           # 重新生成入库 fixture
    python tests/fixtures/replay/_regenerate.py --check   # 只校验，不写盘（漂移则退出码非 0）

为什么需要这个脚本而不是手工拷贝：入库 fixture 是**回归基线**——以后改 canon 规范、
改词表、改事件字段时，这些 cassette 必须仍能被加载并回放。手工拷来的 cassette
一旦与生成它的那段代码脱节，就只能靠"它现在还能跑"来背书，说不清是谁的契约。
脚本重放整条链路（录制 → extract_cassette → 内建自检回放），所以 fixture 永远
与当前代码同源。

`--check` 补的是另一半：光有"老 cassette 还能播"证明不了**今天的录制器还会录出同样的东西**。
它把 fixture 重录到 tmp 下，和入库版本比稳定投影（事件流去掉 ts/会话号这类天然不同的字段，
外加 meta 的契约字段），于是"代码改了但忘了重录 fixture"会当场变成退出码非 0。

日期用一个固定的时间锚（`TRACE_EPOCH_ISO`）钉住：不钉的话每次重录都带着"今天几号"，
差异到底是代码改了还是日期翻了就永远说不清，`--check` 也就无从谈起。

两个 fixture 覆盖两种 playback_mode：

- `fingerprint_tool_round/`：指纹模式。一段"工具轮 + 收尾正文"的正常历史，
  入库目的正是让指纹算法的改动**立刻**暴露为回放失败（指纹变了，匹配不上）。
- `positional_stuck/`：按位模式。thinking-only 被空 nudge 反复重试后 stuck 的历史。
  按位模式跳过请求指纹，只保证响应序列与目标断言可复现——这类历史（同一条 user
  问题被重试多次）用指纹去配会很脆，而它的价值在于"这个失败还能复现"。
"""
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
os.environ.setdefault("DS_KEY", "sk-selftest-not-a-real-key")
os.environ.setdefault("FILTERED_LOG", os.path.join(ROOT, "tmp", "_fixture_filtered.log"))
os.environ.setdefault("CACHE_LOG", os.path.join(ROOT, "tmp", "_fixture_cache.log"))
os.environ["TRACE_ENABLED"] = "1"
os.environ.setdefault("TRACE_EPOCH_ISO", "2026-10-14T14:29:10")
_TRACE_ROOT = os.path.join(ROOT, "tmp", "traces-fixtures")
os.environ["TRACE_DIR"] = _TRACE_ROOT

import loop_agent_core.cassette as cassette_mod  # noqa: E402
import loop_agent_core.checkpoint as cp  # noqa: E402
import loop_agent_core.llm as llm  # noqa: E402
import loop_agent_core.trace as trace  # noqa: E402
import loop_agent_v2 as la  # noqa: E402

FIXTURE_ROOT = os.path.join(ROOT, "tests", "fixtures", "replay")
DERIVED_FROM = "tests/fixtures/replay/_regenerate.py"


# ── 假后端（挂在网关底下，与 run_tests 里同一形状） ────────────────
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
TOOL_BATCH = [[_Chunk(_Delta(tool_calls=[_ToolCall(0, "c1", "get_date", "{}")])),
               _Chunk(_Delta(), finish_reason="tool_calls"),
               _Chunk(usage=True, no_choices=True)],
              [_Chunk(_Delta(content="今天是某天"), finish_reason="stop"),
               _Chunk(usage=True, no_choices=True)]]
THINKING_BATCH = [[_Chunk(_Delta(reasoning="我再想想")),
                   _Chunk(_Delta(), finish_reason="stop"),
                   _Chunk(usage=True, no_choices=True)]]


def _new_conv(sid):
    return {"messages": [{"role": "system", "content": "t"}], "size": None,
            "_archive": [], "_compact_state": {}, "_session_id": sid}


def _record(batches, question, name):
    run_dir = os.path.join(_TRACE_ROOT, f"{name}-{int(time.time() * 1000) % 1000000}")
    shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(run_dir, exist_ok=True)
    sid = os.path.basename(run_dir)
    cp.close_store()
    cp.reset_counters()
    conv = _new_conv(sid)
    llm.set_transport(_FakeTransport(batches))
    prev_tool = la.TOOL_CALL_MAP.get("get_date")
    la.TOOL_CALL_MAP["get_date"] = lambda **kw: _TOOL_TEXT
    w = trace.open_writer(sid, run_dir=run_dir,
                          manifest={"session_id": sid, "backend": "fake",
                                    "model": "deepseek-v4-flash"})
    try:
        out = la.chat(question, _conv=conv)
    finally:
        if prev_tool is not None:
            la.TOOL_CALL_MAP["get_date"] = prev_tool
        llm.reset()
        w.write_index()
        trace.close_writer(w)
        cp.close_store()
    return run_dir, out


def _build(name, batches, question, *, mode, expected, derived_from=None, out_root=None):
    run_dir, out = _record(batches, question, name)
    out_dir = os.path.join(out_root or FIXTURE_ROOT, name)
    shutil.rmtree(out_dir, ignore_errors=True)
    # extract_cassette 内部会立刻回放一次，复现不了 expected 就抛错、不落盘——
    # 所以这个脚本不会产出"看着像 fixture、其实复现不了"的东西。
    meta = cassette_mod.extract_cassette(
        run_dir, out_dir, playback_mode=mode, expected_assertion=expected,
        derived_from=derived_from, workspace_root=ROOT)
    print(f"[fixture] {name}: mode={mode} llm={meta['completeness']['llm']} "
          f"out={json.dumps(out, ensure_ascii=False)[:60]}")
    return out_dir, meta


# 两个 fixture 的唯一真源：生成与校验都读这张表，避免"校验的规格和生成的规格走岔"。
SPECS = [
    {"name": "fingerprint_tool_round", "batches": TOOL_BATCH, "question": "今天几号",
     "mode": "fingerprint", "expected": "turn_end:reason=answer"},
    {"name": "positional_stuck", "batches": THINKING_BATCH, "question": "想个办法",
     "mode": "positional", "expected": "turn_end:reason=stuck"},
]

# 天然每次不同的字段：只比"契约"部分，不比这些
_VOLATILE_EVENT_KEYS = ("ts", "seq", "run_id", "session")
_STABLE_META_KEYS = ("cassette_format", "canon_version", "playback_mode", "completeness",
                     "degraded_reasons", "expected_assertion", "assertion_ids",
                     "checkpoint_count", "derived_from")


def _read_events(d):
    with open(os.path.join(d, "cassette.jsonl"), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _project(events):
    out = []
    for e in events:
        out.append(json.dumps({k: v for k, v in e.items() if k not in _VOLATILE_EVENT_KEYS},
                              sort_keys=True, ensure_ascii=False))
    return out


def _diff_fixture(name, committed_dir, fresh_dir):
    """返回漂移描述（空 = 一致）。只报第一处差异：后面全是它的连锁反应。"""
    problems = []
    with open(os.path.join(committed_dir, "meta.json"), encoding="utf-8") as f:
        cm = json.load(f)
    with open(os.path.join(fresh_dir, "meta.json"), encoding="utf-8") as f:
        fm = json.load(f)
    for k in _STABLE_META_KEYS:
        if cm.get(k) != fm.get(k):
            problems.append(f"{name}: meta.{k} 变了\n      入库={cm.get(k)!r}\n      现在={fm.get(k)!r}")
    cl, fl = _project(_read_events(committed_dir)), _project(_read_events(fresh_dir))
    if len(cl) != len(fl):
        problems.append(f"{name}: 事件条数不同：入库={len(cl)} 现在={len(fl)}")
    for i, (a, b) in enumerate(zip(cl, fl)):
        if a != b:
            problems.append(f"{name}: 事件流第 {i + 1} 条起不一致\n      入库={a}\n      现在={b}")
            break
    return problems


def check(root=None):
    """重录一遍并和入库 fixture 对比；返回漂移列表（空 = 同源）。

    刻意重录到 tmp 下、**不碰**入库目录：校验不该有副作用，否则"跑一次 --check"
    和"跑一次生成"就没区别了，失败时也无从对比。
    """
    fixture_root = root or FIXTURE_ROOT
    tmp_root = os.path.join(ROOT, "tmp", "_fixture_recheck")
    shutil.rmtree(tmp_root, ignore_errors=True)
    os.makedirs(tmp_root, exist_ok=True)
    problems = []
    try:
        for spec in SPECS:
            fresh_dir, _ = _build(**spec, derived_from=DERIVED_FROM, out_root=tmp_root)
            problems += _diff_fixture(spec["name"],
                                      os.path.join(fixture_root, spec["name"]), fresh_dir)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    return problems


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--check" in argv:
        problems = check()
        for p in problems:
            print(f"[drift] {p}")
        if problems:
            print(f"\n入库 fixture 与当前代码不同源（{len(problems)} 处）；"
                  f"确认是预期改动后跑一次 `python {DERIVED_FROM}` 重录。")
            return 1
        print(f"[fixture] {len(SPECS)} 个入库 fixture 与当前代码同源")
        return 0
    os.makedirs(FIXTURE_ROOT, exist_ok=True)
    for spec in SPECS:
        _build(**spec, derived_from=DERIVED_FROM)
    print(f"[fixture] 写入 {FIXTURE_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
