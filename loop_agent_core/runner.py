"""主循环与工具执行：_chat 实现。依赖经参数注入（工具表/delegate/默认事件），不碰薄壳可变全局。
API 调用一律用 conv["messages"]（修 worker 子 agent 把主线程历史发给模型的隔离 bug）。"""
import json
import os
import time
import types
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import _is_local_backend, client, cfg, WORK_DIR
from .compaction import cstate, next_turn
from .control import interrupt_event
from .context import (_append_msg, _prep_context, _prune_cap, _prune_messages,
                      _rollback_tool_round, conversation_problems)
from .events import _default_on_event
from .schemas import tools
from .tokens import estimate_tokens
from .validation import validate_tool_result
from .xmlutil import (_is_garbage_content, _parse_xml_tool_calls, _strip_dsml,
                      _strip_toolcall_xml, _XML_TOOLCALL_RE)


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


def _inject_user_nudge(text, conv):
    """注入一条 user 消息：若末条已是 user 则合并（避免连续 user 违反 API 交替协议），否则追加。"""
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


def _store_tool_args(name, arguments):
    """写入 messages 的工具参数压缩：write_file 的 content 只留占位，避免整份文件内容撑爆本地窗口。"""
    if name == "write_file" and isinstance(arguments, str) and len(arguments) > 500:
        try:
            d = json.loads(arguments)
            if isinstance(d, dict) and isinstance(d.get("content"), str):
                d["content"] = f"[omitted {len(d['content'])} chars]"
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
        _rollback_tool_round(conv)
        return

    results = {}  # call_id -> result
    worker_meta = {}  # call_id -> task（结算阶段补发 worker_done）

    # ── 1) 并发执行阶段（只收集结果）────────────────────────
    # 同步工具：线程池并发。worker 内禁用的工具不执行，结算阶段给错误。
    # 每个工具设结果超时（browser/ds_web 无内部兜底时会卡死线程），超时不阻塞主循环；
    # executor 用 shutdown(wait=False) 防 with 退出 join 死等（后台线程自然结束）。
    sync = [t for t in use_tool_calls
            if t.function.name != "delegate_task"
            and not (is_worker and t.function.name in _WORKER_FORBIDDEN)]
    if sync:
        ex = ThreadPoolExecutor(max_workers=_SYNC_MAX)
        try:
            fut = {ex.submit(_call_tool, t, tool_call_map): t for t in sync}
            for f in as_completed(fut):
                t = fut[f]
                try:
                    cid, res = f.result(timeout=_TOOL_TIMEOUT)
                except TimeoutError:
                    cid, res = t.id, f"[Error] Tool '{t.function.name}' timed out after {_TOOL_TIMEOUT}s"
                except Exception as e:
                    cid, res = t.id, f"[Error] Tool execution failed: {e}"
                results[cid] = res
        finally:
            ex.shutdown(wait=False)

    # delegate_task：子 agent 异步队列（仅主 agent 可委托，受控并发）。
    if not is_worker:
        delegated = [t for t in use_tool_calls if t.function.name == "delegate_task"]
        if delegated:
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
                    futs.append((t, ex.submit(_run_worker, task, delegate_fn)))
                for t, f in futs:
                    try:
                        results[t.id] = f.result(timeout=_WORKER_TIMEOUT)
                    except TimeoutError:
                        results[t.id] = f"[Error] Sub-agent timed out after {_WORKER_TIMEOUT}s"
                    except Exception as e:
                        results[t.id] = f"[Error] Sub-agent execution failed: {e}"
            finally:
                ex.shutdown(wait=False)

    # ── 2) 结算阶段：按原调用顺序，逐分支与旧逻辑一致 ─────────
    critiques = []
    for tool in use_tool_calls:
        if interrupt_event.is_set():
            _rollback_tool_round(conv)
            return
        tool_name = tool.function.name

        if is_worker and tool_name in _WORKER_FORBIDDEN:
            # worker 内禁止委托/记忆工具：发事件 + 落库后 continue（跳过自检）
            err = f"[Error] {tool_name} 在 sub-agent 内不可用。将你的结果返回给主 agent 处理。"
            emit({"type": "tool_result", "tool_name": tool_name, "result": err, "call_id": tool.id})
            _append_msg({"role": "tool", "tool_call_id": tool.id, "content": err}, conv)
            continue

        tool_result = results.get(tool.id)
        if tool_result is None:
            tool_result = "[Error] Tool returned None (timeout or empty response)"

        if tool_name == "delegate_task" and tool.id in worker_meta:
            emit({"type": "worker_done", "task": worker_meta[tool.id][:200], "result": str(tool_result)[:300]})

        emit({"type": "tool_result", "tool_name": tool_name, "result": str(tool_result), "call_id": tool.id})
        # 落库进上下文的结果按预算截断（本地窗口小）；显示仍走上面的完整事件
        stored = str(tool_result)
        # 本地 2500 只能装 ~14 个元素清单，投币按钮排第 43 位看不到 → 提到 6000
        # （16K 窗口 ≈ 3000 token 元素清单，18% 预算，可接受；能覆盖完整操作栏）
        cap = 6000 if _is_local_backend() else 10000
        if len(stored) > cap:
            total = len(stored)
            stored = stored[:cap] + f"\n...(stored truncated, {total} chars total; re-read via read_file if needed)"
        _append_msg({"role": "tool", "tool_call_id": tool.id, "content": stored}, conv)

        try:
            is_valid, critique = validate_tool_result(tool_name, tool_result)
        except Exception:
            is_valid, critique = True, ""  # 自检不该有权限中断整批工具执行
        if not is_valid:
            emit({"type": "self_check", "tool_name": tool_name, "critique": critique})
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
    """create() 包装：API 400 时回滚最后一轮工具调用（避免合成 tool_call_id 污染历史后持续 400），再上抛。"""
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
    def _emit(event):
        if on_event:
            on_event(event)
        else:
            default_on_event(event)

    def _notice(level, message):
        """把"这个回合是怎么结束的"发到面板上（level: info / warn）。

        此前 4 类终局消息（思考循环 / 乱码过滤 / 中断 / 兜底）只在函数末尾 return，
        TUI 又丢掉 chat() 的返回值 → 面板上零输出、状态回 idle，看起来就是
        "agent 静默停止工作"（实测 2026-10-11 20:52 那次：3 轮只出思考 + 1 次中止，
        4 次 API 调用对用户完全不可见）。注入的 nudge 同理，必须显式说明，
        否则用户不知道模型被"催"过几次。
        """
        _emit({"type": "notice", "level": level, "message": message})

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
        return "[Internal error] " + _msg
    # 三层压缩（L0/L1）只在主会话启用；worker 会话生命周期短，本轮不压。
    # L2 硬裁剪对两者都生效（worker 原先也走 _prune_messages）。
    active_summarize = summarize_fn if is_main_session else None
    conv["_user_turn"] = next_turn(conv)
    # 压缩记账必须挂在跨轮存活的 _compact_state 上：conv 每次 chat() 都是新建的包装 dict，
    # 直接写在 conv 上会让计数器每轮归零（MIN_ROUNDS/MAX_PER_SESSION 退化成"每轮"口径）
    _st = cstate(conv)
    _st.setdefault("_compact_count", 0)
    _st.setdefault("_dedup_saved", 0)
    _st.setdefault("_compact_last_turn", None)
    # 添加用户问题（记忆注入合并进问题文本，避免额外 user 消息破坏 user/assistant 交替协议）
    if is_main_session and not any(
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
        if interrupt_event.is_set():  # 用户已按 Esc 中断
            _notice("warn", "已中断（Esc）：本回合没有给出回答（此前已执行的工具结果仍保留在历史里）。")
            return "[Interrupted by user]"
        # 每次 API 调用前整理上下文：L0 去重 → L1 分块摘要 → L2 硬裁剪
        q_idx, pruned = _prep_context(_prune_cap(), q_idx, conv, active_summarize, _emit)
        # 流式调用：思考/正文边生成边上屏，工具调用在流结束时统一解析
        response = _safe_create(conv, model=cfg["model"], messages=conv["messages"], stream=True,
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
                _notice("info", "已中断（Esc）：已保留本回合已生成的部分内容。")
            else:
                _notice("warn", "已中断（Esc）：本回合没有给出回答。")
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
                    return cleaned
                if "<tool_call" in (reasoning + content):
                    # 模型想调工具但 XML 没解析出来（常见于工具调用写在 thinking 里）→ 重试，别掐断回合
                    if not _warned_xml:
                        _warned_xml = True
                        _notice("info", "模型把工具调用写进了思考但没能解析出来，正在重试。")
                    continue
                if reasoning.strip() and not cleaned:
                    # thinking-only：回合没走完，注入提示让模型继续（有界 3 次）
                    conv["_empty_retries"] = conv.get("_empty_retries", 0) + 1
                    if conv["_empty_retries"] <= 3:
                        _inject_user_nudge(_EMPTY_NUDGE, conv)
                        _notice("info", f"模型这一轮只输出了思考过程，已要求它继续（{conv['_empty_retries']}/3）。")
                        continue
                    msg = _STUCK_MSG  # 3 次后仍卡 → 思考循环，说清楚
                    _notice("warn", _STUCK_NOTICE)
                    # 留痕：notice 会随滚动消失，这条能事后 grep（debug_filtered.txt）
                    _dump_filtered("turn_end:stuck",
                                   f"round={tool_round} retries={conv['_empty_retries']} "
                                   f"reasoning={reasoning[:200]}")
                else:
                    msg = "[Response filtered: detected garbled output. Please rephrase your request.]"
                    _notice("warn", "模型输出被判为乱码，已中止本轮。请换个说法重试。")
                _dump_filtered("main_no_toolcall", f"reasoning={reasoning[:400]} content={content[:400]}")
                _append_msg({"role": "assistant", "content": msg}, conv)
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
            _notice("info", f"已用 {tool_round}/{MAX_TOOL_ROUNDS} 轮工具调用，已要求模型收尾并给结论。")
        # 继续循环，让助手处理工具结果

    # 主循环已实时输出正文；若工具轮次耗尽仍无正文，交给下面的最终流式阶段

    # ponytail: 本地模型每轮只调 1-2 个工具，读+写多文件常远超 max_tool_rounds。
    # 最终流式阶段循环直到给出正文答案；期间遇到工具调用（content 或 reasoning 里的 <tool_call>）
    # 就执行并重试。上限固定 12，够写完多文件又不至于死循环拖太久。
    for final_round in range(12):
        if interrupt_event.is_set():  # 用户已按 Esc 中断
            _notice("warn", "已中断（Esc）：本回合没有给出回答（此前已执行的工具结果仍保留在历史里）。")
            return "[Interrupted by user]"
        q_idx, pruned = _prep_context(_prune_cap(), q_idx, conv, active_summarize, _emit)
        response_stream = _safe_create(conv, model=cfg["model"], messages=conv["messages"], stream=True,
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
                _notice("info", "已中断（Esc）：已保留本回合已生成的部分内容。")
            else:
                _notice("warn", "已中断（Esc）：本回合没有给出回答。")
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
                        _notice("info", f"已用 {final_round + 1}/12 轮工具调用，已要求模型收尾并给结论。")
                continue  # 模型还会再请求，直到给出正文

        # 正常答案：剥 XML + 垃圾检测后保存
        save_response = _strip_toolcall_xml(full_response).strip()
        if _is_garbage_content(save_response):
            if full_thinking.strip() and not save_response:
                # thinking-only：回合没走完，注入提示让模型继续（有界 3 次）
                conv["_empty_retries"] = conv.get("_empty_retries", 0) + 1
                if conv["_empty_retries"] <= 3:
                    _inject_user_nudge(_EMPTY_NUDGE, conv)
                    _notice("info", f"模型这一轮只输出了思考过程，已要求它继续（{conv['_empty_retries']}/3）。")
                    continue
                msg = _STUCK_MSG  # 3 次后仍卡 → 思考循环，说清楚
                _notice("warn", _STUCK_NOTICE)
                _dump_filtered("turn_end:stuck",
                               f"final_round={final_round} retries={conv['_empty_retries']} "
                               f"thinking={full_thinking[:200]}")
            else:
                msg = "[Response filtered: detected garbled output. Please rephrase your request.]"
                _notice("warn", "模型输出被判为乱码，已中止本轮。请换个说法重试。")
            _dump_filtered("final_stage", f"thinking={full_thinking[:400]} response={full_response[:400]}")
            _append_msg({"role": "assistant", "content": msg}, conv)
            return msg
        else:
            _append_msg({"role": "assistant", "content": save_response}, conv)
            return save_response

    # 多次重试仍无正文（极端情况），兜底
    _notice("warn", "模型多轮都没有产出可用的正文，已中止本轮。请换个说法重试。")
    _dump_filtered("fallback", f"thinking={full_thinking[:400]} response={full_response[:400]}")
    _append_msg({"role": "assistant", "content": "[Response filtered: detected garbled output. Please rephrase your request.]"}, conv)
    return "[Response filtered: detected garbled output. Please rephrase your request.]"
