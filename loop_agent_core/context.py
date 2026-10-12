"""上下文管理：append/分组/裁剪/回滚。纯函数，只操作传入的 conv dict。"""
import json
import os

from .config import _compact_k, _is_local_backend, cfg
from .tokens import estimate_tokens

_PRUNE_NOTICE = "[Context trimmed] 为控制上下文已删除 {n} 条较早消息（工具调用及结果等）。继续完成任务；如需之前的信息，直接问用户或凭现有结果推断。"


def _prune_cap():
    """Per-backend prune threshold. Local: 50% of model ctx. DeepSeek: 32k (~50% of 65536). Both env-overridable."""
    if _is_local_backend():
        env = os.environ.get("PRUNE_CAP_LOCAL")
        if env:
            return int(env)
        return max(1024, cfg["ctx"] // 2)
    return int(os.environ.get("PRUNE_CAP_DS", "32000"))


def _append_msg(msg, conv):
    """Append a message to a conversation (conv dict) and keep its running token estimate."""
    conv["messages"].append(msg)
    if conv["size"] is not None:
        conv["size"] += estimate_tokens(json.dumps(msg, ensure_ascii=False))


def _find_groups(q_idx, conv):
    """Deletable groups in conv['messages'][1:q_idx]: (start, end, size, priority).
    Priority 0 = tool chains (transient, delete first); 1 = backbone/notice singletons."""
    messages = conv["messages"]
    groups = []
    i = 1  # keep system prompt at index 0
    while i < q_idx:
        m = messages[i]
        if m.get("role") == "assistant" and "tool_calls" in m:
            j = i + 1
            while j < q_idx:
                r = messages[j].get("role")
                if r == "tool" or (r == "user" and str(messages[j].get("content", "")).startswith("[Self-check]")):
                    j += 1
                else:
                    break
            size = sum(estimate_tokens(json.dumps(messages[k], ensure_ascii=False)) for k in range(i, j))
            groups.append((i, j, size, 0))
            i = j
        else:
            size = estimate_tokens(json.dumps(m, ensure_ascii=False))
            groups.append((i, i + 1, size, 1))
            i += 1
    return groups


def _is_trim_notice(m):
    return m.get("role") == "user" and str(m.get("content", "")).startswith("[Context trimmed]")


def _prune_messages(cap, q_idx, conv):
    """Prune conv['messages'][1:q_idx] (everything before the current question) when over cap.
    Deletes tool chains first, then backbone singletons. Inserts a notice before the question
    (recycling older notices so they don't pile up). Returns (new_q_idx, removed)."""
    msgs = conv["messages"]
    if conv["size"] is None or conv["size"] <= cap:
        return q_idx, 0
    groups = sorted(_find_groups(q_idx, conv), key=lambda g: (g[3], g[0]))
    keep = set(range(len(msgs)))
    removed = 0
    for start, end, size, prio in groups:
        if conv["size"] <= cap:
            break
        for k in range(start, end):
            keep.discard(k)
        conv["size"] -= size
        removed += end - start
    if not removed:
        return q_idx, 0
    msgs[:] = [m for i, m in enumerate(msgs) if i in keep]
    new_q = q_idx - removed
    # 回收旧的 [Context trimmed] 提示，只留最新一条，避免多条提示堆叠干扰模型
    # 排除 new_q：当前问题可能已合并了上轮 notice，不能当旧提示删掉
    old_notices = [i for i, m in enumerate(msgs) if _is_trim_notice(m) and i != new_q]
    if old_notices:
        conv["size"] -= sum(estimate_tokens(json.dumps(msgs[i], ensure_ascii=False)) for i in old_notices)
        msgs[:] = [m for i, m in enumerate(msgs) if i not in old_notices]
        new_q -= sum(1 for i in old_notices if i < new_q)
    # 合并提示到问题文本，避免独立 user 消息破坏 user/assistant 交替协议。
    # 计数**累加**（见 render_prune_notice）：反复裁剪时不能让模型只看到最后一次的数字。
    old = str(msgs[new_q]["content"])
    note_text = render_prune_notice(old, removed)
    msgs[new_q]["content"] = note_text
    conv["size"] += estimate_tokens(note_text) - estimate_tokens(old)
    return new_q, removed


def _rollback_tool_round(conv):
    """丢弃最后一轮工具调用（assistant+TC 及其 tool 消息），让会话回到 API 接受的状态。
    找不到可回滚的轮次时返回 False。"""
    msgs = conv["messages"]
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "assistant" and msgs[i].get("tool_calls"):
            del msgs[i:]
            conv["size"] = estimate_tokens(json.dumps(msgs, ensure_ascii=False))
            return True
    return False


def conversation_problems(msgs, sample=3):
    """会话消息数组的**硬性结构**问题清单（空列表 = 可用）。

    为什么需要本地先查一遍：API 端对坏结构只会回一句晦涩的话。实测把"折叠档案 sidecar"
    当成会话加载后，messages 变成档案条目（没有 `role`），此后每次请求都是
    `422 ... messages[0]: missing field 'role'`，而面板上只看到一串英文报错；
    保存出来的历史同样是坏的，用户以为"保存失败"。本地查一次 O(n) 的字典查找，
    换来一句能直接照做的提示。
    """
    if not isinstance(msgs, list) or not msgs:
        return ["不是非空的消息数组"]
    problems = []
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            problems.append(f"messages[{i}] 不是对象")
        elif not m.get("role"):
            problems.append(f"messages[{i}] 缺少 role")
        if len(problems) >= max(1, int(sample)):
            break
    return problems


# ─────────────────────── 三层上下文管理总入口（L0 → L1 → L2） ───────────────────────

def _parse_trim_notice(content):
    """解析问题文本前缀里的 [Context trimmed] 累积计数。"""
    if not content.startswith("[Context trimmed]"):
        return 0
    try:
        return int(content.split("已删除", 1)[1].split("条", 1)[0].strip())
    except (IndexError, ValueError):
        return 0


def render_prune_notice(content, added):
    """给问题文本前缀生成/累加裁剪提示。

    反复裁剪时**累加**计数而不是重写：否则"删了 N 条"会被下一次裁剪抹掉，
    模型只看到最后一次的数字，反而低估自己已经丢了多少历史（实测踩到）。
    """
    total = _parse_trim_notice(str(content or "")) + int(added)
    rest = str(content or "")
    if rest.startswith("[Context trimmed]"):
        idx = rest.find("\n\n")
        rest = rest[idx + 2:] if idx != -1 else ""
    return _PRUNE_NOTICE.format(n=total) + "\n\n" + rest


def _notice_once(conv, kind, emit, payload):
    """同一轮内同类提示只发一次。

    一轮里有 N 次工具调用就有 N 次 API 调用，也就有 N 次 _prep_context，每次都可能命中
    "跳过/失败"分支。没有闸门时面板会被同一句话刷屏（实测被"压缩失败"刷满整屏）。
    """
    from .compaction import cstate
    st = cstate(conv)
    turn = st.get("_user_turn") or 0
    key = f"_notice_once_{kind}"
    if st.get(key) == turn:
        return
    st[key] = turn
    emit(payload)


def _finalize_prune(cap, q_idx, conv, emit):
    """收紧到 cap 以内：尝试最后一次硬裁剪，返回 (new_q_idx, removed)。"""
    new_q, removed = _prune_messages(cap, q_idx, conv)
    if removed:
        emit({"type": "pruned", "removed": removed})
    return new_q, removed


def _prep_context(cap, q_idx, conv, summarize_fn, emit, force=False):
    """每次 API 调用前的上下文准备，严格按 L0 → L1 → L2 顺序。

    - L0 去重：无条件执行（零成本、零信息损失）
    - L1 摘要：达阈值且未撞频率预算/失败退避时，分块滚动摘要后一次性替换
    - L2 硬裁剪：L1 未执行/失败/仍超限时的兜底（绝不因频率预算而失效）

    `summarize_fn(prefix, block_msgs)` 由薄壳注入；为 None 表示本轮不做 L1。
    `force=True` 只影响 L1 的排期闸门（用户显式 `/compact`），**cap 永远按正常 L2 口径传**：
    `cap=0` 会把 L2 变成"删光所有可删组"，实测在低于阈值时直接把历史清空（BUG 30）。
    """
    # 延迟 import：compaction 依赖本模块的 _find_groups（叶子），模块级互相 import 会成环
    from .compaction import (apply_compaction, compaction_span, cstate, dedup_tool_results,
                             failure_hint, log_compaction_failure, log_compaction_success,
                             run_rolling_summary, should_compact, split_blocks)
    k = _compact_k()
    # 记账一律走 cstate：conv 每次 chat() 都是新的，直接写在 conv 上会每轮归零
    st = cstate(conv)

    size_before_dedup = conv.get("size")
    replaced = dedup_tool_results(conv, q_idx)
    if replaced:
        emit({"type": "deduped", "replaced": replaced, "saved": st.get("_dedup_saved") or 0,
              "before": size_before_dedup, "after": conv.get("size")})

    if summarize_fn is None:
        return _finalize_prune(cap, q_idx, conv, emit)

    ok, reason = should_compact(conv, force=force)
    if not ok:
        if reason in ("interval", "budget", "backoff", "failed"):
            _notice_once(conv, f"skip_{reason}", emit,
                         {"type": "compact_skipped", "reason": reason,
                          "retry_in_turns": max(0, (st.get("_compact_retry_turn") or 0)
                                                - (st.get("_user_turn") or 0)),
                          "fails": st.get("_compact_fails") or 0,
                          "max_fails": k["max_fails"]})
        return _finalize_prune(cap, q_idx, conv, emit)

    # 这里**刻意不再有**"同一轮只压一次"的 token 闸门：它的 key 只能是 (轮次, size) 或 id(conv)，
    # 两者都会误判 —— conv 是每次 chat() 新建的临时 dict（上一个被回收后 id 会被复用），
    # 而不变的轮次+不变的上下文会让"显式 /compact 再来一次"静默变成无操作。
    # 真正防重复的是：成功→`_compact_last_turn`（interval）、失败→`_compact_retry_turn`（backoff）、
    # 中断→`retry_turn = turn + 1`；而 force（用户显式 /compact）本就该允许再试一次。

    span = compaction_span(conv, q_idx)
    if span is None:
        _notice_once(conv, "skip_no_span", emit, {"type": "compact_skipped", "reason": "no_span"})
        return _finalize_prune(cap, q_idx, conv, emit)

    start, end = span
    groups = [g for g in _find_groups(q_idx, conv) if start <= g[0] and g[1] <= end]
    if not groups:
        _notice_once(conv, "skip_no_span", emit, {"type": "compact_skipped", "reason": "no_span"})
        return _finalize_prune(cap, q_idx, conv, emit)
    span_msgs = [[conv["messages"][i] for i in range(g[0], g[1])] for g in groups]
    blocks = split_blocks(span_msgs, [g[2] for g in groups], k["block_tokens"])
    before_size = conv.get("size")

    err = {}
    emit({"type": "compacting", "before": before_size, "blocks": len(blocks)})
    result = run_rolling_summary(
        blocks, summarize_fn,
        on_error=lambda why, idx: err.update(reason=why, block=idx))
    if result is None:
        why = err.get("reason") or "summarizer_failed"
        turn = st.get("_user_turn") or 0
        if why == "interrupted":
            # 用户按 Esc：不是摘要器的错，不能计入失败退避
            # （否则按三次 Esc 就把 L1 停用了）；但本轮内也不再重试
            st["_compact_retry_turn"] = turn + 1
            _notice_once(conv, "skip_interrupted", emit,
                         {"type": "compact_skipped", "reason": "interrupted"})
            return _finalize_prune(cap, q_idx, conv, emit)
        fails = (st.get("_compact_fails") or 0) + 1
        st["_compact_fails"] = fails
        backoff = min(k["min_rounds"] * (2 ** (fails - 1)), k["max_backoff"])
        st["_compact_retry_turn"] = turn + backoff
        disabled = fails >= k["max_fails"]
        log_compaction_failure(why, {"turn": turn, "fails": fails, "block": err.get("block"),
                                     "blocks": len(blocks), "before": before_size,
                                     "disabled": disabled})
        _notice_once(conv, "compact_failed", emit, {
            "type": "compact_failed", "reason": why, "block": err.get("block"),
            "attempt": fails, "max_attempts": k["max_fails"], "disabled": disabled,
            "retry_in_turns": 0 if disabled else backoff,
            "hint": failure_hint(why, disabled=disabled), "fallback": "prune"})
        return _finalize_prune(cap, q_idx, conv, emit)

    # 注意：不要在这里额外 record_archive —— apply_compaction 内部会记录一次
    # （此前两处都记，导致每次压缩产生两条重复档案，实测踩到）
    new_q, removed = apply_compaction(conv, q_idx, start, end, result["summary"])
    st["_compact_count"] = (st.get("_compact_count") or 0) + 1
    st["_compact_last_turn"] = st.get("_user_turn") or 0
    st["_compact_fails"] = 0          # 成功即清退避（否则一次偶发失败会一直压着后续压缩）
    st["_compact_retry_turn"] = None

    after_size = conv.get("size") or 0
    reclaimed = (before_size - after_size) / max(1, before_size)
    # 成功也要留证据：prefix_stable/(blocks-1) 是"整块丢弃"的唯一验收指标，
    # 面板提示会随滚动消失（与"失败必须落盘"同一个教训）。
    log_compaction_success({
        "blocks": result["blocks"], "removed": removed,
        "before": before_size, "after": after_size,
        "summary_chars": len(result["summary"]),
        "prefix_stable": result.get("prefix_stable", 0),
        "prefix_miss": result.get("prefix_miss", 0),
        "prefix_dropped": result.get("prefix_dropped", 0)})
    if reclaimed < k["min_reclaim"]:
        # 压缩本身也要花钱：收益太低就靠 L2 把上下文真正压到 cap 以内
        new_q, pruned = _finalize_prune(cap, new_q, conv, emit)
        emit({"type": "compacted", "removed": removed + pruned, "before": before_size,
              "after": conv.get("size") or 0, "summary_chars": len(result["summary"]),
              "blocks": result["blocks"], "low_reclaim": True,
              "prefix_stable": result.get("prefix_stable", 0),
              "prefix_dropped": result.get("prefix_dropped", 0)})
        return new_q, removed + pruned

    if (conv.get("size") or 0) > cap:
        new_q, pruned = _finalize_prune(cap, new_q, conv, emit)
        removed += pruned
    emit({"type": "compacted", "removed": removed, "before": before_size,
          "after": conv.get("size") or 0, "summary_chars": len(result["summary"]),
          "blocks": result["blocks"],
          "prefix_stable": result.get("prefix_stable", 0),
          "prefix_dropped": result.get("prefix_dropped", 0)})
    return new_q, removed
