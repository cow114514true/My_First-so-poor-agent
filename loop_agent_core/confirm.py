"""确认门：敏感工具（analyze_image/write_file/edit_file）的 Allow/Deny/Allow-session 门控。

headless（CLI/测试，未注入 _ask）下自动放行。TUI 注入 _ask 后阻塞等待用户答复。
"""
_ask = None           # callable(kind, target, is_worker) -> "allow"|"allow_session"|"deny"；None => 自动放行
_worker_check = None  # thin shell 注入：thread-local _is_worker()，用于给对话框打"子任务请求"标签
_session_approved = False


def request(kind, target):
    """Return True 表示允许继续。敏感工具在真正执行前调用。"""
    global _session_approved
    if _session_approved or _ask is None:
        return True
    is_worker = bool(_worker_check and _worker_check())
    decision = _ask(kind, target, is_worker)
    if decision == "allow_session":
        _session_approved = True
        return True
    return decision == "allow"
