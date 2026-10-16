"""L3 最小化（ddmin）——施工文档 §7 / 设计文档 §14-§16。

干什么：拿一份**已经能复现某个失败**的 cassette，反复删事件、每次删完真回放一遍，
只留"删掉就不再复现"的那些，最后导出一份按位回放（positional）的最小 cassette。

为什么不自己写判等：断言身份只由 `trace.assertion_id()` 决定（§16.7），谓词里
必须复用现成实现，否则"同一个失败"的定义会在收缩器里长出第二份口径。

谓词三态（§7.2）：

- ``FAIL``：回放候选后，``expected_assertion`` 出现在 assert 级事件序列里。
- ``PASS``：不在。**出现另一个失败 id 也算 PASS**（不是"未决"）：id 只由 reason
  字符串决定，一次**不同**的失败若恰好同 reason 会被误判成同一个失败，所以"换了个
  失败"必须被保守地当成"没复现"，否则收缩器会把 bug A 的现场缩成 bug B 的现场。
  换成了谁记进报告的 ``substituted_assertion``，只记录、不做别的。
- ``UNRESOLVED``：只有两个来源，绝不能再扩展——① 超时（**独立状态**，不与"没复现"
  共用代码路径）；② 确定性护栏（同一个候选连跑两次、断言签名不一致）。按 delta
  debugging 原义，``UNRESOLVED`` 当 PASS 处理：保守拒绝这个候选、继续删。

**不引入概率判据**（"跑 N 次里有 M 次失败就算失败"）。我们的 id 只由 reason 字符串
决定，概率判据会让"大多数时候报另一个 reason"的候选被接受，那是把单次静默变成
统计静默。``--runs`` 只用于第 ② 条护栏。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from loop_agent_core import cassette as cassette_mod  # noqa: E402
from loop_agent_core import checkpoint as checkpoint_mod  # noqa: E402
from loop_agent_core import context as context_mod  # noqa: E402
from loop_agent_core import replay as replay_mod  # noqa: E402
from loop_agent_core import trace as trace_mod  # noqa: E402

FAIL = "FAIL"
PASS = "PASS"
UNRESOLVED = "UNRESOLVED"

RESULT_1MIN = "1-minimal"
DEFAULT_MAX_REPLAYS = 500
DEFAULT_TIMEOUT = 300.0
DEFAULT_RUNS = 2

# 可删除的事件类型：只有 LLM 负载（请求/分片/结束/用量）与工具调用可以整块消失。
# 其余类型（run_start/limits/turn_start/turn_end/checkpoint/notice/...）一律视为
# **控制流与断言面**，永不删除（§16.3 的固定项）。
DELETABLE_TYPES = ("llm_request", "llm_chunk", "llm_end", "token_usage",
                   "tool_call", "tool_result")
RESPONSE_TYPES = ("llm_request", "llm_chunk", "llm_end", "token_usage")

_PROBE_MARK = "@@SHRINK_PROBE@@"


class ShrinkError(RuntimeError):
    """收缩器自己的错误（输入不对、源 cassette 不能复现、导出失败）。"""


class BudgetExhausted(RuntimeError):
    """回放预算耗尽——不是失败，是"没收敛"（§16.4 第 3 条）。"""


def _slug(text: str) -> str:
    out = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in str(text))
    return out.strip("_") or "assertion"


# ── 删除单元：ddmin 的"一个原子单元" ────────────────────────────────────
@dataclass(frozen=True)
class Unit:
    """一个可以整体删除的单元。

    ``indices`` 是它覆盖的**事件下标**（不是事件本身），因为收缩过程中事件列表
    一直是源事件列表，删的只是"保留哪些下标"。``children`` 是细分单元（同一轮里
    的响应包 / 工具包），只在父轮整体存活时才递归处理。
    """

    name: str
    kind: str
    indices: tuple
    pinned: bool = False
    why: str = ""
    children: tuple = ()


def _turn_blocks(events: list) -> list:
    """按 ``turn_start`` 切用户轮；返回 [(起, 止), ...]（止是开区间）。

    首轮之前的 preamble（run_start/limits）不属于任何轮。
    """
    starts = [i for i, e in enumerate(events) if e.get("type") == "turn_start"]
    if not starts:
        return []
    blocks = []
    for k, s in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(events)
        blocks.append((s, end))
    return blocks


def _bundles(events: list, lo: int, hi: int) -> list:
    """把 [lo, hi) 里的**可删事件**切成响应包 / 工具包，返回 [(名, 起, 止), ...]。

    响应包：``llm_request`` 到 ``llm_end``，外加紧随其后的 ``token_usage``。
    工具包：一串相邻的 ``tool_call`` / ``tool_result``。
    保证包与包不交叉、也不留半个包——这是"依赖保持投影"的第一道保险。
    """
    out = []
    i = lo
    while i < hi:
        t = events[i].get("type")
        if t == "llm_request":
            j = i + 1
            while j < hi and events[j].get("type") not in ("llm_request", "llm_end"):
                j += 1
            if j < hi and events[j].get("type") == "llm_end":
                j += 1
            while j < hi and events[j].get("type") == "token_usage":
                j += 1
            ri = events[i].get("request_index")
            out.append((f"response ri={ri}", i, j))
            i = j
        elif t in ("tool_call", "tool_result"):
            j = i
            while j < hi and events[j].get("type") in ("tool_call", "tool_result"):
                j += 1
            cid = events[i].get("call_id") or events[i].get("id") or "?"
            out.append((f"tool {cid}", i, j))
            i = j
        else:
            i += 1
    return out


def build_units(events: list, expected_assertion: str) -> tuple:
    """构造两阶段 ddmin 的单元树（§16.3）。

    阶段 1 单元 = 用户轮（这里就是顶层单元）；阶段 2 单元 = 历史组，由
    history_group_units 从对话态派生。返回 (units, notes)。
    """
    notes = []
    blocks = _turn_blocks(events)
    # 失败轮按**事件下标**定位，不按 ``turn`` 字段：录制侧 ``begin_turn`` 传入的
    # 是本进程 trace 上下文的 turn，而那个值从没被推进过，实测多用户轮的每条
    # ``turn_start`` / ``turn_end`` 都挂着 `turn=0`。按字段匹配会让"含失败断言的轮"
    # 命中所有轮 → 每轮都被钉成固定项，轮级 ddmin 一个整轮也删不掉（这正是
    # L3「真删成功」一直拿不到证据的根因）。改用下标区间，轮身份只由它在事件流里的
    # 位置决定，与 `turn` 字段是否可靠无关。
    failing_index = None
    for i, e in enumerate(events):
        if trace_mod.assertion_id(e) == expected_assertion:
            failing_index = i
            break
    if failing_index is None:
        # 源现场本身没有这个断言：调用方应当先确认它能复现。
        notes.append(f"源事件里找不到断言 {expected_assertion}（按 id 派生口径）")
    units = []
    seen_turns = set()
    for k, (lo, hi) in enumerate(blocks):
        turn = events[lo].get("turn")
        if turn in seen_turns:
            turn = None  # 同一轮被切开只可能是重复 turn_start，按序号区分
        seen_turns.add(events[lo].get("turn"))
        is_first = k == 0
        kids = []
        for name, a, b in _bundles(events, lo, hi):
            kids.append(Unit(name=name, kind="bundle", indices=tuple(range(a, b))))
        why = ""
        if is_first:
            why = "首轮（问题本身）"
        if failing_index is not None and lo <= failing_index < hi:
            why = (why + " + 含失败断言的轮").strip(" +")
        # 轮单元只覆盖**可删载荷**；控制流事件（turn_start/turn_end/...）不在任何
        # 单元里，于是"全部控制流事件永不删"是结构上成立的，不靠自觉。
        payload = set()
        for kid in kids:
            payload.update(kid.indices)
        pinned = bool(why)
        units.append(Unit(name=f"turn={events[lo].get('turn')}#{k}",
                          kind="turn",
                          indices=tuple(sorted(payload)),
                          pinned=pinned, why=why, children=tuple(kids)))
    # preamble 里的可删载荷（正常录制不会有，防御性处理）
    if blocks:
        head = blocks[0][0]
        pre = [r for r in _bundles(events, 0, head)]
        for name, a, b in pre:
            units.append(Unit(name=f"preamble {name}", kind="bundle",
                              indices=tuple(range(a, b)),
                              why="前言（不属任何轮）"))
    return units, notes


def check_projection(events: list) -> list:
    """删完必须校验候选里没有孤立的 result / 调用（§16.3 依赖保持投影）。

    孤立的结果会让回放器以 ``ReplayToolMissing``/漂移直接拒掉候选，白花一次预算；
    所以这一步放在**回放之前**，作为候选的第一道门。
    """
    problems = []
    calls, results = {}, {}
    for i, e in enumerate(events):
        t = e.get("type")
        cid = e.get("call_id") or e.get("id")
        if t == "tool_call":
            calls[cid] = i
        elif t == "tool_result":
            results[cid] = i
    for cid, i in results.items():
        if cid not in calls:
            problems.append(f"tool_result #{i}（call_id={cid}）没有对应的 tool_call")
        elif calls[cid] > i:
            problems.append(f"tool_result #{i}（call_id={cid}）排在它的 tool_call 之前")
    for cid, i in calls.items():
        if cid not in results:
            problems.append(f"tool_call #{i}（call_id={cid}）没有对应的 tool_result")
    reqs, ends, chunks = {}, {}, {}
    for i, e in enumerate(events):
        t = e.get("type")
        ri = e.get("request_index")
        if t == "llm_request":
            reqs.setdefault(ri, i)
        elif t == "llm_end":
            ends.setdefault(ri, i)
        elif t == "llm_chunk":
            chunks.setdefault(ri, i)
    for ri, i in ends.items():
        if ri not in reqs:
            problems.append(f"llm_end #{i}（request_index={ri}）没有对应的 llm_request")
    for ri, i in chunks.items():
        if ri not in reqs:
            problems.append(f"llm_chunk #{i}（request_index={ri}）没有对应的 llm_request")
    return problems


# ── 阶段 2 的守卫 ─────────────────────────────────────────────────────
def positional_spine(events: list) -> tuple:
    """响应序列的"脊"：每条响应的内容指纹，按发生顺序（§16.3）。

    用内容指纹而不是 request_index：request_index 在删除后会重新编号，拿它当身份
    的话"重排"永远测不出来。
    """
    by_req = {}
    order = []
    for e in events:
        t = e.get("type")
        ri = e.get("request_index")
        if t == "llm_request":
            if ri not in by_req:
                by_req[ri] = []
                order.append(ri)
        elif t in ("llm_chunk", "llm_end"):
            if ri in by_req:
                by_req[ri].append({k: e.get(k) for k in
                                   ("type", "content", "reasoning", "tool_calls",
                                    "finish_reason") if k in e})
    return tuple(trace_mod.digest(by_req.get(ri, []), n=16) for ri in order)


def spine_guard(cand_spine, spine) -> tuple:
    """候选的响应序列相对 ``spine`` 只允许**减少或保持**（§16.3 阶段 2 守卫）。

    新增或重排即判该候选无效；返回 ``(有效?, 说明)``。

    注意：指纹相同（内容一模一样的多次重试）的响应彼此不可区分，此时重排测不出来——
    这是内容指纹的固有边界，不是守卫的漏洞；位置上完全等价的响应互换也确实不改变现场。
    """
    it = iter(spine)
    for item in cand_spine:
        for cur in it:
            if cur == item:
                break
        else:
            return False, f"候选里出现了源序列中没有（或次序被改）的响应 {item}"
    return True, "响应序列只减少或保持"


def spine_report(spine_before, spine_after) -> dict:
    """阶段 2 守卫的报告面（#31）：减少要记，新增/重排要判无效。

    差值按**位置对齐**算，不能用集合差：内容指纹相同的响应是常态（实测
    `positional_stuck` 里 4 次 thinking-only 重试的指纹全是 `2313f7b88d64d166`），
    集合差会认为"删掉一个还剩下三个，说明什么都没少"，`reduced` 恒为空——
    报告就变成了"看着在测，实际什么都没记"。
    """
    ok, note = spine_guard(spine_after, spine_before)
    # 差值 = 源序列里没被候选保留下来的那些。**不能只按位置、也不能只按内容**：
    #   · 内容集合差：四次 thinking-only 重试的指纹完全相同（实测全是
    #     `2313f7b88d64d166`），删掉一个还剩三个，"什么都没少"——报告形同虚设；
    #   · 纯位置对齐：中间插进/挪走一条就整体错位，把没删的也报成删了。
    # 所以先用公共前缀吃掉"指纹相同但确实是同一条"的部分，剩下的再按内容对齐。
    head = 0
    while (head < len(spine_before) and head < len(spine_after)
           and spine_before[head] == spine_after[head]):
        head += 1
    before_tail, after_tail = spine_before[head:], spine_after[head:]
    n, m = len(before_tail), len(after_tail)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = (dp[i + 1][j + 1] + 1 if before_tail[i] == after_tail[j]
                        else max(dp[i + 1][j], dp[i][j + 1]))
    kept = [True] * head + [False] * n
    i = j = 0
    while i < n and j < m:
        if before_tail[i] == after_tail[j]:
            kept[head + i] = True
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    reduced = [s for k, s in enumerate(spine_before) if not kept[k]]
    return {"enabled": True, "valid": ok, "note": note,
            "response_count_before": len(spine_before),
            "response_count_after": len(spine_after),
            "reduced": reduced}


# ── 阶段 2：事件流 → 对话态的 converter + 历史组单元 ────────────────────
def _msg_key(msg: dict) -> str:
    "消息身份：按内容做稳定摘要，用来对齐相邻两次请求的 messages 公共前缀。"
    return trace_mod.digest({k: msg.get(k) for k in
                             ("role", "content", "tool_calls", "tool_call_id", "name")},
                            n=16)


def _response_span(events: list, ri) -> tuple:
    "request_index=ri 的响应包覆盖的事件下标：llm_request → llm_end（含 token_usage）。"
    a = None
    for i, e in enumerate(events):
        if e.get("type") == "llm_request" and e.get("request_index") == ri:
            a = i
            break
    if a is None:
        return ()
    j = a + 1
    while j < len(events) and events[j].get("type") not in ("llm_request", "llm_end"):
        j += 1
    if j < len(events) and events[j].get("type") == "llm_end":
        j += 1
    while j < len(events) and events[j].get("type") == "token_usage":
        j += 1
    return tuple(range(a, j))


def _tool_span(events: list, call_id) -> tuple:
    "call_id 对应的 tool_call / tool_result 事件下标（缺一个就返回空，宁可不删）。"
    return tuple(i for i, e in enumerate(events)
                 if e.get("type") in ("tool_call", "tool_result")
                 and (e.get("call_id") or e.get("id")) == call_id)


def _producers_before(events: list, target_pos: int) -> tuple:
    """把事件流重建成 target_pos（一个 llm_request 事件下标）处的对话态。

    返回 (messages, producers)：messages 是那条请求实际发出去的会话
    （llm_request.messages——录制时真正的对话态），producers[k] 是产生
    messages[k] 的事件下标元组，None 表示这条消息不是本 trace 里的事件产生的
    （会话起点带着的历史、控制流注入的 nudge），因此不可删。

    对齐方式：按请求顺序取相邻两次 messages 的公共前缀，尾部新增消息由前一条
    请求的响应包（assistant）或紧随其后的工具包（tool）产生。公共前缀被压缩
    改写而变短时，改写段无法归因，一律记 None——保守地当不可删，绝不替录制侧
    发明历史。
    """
    positions = [i for i, e in enumerate(events) if e.get("type") == "llm_request"]
    if target_pos not in positions:
        return [], []
    previous = list(events[positions[0]].get("messages") or [])
    messages = previous
    producers = [None] * len(messages)
    target_i = positions.index(target_pos)
    for p_i in range(1, target_i + 1):
        pos = positions[p_i]
        cur = list(events[pos].get("messages") or [])
        common = 0
        while (common < len(previous) and common < len(cur)
               and _msg_key(previous[common]) == _msg_key(cur[common])):
            common += 1
        producers = producers[:common]
        prev_ri = events[positions[p_i - 1]].get("request_index")
        for k in range(common, len(cur)):
            msg = cur[k] or {}
            role = msg.get("role")
            if role == "assistant":
                producers.append(_response_span(events, prev_ri) or None)
            elif role == "tool":
                cid = msg.get("tool_call_id") or msg.get("call_id") or msg.get("id")
                producers.append(_tool_span(events, cid) or None)
            else:
                producers.append(None)
        previous = cur
        messages = cur
    return messages, producers


def history_group_units(events: list, expected_assertion=None) -> list:
    """阶段 2 的删除单元 = 失败轮之前的历史组（context._find_groups）。

    converter 不发明语义：对话态直接取 llm_request.messages（录制时真正发出的
    会话），提问下标 q_idx 取该轮首条请求的最后一条 message——runner.py 在轮首设
    q_idx = len(messages) - 1，轮内工具轮不再前移。于是：

    - 首轮失败（q_idx == 1）→ _find_groups 扫 messages[1:1] → 无历史组；
    - 后续轮失败 → 组覆盖之前各轮的消息，按“助手消息→那条请求的响应包、工具消息→
      对应 tool_call/tool_result”映射成事件下标。

    组里只要有一条消息没有产出事件（用户提问本身、注入的 nudge），整组不可删、
    标成固定项。返回 Unit 列表；expected_assertion 不给时取最后一条请求所在轮。
    """
    blocks = _turn_blocks(events)
    failing_index = None
    if expected_assertion:
        for i, e in enumerate(events):
            if trace_mod.assertion_id(e) == expected_assertion:
                failing_index = i
                break
    block = None
    if blocks:
        for lo, hi in blocks:
            if failing_index is not None and lo <= failing_index < hi:
                block = (lo, hi)
                break
        if block is None:
            block = blocks[-1] if failing_index is None else None
    if block is None:
        lo, hi = 0, len(events)
    else:
        lo, hi = block
    reqs = [i for i in range(lo, hi) if events[i].get("type") == "llm_request"]
    if not reqs:
        return []
    first_msgs = list(events[reqs[0]].get("messages") or [])
    q_idx = len(first_msgs) - 1
    if q_idx <= 0:
        return []
    messages, producers = _producers_before(events, reqs[-1])
    if not messages:
        return []
    q_idx = min(q_idx, len(messages))
    units = []
    for start, end, _size, _prio in context_mod._find_groups(q_idx, {"messages": messages}):
        indices, blocked = set(), ""
        for k in range(start, end):
            prod = producers[k] if k < len(producers) else None
            if not prod:
                blocked = "组内含非事件产生的消息（用户提问/注入的 nudge）"
                break
            indices.update(prod)
        if blocked:
            units.append(Unit(name=f"history[{start}:{end}]", kind="history",
                              indices=(), pinned=True, why=blocked))
        else:
            units.append(Unit(name=f"history[{start}:{end}]", kind="history",
                              indices=tuple(sorted(indices))))
    return units


# ── 真回放探针 ────────────────────────────────────────────────────────
def _safe_read_events(run_dir: str) -> list:
    path = os.path.join(run_dir, trace_mod.TRACE_EVENTS)
    if not os.path.isfile(path):
        return []
    try:
        return trace_mod.read_events(run_dir)
    except Exception:  # noqa: BLE001 —— 探针读不到 live 事件不该二次炸
        return []


def _live_snapshot_b(run_dir: str) -> list:
    """活回放里每条请求的 B 组（压缩记账）——按请求序号（§7.2 末条）。"""
    events = _safe_read_events(run_dir)
    ris = [e.get("request_index") for e in events if e.get("type") == "llm_request"]
    out = []
    try:
        store = checkpoint_mod.CheckpointStore(run_dir)
    except Exception:  # noqa: BLE001
        return [None for _ in ris]
    for ri in ris:
        try:
            snap = store.load_for_request(ri, kind="pre_request")
        except Exception:  # noqa: BLE001
            snap = None
        out.append((snap or {}).get("B") or None)
    try:
        store.close()
    except Exception:  # noqa: BLE001
        pass
    return out


def replay_once(cassette_dir: str, run_dir: str) -> dict:
    """真回放一次，返回断言序列 + 被消费的响应位置 + 活快照的 B 组。

    回放**炸了**（``ReplayError``/``NoMatchInWindow``/工具缺失……）在这里被吃掉：
    那不是"判不了"，而是"这个候选没复现"。但错误原文要带回去，报告里能看见。
    """
    if os.path.isdir(run_dir):
        shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(run_dir, exist_ok=True)
    ids, consumed, err, live_calls = [], [], None, []
    try:
        # all_turns=True：L3 的删除空间包含用户轮，回放必须**真的跑完剩下的轮**——
        # 只跑第一轮时"删掉后面某轮仍复现"是假的（那些轮根本没被读过）。
        rep = replay_mod.run_replay(cassette_dir, run_dir=run_dir, check_env=False,
                                    all_turns=True)
        ids = list(rep.assertion_ids or [])
        consumed = [m.get("position") for m in (rep.matches or [])]
    except BaseException as exc:  # noqa: BLE001 —— 回放炸了 = 没复现，不是崩溃
        err = f"{type(exc).__name__}: {exc}"
        ids = trace_mod.assertion_ids(_safe_read_events(run_dir))
    finally:
        try:
            checkpoint_mod.close_store(run_dir)
        except Exception:  # noqa: BLE001
            pass
    for e in _safe_read_events(run_dir):
        if e.get("type") == "tool_call":
            cid = e.get("call_id") or e.get("id")
            if cid is not None:
                live_calls.append(cid)
    live_b = _live_snapshot_b(run_dir)
    shutil.rmtree(run_dir, ignore_errors=True)
    return {"assertion_ids": ids, "consumed": consumed, "error": err,
            "live_call_ids": live_calls, "live_b": live_b}


def probe_cassette(cassette_dir: str, *, runs: int = 2, timeout: float | None = None) -> dict:
    """在**子进程**里真回放 ``runs`` 次，父进程只等结果。

    子进程是必须的：想给单次回放套时间上限，就不能和它共用一个进程。超时独立成
    ``UNRESOLVED``/``timeout``，绝不和"没复现"共用一条路径。
    """
    cmd = [sys.executable, "-m", "tools.shrink", "_probe",
           "--cassette", cassette_dir, "--runs", str(max(1, int(runs)))]
    env = os.environ.copy()
    env.setdefault("DS_KEY", "sk-selftest-not-a-real-key")
    try:
        proc = subprocess.run(cmd, cwd=_ROOT, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=(timeout if timeout and timeout > 0 else None))
    except subprocess.TimeoutExpired:
        return {"status": UNRESOLVED, "kind": "timeout", "replays": max(1, int(runs)),
                "detail": f"回放超时（上限 {timeout}s）", "signatures": [],
                "assertion_ids": [], "consumed": [], "error": None,
                "live_call_ids": [], "live_b": []}
    payload = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith(_PROBE_MARK):
            try:
                payload = json.loads(line[len(_PROBE_MARK):])
            except Exception:  # noqa: BLE001
                payload = None
    if payload is None:
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-6:]
        return {"status": UNRESOLVED, "kind": "harness", "replays": max(1, int(runs)),
                "detail": "探针子进程没有给出结果：" + " / ".join(tail),
                "signatures": [], "assertion_ids": [], "consumed": [], "error": None,
                "live_call_ids": [], "live_b": []}
    payload["replays"] = max(1, int(runs))
    return payload


def _probe_child(args) -> int:
    """``_probe`` 子命令：把 runs 次回放的签名打到 stdout（只有这一行）。"""
    cass = os.path.abspath(args.cassette)
    runs = max(1, int(args.runs))
    sigs, last = [], None
    for k in range(runs):
        run_dir = tempfile.mkdtemp(prefix="shrink-replay-")
        last = replay_once(cass, run_dir)
        sig = list(last["assertion_ids"])
        if last["error"]:
            sig.append("!" + last["error"])
        sigs.append(sig)
    out = {"signatures": sigs, "assertion_ids": last["assertion_ids"],
           "consumed": last["consumed"], "error": last["error"],
           "live_call_ids": last["live_call_ids"], "live_b": last["live_b"]}
    print(_PROBE_MARK + json.dumps(out, ensure_ascii=False))
    return 0


def classify(probe: dict, expected_assertion: str, source_ids=()) -> tuple:
    """把一次探针折算成三态（§7.2）。``UNRESOLVED`` 当 PASS 处理。"""
    kind = probe.get("kind")
    if kind == "timeout":
        return UNRESOLVED, {"kind": "timeout", "detail": probe.get("detail")}
    sigs = [json.dumps(s, ensure_ascii=False, sort_keys=True) for s in probe.get("signatures") or []]
    if len(set(sigs)) > 1:
        return UNRESOLVED, {"kind": "nondeterministic",
                            "detail": "同一候选两次回放的断言签名不一致："
                                      + " vs ".join(sorted(set(sigs)))}
    if kind in ("harness",):
        return UNRESOLVED, {"kind": kind, "detail": probe.get("detail")}
    ids = list(probe.get("assertion_ids") or [])
    if expected_assertion in ids:
        return FAIL, {"assertion_ids": ids, "substituted": None}
    src = set(source_ids or ())
    others = [i for i in ids if i != expected_assertion]
    new = [i for i in others if i not in src]
    return PASS, {"assertion_ids": ids, "substituted": (new or others or [None])[0]}


# ── 候选缓存 + 预算 + 守卫 ────────────────────────────────────────────
class CandidateTester:
    """一次候选 = 写一份候选 cassette + 真回放 ``runs`` 次。

    缓存按"保留的下标集合"去重：ddmin 最坏 O(n²) 次询问，重复候选一律不重放。
    """

    def __init__(self, events, source, *, root, expected, work_dir, max_replays,
                 timeout, runs, source_ids=()):
        self.events = events
        self.source = source
        self.root = root
        self.expected = expected
        self.work_dir = work_dir
        self.max_replays = max(0, int(max_replays))
        self.timeout = timeout
        self.runs = max(1, int(runs))
        self.source_ids = tuple(source_ids)
        self.replays = 0
        self.cache = {}
        self.cand_dir = os.path.join(work_dir, "cand")
        self.notes = []
        self.first = True
        self.spine = None

    def remaining(self) -> int:
        return max(0, self.max_replays - self.replays)

    def run(self, indices) -> tuple:
        key = frozenset(indices)
        if key in self.cache:
            return self.cache[key]
        kept = [self.events[i] for i in sorted(key)]
        if not self.first:
            problems = check_projection(kept)
            if problems:
                res = (PASS, {"kind": "projection", "detail": "; ".join(problems[:3])})
                self.cache[key] = res
                self.notes.append({"unit": None, "why": "依赖投影不成立：" + problems[0]})
                return res
        if not self.first and self.spine is not None:
            ok, note = spine_guard(positional_spine(kept), self.spine)
            if not ok:
                res = (PASS, {"kind": "spine", "detail": note})
                self.cache[key] = res
                self.notes.append({"unit": None, "why": "阶段 2 守卫拒绝：" + note})
                return res
        if not self.first and self.replays + self.runs > self.max_replays:
            self.notes.append({"unit": None, "why": "回放预算耗尽"})
            raise BudgetExhausted(f"已用 {self.replays} 次回放，预算 {self.max_replays}")
        _write_trace(self.root, kept)
        cass = cassette_mod.slice_cassette(
            self.root, self.cand_dir, playback_mode=cassette_mod.PLAYBACK_POSITIONAL,
            derived_from=self._candidate_derived(), expected_assertion=self.expected,
            workspace_root=(self.source.meta.get("workspace_root") if self.source else None),
            force=True)
        probe = probe_cassette(self.cand_dir, runs=self.runs, timeout=self.timeout)
        self.replays += int(probe.get("replays") or self.runs)
        status, info = classify(probe, self.expected, self.source_ids)
        info = dict(info)
        info["probe"] = probe
        info["meta"] = cass
        res = (status, info)
        self.cache[key] = res
        self.first = False
        return res

    def _candidate_derived(self) -> str:
        return f"{self.source.source_id} + ddmin(candidate, runs={self.runs})"


def _write_trace(root: str, events: list) -> None:
    path = os.path.join(root, trace_mod.TRACE_EVENTS)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def _split(items, n):
    if n <= 1:
        return [list(items)] if items else []
    size = max(1, (len(items) + n - 1) // n)
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def _union(units) -> set:
    idx = set()
    for u in units:
        idx.update(u.indices)
    return idx


class Reducer:
    """ddmin 主循环 + 分层删 + 关键元素登记。"""

    def __init__(self, events, tester):
        self.events = events
        self.tester = tester
        self.best = set(range(len(events)))
        self.converged = True
        self.critical = []
        self.solo_tested = set()
        self.deleted = []
        self.groups_total = 0
        self.groups_live = 0

    def test(self, cand) -> tuple:
        status, info = self.tester.run(cand)
        if status == FAIL:
            self.best = set(cand)
        return status, info

    def ddmin(self, units) -> list:
        """标准 ddmin（§16.3）：先删大块、再删补集，粒度倍增。"""
        live = list(units)
        if not live:
            return live
        n = min(2, len(live))
        while True:
            chunks = _split(live, n)
            moved = False
            for chunk in chunks:
                cand = self.best - _union(chunk)
                status, _info = self.test(cand)
                if status == FAIL:
                    live = [u for u in live if u not in chunk]
                    self.deleted.extend(u.name for u in chunk)
                    n = max(2, n - 1)
                    moved = True
                    break
                if len(chunk) == 1:
                    self.solo_tested.add(chunk[0].name)
                    if chunk[0].name not in self.critical:
                        self.critical.append(chunk[0].name)
            if moved:
                n = min(n, max(1, len(live)))
                continue
            for chunk in chunks:
                comp = [u for u in live if u not in chunk]
                if not comp:
                    continue
                cand = self.best - _union(comp)
                status, _info = self.test(cand)
                if status == FAIL:
                    live = chunk
                    self.deleted.extend(u.name for u in comp)
                    n = max(2, n - 1)
                    moved = True
                    break
            if moved:
                n = min(n, max(1, len(live)))
                continue
            if n >= len(live):
                break
            n = min(len(live), n * 2)
        return live

    def run(self, units, groups=None) -> None:
        """两阶段骨架：先删整轮，再进轮内细分，最后按历史组做阶段 2（§16.3 / §7.2）。"""
        top = [u for u in units if not u.pinned and set(u.indices) <= self.best]
        self.groups_total = len(groups or ())
        self.groups_live = 0
        try:
            self.ddmin(top)
            for u in units:
                if not set(u.indices) <= self.best:
                    continue  # 整轮已被删掉
                kids = [c for c in u.children if set(c.indices) <= self.best]
                if kids:
                    self.ddmin(kids)
            # 阶段 2：单元是失败轮之前的历史组，守卫是响应序列只减不增不重排。
            # spine 取阶段 1 收敛后的 c1（§7.2 的 spine = positional_spine(c1)）。
            live = [u for u in (groups or ()) if not u.pinned
                    and set(u.indices) <= self.best]
            self.groups_live = len(live)
            if live:
                c1 = [self.events[i] for i in sorted(self.best)]
                self.tester.spine = positional_spine(c1)
                self.ddmin(live)
        except BudgetExhausted:
            self.converged = False


# ── 报告里那三栏 ───────────────────────────────────────────────────────
def unconsumed_events(events: list, probe: dict) -> list:
    """录制了、但回放中**从未被消费**的事件清单（§7.3）。

    收缩过程里这就是"死代码清单"：一条响应从没被取走，说明它对这个现场没有贡献——
    ddmin 要靠反复试删才能得出的结论，这里一眼就能读。
    """
    if probe.get("error") or not probe.get("consumed"):
        return []
    reqs = [e for e in events if e.get("type") == "llm_request"]
    consumed_ri = set()
    for pos in probe.get("consumed") or []:
        if isinstance(pos, int) and 0 <= pos < len(reqs):
            consumed_ri.add(reqs[pos].get("request_index"))
    live_calls = set(probe.get("live_call_ids") or [])
    out = []
    for e in events:
        t = e.get("type")
        ri = e.get("request_index")
        if t in RESPONSE_TYPES and ri not in consumed_ri:
            out.append(f"{t} #{e.get('seq')}（request_index={ri}）")
        elif t in ("tool_call", "tool_result"):
            cid = e.get("call_id") or e.get("id")
            if cid not in live_calls:
                out.append(f"{t} #{e.get('seq')}（call_id={cid}）")
    return out


def _flatten(units) -> list:
    """把轮单元与它们的细分单元摊平——撤销、报告都要按"所有删除单元"来数。"""
    out = []
    for u in units:
        out.append(u)
        out.extend(u.children or ())
    return out


def critical_elements(reducer: Reducer, units: list) -> list:
    """少了它就复现不了的几个元素（§7.3）——比"删掉了什么"好读。"""
    out = []
    for u in _flatten(units):
        if u.pinned:
            out.append(f"{u.name}（固定项：{u.why}）")
        elif u.name in reducer.critical:
            out.append(f"{u.name}（删掉后不再复现）")
    n_ctrl = sum(1 for e in reducer.events
                 if e.get("type") not in DELETABLE_TYPES)
    if n_ctrl:
        out.append(f"控制流/断言面事件 ×{n_ctrl}（永不删）")
    return out


# ── 导出：B 组按新序列重算 ────────────────────────────────────────────
def repatch_b_group(root: str, kept_events: list, live_b: list) -> int:
    """把 checkpoint 的 B 组（压缩记账）覆盖成活回放里刚算出来的那份（§7.2 末条）。

    B 组是"这一请求之前攒下的压缩计数"，删掉若干请求之后它和源快照对不上了。
    按新序列重算的**唯一可信来源**是活回放自己写下的快照，所以这里直接取活值，
    而不是在收缩器里凭事件重推一遍（那会长出第二套记账口径）。
    """
    ris = [e.get("request_index") for e in kept_events if e.get("type") == "llm_request"]
    if not ris or not live_b:
        return 0
    try:
        src = checkpoint_mod.CheckpointStore(root)
    except Exception:  # noqa: BLE001 —— 没有快照（比如合成 cassette）就跳过
        return 0
    snaps, patched = [], 0
    pos = {ri: k for k, ri in enumerate(ris)}
    for ri in sorted(set(ris)):
        try:
            keys = src.keys_for_request(ri)
        except Exception:  # noqa: BLE001
            keys = []
        for key in keys:
            try:
                snap = src.load(key)
            except Exception:  # noqa: BLE001
                continue
            k = pos.get(ri)
            if (k is not None and k < len(live_b) and live_b[k] is not None
                    and snap.get("kind") == "pre_request"):
                snap["B"] = live_b[k]
                patched += 1
            snaps.append(snap)
    try:
        src.close()
    except Exception:  # noqa: BLE001
        pass
    if not patched:
        return 0
    ckpt_dir = os.path.join(root, cassette_mod.CASSETTE_CHECKPOINTS)
    shutil.rmtree(ckpt_dir, ignore_errors=True)
    os.makedirs(ckpt_dir, exist_ok=True)
    dst = checkpoint_mod.CheckpointStore(root)
    mapping = {}
    for snap in snaps:
        old = snap.pop("_key", None)
        nk = dst.append(snap)
        if old:
            mapping[old] = nk
    try:
        dst.close()
    except Exception:  # noqa: BLE001
        pass
    for e in kept_events:
        if e.get("type") == "checkpoint" and e.get("_key") in mapping:
            e["_key"] = mapping[e["_key"]]
    return patched


# ── 主流程 ────────────────────────────────────────────────────────────
class _Source:
    """源 cassette 的稳定标识与元数据（报告与 derived_from 都要用）。"""

    def __init__(self, meta: dict, cassette_dir: str):
        self.meta = meta or {}
        self.source_id = (self.meta.get("derived_from") or self.meta.get("session_id")
                          or cassette_dir)


def prepare_root(cassette_dir: str, work_dir: str) -> tuple:
    """把源 cassette 摊平成一个"trace 根"：``trace.jsonl`` + ``meta.json`` + 快照。

    为什么绕这一道：纯切片函数 ``slice_cassette`` 读的是 trace 根（它要读 manifest、
    按 request_index 找快照）。收缩时事件流被改过，源 trace 不一定还在现场，所以把
    源 cassette 摊平成本地 trace 根，再由它反复切片——**候选不重复走回放**。
    """
    root = os.path.join(work_dir, "src")
    os.makedirs(root, exist_ok=True)
    cas = cassette_mod.Cassette(cassette_dir, check_env=False)
    _write_trace(root, cas.events)
    with open(os.path.join(root, trace_mod.TRACE_META), "w", encoding="utf-8",
              newline="\n") as fh:
        json.dump(cas.meta, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    src_ckpt = os.path.join(cassette_dir, cassette_mod.CASSETTE_CHECKPOINTS)
    dst_ckpt = os.path.join(root, cassette_mod.CASSETTE_CHECKPOINTS)
    if os.path.isdir(src_ckpt):
        shutil.rmtree(dst_ckpt, ignore_errors=True)
        shutil.copytree(src_ckpt, dst_ckpt)
    return root, cas


def shrink_cassette(cassette_dir: str, out_dir: str, *, expected_assertion=None,
                    max_replays: int = DEFAULT_MAX_REPLAYS, timeout=DEFAULT_TIMEOUT,
                    runs: int = DEFAULT_RUNS, force: bool = False,
                    work_root: str | None = None, log=None) -> dict:
    """收缩一份 cassette，导出最小现场，返回报告（§7.3）。"""
    say = log or (lambda *_a, **_k: None)
    t0 = time.time()
    cassette_dir = os.path.abspath(cassette_dir)
    out_dir = os.path.abspath(out_dir)
    if not os.path.isdir(cassette_dir):
        raise ShrinkError(f"找不到 cassette 目录：{cassette_dir}")
    root_cas = cassette_mod.Cassette(cassette_dir, check_env=False)
    expected = expected_assertion or root_cas.expected_assertion()
    if not expected:
        raise ShrinkError("必须指定 expected_assertion：源 cassette 的 meta 里没有，"
                          "命令行也没给。谓词没有锚就无从判定。")
    source = _Source(root_cas.meta, cassette_dir)
    events = list(root_cas.events)
    before_events = list(events)
    work_root = work_root or os.path.join(_ROOT, "tmp", "shrink")
    os.makedirs(work_root, exist_ok=True)
    work_dir = os.path.join(work_root, f"{_slug(os.path.basename(cassette_dir.rstrip(os.sep)))}"
                                      f"-{int(time.time() * 1000) % 1000000}")
    shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(work_dir, exist_ok=True)
    root, cas = prepare_root(cassette_dir, work_dir)
    units, notes = build_units(events, expected)
    groups = history_group_units(events, expected_assertion=expected)
    say(f"收缩 {cassette_dir} → 断言 {expected}；轮单元 {len(units)} 个、"
        f"历史组 {len(groups)} 个，事件 {len(events)} 条，预算 {max_replays} 次回放")
    tester = CandidateTester(events, source, root=root, expected=expected,
                             work_dir=work_dir, max_replays=max_replays,
                             timeout=timeout, runs=runs,
                             source_ids=root_cas.assert_ids())
    reducer = Reducer(events, tester)
    # 基线：源现场必须复现，否则收缩没有意义（这一步不占预算）
    status, base_info = reducer.test(reducer.best)
    if status != FAIL:
        raise ShrinkError(f"源 cassette 不能复现断言 {expected}"
                          f"（判定 {status}：{(base_info or {}).get('detail') or ''}）；"
                          "收缩的前提是现场真的失败，先修输入或换断言。")
    reducer.run(units, groups)
    best = reducer.best
    kept_events = [events[i] for i in sorted(best)]
    # 最终候选的活值（B 组 / 消费位置 / 断言序列）从缓存里取，不额外回放
    _st, best_info = tester.cache.get(frozenset(best), (None, {}))
    probe = (best_info or {}).get("probe") or {}
    patched = repatch_b_group(root, kept_events, probe.get("live_b") or [])
    _write_trace(root, kept_events)
    # 导出后还要复核一次（下面的 `verify`），那次回放也是本次导出的成本：
    # 报告里的 `replays` 把它算进去了，`derived_from` 不能少记一次——两处口径必须一致。
    export_replays = tester.replays + 1
    derived = (f"{source.source_id} + shrink(units=turns, deleted="
               f"{len(events) - len(kept_events)} events, replays={export_replays}, "
               f"runs={runs}, expected_assertion={expected})")
    meta = cassette_mod.slice_cassette(
        root, out_dir, playback_mode=cassette_mod.PLAYBACK_POSITIONAL,
        derived_from=derived, expected_assertion=expected,
        workspace_root=root_cas.meta.get("workspace_root"), force=force)
    # 导出后复核一次：报告里的"收敛"必须是真收敛，不是写在纸上的
    verify = probe_cassette(out_dir, runs=1, timeout=timeout)
    tester.replays += 1
    vstatus, vinfo = classify(verify, expected, root_cas.assert_ids())
    before_requests = [e for e in before_events if e.get("type") == "llm_request"]
    after_requests = [e for e in kept_events if e.get("type") == "llm_request"]
    spine_before = positional_spine(before_events)
    spine_after = positional_spine(kept_events)
    report = {
        "cassette": out_dir,
        "source": cassette_dir,
        "source_id": source.source_id,
        "units": len(_flatten(units)) + len(groups),
        "units_top": len(units) + len(groups),
        "unit_kinds": sorted({u.kind for u in units} | {u.kind for u in groups}),
        "replays": tester.replays,
        "max_replays": max_replays,
        "converged": bool(reducer.converged),
        "result": RESULT_1MIN,
        "expected_assertion": expected,
        "response_count_before": len(before_requests),
        "response_count_after": len(after_requests),
        "calls_before": len(before_requests),
        "calls_after": len(after_requests),
        "skipped_units": skipped_units(list(units) + groups, reducer, tester),
        "unconsumed": unconsumed_events(kept_events, probe),
        "critical_elements": critical_elements(reducer, list(units) + groups),
        "substituted_assertion": (best_info or {}).get("substituted"),
        "deleted_units": list(reducer.deleted),
        "deleted_events": len(events) - len(kept_events),
        "guard": dict(spine_report(spine_before, spine_after), **{
            "enabled": True,
            "stage2_hook": "history_group_units()",
            "groups": reducer.groups_total,
            "groups_live": reducer.groups_live,
            "groups_deleted": [d for d in reducer.deleted
                               if d.startswith("history[")],
            "why": "" if reducer.groups_live else (
                "阶段 2 无可用历史组：失败轮为首轮（q_idx=1，_find_groups 扫 "
                "messages[1:1] 为空），或组内只有用户提问/nudge 这类非事件产生的消息"),
        }),
        "checkpoint_b_patched": patched,
        "verify": {"status": vstatus, "detail": (vinfo or {}).get("kind")},
        "notes": [n.get("why") for n in tester.notes] + notes,
        "elapsed": round(time.time() - t0, 2),
    }
    say(f"收敛={report['converged']} 回放={tester.replays} 次 "
        f"响应 {report['response_count_before']}→{report['response_count_after']} "
        f"事件删掉 {report['deleted_events']} 条")
    return report


def skipped_units(units, reducer, tester) -> list:
    """没被删、也没被验证为"删了就散"的单元，以及它们为什么被跳过（§7.3）。"""
    out = []
    for u in _flatten(units):
        if u.pinned:
            out.append(f"{u.name}: 固定项（{u.why}）")
    for n in tester.notes:
        if n.get("unit") is None and n.get("why"):
            out.append(str(n["why"]))
    for u in _flatten(units):
        if not u.pinned and u.name not in reducer.solo_tested:
            out.append(f"{u.name}: 未做单元级验证")
    return out


# ── 断言清单：一个 fixture 只带一个 expected_assertion ────────────────
def fixture_assertions(cas) -> list:
    """这份 cassette 里可以当失败锚的断言 id（去重保序，§13.1 第 5 条）。"""
    ids = list(cas.assert_ids() or [])
    out = []
    for i in ids:
        if i not in out:
            out.append(i)
    return out


def plan_fixtures(cas, *, assertion=None, all_assertions=False) -> list:
    """一个断言一个 fixture；多个失败就导出多个（各自只带一个 expected_assertion）。

    默认按 cassette meta 里已经锚定的那一个来（L1 抽取时就是显式给的）；``all_assertions``
    才铺开成"每个断言一份"，用于一条 trace 里有多个失败现场的场合。
    """
    if assertion:
        ids = [assertion]
    elif all_assertions:
        ids = fixture_assertions(cas)
    elif cas.expected_assertion():
        ids = [cas.expected_assertion()]
    else:
        ids = fixture_assertions(cas)
    return [{"assertion": a, "name": _slug(a)} for a in ids]


# ── CLI ───────────────────────────────────────────────────────────────
def cmd_run(args) -> int:
    cass = os.path.abspath(args.cassette)
    cas = cassette_mod.Cassette(cass, check_env=False)
    plans = plan_fixtures(cas, assertion=args.assertion,
                          all_assertions=args.all_assertions)
    if not plans:
        print("✗ 这份 cassette 里没有任何可当失败锚的断言 id", file=sys.stderr)
        return 2
    reports = []
    rc = 0
    for plan in plans:
        out = args.out if len(plans) == 1 else os.path.join(args.out, plan["name"])
        out = os.path.abspath(out)
        if os.path.exists(out) and not args.force:
            print(f"✗ 输出目录已存在（加 --force 覆盖）：{out}", file=sys.stderr)
            return 2
        rep = shrink_cassette(cass, out, expected_assertion=plan["assertion"],
                              max_replays=args.max_replays, timeout=args.timeout,
                              runs=args.runs, force=args.force, log=print)
        rep["fixture"] = plan["name"]
        reports.append(rep)
        if not rep["converged"] or rep["verify"]["status"] != FAIL:
            rc = max(rc, 1)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    if args.report:
        with open(args.report, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(reports if len(reports) > 1 else reports[0], fh,
                      ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"报告写到 {args.report}")
    return rc


def cmd_assertions(args) -> int:
    cas = cassette_mod.Cassette(os.path.abspath(args.cassette), check_env=False)
    exp = cas.expected_assertion()
    print(f"cassette: {cas.dir}")
    print(f"playback_mode: {cas.mode}")
    print(f"expected_assertion: {exp}")
    for a in fixture_assertions(cas):
        mark = "  ← meta 已锚定" if a == exp else ""
        print(f"  {a}{mark}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.shrink",
        description="L3 最小化（ddmin）：删事件 + 真回放，导出可复现失败的最小 cassette")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("run", help="收缩一份 cassette 并导出最小现场")
    q.add_argument("--cassette", required=True, help="源 cassette 目录")
    q.add_argument("--out", required=True, help="导出目录（--all-assertions 时是父目录）")
    q.add_argument("--assertion", default=None, help="目标断言 id（缺省取 meta 里的）")
    q.add_argument("--all-assertions", action="store_true",
                   help="每个断言各导出一份 fixture")
    q.add_argument("--max-replays", type=int, default=DEFAULT_MAX_REPLAYS,
                   help=f"回放预算（默认 {DEFAULT_MAX_REPLAYS}）；耗尽即报未收敛")
    q.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                   help=f"单次回放的时间上限秒（默认 {DEFAULT_TIMEOUT}）")
    q.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                   help=f"每个候选连跑几次（默认 {DEFAULT_RUNS}）：只用于确定性护栏")
    q.add_argument("--force", action="store_true", help="输出目录已存在也覆盖")
    q.add_argument("--report", default=None, help="把报告另存为 JSON")
    q.set_defaults(func=cmd_run)

    q = sub.add_parser("assertions", help="列出这份 cassette 里可当失败锚的断言 id")
    q.add_argument("--cassette", required=True)
    q.set_defaults(func=cmd_assertions)

    q = sub.add_parser("_probe", help="（内部）子进程里真回放 runs 次并打印签名")
    q.add_argument("--cassette", required=True)
    q.add_argument("--runs", type=int, default=1)
    q.set_defaults(func=_probe_child)
    return p


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse 用 SystemExit 表达"参数不对"
        return 2 if exc.code else 0
    try:
        return int(args.func(args))
    except (cassette_mod.CassetteError, replay_mod.ReplayError, ShrinkError) as exc:
        print(f"✗ {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except trace_mod.TraceError as exc:
        print(f"✗ TraceError: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
