"""上下文压缩：L0 重复工具结果去重 + L1 分块滚动摘要 + 档案回滚。

三层分工（每层都是纯函数，只操作传入的 conv dict；不 import runner/薄壳）：
- L0 `dedup_tool_results()`：逐字节重复的工具结果只留最后一份，更早的改写成占位符。
  只改 content，不动消息位置与 tool_call_id，因此不破坏 assistant(tool_calls) ↔ tool 配对。
- L1 `run_rolling_summary()` / `apply_compaction()`：按 `_find_groups()` 的整组边界装箱分块，
  逐块滚动摘要（前序摘要作为稳定前缀，当前块置末尾），全部成功后才一次性替换。
- L2 仍是 context.py 的 `_prune_messages()`，由 context.py 的 `_prep_context()` 兜底。

行业依据（阈值/分块/频率预算的由来）：arXiv 2605.23296 —— 摘要保留率随输入增长断崖下跌
（输入 2k 保留 ~48%，96k 只剩 ~3%），故单次摘要输入必须有硬上限；摘要输出与输入/提示词
近乎无关，故输出规模只能靠结构性手段（max_tokens + 块数）控制；同步压缩可占端到端耗时
最高 62%，故必须有频率预算。
"""
import hashlib
import json
import os
import time

from .config import WORK_DIR, _compact_k
from .context import _find_groups
from .tokens import estimate_tokens

_SUMMARY_HEADINGS = ("## 用户目标", "## 已完成", "## 关键决策与结论", "## 改动的文件", "## 未决问题与下一步")

_SUMMARY_MESSAGE_HEADER = (
    "[上下文摘要] 为控制上下文，较早的历史已被折叠为下面的摘要。"
    "摘要可能遗漏细节：需要原文时调用 recall_archive(query)，不要凭摘要臆测、也不要因为缺少细节就重复执行已完成的工具调用。"
)


# ─────────────────────────── 通用小工具 ───────────────────────────

def _brief(v, limit=120):
    """参数值的单行短摘要（用于占位符文案，避免回显整段内容）。"""
    s = str(v).replace("\n", "\\n")
    return s if len(s) <= limit else s[:limit] + "…"


def _msg_tokens(m):
    return estimate_tokens(json.dumps(m, ensure_ascii=False))


def _coerce_summary(result):
    """摘要器返回值归一化为 (text, digest)。兼容 dict 与纯文本两种返回。"""
    if isinstance(result, dict):
        text = result.get("summary")
        return text, (result.get("digest") or _digest(text or ""))
    if isinstance(result, str):
        return result, _digest(result)
    return None, ""


def _interrupted():
    """控制模块的中断位（延迟 import：control 是叶子，避免加载顺序耦合）。"""
    try:
        from .control import interrupt_event
        return interrupt_event.is_set()
    except Exception:
        return False


# ─────────────────────────── 跨轮持久记账 ───────────────────────────

def cstate(conv):
    """压缩记账的**持久层**。

    conv 是每次 chat() 新建的包装 dict（薄壳里 `{"messages": messages, "size": ...}`），
    所以直接写在 conv 上的计数器**每个用户轮都会归零** —— MIN_ROUNDS/MAX_PER_SESSION
    会退化成"每轮"口径，"距上次压缩不足最小轮次"永远不成立（实测踩到，是"频繁压缩"的成因之一）。
    薄壳把一个跨轮存活的 dict 挂在 conv["_compact_state"] 上，这里统一取它；
    没有该键时退回 conv 自身（单元测试可直接写 conv，生产路径行为不变）。
    """
    st = conv.get("_compact_state")
    return st if isinstance(st, dict) else conv


def next_turn(conv):
    """用户轮次自增并返回（走 cstate，保证跨 chat() 调用连续）。"""
    st = cstate(conv)
    st["_user_turn"] = (st.get("_user_turn") or 0) + 1
    conv["_user_turn"] = st["_user_turn"]  # 镜像：兼容直接读 conv 的调用方/测试
    return st["_user_turn"]


def reset_failures(conv):
    """清空失败退避（/compact 手动重试、或压缩成功后调用）。"""
    st = cstate(conv)
    st["_compact_fails"] = 0
    st["_compact_retry_turn"] = None


# ─────────────────────────── 失败诊断（面板 + 日志） ───────────────────────────

def failure_hint(reason, disabled=False):
    """把失败原因翻成**可执行**的一句话（面板直接显示，不要求用户读源码）。"""
    r = str(reason or "")
    if r.startswith("api_error"):
        hint = "摘要接口调用失败，原始错误见 tmp/compaction_error.log"
    elif r.startswith("output_truncated"):
        hint = "摘要被输出上限截断：调大 COMPACT_MAX_TOKENS，或调小 COMPACT_BLOCK_TOKENS"
    elif r.startswith("missing_headings"):
        hint = "模型没按固定小标题输出：调小 COMPACT_BLOCK_TOKENS，或换更稳的模型"
    elif r.startswith("too_long"):
        hint = "摘要异常长：检查 COMPACT_MAX_TOKENS_HARD"
    elif r.startswith("too_short") or r in ("empty", "no_result", "not_text", "no_blocks",
                                            "summarizer_failed"):
        hint = "摘要返回空/过短：确认摘要后端（与主对话同一后端）是否可用"
    else:
        hint = "已回退 L2 硬裁剪"
    if disabled:
        hint += "；连续失败已达上限，本会话不再自动压缩（/compact 可手动重试）"
    return hint


def skip_label(reason):
    """跳过原因的**用户可见**文案（TUI 与 /compact 共用一份，避免两处各写一遍走样）。"""
    return {
        "interval": "距上次压缩不足最小轮次",
        "budget": "已达本会话压缩次数上限",
        "backoff": "失败退避中",
        "failed": "连续失败已停用自动压缩（/compact 可手动重试）",
        "interrupted": "用户中断",
        "no_span": "没有可压缩的完整轮次",
        "none": "压缩已关闭（COMPACT_ENABLED=0）",
        "no_size": "还没有 token 记账",
    }.get(str(reason), str(reason))


def failure_log_path(path=None):
    """失败日志落盘位置（默认 tmp/compaction_error.log，与 api_error.log 同目录）。

    `COMPACT_ERROR_LOG` 可覆盖：测试必须重定向走，否则跑一次测试就往真实诊断日志灌一批假失败。
    """
    if path:
        return path
    env = os.environ.get("COMPACT_ERROR_LOG")
    if env:
        return env
    return os.path.join(WORK_DIR, "tmp", "compaction_error.log")


def _write_compaction_log(rec, path=None):
    """一行 JSON 追加进压缩日志（失败与成功共用同一个文件，靠 `result` 区分）。"""
    target = failure_log_path(path)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return rec


def log_compaction_failure(reason, detail, path=None):
    """把失败原因 + 上下文追加进日志。

    TUI 下 stdout 会被界面覆盖，`print` 出来的原始异常等于丢掉（本次定位根因就卡在这里）。
    失败必须留可复查的证据。
    """
    rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": str(reason)}
    if isinstance(detail, dict):
        rec.update(detail)
    else:
        rec["detail"] = str(detail)
    return _write_compaction_log(rec, path)


def log_compaction_success(detail, path=None):
    """成功压缩的**证据**一条（同文件，靠 `result="ok"` 与失败记录区分）。

    为什么成功也要落盘：`prefix_stable / (blocks - 1)` 是"整块丢弃"改动的唯一验收指标，
    而 TUI 里那条提示会随滚动消失 —— 与"失败必须落盘"是同一个教训
    （`failure_hint` 里也写着去这个文件复查）。
    """
    rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "result": "ok"}
    if isinstance(detail, dict):
        rec.update(detail)
    else:
        rec["detail"] = str(detail)
    return _write_compaction_log(rec, path)


# ─────────────────────────── L0：重复工具结果去重 ───────────────────────────

def tool_call_key(name, arguments):
    """工具调用的规范化 key：解析 JSON 后按键排序，参数不同即 key 不同。

    因此 read_file(path=x) 与 read_file(path=x, function="f")、不同 start_line/end_line
    区间、不同路径都是不同的 key —— 部分读取的内容本来就不同，绝不能被合并。
    """
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return f"{name}|{raw}"
    try:
        norm = json.dumps(parsed, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        norm = str(parsed)
    return f"{name}|{norm}"


def result_digest(content):
    """结果内容指纹。只比较指纹，不把原文常驻在索引里。"""
    return hashlib.sha256(str(content).encode("utf-8")).hexdigest()


def dedup_key(name, arguments, content):
    """双条件：调用参数 key + 结果内容指纹，两者都相同才算重复。"""
    return (tool_call_key(name, arguments), result_digest(content))


def placeholder_text(name, arguments, latest_idx, latest_len=None):
    """重复工具结果的占位符。

    策略 A（刻意保守）：明确告诉模型"内容已省略、需要时重新调用"，不做"保留前 N 行"的
    伪完整截断 —— 宁可让模型知道自己缺什么，也不要让它以为自己有什么。
    """
    try:
        parsed = json.loads(arguments if isinstance(arguments, str) else str(arguments))
        if isinstance(parsed, dict) and parsed:
            args_desc = ", ".join(f"{k}={_brief(v, 60)}" for k, v in list(parsed.items())[:4])
        else:
            args_desc = _brief(arguments, 120)
    except (json.JSONDecodeError, TypeError):
        args_desc = _brief(arguments, 120)
    size = f"，原结果 {latest_len} 字符" if latest_len else ""
    return (f"[已省略] {name}({args_desc}) 的结果与本次会话中更晚的第 {latest_idx} 条消息逐字节相同{size}；"
            f"完整内容已从上下文移除以节省空间。需要细节请重新调用 {name}。")


def _recompute_size(conv):
    """按当前 messages 精确重算 size。

    必须用**整包** json.dumps 口径，而不是逐条相加：JSON 数组的分隔符/转义在两侧切分不同，
    逐条相加会漂移 1~数 token（实测踩到）。整包口径与 chat_impl 首次初始化 size 的算法一致。
    """
    conv["size"] = estimate_tokens(json.dumps(conv["messages"], ensure_ascii=False))
    return conv["size"]


def dedup_tool_results(conv, q_idx):
    """把 messages[1:q_idx] 内逐字节重复的工具结果只留最后一份，返回被替换的条数。

    不动消息位置/tool_call_id，只改 content；`conv["size"]` 按差额扣减，
    `conv["_dedup_saved"]` 累加本次省下的 token。
    """
    k = _compact_k()
    if k["dedup_enabled"] <= 0:
        return 0

    msgs = conv["messages"]
    calls = {}
    for m in msgs:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                calls[tc.get("id")] = (fn.get("name") or "", fn.get("arguments") or "")

    last = {}   # key -> (idx, content)
    for i in range(1, min(q_idx, len(msgs))):
        m = msgs[i]
        if m.get("role") != "tool":
            continue
        info = calls.get(m.get("tool_call_id"))
        if info is None:
            continue
        content = str(m.get("content") or "")
        if len(content) < k["dedup_min_chars"]:
            continue
        last[dedup_key(info[0], info[1], content)] = (i, content)
    if not last:
        return 0

    size_before = conv.get("size") or 0
    replaced = 0
    for i in range(1, min(q_idx, len(msgs))):
        m = msgs[i]
        if m.get("role") != "tool":
            continue
        info = calls.get(m.get("tool_call_id"))
        if info is None:
            continue
        content = str(m.get("content") or "")
        if len(content) < k["dedup_min_chars"]:
            continue
        key = dedup_key(info[0], info[1], content)
        latest_idx, latest_content = last[key]
        if latest_idx == i:
            continue
        ph = placeholder_text(info[0], info[1], latest_idx, len(latest_content))
        if estimate_tokens(ph) >= estimate_tokens(content):
            continue  # 占位符不比原文省 → 保留原文
        m["content"] = ph
        replaced += 1

    if replaced:
        _recompute_size(conv)
        # 实际省下的 token 用精确重算值相减，避免增量近似漂移
        st = cstate(conv)
        st["_dedup_saved"] = (st.get("_dedup_saved") or 0) + max(0, size_before - conv["size"])
    return replaced


# ─────────────────────────── 阈值与频率预算 ───────────────────────────

def compaction_cap():
    """L1 触发阈值口径：复用 context._prune_cap()，不新增第二套 ctx 计算。"""
    from .context import _prune_cap
    k = _compact_k()
    return max(1, int(_prune_cap() * k["trigger_ratio"]))


def should_compact(conv, force=False):
    """是否应执行 L1。返回 (是否, 跳过原因)；
    原因 ∈ {"none","no_size","below","failed","backoff","interval","budget"}。

    频率预算只约束 L1，不影响 L2 硬裁剪兜底（由 _prep_context 保证）。

    `force=True`（用户显式 `/compact`）：只跳过**排期类**闸门（阈值/间隔/退避/硬停），
    会话次数上限 `COMPACT_MAX_PER_SESSION` 仍然生效（那是成本硬闸门）。

    失败退避是必须的：`_user_turn` 只在一轮**开始时**自增，所以一轮内的 N 次工具调用
    （N 次 API 调用）看到的是**同一个** turn。没有退避时，一次失败会在这一轮里被重试 N 次、
    每次都把 4~5 个 8k token 的块重新喂一遍（实测面板被"压缩失败"刷屏 + 白烧 token）。
    """
    k = _compact_k()
    st = cstate(conv)
    if k["compact_enabled"] <= 0:
        return False, "none"
    if conv.get("size") is None:
        return False, "no_size"
    if not force and conv.get("size") < compaction_cap():
        return False, "below"
    n = st.get("_compact_count") or 0
    if n >= k["max_per_session"]:
        return False, "budget"
    if force:
        return True, ""
    turn = st.get("_user_turn") or 0
    # 连续失败硬停：确定性失败（如输出被截断）重试多少次都一样，只能靠人介入（/compact、调旋钮）
    if (st.get("_compact_fails") or 0) >= k["max_fails"]:
        return False, "failed"
    retry_at = st.get("_compact_retry_turn")
    if retry_at is not None and turn < retry_at:
        return False, "backoff"
    last = st.get("_compact_last_turn")
    if last is not None and (turn - last) < k["min_rounds"]:
        return False, "interval"
    return True, ""


def compaction_span(conv, q_idx):
    """可摘要区间 [start, end)。

    按 `_find_groups()` 的**整组**边界取，工具链永不切半；保留索引 1 起（system 固定在 0）；
    保留最近 keep_rounds 个用户提问及其之后的内容；绝不包含当前问题 q_idx 本身。
    无可摘要内容时返回 None。
    """
    if q_idx <= 1:
        return None
    groups = _find_groups(q_idx, conv)
    if not groups:
        return None
    msgs = conv["messages"]
    keep = int(_compact_k()["keep_rounds"])
    user_idx = [i for i in range(1, q_idx) if msgs[i].get("role") == "user"]
    if keep > 0 and len(user_idx) > keep:
        cut = user_idx[-keep]
    else:
        cut = q_idx  # 轮次不够 keep 轮：能压的都压，仍保留 q_idx 本身
    # 末尾必须**吸附到组边界**：否则某一轮的 user 被保留、其后的 assistant 被抽进摘要区间，
    # 反而留下"当前问题跟在 assistant 后面"的错位历史（实测踩到）。
    cand = [g for g in groups if g[1] <= cut]
    if not cand:
        return None
    # 起点也必须落在"某轮的第一个 user"上：否则该轮的 user 留在上下文里、assistant/tool 被摘要带走，
    # 保留部分的语义就断了。单个 user 消息在 _find_groups 里是独立组，故可直接逐个前移。
    # 注意必须允许一直前移到最后一个候选（连续多条 user，如记忆注入 + 问题合并失败时）：
    # 若因为 len(cand) == 1 就停在一条 user 组上，等于把那条 user 留给上下文而带走其后的 assistant。
    while msgs[cand[0][0]].get("role") != "user":
        cand.pop(0)
        if not cand:
            return None
    start = cand[0][0]
    end = cand[-1][1]
    if end > q_idx:
        end = q_idx
    if end - start < 2:  # 单条不值得摘要（压缩本身也要花钱）
        return None
    return start, end


# ─────────────────────────── L1：分块 + 滚动摘要 ───────────────────────────

def split_blocks(span_msgs, group_sizes, block_tokens):
    """按整组边界贪心装箱成块。单个组自身超过上限时独占一块（不硬切工具链）。"""
    blocks = []
    cur = []
    cur_size = 0
    for size, group in zip(group_sizes, span_msgs):
        if cur and cur_size + size > block_tokens:
            blocks.append(cur)
            cur, cur_size = [], 0
        cur.extend(group)
        cur_size += size
    if cur:
        blocks.append(cur)
    return blocks


def _digest(text):
    """摘要指纹：归一化空白后取 sha256 前 16 位（前缀逐字稳定 → prefix-cache 友好）。"""
    return hashlib.sha256("".join(str(text or "").split()).encode("utf-8")).hexdigest()[:16]


def build_block_prompt(prefix_digest, block_msgs):
    """单块的摘要请求。

    布局刻意与论文的 prefix-aware target-at-end 一致：前序累计摘要（PREFIX_DIGEST）保持
    逐字稳定以命中 prefix cache，当前块包裹在 <TARGET_BLOCK> 里置于**末尾**（注意力最强处）。
    顺序执行（非并行）：本地 llama.cpp 单槽并行无收益，且顺序天然保持因果。
    """
    from .prompts import SUMMARIZER_SYSTEM_PROMPT
    prefix = prefix_digest.strip() or "(无：这是第一块，前面没有更早的历史)"
    block = json.dumps(block_msgs, ensure_ascii=False, indent=1)
    return [
        {"role": "system", "content": SUMMARIZER_SYSTEM_PROMPT},
        {"role": "user", "content":
            "PREFIX_DIGEST（此前各块已生成的累计摘要，供衔接，不要重复它的内容）：\n"
            f"{prefix}\n\n"
            "TARGET_BLOCK（本次只总结这一块，保留因果顺序）：\n"
            "<TARGET_BLOCK>\n" + block + "\n</TARGET_BLOCK>\n\n"
            "只输出这一块的摘要正文（沿用固定小标题）。"},
    ]


def summary_reject_reason(text):
    """摘要不合格的**具体**原因；合格返回 None。

    必须给出原因而不是布尔值：`compact_failed` 只报 "summarizer_failed" 时，用户和日志都
    看不出到底是接口报错、输出被截断，还是模型没按小标题写（实测踩到：真实原因是
    max_tokens 截断，却花了很久才定位）。
    """
    if not isinstance(text, str):
        return "not_text"
    body = text.strip()
    if not body:
        return "empty"
    k = _compact_k()
    if estimate_tokens(body) < 20:
        return "too_short"
    # 输出本来就被 max_tokens（可升到 max_tokens_hard）封顶，超了只可能是摘要器乱返回
    if estimate_tokens(body) > k["max_tokens_hard"]:
        return f"too_long(>{k['max_tokens_hard']}tok)"
    missing = [h for h in _SUMMARY_HEADINGS if h not in body]
    if missing:
        return "missing_headings:" + "/".join(h.lstrip("# ") for h in missing)
    return None


def validate_summary(text):
    """摘要质量门槛：非空、长度合理、含全部固定小标题（防止返回空/解说/截断）。"""
    return summary_reject_reason(text) is None


_PREFIX_SEP = "\n\n---\n\n"       # 累计摘要各块之间的分隔（逐字固定，缓存友好的前提）
_PREFIX_OMITTED = "…（更早内容已省略）"


def bounded_prefix_parts(parts, budget_tokens):
    """把分块累计摘要压到 token 预算内，返回 (前缀, 被丢弃的块数)。

    **必须有界**：否则前缀随块数线性增长（实测 385→770→1155→…），
    "单次摘要输入不超过 COMPACT_BLOCK_TOKENS"这条硬约束就形同虚设。
    保留**尾部**（越近的工作对后续延续越重要），首部只留一个省略标记。

    **丢弃粒度是整块，而不是字符偏移**（2026-10-12 改）：前缀缓存命中的是最长公共
    token 前缀，按任意字符偏移从头部切会让每次调用的前缀**第一个 token 就变** →
    相邻两块之间必然全部 miss，`build_block_prompt` 那句"前缀逐字稳定以命中
    prefix cache"实际落空。改为整块丢弃后，没发生丢弃的相邻两块前缀逐字相接
    （不变量由 `run_rolling_summary` 计入 prefix_stable，回归用例
    test_prefix_stability_invariant 守住），只在真的丢块时付一次 miss。

    单个块摘要自身超预算时**整块保留**：它的规模已被 max_tokens_hard 封顶、
    不会无界增长，而"切半块"恰好破坏了本函数存在的理由。
    """
    kept = [p for p in parts if p]
    dropped = 0
    while len(kept) > 1 and estimate_tokens(_PREFIX_SEP.join(kept)) > budget_tokens:
        kept.pop(0)
        dropped += 1
    prefix = _PREFIX_SEP.join(kept)
    if dropped:
        prefix = f"{_PREFIX_OMITTED}\n{prefix}"
    return prefix, dropped


def _report(on_error, reason, index):
    """把失败原因交给调用方（有回调时）；绝不在这里 print/emit —— 本模块是纯函数叶子。"""
    if on_error is None:
        return
    try:
        on_error(reason, index)
    except Exception:
        pass


def run_rolling_summary(blocks, summarize_fn, prefix_tokens=None, on_error=None):
    """逐块滚动摘要。任一块失败/校验不过/被中断 → 返回 None（整体放弃，不留半折叠状态）。

    每次调用的**总输入**受两个预算约束：当前块 ≤ COMPACT_BLOCK_TOKENS，
    前序摘要前缀 ≤ prefix_tokens（默认块上限的 1/4）。返回
    {"summary": 完整拼接, "digest": 前缀指纹, "blocks": n,
     "prefix_stable": 前缀逐字相接的次数, "prefix_miss": 前缀不接的次数,
     "prefix_dropped": 累计被整块丢弃的块数}。

    这三个字段是**证据**：恒有 prefix_stable + prefix_miss == blocks - 1，所以
    prefix_stable/(blocks-1) 就是摘要链的 prefix-cache 复用率。整块丢弃的改动若不生效
    （例如又有人改回按字符偏移切），prefix_stable 会立刻掉到 0。

    `on_error(reason, block_index)` 可选：把**具体**失败原因回传给调用方（事件/日志用）。
    """
    k = _compact_k()
    budget = int(prefix_tokens if prefix_tokens is not None
                 else max(200, k["block_tokens"] // 4))
    prefix = ""
    parts = []
    stable = 0   # 相邻块之间前缀逐字相接的次数（前缀缓存可复用）
    miss = 0     # 前缀没能逐字相接的次数（丢块造成的必然 miss）
    dropped = 0  # 累计被整块丢弃的块数（丢过的块不会回来，故等于已淘汰的块数）
    for index, block in enumerate(blocks):
        if _interrupted():
            _report(on_error, "interrupted", index)
            return None
        try:
            res = summarize_fn(prefix, block)
        except Exception as e:
            _report(on_error, f"api_error:{type(e).__name__}:{e}", index)
            return None
        # 摘要器可用 {"error": ...} 显式报错（薄壳用它传递截断/接口异常）
        if isinstance(res, dict) and res.get("error"):
            _report(on_error, str(res["error"]), index)
            return None
        if res is None:
            _report(on_error, "no_result", index)
            return None
        text, digest = _coerce_summary(res)
        reason = summary_reject_reason(text)
        if reason:
            _report(on_error, reason, index)
            return None
        text = text.strip()
        parts.append(text)
        prev = prefix
        prefix, d = bounded_prefix_parts(parts, budget)
        dropped += d
        if prev:
            if prefix.startswith(prev):
                stable += 1
            else:
                miss += 1
    if not parts:
        _report(on_error, "no_blocks", 0)
        return None
    return {"summary": _PREFIX_SEP.join(parts), "digest": _digest(prefix),
            "blocks": len(parts), "prefix_stable": stable, "prefix_miss": miss,
            "prefix_dropped": dropped}


def render_summary_message(summary_text):
    return f"{_SUMMARY_MESSAGE_HEADER}\n\n{summary_text}"


def apply_compaction(conv, q_idx, start, end, summary_text):
    """用一条摘要消息替换 messages[start:end]，返回 (new_q_idx, removed)。

    落点沿用 context.py 里已验证的规避法（`[Context trimmed]` 同款）：
    段首紧贴 system 时插入 system 摘要消息；否则把摘要**前置合并进段后第一条存在的 user 消息**，
    避免产生裸 user 消息夹在 assistant 之间触发 API 交替协议 400。
    """
    msgs = conv["messages"]
    end = min(end, q_idx)
    block = msgs[start:end]
    removed = len(block)
    removed_tokens = sum(_msg_tokens(m) for m in block)
    # 先记档案（含被替换的原文），再动 messages
    record_archive(conv, block)
    del msgs[start:end]

    content = render_summary_message(summary_text)
    size_before = conv.get("size") or 0
    inserted = 0
    if start == 1:
        # 段首紧贴 system：独立 system 摘要消息是安全的（不会造成交替协议问题）
        msgs.insert(1, {"role": "system", "content": content})
        added_tokens = _msg_tokens(msgs[1])
        shape = "system"
        inserted = 1
    else:
        nxt = next((i for i in range(start, len(msgs)) if msgs[i].get("role") == "user"), None)
        if nxt is None:
            msgs.insert(start, {"role": "system", "content": content})
            added_tokens = _msg_tokens(msgs[start])
            shape = "system"
            inserted = 1
        else:
            before = _msg_tokens(msgs[nxt])
            msgs[nxt]["content"] = content + "\n\n" + str(msgs[nxt].get("content") or "")
            added_tokens = _msg_tokens(msgs[nxt]) - before
            shape = "merged"  # 合并进已存在的消息 → 净条数变化为 0

    # 当前问题的新位置：删掉 removed 条，并在它之前插入 inserted 条摘要（只可能是 0 或 1）
    new_q = q_idx - removed + inserted
    if new_q < 0:
        new_q = 0
    if new_q >= len(msgs):
        new_q = len(msgs) - 1
    conv["size"] = max(0, size_before - removed_tokens + added_tokens)
    # 还原时需要的精确插入形态与 token 增量（插入在原文位置，故锚点就是 start）
    if conv.get("_archive"):
        conv["_archive"][-1].update(
            {"inserted": content, "shape": shape, "added_tokens": added_tokens,
             "removed_tokens": removed_tokens})
    return new_q, removed


# ─────────────────────────── 档案（可回滚 + 落盘） ───────────────────────────

def record_archive(conv, span_msgs):
    """把被替换的原始消息存进内存档案（可 /expand、可 recall_archive）。"""
    import copy
    k = _compact_k()
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "before_size": conv.get("size"),
        "after_size": None,
        "messages": copy.deepcopy(span_msgs),
    }
    archive = conv.setdefault("_archive", [])
    archive.append(entry)
    while len(archive) > 1 and _archive_chars(archive) > k["archive_max_chars"]:
        archive.pop(0)  # 超上限丢最旧
    return entry


def _archive_chars(archive):
    return sum(len(json.dumps(e.get("messages") or [], ensure_ascii=False)) for e in archive)


def latest_compaction(conv):
    archive = conv.get("_archive") or []
    return archive[-1] if archive else None


def expand_last(conv):
    """还原最近一次压缩：把摘要换回原始消息。成功返回 True。

    依赖 apply_compaction 落在档案里的 `inserted` / `shape`：
    - shape == "system"：摘要是一条独立消息，整体换回原文；
    - shape == "merged"：摘要被前置合并进某条 user 消息，按**精确前缀**剥离后把原文插回它前面。
    """
    entry = latest_compaction(conv)
    if not entry:
        return False
    original = entry.get("messages") or []
    inserted = entry.get("inserted") or ""
    shape = entry.get("shape") or "system"
    if not original or not inserted:
        return False

    msgs = conv["messages"]
    target = next((i for i, m in enumerate(msgs)
                   if isinstance(m.get("content"), str) and m["content"].startswith(_SUMMARY_MESSAGE_HEADER)),
                  None)
    if target is None:
        return False

    size_before = conv.get("size") or 0
    restored_tokens = sum(_msg_tokens(m) for m in original)
    # 撤掉当初记账时的 added_tokens，换回原文的 token 数（两者都是当时实测值，无需重算）
    conv["size"] = max(0, size_before - (entry.get("added_tokens") or 0) + restored_tokens)
    if shape == "system":
        msgs[target:target + 1] = original
    else:
        body = msgs[target]["content"]
        rest = body[len(inserted):] if body.startswith(inserted) else ""
        rest = rest[2:] if rest.startswith("\n\n") else rest
        msgs[target]["content"] = rest
        msgs[target:target] = original
    conv["_archive"] = (conv.get("_archive") or [])[:-1]
    return True


def find_archive(conv, query, index=-1, limit=3):
    """在档案里按关键词检索原始消息，返回 [(entry_idx, msg_idx, role, text), ...]。"""
    archive = conv.get("_archive") or []
    if not archive:
        return []
    q = (query or "").strip().lower()
    hits = []
    order = range(len(archive)) if index is None else [index]
    for ei in order:
        if not (-len(archive) <= ei < len(archive)):
            continue
        entry = archive[ei]
        for mi, m in enumerate(entry.get("messages") or []):
            text = str(m.get("content") or "")
            if q and q not in text.lower():
                continue
            hits.append((ei, mi, m.get("role"), text))
            if len(hits) >= limit:
                return hits
    return hits


def persist_archive(conv_or_list, path):
    """把内存档案落盘到 sidecar（失败静默：档案是附加能力，不阻断压缩）。

    接受 conv dict 或档案 list 本身，便于 TUI 直接传 _agent_messages。
    """
    archive = conv_or_list.get("_archive") if isinstance(conv_or_list, dict) else conv_or_list
    if not archive:
        return False
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(archive, f, ensure_ascii=False)
        return True
    except Exception:
        return False


def load_archive(conv_or_list, path):
    """从 sidecar 恢复档案，使 /expand 与 recall_archive 在 /load 后仍可用。

    接受 conv dict 或档案 list 本身：传 list 时**就地**填充（薄壳的会话档案是同一个 list 对象，
    而 apply/expand 都是就地修改，因此必须共用同一对象）。
    """
    try:
        if not os.path.exists(path):
            return False
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return False
        if isinstance(conv_or_list, dict):
            conv_or_list["_archive"] = data
        else:
            conv_or_list[:] = data
        return bool(data)
    except Exception:
        pass
    return False
