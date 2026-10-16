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

入库 fixture 覆盖两种 playback_mode 和两级回放策略：

- `fingerprint_tool_round/`：指纹模式。一段"工具轮 + 收尾正文"的正常历史，
  入库目的正是让指纹算法的改动**立刻**暴露为回放失败（指纹变了，匹配不上）。
- `positional_stuck/`：按位模式。thinking-only 被空 nudge 反复重试后 stuck 的历史。
  按位模式跳过请求指纹，只保证响应序列与目标断言可复现——这类历史（同一条 user
  问题被重试多次）用指纹去配会很脆，而它的价值在于"这个失败还能复现"。
- `l2_file_ops/`：指纹模式 + L2 overlay。**真**录一段含文件操作的工具轮：
  `write_file`（B 类，在 overlay 里真写）→ `read_file` 同一路径（A 类跨类重读，
  必须读到 overlay 里刚写进去的那份）→ `search_code`（递归扫描类，重写目标为
  overlay 根）。三类重写一次全覆盖。

  为什么这第三个 fixture 值得入库：前两个都是"读录制结果"的单类场景，L2 的
  `--tool-policy class`（B 类真执行、A 类跨类重读、路径三段式映射）此前**零真实
  轨迹覆盖**——合成用例只能证明"我按我以为的方式拼了参数"，证明不了"真录制出来
  的轨迹在真回放下仍然对得上"。L3 要收缩失败现场，而失败现场必须是真录的：手搓
  的现场收出来的最小例，说不清它对应哪个真实 bug。

  断言仍挂在机制上（`turn_end:reason=answer`，与 `fingerprint_tool_round` 同码）：
  §8.4 要的是"断言与正文无关"，不是"每个 fixture 必须发明一个新断言 id"。L2 的
  机制事实（重写发生过、真实 WORK_DIR 一字未动、跨类重读命中、overlay 树结构）
  由 `tests/test_replay.py` 里那条用例逐条断言——那里的断言比一个字符串更具体，
  而且它读的是 trace 里的 `tool_call.overlay` 字段与 `ReplayReport`，不是靠猜。

- `multiturn_stuck/`：按位模式，**多用户轮**。首轮 thinking-only 被空 nudge 反复重试后
  stuck，后两轮正常收尾。它是给 L3 准备的"真删得动"的现场：单轮的 `positional_stuck`
  三条响应都是必需的，ddmin 一个也删不掉；多用户轮才会整轮被删，进而真的触发
  checkpoint B 组重算（`repatch_b_group`）。两条一起构成 L3 的回归面：
  一条证明"不乱删"，一条证明"真删成功"。
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
# B 类工具（write_file）真执行前要过确认门。录制 fixture 的是脚本、不是人，
# 走 ask 只会永远等下去；§9 的 L2 用例证明的是"真执行被限制在 overlay 里"，
# 不是"确认门被绕过了"，所以这里明确放行。
#
# 注意：这里**不能**用 `setdefault`。`tests/test_replay.py` 会 importlib 载入本模块
# 并在同一个进程里跑，那时 `AGENT_CONFIRM_POLICY` 往往已被别的用例设成 ask/deny，
# `setdefault` 就成了空操作；而且 `loop_agent_core.confirm` 的 `_POLICY` 是
# **模块级**读取的，本模块可能只是被后来的 import 顺手牵进来。实测后果：重录时
# write_file 拿到 `[Denied by user]`，录出来的 fixture 与入库版本漂移，
# `test_committed_fixtures_are_regenerable_and_drift_is_caught` 红。
# 所以两处都要落实：环境变量强制覆盖 + 真正生效的 `confirm.set_policy`（见 `_record`）。
os.environ["AGENT_CONFIRM_POLICY"] = "allow_all"
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
# 多用户轮：前三次请求吃掉 THINKING_BATCH（首轮的 3 次空 nudge 重试 → stuck），
# 之后两次落到这里正常收尾。`_FakeTransport` 对超出的调用复用最后一组。
NORMAL_ANSWER_BATCH = [[_Chunk(_Delta(content="好的，记下了"), finish_reason="stop"),
                        _Chunk(usage=True, no_choices=True)]]
MULTITURN_BATCH = [THINKING_BATCH[0], THINKING_BATCH[0], THINKING_BATCH[0],
                   NORMAL_ANSWER_BATCH[0]]


# ── L2 剧本：三类重写各一次 ──────────────────────────────────────
# 路径一律用**相对 WORK_DIR 的逻辑路径**：录制侧 runner 不做重写（那时没有回放
# 会话），回放侧才把它重写成 `tmp/overlay/<sid>/...`。所以录下来的入参天然可移植，
# 而"重写真的发生了"由 L2 用例去 trace 里读，不靠 fixture 自己声明。
L2_WORK_REL = "tmp/_l2fix_work"
L2_FILE_REL = L2_WORK_REL + "/out.txt"
L2_MARKER = "l2fileops-marker-9f3a"          # 只在 overlay 里那份存在
L2_CONTENT = f"first line\n{L2_MARKER}\nthird line\n"


def _l2_tool_batch():
    """写 → 读同一路径 → 递归扫描。三个工具轮，各自一轮收尾。

    **必须分成三轮**：`_replay_plan` 对同一轮里的所有调用一次性做完决定，而 B 类的
    真执行发生在决定之后；同一个 assistant 消息里的 `write_file` + `read_file`
    决定时 `overlay.written` 还空着，跨类重读根本不会命中（§8.2.3 的检查会形同虚设）。
    """
    write_args = json.dumps({"path": L2_FILE_REL, "content": L2_CONTENT,
                             "purpose": "L2 fixture：在 overlay 里真写一份"}, ensure_ascii=False)
    read_args = json.dumps({"path": L2_FILE_REL}, ensure_ascii=False)
    search_args = json.dumps({"pattern": L2_MARKER, "path": L2_WORK_REL,
                              "glob": "*.txt", "output_mode": "content"}, ensure_ascii=False)
    done = [_Chunk(_Delta(content="三类重写都走过了"), finish_reason="stop"),
            _Chunk(usage=True, no_choices=True)]
    return [
        [_Chunk(_Delta(tool_calls=[_ToolCall(0, "l2w", "write_file", write_args)])),
         _Chunk(_Delta(), finish_reason="tool_calls"),
         _Chunk(usage=True, no_choices=True)],
        [_Chunk(_Delta(tool_calls=[_ToolCall(0, "l2r", "read_file", read_args)])),
         _Chunk(_Delta(), finish_reason="tool_calls"),
         _Chunk(usage=True, no_choices=True)],
        [_Chunk(_Delta(tool_calls=[_ToolCall(0, "l2s", "search_code", search_args)])),
         _Chunk(_Delta(), finish_reason="tool_calls"),
         _Chunk(usage=True, no_choices=True)],
        done,
    ]


def _new_conv(sid):
    return {"messages": [{"role": "system", "content": "t"}], "size": None,
            "_archive": [], "_compact_state": {}, "_session_id": sid}


def _record(batches, question, name, seed=None):
    # 确认门：本模块被 importlib 载入时会跟别的用例共用一个进程，`confirm._POLICY`
    # 可能是那边的 ask/deny。**必须显式 set_policy**（环境变量对已 import 的 confirm
    # 无效），并在 finally 里还原，免得污染同进程后面的用例。
    from loop_agent_core import confirm
    prev_policy = confirm.get_policy()
    confirm.set_policy("allow_all")
    # 薄壳的 `_session_state`（`_compact` / `_archive`）是**进程级**的，checkpoint 会把它
    # 的键列表写进 `pre_request` 事件（`C._session_state_keys`）。本模块会被
    # `tests/test_replay.py` importlib 载入、跟别的用例共用一个进程，那边跑过压缩用例后
    # `_session_state` 里就留着 `_compact`，于是重录出来的 checkpoint 记
    # `["_compact"]`、入库的那份记 `[]`，`--check` 报"事件流第 5 条起不一致"——**同一个
    # 录制器、同一份代码，只是上一个用例没收拾干净**。录制现场必须是干净的，所以录前
    # 清空、录后还原。
    shell = sys.modules.get("loop_agent_v2")
    prev_state = getattr(shell, "_session_state", None)
    if isinstance(prev_state, dict):
        shell._session_state = {}
    if seed is not None:
        seed()
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
        # 单条问题保持原样；传 list/tuple 表示多用户轮（同一 conv 顺序问下去）。
        questions = question if isinstance(question, (list, tuple)) else [question]
        for q in questions:
            out = la.chat(q, _conv=conv)
    finally:
        if prev_tool is not None:
            la.TOOL_CALL_MAP["get_date"] = prev_tool
        llm.reset()
        w.write_index()
        trace.close_writer(w)
        cp.close_store()
        confirm.set_policy(prev_policy)
        if isinstance(prev_state, dict):
            shell._session_state = prev_state
    return run_dir, out


def _seed_l2_work():
    """录制前把合成工作目录建成"这次写之前"的样子：一个有旧内容的文件。

    `write_file_mock` 的返回文案随"之前有没有这个文件"变（`Written` vs `Overwritten`），
    而这条消息进会话历史、进而进请求指纹。所以录制期与回放期的起点必须是同一种状态——
    实测反例：录制时目录不存在（`Written`）、回放时它已被别处建出来（`Overwritten`），
    第一个请求就配不上，报 `NoMatchInWindow`。

    这里刻意**建出**旧文件（而不是删掉目录），于是两边都走 `Overwritten` 分支。
    顺带把 §8.1 的"目录副本"也走上一遍：`prepare` 会把整棵合成目录 `copy2` 拷进
    overlay，overlay 里那份旧内容是真躺在副本里的，不是凭空生成的。
    """
    work = os.path.join(ROOT, L2_WORK_REL)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "out.txt"), "w", encoding="utf-8") as f:
        f.write("stale content from before the run\n")


def _build(name, batches, question, *, mode, expected, derived_from=None, out_root=None,
           seed=None):
    run_dir, out = _record(batches, question, name, seed=seed)
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


# 入库 fixture 的唯一真源：生成与校验都读这张表，避免"校验的规格和生成的规格走岔"。
SPECS = [
    {"name": "fingerprint_tool_round", "batches": TOOL_BATCH, "question": "今天几号",
     "mode": "fingerprint", "expected": "turn_end:reason=answer"},
    {"name": "positional_stuck", "batches": THINKING_BATCH, "question": "想个办法",
     "mode": "positional", "expected": "turn_end:reason=stuck"},
    {"name": "l2_file_ops", "batches": _l2_tool_batch(), "question": "改一下那个文件",
     "mode": "fingerprint", "expected": "turn_end:reason=answer", "seed": _seed_l2_work},
    {"name": "multiturn_stuck", "batches": MULTITURN_BATCH,
     "question": ["想个办法", "再想一个", "还有别的吗"],
     "mode": "positional", "expected": "turn_end:reason=stuck"},
]

# 天然每次不同的字段：只比"契约"部分，不比这些。
#
# `duration_ms` / `latency_ms` 是一台机器上这次跑多久的**测量值**，不是契约：同一个
# 工具同样的入参，一次 0ms 一次 1ms 都可能。它们不进这个名单的话，`--check` 会随机
# 红——而一旦它随机红，这个闸门就没人信了（"又飘了，重录一下"正是它要防的事）。
# `latency_ms` 尤其明显：它是上游一次响应的毫秒取整，稳定录成 0/1 全看当次调度，
# `tests/test_trace.py` 的 `_VOLATILE` 早已把它当易变字段。
# 事件里凡是"跑一次的耗时/时刻"，都属于这一类。
_VOLATILE_EVENT_KEYS = ("ts", "seq", "run_id", "session", "duration_ms", "latency_ms")
_STABLE_META_KEYS = ("cassette_format", "canon_version", "playback_mode", "completeness",
                     "degraded_reasons", "expected_assertion", "assertion_ids",
                     "checkpoint_count", "derived_from")
# `session_id` 里带着录制那一刻的毫秒数（`<fixture>-<毫秒>`），每次重录都不同。
# 它是"这份 fixture 是哪一次录的"的**标签**，不是契约——真正参与指纹比对的是事件流
# 里那个 `session` 字段（同样在 `_VOLATILE_EVENT_KEYS` 里）。拿它当契约比，
# `--check` 会在"什么都没改、只是重录了一遍"时红，那是假警报。
_VOLATILE_META_KEYS = ("session_id", "created_at", "recorded_at")


def _read_events(d):
    with open(os.path.join(d, "cassette.jsonl"), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _strip_mtime(obj):
    """递归把 `mtime` 换成固定值。

    `checkpoint` 里的文件清单记的是 `mtime` 的浮点秒——同一个文件、同样的 sha256，
    重录一次就必然差一位小数。它和 `duration_ms` 同类：是"何时跑"不是"跑出了什么"。
    只抹 mtime、保留 sha256/size：后者才是"现场内容没变"的证据，抹掉就等于这个闸门
    什么都不看了。
    """
    if isinstance(obj, dict):
        return {k: (0.0 if k == "mtime" else _strip_mtime(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_mtime(v) for v in obj]
    return obj


def _project(events):
    out = []
    for e in events:
        body = {k: v for k, v in e.items() if k not in _VOLATILE_EVENT_KEYS}
        out.append(json.dumps(_strip_mtime(body), sort_keys=True, ensure_ascii=False))
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


def _build_spec(spec, **kw):
    """跑一条规格；崩了就点名是谁——不然 `--check` 只报一句 traceback，
    在多条 fixture 的套件里等于没说清是哪个现场漂了。"""
    try:
        return _build(**spec, **kw)
    except Exception as e:  # noqa: BLE001
        print(f"[fixture] {spec['name']}: 生成失败：{type(e).__name__}: {e}")
        raise


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
            fresh_dir, _ = _build_spec(spec, derived_from=DERIVED_FROM, out_root=tmp_root)
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
        _build_spec(spec, derived_from=DERIVED_FROM)
    print(f"[fixture] 写入 {FIXTURE_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
