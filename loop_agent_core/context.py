"""上下文管理：append/分组/裁剪/回滚。纯函数，只操作传入的 conv dict。"""
import json
import os

from .config import _is_local_backend, cfg
from .tokens import estimate_tokens

_PRUNE_NOTICE = "[Context trimmed] 为控制上下文已删除 {n} 条较早消息（工具调用及结果等）。如需信息，请用 read_file 重新读取相关文件。"


def _prune_cap():
    """Per-backend prune threshold. Local: 50% of model ctx. DeepSeek: 8000. Both env-overridable."""
    if _is_local_backend():
        env = os.environ.get("PRUNE_CAP_LOCAL")
        if env:
            return int(env)
        return max(1024, cfg["ctx"] // 2)
    return int(os.environ.get("PRUNE_CAP_DS", "8000"))


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
    old_notices = [i for i, m in enumerate(msgs) if _is_trim_notice(m)]
    if old_notices:
        conv["size"] -= sum(estimate_tokens(json.dumps(msgs[i], ensure_ascii=False)) for i in old_notices)
        msgs[:] = [m for i, m in enumerate(msgs) if i not in old_notices]
        new_q -= sum(1 for i in old_notices if i < new_q)
    note = {"role": "user", "content": _PRUNE_NOTICE.format(n=removed)}
    msgs.insert(new_q, note)
    conv["size"] += estimate_tokens(json.dumps(note, ensure_ascii=False))
    return new_q + 1, removed


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
