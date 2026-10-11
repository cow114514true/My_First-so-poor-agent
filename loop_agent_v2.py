"""loop_agent_v2.py — 薄壳：持有可变全局，组装 loop_agent_core，再导出公共 API。

tui.py / 测试 import 的符号都必须保持可访问（详见 HANDOFF_NEXT_SESSION.md 外部契约清单）。
纯实现全部在 loop_agent_core/ 包。
"""

# 薄壳：可变全局与再导出。
import json
import threading

from loop_agent_core.config import (WORK_DIR, _compact_k, _is_local_backend, _backend_config,
                                    cfg, CURRENT_MODEL, client, check_local_llm)
from loop_agent_core.tokens import (estimate_tokens, _read_budget, _char_heuristic,
                                    _local_tokenize, _load_ds_tokenizer)
from loop_agent_core.schemas import tools
from loop_agent_core.prompts import system_prompt, WORKER_SYSTEM_PROMPT
from loop_agent_core.shell_tools import get_date_mock, get_weather_mock
from loop_agent_core.shell_tools import exec_shell_win_mock as _core_exec_shell_win
from loop_agent_core.shell_tools import search_code_mock
from loop_agent_core.shell_tools import code_index_mock
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
                                     _prep_context, _prune_messages, _rollback_tool_round,
                                     render_prune_notice)
# 上下文压缩三层：L0 去重（compaction）/ L1 分块滚动摘要（compaction）/ L2 硬裁剪（context）
# 再导出公共符号，保持 tui/测试对薄壳的既有契约（见 HANDOFF_NEXT_SESSION.md 第三节）
from loop_agent_core.compaction import (tool_call_key, result_digest, dedup_key,
                                        dedup_tool_results, placeholder_text,
                                        compaction_cap, should_compact, compaction_span,
                                        split_blocks, build_block_prompt, validate_summary,
                                        summary_reject_reason, failure_hint, skip_label,
                                        log_compaction_failure, cstate, next_turn,
                                        reset_failures, run_rolling_summary,
                                        render_summary_message, apply_compaction,
                                        record_archive, latest_compaction,
                                        expand_last, find_archive, persist_archive, load_archive)
from loop_agent_core.events import _default_on_event
from loop_agent_core.runner import (_xml_to_tool_calls, _store_tool_args, _stream_strip_xml,
                                    _safe_create, chat_impl)
from loop_agent_core.runner import run_tools as _core_run_tools
from loop_agent_core.memory import recall_mock, remember_mock
import loop_agent_core.memory as memory_mod
from loop_agent_core.vision import analyze_image_mock
from loop_agent_core.visual_locate import visual_locate_mock
from loop_agent_core.browser_observation import observe_browser_mock
import loop_agent_core.confirm as confirm_mod
from loop_agent_core.control import interrupt_event

# ponytail: shared queue for TUI live shell output; None in headless mode
_shell_output_queue = None


def exec_shell_win_mock(shell_cmd, shell="cmd", purpose=""):
    """薄壳包装：把 TUI 注入的 _shell_output_queue 传给 core 实现。

    任意命令执行是最高风险能力（可完成 write_file/edit_file 的一切），
    此前完全不在确认门覆盖内（见 SECURITY_AUDIT_REPORT V2）——这里补上门控。
    purpose 随确认框显示：只有命令原文、没有目的时，审批人无从判断意图。
    """
    preview = str(shell_cmd).replace("\n", " ⏎ ")
    if len(preview) > 300:
        preview = preview[:300] + f"… ({len(str(shell_cmd))} chars)"
    if not confirm_mod.request("exec_shell_win", f"[{shell}] {preview}", purpose):
        return json.dumps({
            "stdout": "", "stderr": "", "returncode": None, "success": False,
            "interrupted": False, "timed_out": False,
            "denied": True,
            "message": "[Denied by user] 命令未执行。如需执行请重新发起并在确认框中允许。",
        }, ensure_ascii=False, indent=2)
    return _core_exec_shell_win(shell_cmd, shell, _shell_output_queue)


_msg_size = None  # running token estimate of messages; synced to exact API usage when available

TOOL_CALL_MAP = {
    "get_date": get_date_mock,
    "get_weather": get_weather_mock,
    "exec_shell_win": exec_shell_win_mock,
    "use_ds_from_web": use_ds_from_web_mock,
    "analyze_image": analyze_image_mock,
    "browser_act": browser_act_mock,
    "observe_browser": observe_browser_mock,
    "visual_locate": visual_locate_mock,
    "search_code": search_code_mock,
    "code_index": code_index_mock,
    "search_web": search_web_mock,
    "fetch_url": fetch_url_mock,
    "read_file": read_file_mock,
    "count_tokens": count_tokens_mock,
    "write_file": write_file_mock,
    "edit_file": edit_file_mock,
    "recall": recall_mock,
    "remember": remember_mock,
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


# ─────────────────────────── 上下文压缩：摘要器注入 ───────────────────────────
# 摘要器必须用**独立的 client**，不能复用全局 client：
#   1) 全局 client 被 headroom 包装（HEADROOM_MODE 可能是 optimize，会对摘要请求做二次变形）；
#   2) 更不能走 runner._safe_create —— 它遇 400 会 _rollback_tool_round(conv) 回滚**主会话**历史，
#      摘要失败绝不该污染主对话。
# 也不传 tools / extra_kwargs：reasoning_effort + extra_body.thinking 是 DeepSeek 专属，
# 本地后端传了会 400，而两层压缩必须在两个后端都可用。
_SUMMARY_CLIENT = None

# 主会话的折叠档案挂在薄壳的会话状态里（跨 chat() 调用存活；/load 时可从 sidecar 恢复）
_session_state = {}


def _archive():
    """当前主会话的档案列表（**同一个 list 对象**，保证 apply/expand 的就地修改对全局可见）。"""
    return _session_state.setdefault("_archive", [])


def _compact_state():
    """压缩记账（轮次/次数/失败退避）的**跨轮**载体。

    conv 每次 chat() 都新建，计数器写在 conv 上会每轮归零；这个 dict 跨调用存活，
    薄壳把它作为 conv["_compact_state"] 传进核心（见 compaction.cstate）。
    """
    return _session_state.setdefault("_compact", {})


def clear_compact_state():
    """换会话（/clear、/load）时重置压缩记账：次数预算与失败退避都属于"这次对话"。"""
    _session_state["_compact"] = {}


def _summary_client():
    global _SUMMARY_CLIENT
    if _SUMMARY_CLIENT is None:
        import httpx
        from openai import OpenAI
        from loop_agent_core.config import cfg
        _SUMMARY_CLIENT = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"],
                                 timeout=httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0))
    return _SUMMARY_CLIENT


def _unsupported_param(e):
    """判断异常是不是"后端不认这个参数"（用于摘掉 extra_body 重试一次，而不是直接放弃）。"""
    if getattr(e, "status_code", None) != 400:
        return False
    text = str(e).lower()
    return any(w in text for w in ("thinking", "extra_body", "unknown", "unsupported",
                                   "unrecognized"))


def _summary_call(messages, max_tokens, extra):
    from loop_agent_core.config import cfg
    return _summary_client().chat.completions.create(
        model=cfg["model"], messages=messages, stream=False,
        max_tokens=max_tokens, temperature=0.2, **extra)


def summarize_block(prefix_digest, block_msgs):
    """L1 摘要器的真实实现：一次非流式调用，只喂当前块 + 有界前缀。

    返回 {"summary", "digest"}；失败返回 {"error": "<可诊断原因>"}（调用方据此整体放弃并回退 L2）。

    两个实测教训（见 BUG_LEDGER 27/28）：
    1. `max_tokens` 必须按"摘要输出的不变规模"给足。论文（arXiv 2605.23296）说摘要输出几乎
       不随输入变化，所以给 1500 时一块 7.6k token 的真实历史会被 finish_reason=length 截断、
       缺小标题 → 整次压缩作废。截断时这里**升级一次**预算重试，而不是把历史退回硬裁剪。
    2. deepseek-v4-flash **默认就开思考**（实测 reasoning_tokens 680~717，吃掉近一半输出预算）。
       摘要是有界的机械压缩任务，思考无收益：走后端配置里的 summary_extra_kwargs 关掉它。
       该参数是云端专属，后端不认就摘掉再来一次（保证本地后端也能用）。
    """
    from loop_agent_core.compaction import _digest, build_block_prompt, summary_reject_reason
    from loop_agent_core.config import cfg, _compact_k
    k = _compact_k()
    messages = build_block_prompt(prefix_digest, block_msgs)
    extra = dict(cfg.get("summary_extra_kwargs") or {})
    budget = int(k["max_tokens"])
    ceiling = max(budget, int(k["max_tokens_hard"]))
    while True:
        try:
            resp = _summary_call(messages, budget, extra)
        except Exception as e:
            if extra and _unsupported_param(e):
                extra = {}  # 后端不认这个参数：摘掉重试，不因此放弃整次压缩
                continue
            return {"error": f"api_error:{type(e).__name__}:{e}"}
        ch = resp.choices[0]
        text = (ch.message.content or "").strip()
        if getattr(ch, "finish_reason", None) == "length":
            if text and summary_reject_reason(text) is None:
                break  # 小标题已齐、只是末尾被切：可用，不白扔这次调用
            if budget < ceiling:
                budget = min(budget * 2, ceiling)
                continue
            return {"error": f"output_truncated(max_tokens={budget})"}
        if not text:
            return {"error": "empty_response"}
        break
    return {"summary": text, "digest": _digest(prefix_digest + text)}


def recall_archive_mock(query="", index=-1, limit=3):
    """从被折叠的原始历史里检索片段（模型侧工具）。"""
    if _is_worker():
        return "[Error] recall_archive 在 sub-agent 内不可用。"
    conv = {"messages": messages, "_archive": _archive()}
    hits = find_archive(conv, query, index=index, limit=max(1, int(limit or 3)))
    if not hits:
        if not conv["_archive"]:
            return ("当前会话没有已折叠的历史档案（尚未发生上下文压缩）。"
                    "请直接使用现有上下文，不要臆测缺失内容。")
        return f"档案中未找到与 {query!r} 匹配的片段。换一个更具体的关键词（文件名/函数名/命令）再试。"
    lines = [f"[已折叠历史的原文片段 — 共命中 {len(hits)} 条，均为压缩前的原始内容]"]
    for ei, mi, role, text in hits:
        snippet = text if len(text) <= 2000 else text[:2000] + f"\n…（该条共 {len(text)} 字符，已截断）"
        lines.append(f"\n--- 档案#{ei} 消息#{mi} ({role}) ---\n{snippet}")
    return "\n".join(lines)


# 注册必须在 mock 定义之后：TOOL_CALL_MAP 是模块级字面量，前面引用会 NameError
TOOL_CALL_MAP["recall_archive"] = recall_archive_mock


def clear_archive():
    """清空折叠档案（/clear 时调用：档案属于本次对话，不能跨会话残留）。"""
    _archive().clear()
    _session_state.pop("_archive", None)
    _session_state["_archive"] = []
    clear_compact_state()  # 次数预算与失败退避同属"本次对话"，一并重置


def archive_sidecar_path(conv_path):
    return f"{conv_path}.archive.json"


def persist_archive_sidecar(conv_path):
    """把当前主会话档案写到 <会话文件>.archive.json（供 TUI 保存时调用）。"""
    return persist_archive(_archive(), archive_sidecar_path(conv_path))


def adopt_archive(conv_path):
    """从 sidecar 把档案取回会话状态（就地填充），使 /load 后 /expand 与 recall_archive 可用。"""
    clear_compact_state()  # 换了对话：别把上一份会话的压缩次数/退避带过来
    return load_archive(_archive(), archive_sidecar_path(conv_path))


def expand_compaction():
    """还原最近一次压缩（TUI /expand）。只还原会话历史，不重绘面板。"""
    global _msg_size
    conv = {"messages": messages, "size": _msg_size, "_archive": _archive(),
            "_compact_state": _compact_state()}
    entry = latest_compaction(conv)
    n = len(entry.get("messages") or []) if entry else 0
    if not expand_last(conv):
        return "没有可还原的压缩记录。"
    _msg_size = conv["size"]
    return f"已还原最近一次压缩：{n} 条原始消息已放回会话历史（面板不重绘；要看旧内容请 /load 或重开会话）。"


def compact_session():
    """强制压缩一次（TUI /compact）。

    `force=True` 让它越过**排期类**闸门（触发阈值/最小间隔/失败退避/连续失败硬停）——手动调用
    是明确意图，否则失败一次后 /compact 会被直接跳过，用户就没有任何自救手段了。
    **cap 必须用正常的 `_prune_cap()`**：曾传 0（"强制"），结果低于阈值时 `_prune_messages(0)`
    把历史删到只剩 system + 当前问题（实测 8 条 → 2 条，BUG 30）。
    """
    global _msg_size
    from loop_agent_core.compaction import cstate
    from loop_agent_core.context import _prep_context, _prune_cap
    conv = {"messages": messages, "size": _msg_size, "_archive": _archive(),
            "_compact_state": _compact_state()}
    st = cstate(conv)
    st["_compact_fails"] = 0        # 手动重试：清退避
    st["_compact_retry_turn"] = None
    events = []
    before = len(messages)
    q_idx = len(messages) - 1
    _prep_context(_prune_cap(), q_idx, conv, summarize_block, events.append, force=True)
    _msg_size = conv["size"]
    for e in events:
        if e.get("type") == "compacted":
            return (f"已压缩：{before} → {len(messages)} 条消息，"
                    f"{e.get('before')} → {e.get('after')} token（{e.get('blocks')} 块）")
        if e.get("type") == "compact_skipped":
            extra = (f"，还需 {e['retry_in_turns']} 轮" if e.get("reason") == "backoff"
                     and e.get("retry_in_turns") else "")
            return f"未压缩（{skip_label(e.get('reason'))}{extra}）。"
        if e.get("type") == "compact_failed":
            return f"压缩失败（{e.get('reason')}）：{e.get('hint') or '已回退到硬裁剪'}。"
    return "没有可压缩的内容。"


def chat(question, on_event=None, _conv=None):
    """Public entry. Runs the loop over the global conversation (or an isolated _conv for workers).
    Returns the final assistant text. For the global conversation, syncs the running token estimate."""
    global _msg_size
    if _conv is None:
        _conv = {"messages": messages, "size": _msg_size, "_archive": _archive(),
                 "_compact_state": _compact_state()}
    
    is_main = _conv["messages"] is messages
    try:
        return chat_impl(question, on_event, _conv, TOOL_CALL_MAP,
                         lambda task: delegate_task_mock(task), is_main_session=is_main,
                         summarize_fn=summarize_block)
    finally:
        if _conv["messages"] is messages:
            _msg_size = _conv["size"]
            _session_state["_archive"] = _conv.get("_archive") or _session_state.get("_archive") or []


# 交互式连续对话
if __name__ == "__main__":
    print(" 多轮对话🤣（输入 'exit' 退出）\n")
    while True:
        question = input("😎 You: ")
        if question.lower() == 'exit':
            break
        chat(question)
