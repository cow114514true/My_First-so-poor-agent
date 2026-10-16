"""主循环与工具执行：_chat 实现。依赖经参数注入（工具表/delegate/默认事件），不碰薄壳可变全局。
API 调用一律用 conv["messages"]（修 worker 子 agent 把主线程历史发给模型的隔离 bug）。"""
import copy
import hashlib
import json
import os
import time
import types
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import _is_local_backend, cfg, WORK_DIR
from . import llm
from .trace import TURN_END_REASONS

# 模块级 client：OpenAI 兼容外观，实现是 llm 网关（见 loop_agent_core/llm.py）。
# 保留这个名字是因为它是**测试的注入点**（tests/test_runner_visibility.py 整体替换
# runner.client 来驱动真循环），也是历史上 `from .config import client` 的下游契约。
client = llm.get()
from .compaction import cstate, next_turn
from .control import interrupt_event
from .context import (_append_msg, _prep_context, _prune_cap, _prune_messages,
                      _rollback_tool_round, conversation_problems)
from .events import _default_on_event
from .llm import _usage_dict
from .schemas import tools
from .tokens import estimate_tokens
from .validation import validate_tool_result
from .xmlutil import (_is_garbage_content, _parse_xml_tool_calls, _strip_dsml,
                      _strip_toolcall_xml, _XML_TOOLCALL_RE)


def _digest_text(text, n=16):
    """文本指纹：trace 里存**摘要**而不是全文（全文在 llm_end 的 messages 里）。"""
    import hashlib
    s = str(text or "")
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:n]


def _tool_args_dict(tool):
    """从 tool_call 取出参数字典（坏 JSON 也不炸：回放要的是"当时喂了什么"）。"""
    try:
        return json.loads(tool.function.arguments)
    except (ValueError, TypeError, AttributeError):
        return {"_raw": str(getattr(getattr(tool, "function", None), "arguments", ""))[:4000]}


def _tool_error_code(result):
    """工具结果的错误码：None 表示正常返回。"""
    s = str(result or "")
    if s.startswith("[Denied"):
        return "denied"
    if not s.startswith("[Error]"):
        return None
    code = s[len("[Error]"):].strip().split(":", 1)[0].strip()
    return (code or "unknown")[:60]


def _tool_class(name):
    """工具的副作用类别（A/B/C）。未登记工具返回 "?" —— 只用于记录，
    真正的拒绝发生在回放引擎里（那里必须 fail-closed）。"""
    from .toolclass import TOOL_CLASS
    entry = TOOL_CLASS.get(name)
    return entry["class"] if entry else "?"


def _trace_ev(etype, **fields):
    """发一条 trace 事件（无活跃 trace 时是空操作）。供模块级函数使用。"""
    from . import trace
    return trace.emit_event({"type": etype, **fields})


def _limits(site=""):
    """记一条 limits（按 site+值去重，重复调用是空操作）。"""
    try:
        from . import trace
        return trace.emit_limits(site=site)
    except Exception:  # noqa: BLE001
        return None


def _rewrite_scope(name):
    """路径重写的适用面（structured / best_effort / None），仅记录用。"""
    from .toolclass import rewrite_scope_of
    try:
        return rewrite_scope_of(name)
    except Exception:  # noqa: BLE001 —— 只影响 trace 字段，不该拖垮工具轮
        return None


# ── 回放钩子（§4.5 / §8.2 / §8.3）──────────────────────────────────
# L1（`policy="cassette"`）：工具结果从 cassette 回吐，**不真执行**——真执行会写工作区、
# 打网络，那样跑出来的就不是"同一段历史再跑一遍"，而是"在另一台机器上重做一遍"，
# 失败复现与否都不再说明问题。
# L2（`policy="class"`）：B 类在 overlay 里真执行（§8.3）。隔离与路径映射都在
# `loop_agent_core/overlay.py`，这里只负责在**入参层**（§8.2.1）与**结算层**（§8.2.2）接上。
def _active_overlay():
    """当前回放的活动 overlay（非回放 / L1 回放返回 None）。"""
    try:
        from . import overlay as ov_mod
        return ov_mod.active_overlay()
    except Exception:  # noqa: BLE001
        return None


def _replay_plan(use_tool_calls):
    """回放中：为每个工具取一次供给决定。

    返回 `{call_id: LiveCall | str}`；非回放 / 无活页返回 `{}`。

    `str` 是**录制结果**，而且是 `result_full`——只把完整的那一份交回主循环，让主循环
    自己再截断一次，这正是 §4.5 要检验的那一步：现在的预算算出来的 `result_stored`
    必须和录制时的那一份一样。

    **重写也在这里做完**（`_replay_args_json`），而不是等到执行分支：`tool_call` 事件在
    执行之前就发出去，重写晚于它的话，"这次到底把路径改成了什么"就没有地方挂了——
    事件里只会留下一个 `written: null`，看起来像"重写没发生"，实际只是记账晚了一步。
    """
    try:
        from . import replay
    except Exception:  # noqa: BLE001
        return {}
    if replay.active_session() is None:
        return {}
    out = {}
    for tool in use_tool_calls:
        name = tool.function.name
        raw = str(getattr(tool.function, "arguments", "") or "")
        d = replay.decision_for(name, tool.id)
        if d is None:  # 理论上不可达：上面刚判过有活页
            continue
        if isinstance(d, replay.LiveCall):
            # 记下**逻辑**入参：真执行时送给工具的是另一份（overlay 路径），但断言面
            # 与屏幕事件看到的调用必须是模型当初发起的那个，否则回放会把历史改写掉。
            d.args = _tool_args_dict(tool)
            # 跨类重读（下面 `hit` 命中）时入参已经指到 overlay 里了，不需要再重写。
            if d.run_arguments is None:
                d.run_arguments = _replay_args_json(name, raw, d)
            out[tool.id] = d
            continue
        # 跨类污染（§8.2.3）：这个路径本次回放里已经被 B 类工具写过，录制的旧内容是
        # 写之前的，照吐就等于"读了但没看见自己刚写的东西"。改成真读 overlay 里那一份。
        hit = _overlay_read(name, _tool_args_dict(tool))
        if hit is not None:
            live = replay.LiveCall(name=name, call_id=tool.id,
                                   cls=replay.class_of(name),
                                   kind="live", args=_tool_args_dict(tool))
            live.rewritten = {"path": hit}
            live.rewrite = "overlay:read"
            live.run_arguments = _replay_args_json(name, raw, live)
            out[tool.id] = live
            continue
        out[tool.id] = d
    return out


def _overlay_read(name, args):
    """A 类要读的路径若已被本次回放写过 → overlay 内的真实文件路径，否则 None。"""
    try:
        from . import overlay as ov_mod
        return ov_mod.overlay_result_for(name, args or {})
    except Exception:  # noqa: BLE001 —— 读不到就退回落录播，不该让整轮工具失败
        return None


def _replay_args_json(name, arguments, live_call=None):
    """真执行时送给工具的入参 JSON：回放里是 overlay 重写过的那一份。

    返回改写后的 JSON 串；不需要改写时返回原串。**只返回字符串**，不改任何 tool 对象——
    会话历史里那条 assistant 消息的 `function.arguments` 必须保持逻辑路径，否则"模型当时
    发起的调用"会在回放中被悄悄改写成 overlay 路径（§8.2.1 只允许改执行用的那一份副本）。

    改写前的入参留在 `live_call.args`（正向重写前记的），改写后的留在 `live_call.rewritten`：
    现场只看到"结果对不上"时，这两个值一起才能说明重写到底做了什么。
    """
    try:
        args = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return arguments
    if not isinstance(args, dict):
        return arguments
    from . import overlay as ov_mod
    rewritten, note = ov_mod.rewrite_for_replay(name, args)
    if note is None:
        return arguments
    if live_call is not None:
        live_call.rewritten = rewritten
        live_call.rewrite = note
    return json.dumps(rewritten, ensure_ascii=False)


def _reverse_tool_result(result, tool_name):
    """结算层反向映射（§8.2.2）：出参里的 overlay 路径换回逻辑路径后再写 conv。"""
    try:
        from . import overlay as ov_mod
        return ov_mod.reverse_for_replay(result, tool_name=tool_name)
    except Exception:  # noqa: BLE001 —— 只影响文本形态，不该拖垮工具轮
        return result if isinstance(result, str) else str(result)


def _replay_verify_result(tool, result_full, stored, *, class_changed=False):
    """回放中对账：`truncate(result_full, 现在的预算)` 是否等于录制的 result_stored。

    `result_full` 必须是**工具自己的出参原文**（含 overlay 路径）；真执行的 B 类调用
    在账本里另记一份、且不与录制结果比截断预算：它的结果本来就与录制不同，那是 L2 的
    明文事实，不该伪装成"截断逻辑漂移"。
    """
    try:
        from . import replay
    except Exception:  # noqa: BLE001
        return
    session = replay.active_session()
    if session is None:
        return
    if class_changed:
        session.tools.live_detail.append({
            "call_id": tool.id, "name": tool.function.name,
            "result_full_chars": len(str(result_full or "")),
            "result_stored_chars": len(str(stored or ""))})
        return
    rec = session.cassette.tool_result(tool.id) or {}
    limits_recorded = session.cassette.limits_for(rec.get("request_index"))
    replay.attribute_truncation(result_full, stored, rec, trace_limits(), limits_recorded,
                                report=session.report)


def trace_limits():
    """当前生效的 limits 载荷（回放对账用）。"""
    try:
        from . import trace
        return trace.limits_payload()
    except Exception:  # noqa: BLE001
        return None


# 本地 Qwen 经常整轮只输出 thinking（reasoning_content）而 content 为空、也没工具调用 →
# 不是垃圾，是回合没走完。注入提示让模型继续，而不是掐断整回合。
_EMPTY_NUDGE = ("[Self-check] 你刚才只输出了思考过程，没有调用工具也没有给出回答。"
                "请继续：调用下一步需要的工具，或直接给出最终回答。")
# nudge 耗尽仍卡 → 准确说明是思考循环，不是乱码
# （不要再写死"换用 DeepSeek 后端"：实测云端后端同样会卡在只输出思考，
#   而这条文案正好在 DeepSeek 会话里出现，等于给了反向建议。）
_STUCK_MSG = ("[模型陷入思考循环：连续多轮只输出思考、没有工具调用或回答。"
              "本轮已中止，任务可能未完成——请重试、把要求说得更具体，或新开一个会话。]")
# 与 _STUCK_MSG 同因的**可见**提示：历史里存的那条消息以前只 return、不上屏，
# 用户看到的就是"agent 静默停止工作"。
_STUCK_NOTICE = "模型连续多轮只输出思考、没有工具调用也没有回答，已中止本轮（任务可能未完成）。"

# 工具轮次接近上限时注入收尾提示，不依赖模型自觉
_WRAPUP_NUDGE = ("[System] 已使用 {used}/{max} 轮工具调用。"
                 "如果核心需求已经实现，请现在输出完成报告（做了什么、改了什么、如何验证），"
                 "不要再继续探索或调用工具。仅当确有未完成的关键步骤时才继续。")


# ── notice 的稳定语义码（断言靠它，不靠文案）─────────────────────────
# 面板文案改一个字就会让断言全红——所以断言只认 code；文案随便改。
# 词表见 TRACE_REPLAY_BUILD.md §5.2，改名要走 cassette 格式迁移。
NOTICE_CODES = {
    "interrupted": ("warn", "interrupted",
                    "已中断（Esc）：本回合没有给出回答（此前已执行的工具结果仍保留在历史里）。"),
    "interrupted_partial": ("info", "interrupted_partial",
                            "已中断（Esc）：已保留本回合已生成的部分内容。"),
    "interrupted_empty": ("warn", "interrupted",
                          "已中断（Esc）：本回合没有给出回答。"),
    "stuck": ("warn", "stuck", _STUCK_NOTICE),
    "garbled": ("warn", "garbled", "模型输出被判为乱码，已中止本轮。请换个说法重试。"),
    "empty_nudge": ("info", "empty_nudge",
                    "模型这一轮只输出了思考过程，已要求它继续（{n}/3）。"),
    "wrapup_nudge": ("info", "wrapup_nudge",
                     "已用 {n}/{m} 轮工具调用，已要求模型收尾并给结论。"),
    "xml_toolcall_retry": ("info", "xml_toolcall_retry",
                           "模型把工具调用写进了思考但没能解析出来，正在重试。"),
    "fallback": ("warn", "fallback",
                 "模型多轮都没有产出可用的正文，已中止本轮。请换个说法重试。"),
    # 这条**不由 runner 发**：旧会话迁移器写的（`tools/trace_migrate.py`）。旧格式不记录
    # turn_end reason，迁移器不臆造一个看起来合理的 reason，只记一条说明。放进词表是因为
    # 断言面的判定口径是"这个词表认不认识"——词表外的 code 会让整份迁移产物加载即报
    # `CassetteFormatError`（实测），等于把"如实记录缺什么"变成了"产物坏了"。
    "migration_no_reason": ("info", "migration_no_reason",
                            "本次会话由旧格式迁移而来，不记录 turn_end reason。"),
    # 同样由迁移器写：旧语料里同一个 call_id 被复用（实测 3 次工具调用都叫 `xml0`），
    # 迁移器加 `#N` 后缀消歧——不加的话 cassette 的 call_id 索引会把它们挤成一条，
    # 全量实测 136 次调用凭空消失。这条 notice 是那份丢失的"证人"。
    "migration_dup_call_id": ("info", "migration_dup_call_id",
                              "旧会话里有被复用的 call_id，迁移时为消歧加了 #N 后缀。"),
}


def _dump_filtered(reason, snippet):
    """Diagnostic: dump what got filtered so we can see why (debug_filtered.txt).
    路径可用 FILTERED_LOG 覆盖：自检套件会跑到这些分支，别污染真实诊断日志。"""
    path = os.environ.get("FILTERED_LOG") or os.path.join(WORK_DIR, "debug_filtered.txt")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"=== {reason} ===\n{snippet}\n\n")
    except Exception:
        pass


def _usage_field(usage, name):
    """usage 可能是 SDK 对象、也可能是 dict；两种都取，取不到返回 None。"""
    if usage is None:
        return None
    v = getattr(usage, name, None)
    if v is None and isinstance(usage, dict):
        v = usage.get(name)
    return v


def _cache_hit_tokens(usage):
    """前缀缓存**命中**的 token 数；取不到返回 None。

    必须是 None 而不是 0：0 和"provider 根本没报这个字段"是两件事，混起来会把
    "没有统计"误判成"缓存完全没命中"。字段名各家不同是常态，所以逐个探：
    DeepSeek 用 prompt_cache_hit_tokens，OpenAI 兼容网关塞在
    prompt_tokens_details.cached_tokens，Anthropic 叫 cache_read_input_tokens。
    """
    for name in ("prompt_cache_hit_tokens", "cache_read_input_tokens"):
        v = _usage_field(usage, name)
        if isinstance(v, int):
            return v
    details = _usage_field(usage, "prompt_tokens_details")
    if details is not None:
        for name in ("cached_tokens", "cache_read"):
            v = getattr(details, name, None)
            if v is None and isinstance(details, dict):
                v = details.get(name)
            if isinstance(v, int):
                return v
    return None


def _usage_sample(usage, gap_s=None, model="", kind="chat"):
    """一次 API 调用的用量样本：缓存命中率 + 距上次调用的间隔。

    间隔是**实测 TTL 的原料**：攒够样本后按 gap_s 分桶看 hit_ratio 掉在哪一档，
    就是这台机器上真实的前缀缓存存活时间。没有它就无从判断"为了让前缀稳定而
    多留 token"到底值不值。
    """
    prompt = _usage_field(usage, "prompt_tokens")
    cached = _cache_hit_tokens(usage)
    ratio = None
    if isinstance(prompt, int) and prompt > 0 and isinstance(cached, int):
        ratio = round(min(cached, prompt) / prompt, 4)
    return {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind,
            "model": model,
            "prompt_tokens": prompt,
            "cached_tokens": cached,
            "completion_tokens": _usage_field(usage, "completion_tokens"),
            "total_tokens": _usage_field(usage, "total_tokens"),
            "hit_ratio": ratio,
            "gap_s": gap_s}


def _dump_cache(sample):
    """一行 JSON 落进 cache_usage.log（CACHE_LOG 可覆盖，自检套件不污染真实统计）。"""
    path = os.environ.get("CACHE_LOG") or os.path.join(WORK_DIR, "cache_usage.log")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _record_usage(usage, st, model="", kind="chat"):
    """用量记账：算间隔 → 回写 st → 落样本 → 返回样本。

    间隔的载体必须是**跨轮存活**的 st（cstate / 薄壳的 _compact_state）：挂在 conv 上
    会每轮归零，gap 恒为 None，就永远量不出 TTL。

    间隔**按 kind 分开记**：摘要调用与主对话是两个不同的前缀，共用一根时间戳会让
    "用户停了 5 分钟"被中间的摘要调用抹成几秒，把 TTL 量短（实测的坑：测出来的存活时间
    会明显偏保守，从而错误地得出"留前缀没意义"）。
    """
    now = time.monotonic()
    gaps = st.get("_last_call_ts") if isinstance(st, dict) else None
    if not isinstance(gaps, dict):   # 含旧版本留下的 float 形态：直接换掉
        gaps = {}
        if isinstance(st, dict):
            st["_last_call_ts"] = gaps
    last = gaps.get(kind)
    gap = None if last is None else round(now - last, 3)
    gaps[kind] = now
    sample = _usage_sample(usage, gap_s=gap, model=str(model or ""), kind=kind)
    _dump_cache(sample)
    return sample


def _dump_api_error(e, conv, kwargs):
    """API 400 时把错误正文 + 消息结构落盘（api_error.log），便于追查。
    含 assistant tool_calls 的 arguments（最常背锅：参数非合法 JSON 会被 DeepSeek 拒）。"""
    import time as _t
    try:
        msgs = conv.get("messages", [])
        tool_calls = [
            {"idx": i, "id": tc.get("id"), "name": tc.get("function", {}).get("name"),
             "arguments": tc.get("function", {}).get("arguments", "")[:2000]}
            for i, m in enumerate(msgs) if m.get("role") == "assistant" and m.get("tool_calls")
            for tc in m["tool_calls"]
        ]
        summary = {
            "time": _t.strftime("%Y-%m-%d %H:%M:%S"),
            "status": getattr(e, "status_code", None),
            "error": getattr(e, "body", None) or str(e)[:1000],
            "n_messages": len(msgs),
            "roles": [m.get("role") for m in msgs][-30:],
            "tool_calls": tool_calls,
        }
        with open(os.path.join(WORK_DIR, "api_error.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _append_thinking_turn(reasoning, conv):
    """把"只出思考、无正文"的那一轮**记进历史**（`content` 就是思考原文）。

    为什么必须记：以前这一轮被整个丢掉，于是 nudge 注入时末条仍是 user，只能
    **拼进上一条 user 消息**。两个后果都在真录的 trace 里看得见（`tmp/_probe_stuck/`）：

    - 用户的原话被改写了。四个请求摊开是 `user('干个活…')` → `user('[Self-check]…\\n\\n干个活…')`
      → 三遍自检叠在同一条 user 消息里（`messages_bytes` 122→292→462→632）。事后从
      `conv` 里**重建不出"用户到底说了什么"**，而"用户说的"是回放比对与最小化的锚。
    - 模型侧看到的是"用户从头到尾在抱怨它没产出"，而不是"我上一轮只想了没答"。
      DeepSeek 的 tool_calls 契约要求把 `reasoning_content` 回传，本身就说明思考是
      对话的一部分、不该凭空消失。

    记下这一轮之后，末条是 assistant，`_inject_user_nudge` 自然走"追加"分支——
    交替协议不靠合并来维持，而且 `reasoning::` 前缀让它一眼可分、不冒充模型正文。
    """
    text = (reasoning or "").strip()
    if not text:
        return
    _append_msg({"role": "assistant", "content": f"reasoning:: {text}"}, conv)


def _nudge_is_last(conv):
    """末条消息是不是我们注入的空 nudge（即"上一轮只思考、已被催过"）。

    只用它回答一个问题：**这次请求还该不该发**。判据取末条消息而不是回合号/计数，
    因为收尾段那个 `for final_round` 在"注入 nudge → continue"与"进入收尾段"之间
    是**同一个迭代号**，拿它当"新请求"的证据必然误判。
    """
    msgs = conv.get("messages") or []
    if not msgs or msgs[-1].get("role") != "user":
        return False
    return str(msgs[-1].get("content", "")).startswith(_EMPTY_NUDGE)


def _inject_user_nudge(text, conv):
    """注入一条 user 消息：末条已是 user 则合并（避免连续 user 违反 API 交替协议），否则追加。

    "末条是 user"通常只在**模型的这一轮没有产出**时出现，而那个情形现在由
    `_append_thinking_turn()` 先补上 assistant 轮，所以正常路径走的是追加分支。
    保留合并分支是给"既无正文也无思考"的乱码轮兜底：那种轮不值得记进历史，
    但提示仍要发出去。
    """
    msgs = conv["messages"]
    if msgs and msgs[-1].get("role") == "user":
        msgs[-1]["content"] = text + "\n\n" + str(msgs[-1].get("content", ""))
        conv["size"] = conv.get("size", 0) + estimate_tokens(text)
    else:
        _append_msg({"role": "user", "content": text}, conv)


def _xml_to_tool_calls(xml_calls):
    """把 _parse_xml_tool_calls 的结果转成 SimpleNamespace 工具对象列表（与主循环共用）。"""
    return [
        types.SimpleNamespace(id=f"xml{i}", type="function",
                              function=types.SimpleNamespace(name=c["name"], arguments=c["arguments"]))
        for i, c in enumerate(xml_calls)
    ]


_STORE_ARGS_THRESHOLD = 500  # write_file 参数超过这个长度才在 conv 里换成 $omitted 占位


def _store_tool_args(name, arguments):
    """写入 messages 的工具参数压缩：write_file 的 content 只留占位，避免整份文件内容撑爆本地窗口。

    占位必须是**可复原**的：回放时 conv 里那条 assistant 消息只剩占位，若占位里不带
    原始长度与哈希，就永远无法证明"这一轮发出去的参数"和录制时是同一条——回放只能
    相信它。所以这里存 `{"$omitted": {"len": N, "sha256": ...}}`，原文另存
    trace 里的 tool_call.arguments_raw，回放侧用两者互相校验（§4.5）。
    """
    if name == "write_file" and isinstance(arguments, str) and len(arguments) > _STORE_ARGS_THRESHOLD:
        try:
            d = json.loads(arguments)
            if isinstance(d, dict) and isinstance(d.get("content"), str):
                body = d["content"]
                d["content"] = {"$omitted": {
                    "len": len(body),
                    "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                }}
                _limits("store_tool_args")
                return json.dumps(d, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pass
    return arguments


_WORKER_FORBIDDEN = ("delegate_task", "recall", "remember", "recall_archive")  # worker 内禁用的工具
_SYNC_MAX = 8  # 同步工具并发上限（工具多为 I/O 密集，线程池并发即够）
_TOOL_TIMEOUT = 150  # 单工具结果超时（秒）：browser/ds_web 内部无兜底会卡死线程，这里兜底
_WORKER_TIMEOUT = 600  # 子 agent 超时（秒）：长任务给足 10 分钟，防误杀合法委托


def _worker_pool_size():
    """子 agent 并发上限：本地单槽 server 并行无收益（还会抢同一 ctx）；DeepSeek 无本地模型约束。"""
    return 1 if _is_local_backend() else 3


def _call_tool(tool, tool_call_map):
    """并发执行单个同步工具，返回 (call_id, result)。线程安全：不碰 conv/emit。"""
    name = tool.function.name
    try:
        args = json.loads(tool.function.arguments)
    except json.JSONDecodeError as e:
        return tool.id, f"[Error] Invalid JSON arguments: {e}\nRaw: {tool.function.arguments}"
    fn = tool_call_map.get(name)
    if fn is None:
        available = ", ".join(tool_call_map.keys())
        return tool.id, f"[Error] Unknown tool: '{name}'. Available tools: {available}"
    try:
        return tool.id, fn(**args)
    except Exception as e:
        return tool.id, f"[Error] Tool execution failed: {e}"


def _run_worker(task, delegate_fn):
    """在 worker 线程跑单个子 agent（阻塞 chat），结果回主线程结算。"""
    return delegate_fn(task)


def run_tools(use_tool_calls, emit, conv, tool_call_map, delegate_fn,is_worker=False):
    """执行一批工具调用：同步工具并发（线程池）+ delegate_task 子 agent 受控并发（异步队列）。
    并发阶段只收集结果（不碰 conv/emit，线程安全）；结算阶段按原调用顺序发事件、追加消息、结果自检。
    delegate_fn 由薄壳注入（运行时查壳命名空间，保留 monkeypatch 语义）。"""
    if interrupt_event.is_set():
        # 中断：回滚本工具轮（assistant tool_calls 与其 tool 结果），保持历史一致
        from . import trace
        trace.emit_event({"type": "interrupt", "cause": "user_esc", "phase": "pre_round"})
        _rollback_tool_round(conv, cause="interrupt")
        return

    results = {}  # call_id -> result
    _dur = {}  # call_id -> 执行耗时（ms）；trace 用，取不到就 None
    worker_meta = {}  # call_id -> task（结算阶段补发 worker_done）
    _worker_t0 = {}

    def _trace_call(tool, live_call=None):
        """记下工具的**发起**（含完整入参）：回放按这份入参决定吐结果还是重跑。

        `arguments_raw` 必须是**原始完整**参数：会话历史里的 assistant tool_calls 会被
        `_store_tool_args` 换成占位符（write_file 的大正文尤其），回放时正是靠这一份把
        真实入参还原回来（§4.5 两通道）。

        回放中真执行的调用额外标 `overlay`（§8.2.1）：`rewrite` 保留 `structured` /
        `best_effort` 的原义——它是**工具的分类属性**，写成"这次到底改没改"会让同一个工具
        在不同调用上出现不同取值，读 trace 的人再也分不清"这类工具就是这么重写的"还是
        "这一次重写命中了"。
        """
        from . import trace
        name = tool.function.name
        raw = str(getattr(tool.function, "arguments", "") or "")
        ev = {"type": "tool_call", "name": name, "tool_name": name,
              "call_id": tool.id,
              "class": _tool_class(name), "tool_class": _tool_class(name),
              "rewrite": _rewrite_scope(name),
              "arguments_raw": raw,
              "arguments": _tool_args_dict(tool),
              "args_digest": _digest_text(raw)}
        if live_call is not None:
            # 逻辑入参 + overlay 实际执行入参并录：这是"重写到底做了什么"的现场证据，
            # 只记改写后的一份，就没法再判断"重写是不是把路径改错了"。
            ov = _active_overlay()
            ev["overlay"] = {"exec": "live", "root": getattr(ov, "root", None),
                             "written": getattr(live_call, "rewritten", None),
                             "rewrite_detail": getattr(live_call, "rewrite", None)}
        trace.emit_record(ev)

    def _trace_result(tool, result, stored, cap, duration_ms=None, from_replay=False):
        """记下工具的**结果**：result_full 是喂给模型之外的那一份，result_stored 才是进
        上下文的那一份（可能被 cap 截断）。

        两通道分开记（§4.5）：回放要能重算 `truncate(result_full, limits_now)` 并与
        `result_stored` 比对——只记一份的话，截断预算被改过就再也查不出差异从哪来。
        """
        from . import trace
        name = tool.function.name
        code = _tool_error_code(result)
        text = str(result if result is not None else "")
        stored_text = str(stored if stored is not None else "")
        trace.emit_record({"type": "tool_result", "name": name, "tool_name": name,
                           "call_id": tool.id,
                           "class": _tool_class(name), "tool_class": _tool_class(name),
                           "error": bool(code), "error_code": code,
                           "result_full": text,
                           "result_full_chars": len(text),
                           "result_full_digest": _digest_text(text),
                           "result_stored": stored_text,
                           "result_stored_chars": len(stored_text),
                           "result_stored_digest": _digest_text(stored_text),
                           "duration_ms": duration_ms,
                           "from_replay": bool(from_replay),
                           "truncation": {"cap": cap, "raw_len": len(text),
                                          "stored_len": len(stored_text),
                                          "truncated": len(stored_text) < len(text),
                                          "recorded_at": trace.trace_epoch()["iso"]},
                           # 显示通道：仍是截断后的前 2000 字符（trace 不该被一条结果撑爆）
                           "result_digest": _digest_text(text),
                           "result_chars": len(text),
                           "result": text[:2000]})
        if code:
            # 工具失败在断言面里必须有自己的 id（§3.4）：只靠 tool_result.error=True
            # 没法区分"哪个工具的哪种失败"，L3 也就无法声明"我要复现的是哪一个失败"。
            trace.emit_record({"type": "tool_error", "name": name, "tool_name": name,
                               "call_id": tool.id, "code": code,
                               "tool_class": _tool_class(name),
                               "message": text[:500]})

    # ── 1) 并发执行阶段（只收集结果）────────────────────────
    # 回放：先为每个工具取供给决定（§8.3）。A/C 类拿到录制结果、B 类拿到 LiveCall；
    # 没录到的工具由 `decision()` fail-closed 抛 ReplayToolMissing——不允许"没录到就
    # 现跑一个"来蒙混过关。
    _plan = _replay_plan(use_tool_calls)
    _live = {cid: d for cid, d in _plan.items() if not isinstance(d, str)}
    _replay_results = {cid: d for cid, d in _plan.items() if isinstance(d, str)}

    for _tool in use_tool_calls:
        _trace_call(_tool, _live.get(_tool.id))

    if _live:
        ov = _active_overlay()
        if ov is not None:
            # "写过哪些路径"必须在执行**之前**记账：`_live` 是本次要真跑的集合，
            # 等到执行完再记的话，中途抛出的那次写入就永远落在账本外（§8.2.3 的判据）。
            for _cid, _d in _live.items():
                ov.note_live_call(_cid, _d.name, _d.args)
    # 同步工具：线程池并发。worker 内禁用的工具不执行，结算阶段给错误。
    # 每个工具设结果超时（browser/ds_web 无内部兜底时会卡死线程），超时不阻塞主循环；
    # executor 用 shutdown(wait=False) 防 with 退出 join 死等（后台线程自然结束）。
    #
    # L2 的 B 类真执行必须**串行**且按录制发起顺序（§8.3）：写操作并发跑的话，
    # `edit_file` 与 `write_file` 撞同一路径的顺序就不确定了，回放结果随之不可复现。
    sync = [t for t in use_tool_calls
            if t.id not in _live
            and t.function.name != "delegate_task"
            and not (is_worker and t.function.name in _WORKER_FORBIDDEN)]
    if sync:
        ex = ThreadPoolExecutor(max_workers=_SYNC_MAX)
        try:
            fut, _t0 = {}, {}
            for t in sync:
                _t0[t.id] = time.time()
                fut[ex.submit(_call_tool, t, tool_call_map)] = t
            for f in as_completed(fut):
                t = fut[f]
                try:
                    cid, res = f.result(timeout=_TOOL_TIMEOUT)
                except TimeoutError:
                    cid, res = t.id, f"[Error] Tool '{t.function.name}' timed out after {_TOOL_TIMEOUT}s"
                except Exception as e:
                    cid, res = t.id, f"[Error] Tool execution failed: {e}"
                results[cid] = res
                _dur[t.id] = int((time.time() - _t0[t.id]) * 1000)
        finally:
            ex.shutdown(wait=False)

    # L2：B 类按录制顺序真执行（入参已由 overlay 重写到 `tmp/overlay/<sid>/`）。
    for _t in use_tool_calls:
        if _t.id not in _live:
            continue
        # 深一份再改：那条 assistant 消息里的 tool_call 对象与会话历史共用，改它的
        # `function.arguments` 会让"模型当时发起的调用"变样（§8.2.1 只允许改执行用的副本）。
        # 重写已在 `_replay_plan` 里做完（`run_arguments`），这里只取用。
        _t_run = copy.deepcopy(_t)
        _run_args = getattr(_live[_t.id], "run_arguments", None)
        if _run_args is not None:
            _t_run.function.arguments = _run_args
        _t0 = time.time()
        try:
            _cid, _res = _call_tool(_t_run, tool_call_map)
        except Exception as _e:  # noqa: BLE001 —— 与并发分支保持同一种失败文本
            _cid, _res = _t.id, f"[Error] Tool execution failed: {_e}"
        results[_cid] = _res
        _dur[_t.id] = int((time.time() - _t0) * 1000)

    # delegate_task：子 agent 异步队列（仅主 agent 可委托，受控并发）。
    if not is_worker:
        delegated = [t for t in use_tool_calls
                     if t.function.name == "delegate_task" and t.id not in _replay_results]
        if delegated:
            from . import trace
            ex = ThreadPoolExecutor(max_workers=_worker_pool_size())
            try:
                futs = []
                for t in delegated:
                    try:
                        task = str(json.loads(t.function.arguments).get("task", ""))
                    except json.JSONDecodeError as e:
                        results[t.id] = f"[Error] Invalid JSON arguments: {e}\nRaw: {t.function.arguments}"
                        continue
                    worker_meta[t.id] = task
                    emit({"type": "worker_start", "task": task[:200]})  # 主线程发，worker 跑起来前先上屏
                    _worker_t0[t.id] = time.time()
                    trace.emit_event({"type": "worker_dispatch", "call_id": t.id,
                                      "task_digest": _digest_text(task),
                                      "task_chars": len(task)})
                    futs.append((t, ex.submit(_run_worker, task, delegate_fn)))
                for t, f in futs:
                    try:
                        results[t.id] = f.result(timeout=_WORKER_TIMEOUT)
                    except TimeoutError:
                        results[t.id] = f"[Error] Sub-agent timed out after {_WORKER_TIMEOUT}s"
                    except Exception as e:
                        results[t.id] = f"[Error] Sub-agent execution failed: {e}"
                    _dur[t.id] = int((time.time() - _worker_t0.get(t.id, time.time())) * 1000)
                    trace.emit_event({"type": "worker_settle", "call_id": t.id,
                                      "result_digest": _digest_text(results[t.id]),
                                      "result_chars": len(str(results[t.id])),
                                      "duration_ms": _dur[t.id]})
            finally:
                ex.shutdown(wait=False)

    # 回放结果并进 results；_dur 记 0（没有真实耗时，记个假的更糟）
    if _replay_results:
        results.update(_replay_results)
        for _cid in _replay_results:
            _dur.setdefault(_cid, 0)

    # ── 2) 结算阶段：按原调用顺序，逐分支与旧逻辑一致 ─────────
    critiques = []
    for tool in use_tool_calls:
        if interrupt_event.is_set():
            from . import trace
            trace.emit_event({"type": "interrupt", "cause": "user_esc", "phase": "during_tools"})
            _rollback_tool_round(conv, cause="interrupt")
            return
        tool_name = tool.function.name

        if is_worker and tool_name in _WORKER_FORBIDDEN:
            # worker 内禁止委托/记忆工具：发事件 + 落库后 continue（跳过自检）
            err = f"[Error] {tool_name} 在 sub-agent 内不可用。将你的结果返回给主 agent 处理。"
            emit({"type": "tool_result", "tool_name": tool_name, "result": err, "call_id": tool.id})
            _trace_result(tool, err, err, 0, from_replay=False)
            _append_msg({"role": "tool", "tool_call_id": tool.id, "content": err}, conv)
            continue

        tool_result = results.get(tool.id)
        if tool_result is None:
            tool_result = "[Error] Tool returned None (timeout or empty response)"

        # 反向映射（§8.2.2）：真执行发生在 overlay 里，所以出参里的路径是 overlay 路径。
        # 写进会话历史的必须是逻辑路径——否则回放出来的会话与录制的那份一比就全是对不上的
        # `tmp/overlay/<sid>/...`，而且 overlay 回放完就删了，历史里留下一堆死路径。
        # 只对真执行的调用做：录制结果本来就是录的时候那台机器上的逻辑路径，再"换回去"
        # 等于对已正确的文本做一次猜测性替换。
        if tool.id in _live:
            tool_result = _reverse_tool_result(tool_result, tool_name)

        if tool_name == "delegate_task" and tool.id in worker_meta:
            emit({"type": "worker_done", "task": worker_meta[tool.id][:200], "result": str(tool_result)[:300]})

        emit({"type": "tool_result", "tool_name": tool_name, "result": str(tool_result), "call_id": tool.id})
        # 落库进上下文的结果按预算截断（本地窗口小）；显示仍走上面的完整事件。
        # 必须在发 trace 之前算出来：trace 记的 result_stored 就是这一份（§4.5）。
        stored = str(tool_result)
        # 本地 2500 只能装 ~14 个元素清单，投币按钮排第 43 位看不到 → 提到 6000
        # （16K 窗口 ≈ 3000 token 元素清单，18% 预算，可接受；能覆盖完整操作栏）
        cap = 6000 if _is_local_backend() else 10000
        _limits("tool_result_cap")
        if len(stored) > cap:
            total = len(stored)
            stored = stored[:cap] + f"\n...(stored truncated, {total} chars total; re-read via read_file if needed)"
        _trace_result(tool, tool_result, stored, cap, duration_ms=_dur.get(tool.id),
                      from_replay=tool.id in _replay_results)
        # 回放对账（§4.5）：现在的预算算出来的 stored 必须等于录制的那一份
        if tool.id in _replay_results:
            _replay_verify_result(tool, str(tool_result), stored)
        elif tool.id in _live:
            # 真执行的调用**本来就不该**等于录制结果（B 类写文件/跑命令）：对它做"
            # truncate 是否等于录制 stored"的对账，只会把每次正确的真执行报成 drift。
            # 改成只登记"真的跑了、跑出多大"，把它和"对不上"这件事分开记。
            _replay_verify_result(tool, str(tool_result), stored, class_changed=True)
        _append_msg({"role": "tool", "tool_call_id": tool.id, "content": stored}, conv)

        try:
            is_valid, critique = validate_tool_result(tool_name, tool_result)
        except Exception:
            is_valid, critique = True, ""  # 自检不该有权限中断整批工具执行
        if not is_valid:
            emit({"type": "self_check", "tool_name": tool_name, "critique": critique})
            # 自检结论进 trace：它是"这一轮结果为什么被质疑"的唯一记录，只发屏幕事件
            # 的话，回放里就只剩一个"模型绕开了这个结果"而看不出原因。
            _trace_ev("self_check", tool_name=tool_name, call_id=tool.id,
                      content=critique, critique=critique,
                      critique_digest=_digest_text(critique))
            critiques.append((tool_name, critique))

    # 合并所有自检为一条 user 消息，避免多 tool 失败时连续 user 消息触发 API 400
    if critiques:
        lines = ["[Self-check] The following tool results look questionable:"]
        for tname, crit in critiques:
            lines.append(f"\n- {tname}: {crit}")
        lines.append("\n\nPlease critically evaluate these results and retry if needed. If usable, explain why and proceed.")
        _append_msg({"role": "user", "content": "\n".join(lines)}, conv)


def _stream_strip_xml(buf, emit_fn):
    """从流式累积缓冲里剥掉已闭合的 <tool_call> 块，纯文本经 emit_fn 上屏，返回剩余缓冲。
    未闭合的块（可能跨 chunk）暂缓，等后续块闭合后再处理。"""
    buf = _strip_dsml(buf)
    while True:
        m = _XML_TOOLCALL_RE.search(buf)
        if not m:
            break
        plain, buf = buf[:m.start()], buf[m.end():]
        if plain:
            emit_fn(plain)
    open_pos = buf.find("<tool_call")
    if open_pos == -1:
        if buf:
            emit_fn(buf)
            buf = ""
    else:
        if buf[:open_pos]:
            emit_fn(buf[:open_pos])
        buf = buf[open_pos:]
    return buf


def _safe_create(conv, **kwargs):
    """create() 包装：API 400 时回滚最后一轮工具调用（避免合成 tool_call_id 污染历史后持续 400），再上抛。

    出口是模块级 `client`——一个 OpenAI 兼容外观，实际实现是后端无关的 llm 网关
    （loop_agent_core/llm.py）。这样回放引擎只需替换网关 transport 就能接管**所有**
    模型调用（含摘要/视觉），且测试仍可整体替换 `runner.client`。
    """
    try:
        return client.chat.completions.create(**kwargs)
    except Exception as e:
        if getattr(e, "status_code", None) == 400:
            _rollback_tool_round(conv)
            _dump_api_error(e, conv, kwargs)
        raise


def chat_impl(question, on_event, conv, tool_call_map, delegate_fn,
              default_on_event=_default_on_event, is_main_session=False,
              summarize_fn=None):
    """主循环实现。API 调用一律 messages=conv["messages"]（worker 隔离）。

    summarize_fn(prefix, block_msgs) 由薄壳注入，用于 L1 分块滚动摘要；
    为 None（或非主会话）时只做 L0 去重 + L2 硬裁剪。
    """
    # run 起点：清掉上一轮遗留的进程级打印状态（否则它会渗进 checkpoint 的 D 组，
    # 让"同一段历史录两次"产出不同的快照）。见 events.reset_streaming_flag。
    from .events import reset_streaming_flag
    reset_streaming_flag()

    def _emit(event):
        if on_event:
            on_event(event)
        else:
            default_on_event(event)

    def _notice(level, message, code=""):
        """把"这个回合是怎么结束的"发到面板上（level: info / warn）。

        此前 4 类终局消息（思考循环 / 乱码过滤 / 中断 / 兜底）只在函数末尾 return，
        TUI 又丢掉 chat() 的返回值 → 面板上零输出、状态回 idle，看起来就是
        "agent 静默停止工作"（实测 2026-10-11 20:52 那次：3 轮只出思考 + 1 次中止，
        4 次 API 调用对用户完全不可见）。注入的 nudge 同理，必须显式说明，
        否则用户不知道模型被"催"过几次。

        `code` 是**给机器看**的稳定语义标签（notice:<level>:<code>），
        回放按它做断言；面板文案是给人看的，两者不能互相绑死（文案会改，断言不会）。
        """
        ev = {"type": "notice", "level": level, "message": message}
        if code:
            ev["code"] = code
        _emit(ev)

    def _trace(etype, **fields):
        """只进 trace、不上屏的事实（终局原因、回放对账所需的摘要）。"""
        from . import trace
        trace.emit_event({"type": etype, **fields})

    _final = {}  # {"reason": ...}：确保一轮只写一条 turn_end

    def _checkpoint(kind, q_idx=None):
        """采集一份 checkpoint。pre_request 必须发生在 llm_request 之前。

        post_turn 只在**本回合已经采过** pre_request 时才补：否则会在会话刚开头
        （还没发出任何请求）凭空写一份"轮末"快照，回放的 request_index → checkpoint
        映射上就多出一个永远对不上的条目。
        """
        from . import checkpoint
        if kind == "post_turn" and "request_index" not in _final:
            return None
        snap = checkpoint.capture(conv, q_idx, kind=kind)
        if snap is not None and kind == "pre_request":
            _final["request_index"] = snap.get("request_index")
        return snap

    def _noticec(key, **fmt):
        """按词表发带 code 的 notice。

        level 与 code 成对定义（§5.2 词表），文案模板也放一起：断言只认 code，
        但人在面板上看到的那句话必须仍然是原来那句——两件事不能互相绑死，
        也不能因为"加了 code"就把文案丢掉。
        """
        level, code, template = NOTICE_CODES[key]
        text = template.format(**fmt) if fmt else template
        ev = {"type": "notice", "level": level, "message": text, "code": code}
        _emit(ev)
        # trace 侧单独落一条：notice 是 L3 断言的主要来源（notice:<level>:<code>），
        # 不能只活在屏幕事件里——没有回调方时那些断言就无从谈起。
        _trace("notice", level=level, code=code, message=text)

    def _turn_end(reason, answer=""):
        """回合结束的唯一出口记录：回放按 reason 走不同断言，缺它整条回放链就断了。

        幂等：先到的调用赢。这样"正常 return 路径"和"异常兜底路径"可以都调用它，
        而不会在 trace 里留下两条 turn_end（回放会以为跑了两轮）。
        """
        if _final.get("reason"):
            return
        if reason not in TURN_END_REASONS:
            # 词表外的 reason 会让回放的 reason→断言映射落空。宁可当场炸，
            # 也不要写出一条"回放不知道该怎么判"的终局记录。
            raise ValueError(f"turn_end.reason 不在词表内：{reason!r}（允许：{TURN_END_REASONS}）")
        _final["reason"] = reason
        is_garbage = str(answer).startswith("[Response filtered")
        # 先写 turn_end 再补 post_turn 快照：快照的 request_index 取的是"下一个请求号"，
        # 反过来的话 turn_end 会拿到一个比刚才那条 post_turn 更小的号，校验会判成回退。
        _trace("turn_end", reason=reason, answer_chars=len(str(answer or "")),
               answer_digest=_digest_text(answer),
               outcome="filtered" if is_garbage else "answered")
        _checkpoint("post_turn")

    def _finish(reason, answer):
        """终局：先落 turn_end（事实），再原样返回答案（不改调用方语义）。"""
        _turn_end(reason, answer)
        return answer

    def _model_call(conv, **kwargs):
        """一次模型调用。失败时上抛，但**先**把 api_error 事实和 turn_end 记进 trace。

        记在这里而不是让异常一路冒到调用者：调用者拿到的还是同一个异常（行为不变），
        但 trace 里留下了"这一轮是因为什么死的"——否则回放只能看到一串没有结尾的事件。
        """
        try:
            return _safe_create(conv, **kwargs)
        except Exception as exc:  # noqa: BLE001 —— 原样重抛，只加记录
            from . import trace
            _body = f"{type(exc).__name__}: {str(exc)[:800]}"
            _trace("api_error", status=getattr(exc, "status_code", None),
                   body_digest=_digest_text(_body),
                   exception_id=trace.exception_id(exc),
                   exc_type=type(exc).__name__,
                   func=trace.exception_site(exc),
                   error=_body)
            _turn_end("api_error")
            raise

    # ── trace 上下文：本回合的 turn 号 + 会话标识 ───────────────
    # 在写任何事件之前绑定：后面 llm/tool/turn_end 事件都按这个 turn 归组，
    # 序列对不上时回放就能明确报"是这一轮的哪个位置开始分叉"。
    #
    # turn 号取会话级自增的 next_turn：早先写的是 trace.current_context().turn，
    # 而该字段从没人推进，于是**每个用户轮的 turn_start/turn_end 都记成 turn=0**，
    # trace 里分不出轮。多轮回放（replay.all_turns）正需要这个轮号来定位。
    from . import trace
    conv["_user_turn"] = _user_turn = next_turn(conv)
    trace.begin_turn(_user_turn, session=str(conv.get("_session_id") or "main"))
    _turn_started = time.time()

    # 初始化消息 token 记账（首次）
    conv["_empty_retries"] = 0  # thinking-only 回合的重试计数（有界，防死循环）
    if conv["size"] is None:
        conv["size"] = estimate_tokens(json.dumps(conv["messages"], ensure_ascii=False))
    # 发出去之前先查一遍结构：坏结构在 API 端只会回一句晦涩的 422
    # （实测 "messages[0]: missing field `role`"，面板上无从判断该怎么办）
    _problems = conversation_problems(conv["messages"])
    if _problems:
        _msg = ("会话历史结构已损坏（" + "；".join(_problems) + "），已拒绝发送。"
                "请 /load 一个有效会话，或 /clear 重置后重来。")
        _emit({"type": "error", "message": _msg})
        _turn_end("api_error", _msg)
        return "[Internal error] " + _msg
    # 三层压缩（L0/L1）只在主会话启用；worker 会话生命周期短，本轮不压。
    # L2 硬裁剪对两者都生效（worker 原先也走 _prune_messages）。
    active_summarize = summarize_fn if is_main_session else None
    # turn 号的推进已提到上面的 begin_turn 处，这里不再重复 next_turn。
    # 压缩记账必须挂在跨轮存活的 _compact_state 上：conv 每次 chat() 都是新建的包装 dict，
    # 直接写在 conv 上会让计数器每轮归零（MIN_ROUNDS/MAX_PER_SESSION 退化成"每轮"口径）
    _st = cstate(conv)
    _st.setdefault("_compact_count", 0)
    _st.setdefault("_dedup_saved", 0)
    _st.setdefault("_compact_last_turn", None)
    # 添加用户问题（记忆注入合并进问题文本，避免额外 user 消息破坏 user/assistant 交替协议）
    # 回放时**不再注入记忆**：录制下来的那条用户消息已经包含注入文本，回放器传的就是它；
    # 再注入一次会让请求与录制指纹不符，多轮回放从第二轮起就全错位。
    from . import replay as replay_mod
    _in_replay = replay_mod.active_session() is not None
    if is_main_session and not _in_replay and not any(
        m.get("role") == "user" and "[长期记忆" in str(m.get("content", ""))
        for m in conv["messages"]
    ):
        from .memory import build_memory_injection
        injection = build_memory_injection()
        if injection is not None:
            question = injection + "\n\n" + question
    _append_msg({"role": "user", "content": question}, conv)
    q_idx = len(conv["messages"]) - 1  # 当前问题所在索引，其之前都是可裁剪的旧内容

    MAX_TOOL_ROUNDS = cfg["max_tool_rounds"]
    tool_round = 0
    _warned_xml = False  # 工具调用藏在思考里→重试：整回合最多提示一次，别刷屏

    # 循环处理工具调用
    while tool_round < MAX_TOOL_ROUNDS:
        tool_round += 1
        trace.set_tool_round(tool_round)
        if interrupt_event.is_set():  # 用户已按 Esc 中断
            _noticec("interrupted")
            _turn_end("interrupted")
            return "[Interrupted by user]"
        # 预算已饱和且末条就是我们注入的 nudge → **这次请求不该再发**。
        # 不判的话：3 次重试用光后 `continue` 回到这里，第 4 次请求照发，模型照旧只思考，
        # 然后才在下面的饱和分支收摊——白花一次往返，`retries` 也记成 4（越过 `<= 3`）。
        # 实测证据：`tmp/_probe_stuck/`（改前 llm_request 4 次、thinking_only_retry 3 条）。
        #
        # 位置必须在 `_prep_context` / `_checkpoint` **之前**：那两处会为一次并不存在的
        # 请求推进 request_index，而随后 `_noticec` / `_turn_end` 取不到请求号只能退回 0
        # ——trace 里就出现 `request_index 回退：4 → 0`。既然不打算干活，就别先记账。
        if conv.get("_empty_retries", 0) >= 3 and _nudge_is_last(conv):
            _noticec("stuck")
            _dump_filtered("turn_end:stuck",
                           f"round={tool_round} retries={conv['_empty_retries']} "
                           f"thinking_only（预算已用尽，未再发请求）")
            _append_msg({"role": "assistant", "content": _STUCK_MSG}, conv)
            _turn_end("stuck", _STUCK_MSG)
            return _STUCK_MSG
        # 每次 API 调用前整理上下文：L0 去重 → L1 分块摘要 → L2 硬裁剪
        q_idx, pruned = _prep_context(_prune_cap(), q_idx, conv, active_summarize, _emit)
        _checkpoint("pre_request", q_idx)
        # 流式调用：思考/正文边生成边上屏，工具调用在流结束时统一解析
        response = _model_call(conv, model=cfg["model"], messages=conv["messages"], stream=True,
                               tools=tools, stream_options={"include_usage": True}, **cfg["extra_kwargs"])

        content, reasoning = "", ""
        _cbuf, _rbuf = "", ""
        tc_frags = {}  # OpenAI JSON tool_calls 分片（index -> 累积片段），DeepSeek 用
        for chunk in response:
            if interrupt_event.is_set():  # 流式中断：停流，部分内容稍后落库
                break
            # include_usage：usage 只在最终 chunk（choices=[]）携带，须先于空 choices 分支处理
            if chunk.usage is not None:
                _emit({"type": "token_usage", "usage": chunk.usage,
                       "cache": _record_usage(chunk.usage, cstate(conv),
                                              model=cfg.get("model"))})
                _trace("token_usage", usage=_usage_dict(chunk.usage))
                conv["size"] = chunk.usage.prompt_tokens  # 用 API 精确计数同步记账
                if not chunk.choices:
                    continue
            elif not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            r = getattr(delta, "reasoning_content", None)
            if r:
                reasoning += r
                _rbuf = _stream_strip_xml(_rbuf + r, lambda s: _emit({"type": "thinking_chunk", "content": s, "round": tool_round}))
            c = delta.content
            if c:
                content += c
                _cbuf = _stream_strip_xml(_cbuf + c, lambda s: _emit({"type": "response_chunk", "content": s}))
            for tc in (getattr(delta, "tool_calls", None) or []):
                frag = tc_frags.setdefault(tc.index, {"id": None, "name": "", "arguments": ""})
                if tc.id:
                    frag["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        frag["name"] += tc.function.name
                    if tc.function.arguments:
                        frag["arguments"] += tc.function.arguments

        # 流结束：flush 残余缓冲（未闭合的 XML 片段不上屏）
        if _rbuf and "<tool_call" not in _rbuf:
            _emit({"type": "thinking_chunk", "content": _rbuf, "round": tool_round})
        if _cbuf and "<tool_call" not in _cbuf:
            _emit({"type": "response_chunk", "content": _cbuf})

        if interrupt_event.is_set():
            # 中断：把已生成的部分正文落库（避免流式信息丢失），再停回合
            try:
                response.close()
            except Exception:
                pass
            partial = _strip_toolcall_xml(content).strip()
            if partial and not _is_garbage_content(partial):
                _append_msg({"role": "assistant", "content": partial}, conv)
                _noticec("interrupted_partial")
            else:
                _noticec("interrupted_empty")
            _turn_end("interrupted")
            return "[Interrupted by user]"

        # 组装工具调用：优先 OpenAI JSON tool_calls，其次本地模型的 XML（在 reasoning/content 里）。
        # 非本地后端忽略 XML 调用（DeepSeek 只认自己签发的 tool_call id，合成 id 回送会 400）。
        if tc_frags:
            use_tool_calls = [
                types.SimpleNamespace(
                    id=frag["id"] or f"tc{idx}", type="function",
                    function=types.SimpleNamespace(name=frag["name"], arguments=frag["arguments"]))
                for idx, frag in sorted(tc_frags.items())
            ]
        else:
            xml_calls = (_parse_xml_tool_calls(reasoning) + _parse_xml_tool_calls(content)) if _is_local_backend() else []
            if xml_calls:
                use_tool_calls = _xml_to_tool_calls(xml_calls)
            else:
                # 无工具调用：正文已实时上屏，直接保存（不再走 response_done 一次性输出）
                cleaned = _strip_toolcall_xml(content).strip()
                if cleaned and not _is_garbage_content(cleaned):
                    _append_msg({"role": "assistant", "content": cleaned}, conv)
                    _turn_end("answer", cleaned)
                    return cleaned
                if "<tool_call" in (reasoning + content):
                    # 模型想调工具但 XML 没解析出来（常见于工具调用写在 thinking 里）→ 重试，别掐断回合
                    if not _warned_xml:
                        _warned_xml = True
                        _noticec("xml_toolcall_retry")
                    _trace("xml_toolcall_retry", round=tool_round,
                           count=int(conv.get("_xml_retries", 0)) + 1)
                    conv["_xml_retries"] = int(conv.get("_xml_retries", 0)) + 1
                    continue
                if reasoning.strip() and not cleaned:
                    # thinking-only：回合没走完，注入提示让模型继续（有界 3 次）
                    # 先判后加：旧写法是 `+= 1` 之后才 `<= 3`，于是计到 4 才落在判断外，
                    # 而那时**第 4 次请求已经发出去了**（trace 里 llm_request 4 次、
                    # thinking_only_retry 只有 3 条，`retries=4` 就是这么来的）。
                    if conv.get("_empty_retries", 0) < 3:
                        conv["_empty_retries"] = conv.get("_empty_retries", 0) + 1
                        _append_thinking_turn(reasoning, conv)
                        _trace("thinking_only_retry", round=tool_round,
                               count=conv["_empty_retries"])
                        _inject_user_nudge(_EMPTY_NUDGE, conv)
                        _noticec("empty_nudge", n=conv["_empty_retries"])
                        _trace("nudge_injected", kind="empty", attempt=conv["_empty_retries"])
                        continue
                    msg = _STUCK_MSG  # 3 次后仍卡 → 思考循环，说清楚
                    _noticec("stuck")
                    # 留痕：notice 会随滚动消失，这条能事后 grep（debug_filtered.txt）
                    _dump_filtered("turn_end:stuck",
                                   f"round={tool_round} retries={conv.get('_empty_retries', 0)} "
                                   f"reasoning={reasoning[:200]}")
                else:
                    msg = "[Response filtered: detected garbled output. Please rephrase your request.]"
                    _noticec("garbled")
                _dump_filtered("main_no_toolcall", f"reasoning={reasoning[:400]} content={content[:400]}")
                _append_msg({"role": "assistant", "content": msg}, conv)
                _turn_end("stuck" if msg == _STUCK_MSG else "garbled", msg)
                return msg

        # 有工具调用
        _emit({"type": "tool_calls", "calls": [{"name": tc.function.name, "args": tc.function.arguments} for tc in use_tool_calls]})

        # 添加助手的工具调用消息（plain dict，避免 SDK 对象无法 JSON 序列化）
        # content 可能夹带 DSML / 工具调用 XML，剥掉后再存
        safe_content = _strip_toolcall_xml(content)
        if _is_garbage_content(safe_content):
            safe_content = ""
        _append_msg({
            "role": "assistant",
            "content": safe_content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": _store_tool_args(tc.function.name, tc.function.arguments),
                    }
                }
                for tc in use_tool_calls
            ]
        }, conv)

        run_tools(use_tool_calls, _emit, conv, tool_call_map, delegate_fn,
                  is_worker=not is_main_session)
        conv["_empty_retries"] = 0  # 有产出（工具调用）→ 重置 thinking-only 计数
        # 工具轮次逼近上限时注入收尾提示（合并到末条 user 或追加，不破坏交替协议）
        if tool_round == MAX_TOOL_ROUNDS - 4:
            _inject_user_nudge(_WRAPUP_NUDGE.format(used=tool_round, max=MAX_TOOL_ROUNDS), conv)
            _noticec("wrapup_nudge", n=tool_round, m=MAX_TOOL_ROUNDS)
            _trace("nudge_injected", kind="wrapup", attempt=tool_round)
        # 继续循环，让助手处理工具结果

    # 主循环已实时输出正文；若工具轮次耗尽仍无正文，交给下面的最终流式阶段

    # ponytail: 本地模型每轮只调 1-2 个工具，读+写多文件常远超 max_tool_rounds。
    # 最终流式阶段循环直到给出正文答案；期间遇到工具调用（content 或 reasoning 里的 <tool_call>）
    # 就执行并重试。上限固定 12，够写完多文件又不至于死循环拖太久。
    for final_round in range(12):
        trace.set_tool_round(MAX_TOOL_ROUNDS + final_round + 1)  # 收尾轮并入同一轮号空间
        if interrupt_event.is_set():  # 用户已按 Esc 中断
            _noticec("interrupted")
            _turn_end("interrupted")
            return "[Interrupted by user]"
        # 预算已饱和且末条就是我们注入的 nudge → **这次请求不该再发**（理由见工具轮同款守卫：
        # 位置必须在 `_prep_context` / `_checkpoint` 之前，否则会为不存在的请求推进
        # request_index，trace 里出现回退）。
        if conv.get("_empty_retries", 0) >= 3 and _nudge_is_last(conv):
            _noticec("stuck")
            _dump_filtered("turn_end:stuck",
                           f"final_round={final_round} retries={conv['_empty_retries']} "
                           f"thinking_only（预算已用尽，未再发请求）")
            _append_msg({"role": "assistant", "content": _STUCK_MSG}, conv)
            _turn_end("stuck", _STUCK_MSG)
            return _STUCK_MSG
        q_idx, pruned = _prep_context(_prune_cap(), q_idx, conv, active_summarize, _emit)
        _checkpoint("pre_request", q_idx)
        response_stream = _model_call(conv, model=cfg["model"], messages=conv["messages"], stream=True,
                                      tools=tools, stream_options={"include_usage": True}, **cfg["extra_kwargs"])

        full_response = ""
        full_thinking = ""  # 原始 reasoning（未剥 XML），用于识别藏在思考里的工具调用
        _emit({"type": "status", "state": "generating"})
        _buf, _think_buf = "", ""
        for chunk in response_stream:
            if interrupt_event.is_set():  # 流式中断：停流，部分内容稍后落库
                break
            # include_usage：usage 只在最终 chunk（choices=[]）携带，须先于空 choices 跳过处理
            if chunk.usage is not None:
                conv["size"] = chunk.usage.prompt_tokens  # 流式结束也用精确计数同步
                _emit({"type": "stream_usage", "usage": chunk.usage,
                       "cache": _record_usage(chunk.usage, cstate(conv),
                                              model=cfg.get("model"))})
                _trace("token_usage", usage=_usage_dict(chunk.usage))
                if not chunk.choices:
                    continue
            elif not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            r = getattr(delta, 'reasoning_content', None)
            if r:
                full_thinking += r
                _think_buf += r
                _think_buf = _stream_strip_xml(_think_buf, lambda s: _emit({"type": "thinking_chunk", "content": s, "round": tool_round}))
            if delta.content:
                _buf += delta.content
                full_response += delta.content
                _buf = _stream_strip_xml(_buf, lambda s: _emit({"type": "response_chunk", "content": s}))

        # 流结束：flush 残余缓冲（未闭合的 XML 片段不上屏）
        if _think_buf and "<tool_call" not in _think_buf:
            _emit({"type": "thinking_chunk", "content": _think_buf, "round": tool_round})
        if _buf and "<tool_call" not in _buf:
            _emit({"type": "response_chunk", "content": _buf})

        if interrupt_event.is_set():
            try:
                response_stream.close()
            except Exception:
                pass
            partial = _strip_toolcall_xml(full_response).strip()
            if partial and not _is_garbage_content(partial):
                _append_msg({"role": "assistant", "content": partial}, conv)
                _noticec("interrupted_partial")
            else:
                _noticec("interrupted_empty")
            _turn_end("interrupted")
            return "[Interrupted by user]"

        # 模型还没读完/还在发工具调用（本地模型常把 <tool_call> 写在 reasoning 里）→ 执行后重试。
        # 非本地后端忽略 XML 调用（DeepSeek 只认自己签发的 id，合成 id 回送会 400）。
        if _is_local_backend():
            xml_calls = _parse_xml_tool_calls(full_thinking) + _parse_xml_tool_calls(full_response)
            if xml_calls or "<tool_call" in full_thinking or "<tool_call" in full_response:
                if xml_calls:
                    use_tool_calls = _xml_to_tool_calls(xml_calls)
                    _emit({"type": "tool_calls", "calls": [{"name": tc.function.name, "args": tc.function.arguments} for tc in use_tool_calls]})
                    _append_msg({
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": tc.id, "type": tc.type,
                             "function": {"name": tc.function.name, "arguments": _store_tool_args(tc.function.name, tc.function.arguments)}}
                            for tc in use_tool_calls
                        ]
                    }, conv)
                    run_tools(use_tool_calls, _emit, conv, tool_call_map, delegate_fn,is_worker=not is_main_session)
                    conv["_empty_retries"] = 0  # 有产出（工具调用）→ 重置 thinking-only 计数
                    # 同样在逼近上限时注入收尾提示
                    if final_round == 8:  # 12 轮中的第 9 轮（0-indexed）
                        _inject_user_nudge(_WRAPUP_NUDGE.format(used=final_round + 1, max=12), conv)
                        _noticec("wrapup_nudge", n=final_round + 1, m=12)
                        _trace("nudge_injected", kind="wrapup", attempt=final_round + 1)
                continue  # 模型还会再请求，直到给出正文

        # 正常答案：剥 XML + 垃圾检测后保存
        save_response = _strip_toolcall_xml(full_response).strip()
        if _is_garbage_content(save_response):
            if full_thinking.strip() and not save_response:
                # thinking-only：回合没走完，注入提示让模型继续（有界 3 次）
                # 先判后加，理由同工具轮那处：否则会白跑一次请求才收摊。
                if conv.get("_empty_retries", 0) < 3:
                    conv["_empty_retries"] = conv.get("_empty_retries", 0) + 1
                    _append_thinking_turn(full_thinking, conv)
                    _trace("thinking_only_retry", round=final_round,
                           count=conv["_empty_retries"])
                    _inject_user_nudge(_EMPTY_NUDGE, conv)
                    _noticec("empty_nudge", n=conv["_empty_retries"])
                    _trace("nudge_injected", kind="empty", attempt=conv["_empty_retries"])
                    continue
                msg = _STUCK_MSG  # 3 次后仍卡 → 思考循环，说清楚
                _noticec("stuck")
                _dump_filtered("turn_end:stuck",
                               f"final_round={final_round} retries={conv.get('_empty_retries', 0)} "
                               f"thinking={full_thinking[:200]}")
            else:
                msg = "[Response filtered: detected garbled output. Please rephrase your request.]"
                _noticec("garbled")
            _dump_filtered("final_stage", f"thinking={full_thinking[:400]} response={full_response[:400]}")
            _append_msg({"role": "assistant", "content": msg}, conv)
            _turn_end("stuck" if msg == _STUCK_MSG else "garbled", msg)
            return msg
        else:
            _append_msg({"role": "assistant", "content": save_response}, conv)
            _turn_end("answer", save_response)
            return save_response

    # 多次重试仍无正文（极端情况），兜底
    _noticec("fallback")
    _dump_filtered("fallback", f"thinking={full_thinking[:400]} response={full_response[:400]}")
    _final_answer = "[Response filtered: detected garbled output. Please rephrase your request.]"
    _append_msg({"role": "assistant", "content": _final_answer}, conv)
    _turn_end("fallback", _final_answer)
    return _final_answer
