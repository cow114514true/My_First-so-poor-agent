"""主循环与工具执行：_chat 实现。依赖经参数注入（工具表/delegate/默认事件），不碰薄壳可变全局。
API 调用一律用 conv["messages"]（修 worker 子 agent 把主线程历史发给模型的隔离 bug）。"""
import json
import types

from .config import _is_local_backend, client, cfg
from .context import _append_msg, _prune_cap, _prune_messages, _rollback_tool_round
from .events import _default_on_event
from .schemas import tools
from .tokens import estimate_tokens
from .validation import validate_tool_result
from .xmlutil import (_is_garbage_content, _parse_xml_tool_calls, _strip_dsml,
                      _strip_toolcall_xml, _XML_TOOLCALL_RE)


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


def run_tools(use_tool_calls, emit, conv, tool_call_map, delegate_fn,is_worker=False):
    """执行一批工具调用：逐个执行、发事件、追加消息、结果自检。
    delegate_fn 由薄壳注入（运行时查壳命名空间，保留 monkeypatch 语义）。"""
    for tool in use_tool_calls:
        tool_name = tool.function.name

        if is_worker and tool_name in ("delegate_task", "recall", "remember"):
            emit({"type": "tool_result", "tool_name": tool_name,
                "result": f"[Error] {tool_name} 在 sub-agent 内不可用。将你的结果返回给主 agent 处理。",
                "call_id": tool.id})
            # 追加 tool 结果消息后 continue（跳过实际工具调用）
            _append_msg({"role": "tool", "tool_call_id": tool.id,
                    "content": f"[Error] {tool_name} 在 sub-agent 内不可用。"}, conv)
            continue
        try:
            tool_args = json.loads(tool.function.arguments)
        except json.JSONDecodeError as e:
            tool_result = f"[Error] Invalid JSON arguments: {e}\nRaw: {tool.function.arguments}"
        else:
            try:
                if tool_name == "delegate_task":
                    # 特判：不在 tool_call_map 中，worker 无法嵌套调用（递归守卫见薄壳 delegate_task_mock）
                    task = str(tool_args.get("task", ""))
                    emit({"type": "worker_start", "task": task[:200]})
                    try:
                        tool_result = delegate_fn(task)
                    except Exception as e:
                        tool_result = f"[Error] Sub-agent execution failed: {e}"
                    emit({"type": "worker_done", "task": task[:200], "result": str(tool_result)[:300]})
                else:
                    tool_fn = tool_call_map.get(tool_name)
                    if tool_fn is None:
                        available = ", ".join(tool_call_map.keys())
                        tool_result = f"[Error] Unknown tool: '{tool_name}'. Available tools: {available}"
                    else:
                        tool_result = tool_fn(**tool_args)
            except Exception as e:
                tool_result = f"[Error] Tool execution failed: {e}"

        if tool_result is None:
            tool_result = "[Error] Tool returned None (timeout or empty response)"

        emit({"type": "tool_result", "tool_name": tool_name, "result": str(tool_result), "call_id": tool.id})
        # 落库进上下文的结果按预算截断（本地窗口小）；显示仍走上面的完整事件
        stored = str(tool_result)
        cap = 2500 if _is_local_backend() else 10000
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
            _append_msg({"role": "user",
                         "content": f"[Self-check] Tool '{tool_name}' result is questionable:\n{critique}\n\nPlease critically evaluate this result and retry the tool if needed. If the result is actually usable, explain why and proceed."}, conv)


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
        raise


def chat_impl(question, on_event, conv, tool_call_map, delegate_fn,
              default_on_event=_default_on_event,is_main_session=False):
    """主循环实现。API 调用一律 messages=conv["messages"]（worker 隔离）。"""
    def _emit(event):
        if on_event:
            on_event(event)
        else:
            default_on_event(event)

    # 初始化消息 token 记账（首次）
    if conv["size"] is None:
        conv["size"] = estimate_tokens(json.dumps(conv["messages"], ensure_ascii=False))
    if is_main_session and not any(
        m.get("role") == "user" and str(m.get("content", "")).startswith("[长期记忆")
        for m in conv["messages"]
    ):
        from .memory import build_memory_injection
        injection = build_memory_injection()
        if injection is not None:
            _append_msg({"role": "user", "content": injection}, conv)
    # 添加用户问题
    _append_msg({"role": "user", "content": question}, conv)
    q_idx = len(conv["messages"]) - 1  # 当前问题所在索引，其之前都是可裁剪的旧内容

    MAX_TOOL_ROUNDS = cfg["max_tool_rounds"]
    tool_round = 0

    # 循环处理工具调用
    while tool_round < MAX_TOOL_ROUNDS:
        tool_round += 1
        # 每次 API 调用前裁剪旧内容（超预算时）
        q_idx, pruned = _prune_messages(_prune_cap(), q_idx, conv)
        if pruned:
            _emit({"type": "pruned", "removed": pruned})
        # 流式调用：思考/正文边生成边上屏，工具调用在流结束时统一解析
        response = _safe_create(conv, model=cfg["model"], messages=conv["messages"], stream=True,
                                tools=tools, stream_options={"include_usage": True}, **cfg["extra_kwargs"])

        content, reasoning = "", ""
        _cbuf, _rbuf = "", ""
        tc_frags = {}  # OpenAI JSON tool_calls 分片（index -> 累积片段），DeepSeek 用
        for chunk in response:
            # include_usage：usage 只在最终 chunk（choices=[]）携带，须先于空 choices 分支处理
            if chunk.usage is not None:
                _emit({"type": "token_usage", "usage": chunk.usage})
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
                else:
                    _append_msg({"role": "assistant", "content": "[Response filtered: detected garbled output. Please rephrase your request.]"}, conv)
                    return "[Response filtered: detected garbled output. Please rephrase your request.]"

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

        run_tools(use_tool_calls, _emit, conv, tool_call_map, delegate_fn)
        # 继续循环，让助手处理工具结果

    # 主循环已实时输出正文；若工具轮次耗尽仍无正文，交给下面的最终流式阶段

    # ponytail: 本地模型每轮只调 1-2 个工具，读+写多文件常远超 max_tool_rounds。
    # 最终流式阶段循环直到给出正文答案；期间遇到工具调用（content 或 reasoning 里的 <tool_call>）
    # 就执行并重试。上限固定 12，够写完多文件又不至于死循环拖太久。
    for _ in range(12):
        q_idx, pruned = _prune_messages(_prune_cap(), q_idx, conv)
        if pruned:
            _emit({"type": "pruned", "removed": pruned})
        response_stream = _safe_create(conv, model=cfg["model"], messages=conv["messages"], stream=True,
                                       tools=tools, stream_options={"include_usage": True}, **cfg["extra_kwargs"])

        full_response = ""
        full_thinking = ""  # 原始 reasoning（未剥 XML），用于识别藏在思考里的工具调用
        _emit({"type": "status", "state": "generating"})
        _buf, _think_buf = "", ""
        for chunk in response_stream:
            # include_usage：usage 只在最终 chunk（choices=[]）携带，须先于空 choices 跳过处理
            if chunk.usage is not None:
                conv["size"] = chunk.usage.prompt_tokens  # 流式结束也用精确计数同步
                _emit({"type": "stream_usage", "usage": chunk.usage})
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
                continue  # 模型还会再请求，直到给出正文

        # 正常答案：剥 XML + 垃圾检测后保存
        save_response = _strip_toolcall_xml(full_response).strip()
        if _is_garbage_content(save_response):
            _append_msg({"role": "assistant", "content": "[Response filtered: detected garbled output. Please rephrase your request.]"}, conv)
            return "[Response filtered: detected garbled output. Please rephrase your request.]"
        else:
            _append_msg({"role": "assistant", "content": save_response}, conv)
            return save_response

    # 多次重试仍无正文（极端情况），兜底
    _append_msg({"role": "assistant", "content": "[Response filtered: detected garbled output. Please rephrase your request.]"}, conv)
    return "[Response filtered: detected garbled output. Please rephrase your request.]"
