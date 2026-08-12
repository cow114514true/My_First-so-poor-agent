"""loop_agent_v2.py — 薄壳：持有可变全局，组装 loop_agent_core，再导出公共 API。

tui.py / 测试 import 的符号都必须保持可访问（详见 HANDOFF_NEXT_SESSION.md 外部契约清单）。
纯实现全部在 loop_agent_core/ 包。
"""

# 薄壳：可变全局与再导出。
import threading

from loop_agent_core.config import (WORK_DIR, _is_local_backend, _backend_config,
                                    cfg, CURRENT_MODEL, client, check_local_llm)
from loop_agent_core.tokens import (estimate_tokens, _read_budget, _char_heuristic,
                                    _local_tokenize, _load_ds_tokenizer)
from loop_agent_core.schemas import tools
from loop_agent_core.prompts import system_prompt, WORKER_SYSTEM_PROMPT
from loop_agent_core.shell_tools import get_date_mock, get_weather_mock
from loop_agent_core.shell_tools import exec_shell_win_mock as _core_exec_shell_win
from loop_agent_core.file_tools import (read_file_mock, count_tokens_mock, write_file_mock,
                                        edit_file_mock, _resolve_path, build_outline, extract_function)
from loop_agent_core.xmlutil import (_DSML_RE, _strip_dsml, _is_garbage_content,
                                     _parse_xml_tool_calls, _strip_toolcall_xml,
                                     _normalize_toolcall_xml, _XML_TOOLCALL_RE)
from loop_agent_core.validation import validate_tool_result
from loop_agent_core.ds_web import (log_in, upload_files, input_prompt, enter_confirm,
                                    get_response, _get_playwright, use_ds_from_web_mock)
from loop_agent_core.browse import (_validate_url, _get_browser_page, _extract_page_text,
                                    _take_screenshot, _save_profile, browser_act_mock)
from loop_agent_core.tavily_tool import search_web_mock, fetch_url_mock
from loop_agent_core.context import (_prune_cap, _append_msg, _find_groups, _is_trim_notice,
                                     _prune_messages, _rollback_tool_round)
from loop_agent_core.events import _default_on_event
from loop_agent_core.runner import (_xml_to_tool_calls, _store_tool_args, _stream_strip_xml,
                                    _safe_create, chat_impl)
from loop_agent_core.runner import run_tools as _core_run_tools
from loop_agent_core.memory import recall_mock, remember_mock
import loop_agent_core.memory as memory_mod
from loop_agent_core.vision import analyze_image_mock
from loop_agent_core.visual_locate import visual_locate_mock
import loop_agent_core.confirm as confirm_mod
from loop_agent_core.control import interrupt_event

# ponytail: shared queue for TUI live shell output; None in headless mode
_shell_output_queue = None


def exec_shell_win_mock(shell_cmd, shell="cmd"):
    """薄壳包装：把 TUI 注入的 _shell_output_queue 传给 core 实现。"""
    return _core_exec_shell_win(shell_cmd, shell, _shell_output_queue)


_msg_size = None  # running token estimate of messages; synced to exact API usage when available

TOOL_CALL_MAP = {
    "get_date": get_date_mock,
    "get_weather": get_weather_mock,
    "exec_shell_win": exec_shell_win_mock,
    "use_ds_from_web": use_ds_from_web_mock,
    "analyze_image": analyze_image_mock,
    "browser_act": browser_act_mock,
    "visual_locate": visual_locate_mock,
    "search_web": search_web_mock,
    "fetch_url": fetch_url_mock,
    "read_file": read_file_mock,
    "count_tokens": count_tokens_mock,
    "write_file": write_file_mock,
    "edit_file": edit_file_mock,
    "recall": recall_mock,
    "remember": remember_mock
}

_worker_state = threading.local()  # 每线程 worker 标志：并发子 agent 互不干扰（thread-local，非全局布尔）


def _is_worker():
    """当前线程是否运行在子 agent 内（递归守卫 / worker 内工具禁用判断）。"""
    return getattr(_worker_state, "in_worker", False)


memory_mod._worker_check = _is_worker
confirm_mod._worker_check = _is_worker

messages = [
    {"role": "system", "content": system_prompt}
]


def delegate_task_mock(task):
    """Run a sub-agent with a fresh isolated context. Returns its final text answer."""
    if _is_worker():
        return "[Error] delegate_task is not allowed inside a sub-agent. Handle the task directly in your current context."
    worker_conv = {"messages": [{"role": "system", "content": WORKER_SYSTEM_PROMPT}], "size": None}
    _worker_state.in_worker = True
    try:
        return chat(task, on_event=lambda e: None, _conv=worker_conv)
    finally:
        _worker_state.in_worker = False


def _run_tools(use_tool_calls, emit, conv):
    """薄壳包装：注入 TOOL_CALL_MAP 与 delegate（保留 la._run_tools 3 参数签名 + monkeypatch 语义）。"""
    return _core_run_tools(use_tool_calls, emit, conv, TOOL_CALL_MAP,
                           lambda task: delegate_task_mock(task),is_worker=_is_worker())


def chat(question, on_event=None, _conv=None):
    """Public entry. Runs the loop over the global conversation (or an isolated _conv for workers).
    Returns the final assistant text. For the global conversation, syncs the running token estimate."""
    global _msg_size
    if _conv is None:
        _conv = {"messages": messages, "size": _msg_size}
    
    is_main = _conv["messages"] is messages
    try:
        return chat_impl(question, on_event, _conv, TOOL_CALL_MAP,
                         lambda task: delegate_task_mock(task),is_main_session=is_main)
    finally:
        if _conv["messages"] is messages:
            _msg_size = _conv["size"]


# 交互式连续对话
if __name__ == "__main__":
    print(" 多轮对话🤣（输入 'exit' 退出）\n")
    while True:
        question = input("😎 You: ")
        if question.lower() == 'exit':
            break
        chat(question)
