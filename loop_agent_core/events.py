"""headless 模式事件打印（_default_on_event）。"""
_headless_streaming = False


def _default_on_event(event):
    """headless 模式下复刻原有 print 行为。"""
    global _headless_streaming
    etype = event.get("type")

    if etype == "token_usage":
        print(f"[DEBUG]:token{event.get('usage')}\n")
    elif etype == "thinking":
        content = event.get("content", "")
        print(f"🧠 Thinking ({len(content)} chars): {content[:300]}{'...' if len(content) > 300 else ''}")
    elif etype == "thinking_chunk":
        pass  # headless: thinking in streaming is bundled with answer
    elif etype == "tool_calls":
        calls = event.get("calls", [])
        print(f"Agent调用工具: {[c['name'] for c in calls]}")
    elif etype == "tool_result":
        print(f"tool result for {event.get('tool_name')}:\n {str(event.get('result', ''))[:300]}\n")
    elif etype == "self_check":
        print(f"Tool result validation failed: {event.get('critique')}\n")
    elif etype == "response_chunk":
        if not _headless_streaming:
            print("🤖: ", end="")
            _headless_streaming = True
        print(event.get("content", ""), end="", flush=True)
    elif etype == "response_done":
        print(f"🤖: {event.get('content', '')}")
    elif etype == "stream_usage":
        print(f"\n流式总量：{event.get('usage', '')}")
        _headless_streaming = False
    elif etype == "pruned":
        print(f"[pruned] 上下文裁剪：删除了 {event.get('removed')} 条旧消息")
    elif etype == "worker_start":
        print(f"⏳ 子任务派发: {event.get('task', '')}\n")
    elif etype == "worker_done":
        print(f"✅ 子任务完成: {event.get('result', '')}\n")
    elif etype == "status":
        pass
