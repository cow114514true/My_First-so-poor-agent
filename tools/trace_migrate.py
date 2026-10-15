"""旧会话迁移器（施工文档 §5，设计文档 §8）。

```
python -m tools.trace_migrate conversations/*.json --out tmp/traces/
```

**迁移的价值不是「能回放」**。旧会话里只有 message 级轨迹：没有 LLM 请求体、没有流式
chunk、没有 `finish_reason`、没有 `limits` 记录。所以迁出来的每份 cassette 必然是
`completeness.llm == "missing"` 的**降级 cassette**，回放器见到它会直接抛
`CassetteIncomplete` —— 这是设计要的，不是缺陷（§8.2：一律拒绝，不许静默 stub）。

那迁移为什么还值得做，三件事：

1. **量出可回放比例这个天花板**（§5.2 的 2×3 矩阵）。先量再投：如果历史语料在
   `body_missing` 那两格里，L2（overlay 与工具策略）对它们的帮助就比 L1（LLM 回放）大。
2. 把 111 份真实会话变成**有结构、可查询**的语料（此前只能 grep）。
3. 从语料里挑出值得固化成测试的失败轮次——L3 的输入。

矩阵按**用户轮**统计，两轴正交（§8.1）：

| | `args_ok` | `args_partial` | `args_omitted` |
|---|---|---|---|
| `body_ok` | 完全可回放 | LLM 侧可回放，B 类降级 stub | LLM 侧可回放，B 类不可回放 |
| `body_missing` | 可重建调用序列 | 可分析 | 不可用 |

实测口径（本仓库 111 份会话，见 `--matrix` 输出）：`body_ok` 一格都没有——旧格式
根本不存请求体，这条轴不是"退化"而是"从来不存在"。`args_ok` 则几乎是全部：那 111 份
会话是**在 `_store_tool_args` 开始抹参数之前/之外**存的，1107 个调用的 `arguments`
逐个验过都是原始串。真正的降级来自第三处、也是报告里最该看的一栏：**工具名失配**
（`browse_web` 9 次，当前 `TOOL_CLASS` 里没有它），回放侧对这些调用只能
`ReplayToolMissing`。

为什么 `write_file` 的"省略"不是 redaction（§9 的反例警示）：`_store_tool_args` 的占位
是**不可逆的数据丢失**，不是脱敏。所以本模块把它当降级如实记账，绝不"补个空的继续跑"。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loop_agent_core import cassette as cassette_mod  # noqa: E402
from loop_agent_core import toolclass as toolclass_mod  # noqa: E402
from loop_agent_core import trace as trace_mod  # noqa: E402

# 输入过滤复用 tui 的口径（§5.1）：侧车 `<会话>.json.archive.json` 同样满足
# `chat_*.json`，用裸前缀匹配会把折叠档案当成会话（Bug 31 踩过）。
_AUTOSAVE_RE = re.compile(r"\.autosave(\.\d+)?\.json$")
# `_store_tool_args` 的占位形状（§4.5）：`{"$omitted": {"len": N, "sha256": ...}}`。
# 见到它就是这个调用的原始参数**已经永久丢了**。
_OMITTED_MARKER = "$omitted"

# 当前词表认识的工具名（冻结在导入时）。旧语料里会出现**已经不存在的工具**
# （实测 `browse_web` 9 次），这是迁移必须如实报出来的一栏：回放侧遇到它只能
# ReplayToolMissing，与"参数被抹掉"是两种完全不同的补救方式（前者要改写调用、
# 后者要重录现场）。
_REGISTERED_NAMES = frozenset(toolclass_mod.registered_names())

BODY_OK = "ok"
BODY_MISSING = "missing"
ARGS_OK = "ok"
ARGS_PARTIAL = "partial"
ARGS_OMITTED = "omitted"

ARGS_STATES = (ARGS_OK, ARGS_PARTIAL, ARGS_OMITTED)
BODY_STATES = (BODY_OK, BODY_MISSING)

# 矩阵每格的中文结论（§5.2 表格原文），报告里直接给人看。
CELL_LABEL = {
    (BODY_OK, ARGS_OK): "完全可回放",
    (BODY_OK, ARGS_PARTIAL): "LLM 侧可回放，B 类降级 stub",
    (BODY_OK, ARGS_OMITTED): "LLM 侧可回放，B 类不可回放",
    (BODY_MISSING, ARGS_OK): "可重建调用序列",
    (BODY_MISSING, ARGS_PARTIAL): "可分析",
    (BODY_MISSING, ARGS_OMITTED): "不可用",
}


def is_conversation_file(name: str) -> bool:
    """是否是**会话**文件——复用 `tui.is_conversation_file()` 的口径（§5.1）。

    直接转发而不是抄一份：这条口径被 Bug 31 咬过（sidecar 被当成会话加载，
    `messages` 被换成折叠档案条目），两处各写一份迟早会漂移。
    """
    from tui import is_conversation_file as _impl  # noqa: PLC0415 —— 惰性：tui 很重
    return _impl(name)


def is_autosave(name: str) -> bool:
    """`.autosave*.json` 是**中断现场**（§5.1）：照样迁，但要单独标记。"""
    return bool(_AUTOSAVE_RE.search(name))


def expand_inputs(patterns, *, include_autosave: bool = False) -> list:
    """把命令行给的路径/通配展开成"要迁的会话文件"清单（保序去重）。

    过滤在这里一次做干净：`include_autosave` 不开时 autosave 被排除，`.archive.json`
    永远排除。返回值里带 `interrupted` 标记，后面的 manifest 直接用它。

    默认**不收** autosave，用显式参数而不是模块级开关：模块级全局状态在 CLI 与测试
    交替调用时会互相污染，而"这批输入到底收了什么"必须能从一个调用点读出来。
    """
    out = []
    seen = set()
    for pat in patterns or []:
        hits = sorted(glob.glob(pat))
        if not hits and os.path.exists(pat):
            hits = [pat]
        for h in hits:
            p = os.path.abspath(h)
            if not os.path.isfile(p):
                continue
            name = os.path.basename(p)
            interrupted = is_autosave(name)
            if not (is_conversation_file(name) or interrupted):
                continue
            if interrupted and not include_autosave:
                continue
            if p in seen:
                continue
            seen.add(p)
            out.append((p, interrupted))
    if not out:
        # 输入的**存在性**要在这里就失败。放过去的话 `cmd_migrate` 会产出
        # `migration_index.json`（`n_sessions: 0`）并打印一张全 0 的矩阵——看上去
        # 像"迁移成功，只是旧会话确实不可回放"，而实际上是通配符打错了。
        raise SystemExit(f"✗ 输入没有匹配到任何会话文件：{list(patterns or [])}")
    return out


def session_id_for(path: str) -> str:
    """会话 id = 文件名去掉 `.json`（含 autosave 的后缀整段去掉）。"""
    name = os.path.basename(path)
    if name.endswith(".json"):
        name = name[: -len(".json")]
    return name


# ── 旧会话 → 事件序列 ────────────────────────────────────────────────
def _raw_arguments(tool_call: dict):
    """取工具调用的原始 arguments 串，并判断它是不是被抹过的占位。

    返回 `(raw, omitted)`。`$omitted` 占位是**不可逆的数据丢失**（§9 的反例警示），
    所以这里如实标出，绝不"当成空参数继续"。
    """
    fn = tool_call.get("function") or {}
    arg = fn.get("arguments")
    if arg is None or arg == "":
        return "", True
    if isinstance(arg, str):
        return arg, _OMITTED_MARKER in arg
    try:
        text = json.dumps(arg, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(arg), True
    return text, _OMITTED_MARKER in text


def split_turns(messages: list) -> list:
    """把 message 列表切成**用户轮**（§15 的轮次单位，也是 §5.2 的统计单位）。

    为什么按用户轮切而不是按 assistant 消息切：一个用户轮里有多个 LLM 请求、多个工具
    调用，矩阵的两轴都要在轮级聚合——"第 3 轮那个 write_file 参数丢了"才是可行动的结论。

    前导消息（system 等，在第一个 user 之前）**归入第 1 轮**，不单开一轮。曾经让它
    自己开一轮（`if role == "user" or cur is None`），结果每份会话都多报一轮——
    实测 108 份语料报出 494 轮，而真实 user 消息只有 417 条。矩阵单位是**用户轮**，
    "system prompt 那一轮"不是用户发起的一轮，把它算进去就是虚报可回放比例的分母。
    """
    turns = []
    cur = None
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if cur is None:
            # 第一个 user 之前的前导消息（system）：**先不开轮**，挂在 cur=None 上，
            # 等第一个 user 到来时一起装进第 1 轮。给它们单开一轮会让每份会话多报一轮。
            cur = {"index": 1, "messages": [], "tools": [], "open": True}
            turns.append(cur)
        if role == "user":
            if cur["open"]:
                # 这一轮是先建的空壳（装前导消息用的），第一个 user 进来就落进它。
                cur["open"] = False
            else:
                cur = {"index": len(turns) + 1, "messages": [], "tools": [], "open": False}
                turns.append(cur)
        cur["messages"].append(m)
        for tc in (m.get("tool_calls") or []):
            if isinstance(tc, dict):
                cur["tools"].append(tc)
    for turn in turns:
        turn.pop("open", None)
    return turns


def classify_turn(turn: dict) -> dict:
    """判定一个用户轮的矩阵格 + 降级原因。

    `body` 轴 = 该轮内**全部** LLM 请求体 + 流式 chunk + `finish_reason` 都在（§5.2）。

    实测：旧会话是 message 级轨迹，`assistant` 消息的键只有
    `('content', 'role', 'tool_calls')` / `('content', 'role')`——**没有一条**带
    `finish_reason`，请求体与 chunk 更是从不存在。所以 body 轴不是"退化"，
    而是**从来不存在**：这一轴恒为 `missing`，`body_ok` 两格在旧语料上必然空着。

    这里不写死 `missing`，而是真去查消息里有没有这些字段：将来若有人补录了请求体，
    判定要跟着变，而不是继续报"就是没有"。
    """
    n_assistant = 0
    n_with_finish = 0
    for m in turn["messages"]:
        if m.get("role") == "assistant":
            n_assistant += 1
            if m.get("finish_reason"):
                n_with_finish += 1
    # 复现面三件套按"旧会话实际能提供的"逐项查：请求体与 chunk 在 message 列表里
    # 没有对应字段（它们属于 LLM 出口，不落在会话文件里），所以这里只有
    # finish_reason 是可观测项；三者缺任一即 body_missing。
    body = BODY_OK if (n_assistant > 0 and n_with_finish == n_assistant) else BODY_MISSING

    degraded_tools = []
    omitted_tools = []
    unregistered = []
    n = 0
    for tc in turn["tools"]:
        n += 1
        name = ((tc.get("function") or {}).get("name")) or ""
        _raw, omitted = _raw_arguments(tc)
        if omitted:
            omitted_tools.append(name)
            degraded_tools.append(name)
        if name and name not in _REGISTERED_NAMES:
            # 这不是"参数丢了"，是**工具本身没有了**：回放侧只能 ReplayToolMissing。
            # 单列一栏，因为它要求的投入完全不同（改写调用 vs 重录现场）。
            unregistered.append(name)
            degraded_tools.append(name)

    if n == 0:
        # 这一轮没调工具：矩阵的 args 轴对它没有意义，按最保守的 args_ok 记
        # （没有"原始参数缺失"这回事），并在报告里注明 n_tool_calls=0。
        args = ARGS_OK
    elif not degraded_tools:
        args = ARGS_OK
    elif len(omitted_tools) + len(unregistered) >= n:
        args = ARGS_OMITTED
    else:
        args = ARGS_PARTIAL

    return {"turn": turn["index"], "body": body, "args": args,
            "degraded_tools": sorted(set(degraded_tools)),
            "omitted_tools": sorted(set(omitted_tools)),
            "unregistered_tools": sorted(set(unregistered)),
            "n_tool_calls": n, "n_results": _count_results(turn)}


def _count_results(turn: dict) -> int:
    """这一轮里真的落了结果的工具调用数（按 `tool_call_id` 认领）。

    数量对不上就是"结果丢了"，与"参数丢了"是两回事：前者是回放时没有东西可吐，
    后者是吐得出但可能吐错。cassette 的 `completeness.tools` 只关心前者。
    """
    called = {((tc.get("id")) or "") for tc in turn["tools"]}
    called.discard("")
    seen = set()
    for m in turn["messages"]:
        if m.get("role") != "tool":
            continue
        cid = m.get("tool_call_id")
        if cid:
            seen.add(cid)
    return len(called & seen)


def _stable_call_ids(messages: list) -> dict:
    """给每个 `tool_call` 元素算一个**唯一且稳定**的 call_id。

    为什么需要这一步（实测结论，不是防御性编程）：旧语料里**同一个 call_id 会被
    重复使用**。`chat_20260803_234135` 一轮里 6 次 `read_file`，其中 3 次的 id 就是
    字面量 `xml0`——录制侧当时拿到的 id 本身就是坏的/被复用过的，而这是**已发生的
    历史**，改不了。

    为什么必须唯一：cassette 以 `call_id` 建索引（`tool_calls()` / `tool_result()`），
    重复 id 会让后面的调用把前面的挤掉。实测全量 1107 次调用只有 971 个 key 落进
    cassette——**136 次调用凭空消失**，而矩阵那边看起来一切正常。

    怎么取名：
    - 本来唯一的 id 原样保留（绝大多数），回放时对得上；
    - 缺失的用 `mig-<turn>-<tool_round>`（不再掺事件序号，重跑要得到同一份 id）；
    - 撞车的返回**所有**同名位置（`_find_result` 用它把同一份结果派给每个位置，
      这正是历史语义——当时本来就分不清这几条结果谁是谁），并加 `#2` `#3` 后缀。
    """
    id_map = {}
    by_id = {}
    for turn in split_turns(messages):
        t = turn["index"]
        for seq, tc in enumerate(turn["tools"]):
            cid = tc.get("id") or f"mig-{t}-{seq}"
            by_id.setdefault(cid, []).append((turn["index"], seq))
    for cid, occ in by_id.items():
        if len(occ) == 1:
            id_map[occ[0]] = cid
        else:
            for dup_i, key in enumerate(occ):
                id_map[key] = cid if dup_i == 0 else f"{cid}#{dup_i + 1}"
    return id_map


def build_events(session_id: str, messages: list, meta: dict) -> list:
    """旧会话 → 与 trace schema 同构的事件序列（§5.1「迁移器写的是 trace」）。

    **只返回轮内事件**：`run_start` / `limits` / `run_end` 由 `trace.open_writer` /
    `close_writer` 自己发，这里不再造一份——两份 run_start 会让"这份 trace 是不是从头
    录的"这个判断失效，而它正是回放拒绝半截 trace 的依据。

    写出来的是**能表达的内容**：调用了什么、参数是什么、结果是什么。写不出来的是
    复现面（请求/响应体/finish_reason），事件流里就一份 `llm_request` 都不发——这恰恰是
    `completeness()` 判 `llm: missing` 的依据，回放器据此拒绝，而不是"缺就缺吧"。
    """
    events = []
    id_map = _stable_call_ids(messages)
    n_dup_calls = 0
    for turn in split_turns(messages):
        t = turn["index"]
        events.append({"type": "turn_start", "turn": t})
        for seq, tc in enumerate(turn["tools"]):
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            raw, _omitted = _raw_arguments(tc)
            call_id = id_map.get((t, seq)) or tc.get("id") or f"mig-{t}-{seq}"
            dup = "#" in call_id
            if dup:
                n_dup_calls += 1
            events.append({
                "type": "tool_call", "call_id": call_id, "turn": t, "tool_round": seq,
                "name": name, "tool_name": name, "arguments_raw": raw,
                "class": _class_of(name),
                "rewrite": _rewrite_of(name),
                "migrated": True,
                "args_omitted": _OMITTED_MARKER in raw,
                # 历史里这个 id 被复用过，后缀是本迁移器为消歧加的（见 `_stable_call_ids`）。
                "call_id_disambiguated": dup,
            })
            result = _find_result(messages, call_id.split("#")[0])
            if result is not None:
                events.append({
                    "type": "tool_result", "call_id": call_id, "name": name,
                    "class": _class_of(name),
                    "result_full": result, "result_stored": result,
                    "truncation": None, "duration_ms": None, "from_replay": False,
                    "migrated": True,
                    # 旧会话没有 limits 记录，所以"存下来的是不是被截断过"**无法归因**。
                    # 明写成字段：回放侧的截断闭环宁可判 Unattributable，也不能拿今天的
                    # cap 去比一段没有记录的现场（§4.5 表末行就是这个场景）。
                    "truncation_unknown": True,
                })
    if n_dup_calls:
        events.append({"type": "notice", "level": "info", "code": "migration_dup_call_id",
                       "message": f"{n_dup_calls} 次工具调用的 call_id 在旧会话里被复用，"
                                  f"迁移时加了 #N 后缀消歧（结果按首次出现派发）"})
    # `turn_end` 是断言面词表的一员（§4.3）。旧会话里没有它，这里**不编**一个看起来
    # 合理的 reason：编出来的 reason 会让"这个词表里存在这个断言"变成假象，而回放的
    # target_assertion 正是按词表取值的。缺就缺，一条会话级 notice 说明一次即可——
    # 逐轮发会让断言面被同一句话灌满，反而把真正的断言淹掉。
    events.append({"type": "notice", "level": "info", "code": "migration_no_reason",
                   "message": "旧会话不记录 turn_end reason，迁移不臆造"})
    return events


def _class_of(name: str):
    try:
        return toolclass_mod.class_of(name)
    except Exception:  # noqa: BLE001 —— 未登记工具不该让整份迁移失败
        return None


def _rewrite_of(name: str):
    """同 `_class_of`：未登记工具的名字**必须能写进 trace**，哪怕它今天已不存在。

    实测旧语料里有 `browse_web`（9 次调用）。它不在 `TOOL_CLASS` 里，
    `toolclass.rewrite_scope_of` 对未登记工具**抛异常**——不接住的话，5 份会话的
    迁移会整个失败。而"这份会话调过一个已经没有的工具"恰恰是迁移最该报出来的结论
    （回放侧遇到它只能 ReplayToolMissing）。
    """
    try:
        return toolclass_mod.rewrite_scope_of(name)
    except Exception:  # noqa: BLE001
        return None


def _find_result(messages: list, call_id: str):
    """按 `tool_call_id` 找工具结果。找不到返回 None（会造成 `tools: partial`）。"""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id") == call_id:
            return m.get("content")
    return None


# ── 单份迁移 ────────────────────────────────────────────────────────
def migrate_one(path: str, *, out_root: str, interrupted: bool = False,
                force: bool = False, make_cassette: bool = True,
                workspace_root: str | None = None) -> dict:
    """迁一份会话：写 trace 目录 + `migration_report.json` + 降级 cassette。

    返回单会话报告（也是 `migration_report.json` 的内容）。
    """
    from tui import read_conversation  # noqa: PLC0415 —— 惰性：tui 很重

    sid = session_id_for(path)
    run_dir = os.path.join(out_root, sid)
    messages, err = read_conversation(path)
    report = {
        "session": sid, "source_file": os.path.abspath(path), "units": "turn",
        "interrupted_session": bool(interrupted),
        "migrated_at": trace_mod.trace_epoch()["iso"],
        "matrix": {}, "degraded_tools": {}, "per_turn": [],
        "args_omitted_tools": {}, "unregistered_tools": {},
        "n_turns": 0, "n_tool_calls": 0, "n_messages": 0,
        "trace_dir": run_dir, "cassette_dir": None,
    }
    if err or messages is None:
        # 会话文件坏掉不该让整批迁移崩：如实记下来，继续下一份。
        report["error"] = err or "read_conversation 返回空"
        return report

    report["n_messages"] = len(messages)
    manifest = {
        "session_id": sid,
        "model": None, "backend": None,
        "migrated": True,
        "migrated_at": report["migrated_at"],
        "source_file": report["source_file"],
        "source_format": "chat_json_message_list",
        "interrupted": bool(interrupted),
        # 复现面**不存在**——这是判定，不是缺失：写进 manifest 让下游一眼看到，
        # 而不是读了半天 llm_request 一条没有才发现。
        "completeness": {"llm": BODY_MISSING, "tools": "full"},
        "degraded_reasons": [
            "body_missing:旧会话格式不存 LLM 请求体/chunk/finish_reason",
            "limits_missing:旧会话不记录 limits，截断无法归因",
        ],
        "workspace_root": workspace_root or os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))),
    }

    events = build_events(sid, messages, manifest)
    if os.path.exists(run_dir) and not force:
        raise SystemExit(f"{run_dir} 已存在（加 --force 覆盖）")
    writer = trace_mod.open_writer(sid, level="assert", run_dir=run_dir, manifest=manifest)
    try:
        for e in events:
            writer.append(e)
    finally:
        trace_mod.close_writer(writer, reason="migrated")

    # 矩阵与明细
    turns = split_turns(messages)
    matrix = {}
    degraded = {}
    omitted = {}
    unregistered = {}
    n_calls = 0
    n_results = 0
    for turn in turns:
        cls = classify_turn(turn)
        report["per_turn"].append(cls)
        key = f"{cls['body']}/{cls['args']}"
        matrix[key] = matrix.get(key, 0) + 1
        report["n_tool_calls"] += cls["n_tool_calls"]
        n_calls += cls["n_tool_calls"]
        n_results += cls["n_results"]
        for name in cls["degraded_tools"]:
            degraded[name] = degraded.get(name, 0) + 1
        for name in cls["omitted_tools"]:
            omitted[name] = omitted.get(name, 0) + 1
        for name in cls["unregistered_tools"]:
            unregistered[name] = unregistered.get(name, 0) + 1
    report["matrix"] = matrix
    report["degraded_tools"] = dict(sorted(degraded.items(), key=lambda kv: -kv[1]))
    report["args_omitted_tools"] = dict(sorted(omitted.items(), key=lambda kv: -kv[1]))
    report["unregistered_tools"] = dict(sorted(unregistered.items(), key=lambda kv: -kv[1]))
    report["n_turns"] = len(turns)

    if make_cassette:
        # 降级 cassette 落在 trace 目录**里面**：`tmp/traces/` 整体是 .gitignore 覆盖的
        # 派生物，一份 `llm: missing` 的 cassette 只有分析价值，不该混进 git 管理的
        # `tests/fixtures/replay/`（那里的每一份都必须是能通过回放自检的证据）。
        cas_dir = os.path.join(run_dir, "cassette")
        try:
            meta = cassette_mod.extract_cassette(
                run_dir, cas_dir, force=True, workspace_root=manifest["workspace_root"],
                extra_degraded=manifest["degraded_reasons"])
            # 补一处 `build_meta` 从事件里推不出来的账：
            # `completeness.tools`。`completeness()` 在**一条 llm_request 都没有**时
            # 把 tools 判成 `missing`——它把"没有请求"当成了"工具也没记"。迁移的 trace
            # 恰恰全是这种情况，于是 tools 轴对**每一份**快照都错报。按 tool_call 与
            # tool_result 的真实配对给：配齐 = 完整；配上一些 = 部分；一条调用都没有
            # 就是**没有调用可缺结果**，那是完整。
            if n_calls == 0 or n_results >= n_calls:
                tools_state = "full"
            else:
                tools_state = "partial"
            # `tools` 轴不是"降级程度"，是"缺不缺东西"：迁移的 tool 面本来就是完整的
            # （参数全是原文），所以这里给 full 不是粉饰，而是原来那一栏算错了。
            meta_path = os.path.join(cas_dir, cassette_mod.CASSETTE_META)
            with open(meta_path, "r", encoding="utf-8") as f:
                disk_meta = json.load(f)
            disk_meta["completeness"] = {"llm": "missing", "tools": tools_state}
            disk_meta["migrated"] = True
            disk_meta["source_file"] = report["source_file"]
            with open(meta_path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(disk_meta, f, ensure_ascii=False, indent=2)
                f.write("\n")
            report["cassette_dir"] = cas_dir
            report["cassette_completeness"] = disk_meta["completeness"]
            # `expected_assertion` 为空时 `extract_cassette` 不会因"没复现"而失败；
            # 这里再记一条自检结论，避免读者以为它**通过了**回放验证。
            report["cassette_replay_check"] = (disk_meta.get("replay_check") or {}).get("reason")
        except Exception as exc:  # noqa: BLE001 —— 单份 cassette 抽失败不该毁掉 trace
            report["cassette_error"] = f"{type(exc).__name__}: {exc}"

    with open(os.path.join(run_dir, "migration_report.json"), "w",
              encoding="utf-8", newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


# ── 汇总 ────────────────────────────────────────────────────────────
def merge_reports(reports: list) -> dict:
    """把逐份报告合成总体矩阵（§5.2「矩阵每格计数 + 降级工具归因」）。"""
    matrix = {}
    degraded = {}
    omitted = {}
    unregistered = {}
    for r in reports:
        for k, v in (r.get("matrix") or {}).items():
            matrix[k] = matrix.get(k, 0) + v
        for k, v in (r.get("degraded_tools") or {}).items():
            degraded[k] = degraded.get(k, 0) + v
        for k, v in (r.get("args_omitted_tools") or {}).items():
            omitted[k] = omitted.get(k, 0) + v
        for k, v in (r.get("unregistered_tools") or {}).items():
            unregistered[k] = unregistered.get(k, 0) + v
    out = {
        "units": "turn",
        "n_sessions": len(reports),
        "n_sessions_failed": sum(1 for r in reports if r.get("error")),
        "n_sessions_interrupted": sum(1 for r in reports if r.get("interrupted_session")),
        "n_turns": sum(r.get("n_turns", 0) for r in reports),
        "n_tool_calls": sum(r.get("n_tool_calls", 0) for r in reports),
        "matrix": matrix,
        "cell_labels": {k: CELL_LABEL[tuple(k.split("/"))]
                        for k in matrix if tuple(k.split("/")) in CELL_LABEL},
        "degraded_tools": dict(sorted(degraded.items(), key=lambda kv: -kv[1])),
        "args_omitted_tools": dict(sorted(omitted.items(), key=lambda kv: -kv[1])),
        "unregistered_tools": dict(sorted(unregistered.items(), key=lambda kv: -kv[1])),
        "sessions": [],
    }
    for r in reports:
        out["sessions"].append({
            "session": r.get("session"), "n_turns": r.get("n_turns"),
            "matrix": r.get("matrix"), "error": r.get("error"),
            "interrupted_session": r.get("interrupted_session"),
        })
    return out


def format_matrix(summary: dict) -> str:
    """把 2×3 矩阵打成表（§5.2 的形状，人直接读）。"""
    lines = []
    head = f"{'':<14}" + "".join(f"{a:<22}" for a in
                                 ("args_ok", "args_partial", "args_omitted"))
    lines.append(head)
    lines.append("-" * len(head))
    for b in BODY_STATES:
        cells = []
        for a in ARGS_STATES:
            key = f"{b}/{a}"
            n = (summary.get("matrix") or {}).get(key, 0)
            cells.append(f"{n} {CELL_LABEL[(b, a)]}".ljust(22))
        lines.append(f"{b:<14}" + "".join(cells))
    lines.append("")
    lines.append(f"会话 {summary.get('n_sessions')}（失败 {summary.get('n_sessions_failed')}，"
                 f"中断现场 {summary.get('n_sessions_interrupted')}）"
                 f"｜用户轮 {summary.get('n_turns')}｜工具调用 {summary.get('n_tool_calls')}")
    dt = summary.get("degraded_tools") or {}
    lines.append("降级工具归因：" + (", ".join(f"{k}×{v}" for k, v in dt.items()) or "无"))
    ur = summary.get("unregistered_tools") or {}
    lines.append("工具名已不存在（回放只能 ReplayToolMissing）："
                 + (", ".join(f"{k}×{v}" for k, v in ur.items()) or "无"))
    om = summary.get("args_omitted_tools") or {}
    lines.append("参数被 $omitted 抹掉（不可逆丢失）："
                 + (", ".join(f"{k}×{v}" for k, v in om.items()) or "无"))
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────
def cmd_migrate(args) -> int:
    inputs = expand_inputs(args.inputs, include_autosave=bool(args.include_autosave))
    if not inputs:
        print("✗ 没有可迁移的会话文件（只收 chat_*.json；autosave 需 --include-autosave）",
              file=sys.stderr)
        return 2
    out_root = os.path.abspath(args.out)
    os.makedirs(out_root, exist_ok=True)
    trace_mod.harden_dir(out_root)

    reports = []
    for i, (path, interrupted) in enumerate(inputs, 1):
        sid = session_id_for(path)
        try:
            rep = migrate_one(path, out_root=out_root, interrupted=interrupted,
                              force=args.force, make_cassette=not args.no_cassette,
                              workspace_root=args.workspace_root)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 —— 单份失败不终止整批：语料迁移要能跑完
            rep = {"session": sid, "source_file": path, "units": "turn",
                   "error": f"{type(exc).__name__}: {exc}", "matrix": {}, "per_turn": [],
                   "degraded_tools": {}, "n_turns": 0, "n_tool_calls": 0}
        reports.append(rep)
        if not args.quiet:
            tag = "（中断现场）" if interrupted else ""
            if rep.get("error"):
                print(f"[{i}/{len(inputs)}] {sid} ✗ {rep['error']}")
            else:
                cells = ", ".join(f"{k}={v}" for k, v in (rep.get("matrix") or {}).items())
                print(f"[{i}/{len(inputs)}] {sid}{tag} 轮={rep.get('n_turns')} "
                      f"调用={rep.get('n_tool_calls')} {cells}")

    summary = merge_reports(reports)
    summary["out"] = out_root
    summary["generated_at"] = trace_mod.trace_epoch()["iso"]
    idx = os.path.join(out_root, "migration_index.json")
    with open(idx, "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif not args.quiet:
        print()
        print(format_matrix(summary))
        print(f"\n索引：{idx}")
    return 0


def load_index(out_root: str) -> dict:
    """读回一份迁移汇总。没有索引时**从各会话报告现拼**——

    索引是汇总产物，不该是唯一副本：先跑 `migrate` 再删掉索引、或者只保留了几个
    会话目录的场景（挑失败轮次做测试就是这样）都得能读回同一套数字。
    """
    root = os.path.abspath(out_root)
    idx = os.path.join(root, "migration_index.json")
    if os.path.exists(idx):
        return json.load(open(idx, encoding="utf-8"))
    reports = [json.load(open(p, encoding="utf-8"))
               for p in sorted(glob.glob(os.path.join(root, "*", "migration_report.json")))]
    if not reports:
        raise FileNotFoundError(f"{root} 下没有 migration_index.json / migration_report.json")
    summary = merge_reports(reports)
    summary["out"] = root
    return summary


def cmd_matrix(args) -> int:
    """只看已有迁移结果的矩阵（不再跑迁移）。"""
    root = os.path.abspath(args.out)
    try:
        summary = load_index(root)
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(format_matrix(summary))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.trace_migrate",
        description="旧会话 → trace + 降级 cassette + 可回放比例矩阵（施工文档 §5）")
    sub = p.add_subparsers(dest="cmd")

    q = sub.add_parser("migrate", help="迁移会话文件")
    q.add_argument("inputs", nargs="*", help="会话文件或通配（如 conversations/chat_*.json）")
    q.add_argument("--out", default=os.path.join("tmp", "traces"))
    q.add_argument("--include-autosave", action="store_true",
                   help="同时迁 .autosave*.json（标记为 interrupted_session）")
    q.add_argument("--no-cassette", action="store_true", help="只写 trace，不抽降级 cassette")
    q.add_argument("--workspace-root", default=None)
    q.add_argument("--force", action="store_true", help="覆盖已存在的 trace 目录")
    q.add_argument("--json", action="store_true", help="打印整份汇总 JSON")
    q.add_argument("--quiet", action="store_true", help="不逐份打印进度")
    q.set_defaults(func=cmd_migrate)

    q = sub.add_parser("matrix", help="读已有迁移结果并打印矩阵")
    q.add_argument("--out", default=os.path.join("tmp", "traces"))
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_matrix)
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 兼容文档里的直接写法 `python -m tools.trace_migrate conversations/*.json --out ...`
    # （没有子命令）。有子命令就走子命令，否则视为 migrate。
    if not argv or argv[0] not in ("migrate", "matrix", "-h", "--help"):
        argv = ["migrate"] + argv
    p = build_parser()
    try:
        args = p.parse_args(argv)
        return args.func(args)
    except SystemExit as exc:
        # 参数错、输入没匹配到、单份失败……都归到"这次迁移没成功"。
        # 不再往外抛：调用方（CLI 入口、测试）看到的是退出码，不是一个异常。
        if exc.code not in (0, None):
            print(f"✗ {exc}", file=sys.stderr)
        return 0 if exc.code in (0, None) else 2


if __name__ == "__main__":
    sys.exit(main())
