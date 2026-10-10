"""确认门：敏感工具的 Allow/Deny/Allow-session 门控。

修复要点（见 SECURITY_AUDIT_REPORT.md V1/V2/V8）：
- **fail-closed**：无人可问（`_ask is None`）时一律**拒绝**，而不是放行。
  非交互场景若确实要自动放行，必须**显式**调用 `set_policy("allow_all")`，
  让"放行"成为一个有意识的决定，而不是默认值。
- **会话放行按类别 + 作用域隔离**：批准 `write` 不会顺带放行 `exec`；
  主会话的批准不会外溢到并发运行的 worker 子 agent。
- **可撤销**：`revoke()` / `reset()`；TUI 的 `/revoke` 命令调用它。
- **目的透传**：`request(kind, target, purpose)` 把模型自述的"操作目的"带给审批界面；
  只给命令原文、不给意图时，审批人无从判断，确认框会退化成形式。
"""
import inspect
import os

# ── 策略 ──────────────────────────────────────────────────────────────
# "ask"        : 有 _ask 时问，没有则拒绝（默认，安全）
# "deny_all"   : 一律拒绝（最严）
# "allow_all"  : 一律放行（仅限测试/受控脚本，需显式声明）
_POLICY = os.environ.get("AGENT_CONFIRM_POLICY", "ask").strip().lower()

_ask = None           # callable(kind, target, is_worker) 或 callable(kind, target, is_worker, purpose)
                      #   -> "allow"|"allow_session"|"deny"；None => 拒绝。
                      # 3 参向 4 参（带 purpose）向后兼容：见 _invoke_ask。
_worker_check = None  # thin shell 注入：thread-local _is_worker()，用于给对话框打"子任务请求"标签

# 已在本会话放行的类别：(kind, scope) 集合，scope ∈ {"main", "worker"}
_approved: set = set()


def set_policy(policy: str) -> None:
    """显式设置策略。非交互场景要自动放行必须调用本函数（默认是拒绝）。"""
    global _POLICY
    p = str(policy).strip().lower()
    if p not in ("ask", "deny_all", "allow_all"):
        raise ValueError(f"unknown confirm policy: {policy!r}")
    _POLICY = p


def get_policy() -> str:
    return _POLICY


def revoke(kind=None) -> None:
    """撤销会话放行。kind 为空则全部撤销。"""
    if kind is None:
        _approved.clear()
    else:
        _approved.difference_update({(k, s) for (k, s) in _approved if k == kind})


def reset() -> None:
    """测试/新会话用：清空策略状态（不含 _ask）。"""
    _approved.clear()


# 兼容旧测试与旧代码对 _session_approved 的读写
class _SessionApprovedProxy:
    """布尔视图：任何类别被放行即为 True；赋 False 表示全部撤销。"""

    def __bool__(self):
        return bool(_approved)

    def __eq__(self, other):
        return bool(self) == bool(other)

    def __hash__(self):
        return hash(bool(self))


_session_approved = _SessionApprovedProxy()


def _scope() -> str:
    return "worker" if bool(_worker_check and _worker_check()) else "main"


def _invoke_ask(kind, target, is_worker, purpose):
    """调用注入的 _ask，兼容 3 参（旧）与 4 参（带 purpose）两种签名。

    旧测试把 `_ask` 定义成 `lambda k, t, w: ...`；新 TUI 定义成 4 参以接收 purpose。
    用签名判断而非 try/except TypeError，避免把 _ask 内部真正的 TypeError 误当成"少参"。
    """
    try:
        params = inspect.signature(_ask).parameters
    except (TypeError, ValueError):
        params = None
    if params is not None and len(params) == 3:
        return _ask(kind, target, is_worker)
    return _ask(kind, target, is_worker, purpose)


def request(kind, target, purpose=""):
    """Return True 表示允许继续。敏感工具在真正执行前调用。

    kind 既接受工具名（write_file/exec_shell_win/...），也接受类别名（write/exec/...）。
    purpose：可选的"操作目的"短语（模型自述），随确认框显示给用户判断意图；缺省为空串。
    """
    if _POLICY == "deny_all":
        return False
    if _POLICY == "allow_all":
        return True

    scope = _scope()
    if (kind, scope) in _approved:
        return True

    if _ask is None:
        # 无人可问 → 拒绝（fail-closed）。要放行请显式 set_policy("allow_all")。
        return False

    decision = _invoke_ask(kind, target, scope == "worker", purpose)
    if decision == "allow_session":
        _approved.add((kind, scope))
        return True
    return decision == "allow"
