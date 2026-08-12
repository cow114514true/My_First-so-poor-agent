"""工具结果自检：可疑结果返回 critique 喂给模型质疑。"""
import json


def validate_tool_result(tool_name, result):
    """返回 (is_valid, critique)。
    critique 为空表示通过；不通过时 critique 直接作为质疑消息喂给模型。"""
    text = str(result) if result else ""

    # -- 通用检查 --
    if not result:
        return False, f"Tool '{tool_name}' returned empty/None. Check for timeout, selector mismatch, or network issue."
    # worker 可能合法地返回极短结果（如 "Done."），跳过短结果质疑
    if len(text) < 10 and tool_name != "delegate_task":
        return False, f"Tool '{tool_name}' result is too short ({len(text)} chars). Possibly truncated or failed silently."

    # -- 工具专属检查 --
    if tool_name == "exec_shell_win":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return True, ""  # 非标准输出不过度质疑
        # stderr 恒为空（shell_tools 已并入 stdout），唯一信号是 returncode。
        # 非零退出 ≠ 命令失败：grep 无匹配 / diff 有差异 / find 空结果都返回 1。
        # 只上屏信息，不逼重试——让模型看 stdout 自己判断是否真失败。
        if data.get("returncode", 0) != 0:
            return False, (f"Shell exited with returncode={data['returncode']}. Non-zero is often expected "
                           "(grep no match, diff differs, find empty). Inspect stdout and judge whether it's a real failure.")
        return True, ""

    elif tool_name == "use_ds_from_web":
        if "[get_response timeout]" in text or "[Error]" in text:
            return False, f"Web capture failed: {text[:200]}. Retry or simplify the ask_prompt."
        if len(text) < 50:
            return False, f"Web capture too short ({len(text)} chars), likely truncated. Retry with shorter ask_prompt or check page state."

    elif tool_name == "browser_act":
        if "[Error]" in text and "Access denied" in text:
            return False, f"URL blocked: {text[:200]}. Use a different URL."
        if text.startswith("[Error]"):
            return False, f"browser_act failed: {text[:300]}. Retry with a different action or element index."
    elif tool_name == "visual_locate":
        if text.startswith("[Error]"):
            return False, f"visual_locate failed: {text[:200]}. Retry with a clearer target description or verify the page is open."
        if text.startswith("[visual_locate]") and "编号 [" in text:
            return True, ""
        return False, f"visual_locate 结果异常: {text[:100]}"
    elif tool_name in ("search_web", "fetch_url"):
        if text.startswith("[Error]"):
            return False, f"{tool_name} failed: {text[:200]}. Retry or handle it directly."

    elif tool_name == "delegate_task":
        if "[Error]" in text:
            return False, f"Sub-agent failed: {text[:200]}. Retry with an adjusted task or handle it directly."

    elif tool_name == "get_weather":
        if "Error" in text or "fail" in text.lower():
            return False, f"Weather result looks wrong: {text[:200]}. Verify location and date, retry."

    elif tool_name == "read_file":
        if text.startswith("[Error]"):
            return False, f"Read failed: {text[:200]}. Check path and try again."

    elif tool_name == "write_file":
        if text.startswith("[Error]") or "Access denied" in text:
            return False, f"Write failed: {text[:200]}. Check path and retry."
        if "Written" in text and "bytes" in text:
            return True, ""  # 明确成功
    elif tool_name == "edit_file":
        if text.startswith("[Error]"):
            return False, f"Edit failed: {text[:200]}"
        if text.startswith("Edited"):
            return True, ""
    elif tool_name == "recall":
        if len(str(result)) < 10:
            return False, "recall 返回过短"
        return True, ""
    elif tool_name == "remember":
        ts = str(result)
        if ts.startswith("[Error]"):
            return False, ts
        # "已记住" 或 "已更新" 开头即为正常
        if ts.startswith("已记住") or ts.startswith("已更新"):
            return True, ""
        return False, f"remember 结果异常: {ts[:100]}"
    # get_date: 基本不可能出错，跳过

    return True, ""
