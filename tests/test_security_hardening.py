"""安全回归测试：把 SECURITY_AUDIT_REPORT 的实测载荷固化为用例。

覆盖 V1/V2/V3/V4/V5/V6/V8/V13。纯逻辑，不触网、不执行真实命令、不改动项目文件。
Run: python test_security_hardening.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import loop_agent_core.confirm as confirm
from loop_agent_core import net_guard
from loop_agent_core.file_tools import _resolve_path
from loop_agent_core.shell_tools import (
    code_index_mock, exec_shell_win_mock, search_code_mock, strip_terminal_escapes,
)

_fails = []


def _reset_confirm():
    confirm._ask = None
    confirm._worker_check = None
    confirm.reset()
    confirm.set_policy("ask")


def check(cond, label):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        _fails.append(label)


# ── V4: 路径沙箱（read_file 侧本来就对，这里固化防回归）────────────────
def test_resolve_path_blocks_escapes():
    print("[V4] _resolve_path 拒绝越界")
    payloads = [
        r"..\..\..\..\..\Windows\System32\drivers\etc\hosts",
        r"C:\Windows\System32\drivers\etc\hosts",
        r"C:\Users\someone\.dsh\.credentials.yaml",
        r"..\profile.json",
        r"C:/Windows/win.ini",
        r"C:\Windows\..\Windows\win.ini",
        r"tmp\..\..\..\Windows\win.ini",
        r"\\?\C:\Windows\win.ini",
        r"\\localhost\C$\Windows\win.ini",
    ]
    for p in payloads:
        try:
            _resolve_path(p)
            check(False, f"_resolve_path should block {p!r}")
        except Exception:
            check(True, f"blocked {p!r}")


def test_search_code_and_code_index_enforce_sandbox():
    """V4 核心：这两个工具此前完全绕过沙箱，可读任意绝对路径。"""
    print("[V4] search_code / code_index 服从沙箱")
    out = search_code_mock(pattern=r"\S", path=r"C:\Windows\System32\drivers\etc", glob="hosts")
    check("[Error]" in out and "Access denied" in out, "search_code 拒绝沙箱外绝对路径")

    out = search_code_mock(pattern=r"\S", path=r"C:\Users", glob="*.yaml")
    check("[Error]" in out, "search_code 拒绝用户目录")

    out = code_index_mock(path=r"C:\Windows")
    check("[Error]" in out and "Access denied" in out, "code_index 拒绝沙箱外目录")

    out = code_index_mock(path=r"..\..")
    check("[Error]" in out, "code_index 拒绝相对越界")

    # 沙箱内仍应正常工作
    out = search_code_mock(pattern=r"def request", path="loop_agent_core", glob="confirm.py")
    check("[Error]" not in out, "search_code 在沙箱内仍可用")


# ── V1: 确认门 fail-closed ──────────────────────────────────────────────
def test_confirm_fail_closed():
    print("[V1] 确认门无人可问 => 拒绝")
    _reset_confirm()
    check(confirm.request("write_file", "/x") is False, "_ask=None 时拒绝")

    _reset_confirm()
    confirm.set_policy("allow_all")
    check(confirm.request("write_file", "/x") is True, "显式 allow_all 才放行")
    _reset_confirm()


# ── V2: exec_shell_win 受门控 ───────────────────────────────────────────
def test_shell_is_gated():
    print("[V2] exec_shell_win 受确认门管控")
    import loop_agent_v2 as la

    _reset_confirm()
    # 无人可问 => 拒绝，且不得真的执行命令
    res = la.exec_shell_win_mock("echo SHOULD_NOT_RUN")
    data = json.loads(res)
    check(data.get("denied") is True, "无确认时返回 denied")
    check("SHOULD_NOT_RUN" not in data.get("stdout", ""), "无确认时命令未执行")

    # 明确拒绝 => 同样不执行
    _reset_confirm()
    confirm._ask = lambda k, t, w: "deny"
    res = la.exec_shell_win_mock("echo STILL_NOT_RUN")
    check(json.loads(res).get("denied") is True, "显式拒绝时不执行")

    # 允许 => 正常执行
    _reset_confirm()
    confirm._ask = lambda k, t, w: "allow"
    res = la.exec_shell_win_mock("echo GATED_OK")
    data = json.loads(res)
    check(data.get("returncode") == 0 and "GATED_OK" in data.get("stdout", ""),
          "允许后正常执行")
    _reset_confirm()


# ── V3: use_ds_from_web 上传受门控 ──────────────────────────────────────
def test_upload_is_gated():
    print("[V3] use_ds_from_web 文件上传受确认门管控")
    from loop_agent_core.ds_web import use_ds_from_web_mock

    _reset_confirm()
    res = use_ds_from_web_mock(r"C:\Users\someone\.ssh\id_ed25519", "summarize")
    check("Denied" in res, "无确认时上传被拒（且未触网）")

    _reset_confirm()
    confirm._ask = lambda k, t, w: "deny"
    res = use_ds_from_web_mock("/etc/passwd", "summarize")
    check("Denied" in res, "显式拒绝时上传被拒")
    _reset_confirm()


# ── V5: SSRF —— DNS 解析校验 ────────────────────────────────────────────
def test_net_guard_blocks_private():
    print("[V5] net_guard 拒绝环回/内网/危险 scheme")
    blocked = [
        ("http://127.0.0.1:19387/", None),
        ("http://localhost:8080/v1", None),
        ("http://169.254.169.254/latest/meta-data/", None),
        ("http://10.0.0.5/", None),
        ("http://192.168.1.1/", None),
        ("http://[::1]/", None),
        ("file:///C:/Windows/win.ini", None),
        ("javascript:alert(1)", None),
        ("http://0.0.0.0/", None),
    ]
    for url, _ in blocked:
        check(not net_guard.is_allowed(url), f"blocked {url}")

    # 说明：_classify 区分"顶层导航"与"子资源"。
    # data:/blob: 作为子资源必须放行（普通页面内联图片/媒体），作为导航必须拦。
    check(net_guard._classify("data:text/html,<script>x</script>") == "unknown",
          "data: 子资源不 abort（避免搞坏正常网页）")
    check(net_guard._classify("data:text/html,<script>x</script>", navigation=True) == "deny",
          "data: 顶层导航被拦")
    check(net_guard._classify("blob:https://example.com/x", navigation=True) == "deny",
          "blob: 顶层导航被拦")
    check(not net_guard.is_allowed("data:text/html,<script>x</script>"),
          "不是合法 open 目标")
    check(not net_guard.is_allowed("file:///C:/Windows/win.ini"),
          "不是合法 open 目标（file:）")

    ok = ["http://example.com/", "https://www.bilibili.com/video/BV1"]
    for url in ok:
        check(net_guard.is_allowed(url), f"allowed {url}")


def test_net_guard_blocks_dns_bypass():
    """V5 核心：域名解析到环回地址必须被拦（这正是原实现的绕过点）。"""
    print("[V5] DNS 解析绕过被拦")

    def fake_addrinfo(host, port, **kw):
        # 模拟 localtest.me / 127.0.0.1.nip.io 这类解析到环回的域名
        return [(2, 1, 6, "", ("127.0.0.1", port or 80))]

    orig = net_guard.socket.getaddrinfo
    net_guard.socket.getaddrinfo = fake_addrinfo
    try:
        for host in ("localtest.me", "127.0.0.1.nip.io", "spoof.example.com"):
            net_guard.clear_cache()  # 每个主机单独判定，避免缓存串味
            check(not net_guard.is_allowed(f"http://{host}:19387/"),
                  f"blocked DNS-rebind host {host}")
    finally:
        net_guard.socket.getaddrinfo = orig
        net_guard.clear_cache()


def test_net_guard_allowlist():
    print("[V5] AGENT_ALLOW_HOSTS 显式放行")
    orig = net_guard._ALLOW_HOSTS
    net_guard._ALLOW_HOSTS = {"127.0.0.1:8080"}
    try:
        check(net_guard.is_allowed("http://127.0.0.1:8080/v1/models"),
              "显式放行的 loopback 允许")
        check(not net_guard.is_allowed("http://127.0.0.1:9999/"),
              "未放行的 loopback 仍拒绝")
    finally:
        net_guard._ALLOW_HOSTS = orig


# ── V13: 终端转义中和 ───────────────────────────────────────────────────
def test_terminal_escape_stripping():
    print("[V13] 终端控制序列被中和")
    cases = [
        ("\x1b[2Jcleared", "cleared"),
        ("\x1b]0;evil title\x07text", "text"),
        ("\x1b[31mred\x1b[0m", "red"),
        ("plain text", "plain text"),
        ("tab\tand newline\nkept", "tab\tand newline\nkept"),
        ("\x00\x01\x02nul", "nul"),
    ]
    for raw, expect in cases:
        got = strip_terminal_escapes(raw)
        check(got == expect, f"strip {raw!r} -> {expect!r} (got {got!r})")
    check("\x1b" not in strip_terminal_escapes("\x1b[2J\x1b]0;x\x07"), "无 ESC 残留")


def test_route_interceptor_blocks_navigation():
    """V6 核心：click / 重定向 / 子资源走的都是这条路，必须真的 abort。"""
    print("[V6] Playwright 路由拦截器真实 abort 内网请求")

    class _FakeRoute:
        def __init__(self, url, navigation=True):
            self.request = type(
                "R", (), {
                    "url": url,
                    "is_navigation_request": lambda self=None: navigation,
                    "resource_type": "document" if navigation else "image",
                }
            )()
            self.aborted = False
            self.continued = False

        def abort(self):
            self.aborted = True

        def continue_(self):
            self.continued = True

    class _FakePage:
        def __init__(self):
            self.handler = None

        def route(self, pattern, handler):
            self.handler = handler

    page = _FakePage()
    net_guard.block_private_requests(page)
    check(page.handler is not None, "路由拦截器已注册到 page")

    # 内网/环回 + 危险 scheme → abort
    for url in ("http://127.0.0.1:19387/api", "http://169.254.169.254/latest/meta-data/",
                "http://localhost:8080/v1/models", "file:///C:/Windows/win.ini",
                "javascript:alert(1)"):
        r = _FakeRoute(url)
        page.handler(r)
        check(r.aborted and not r.continued, f"abort {url}")

    # 公网 http/https → continue
    for url in ("https://www.bilibili.com/video/BV1", "https://example.com/a.js"):
        r = _FakeRoute(url)
        page.handler(r)
        check(r.continued and not r.aborted, f"continue {url}")

    # 页面内联资源（data:/blob: 作为子资源）不能被误杀，否则正常网页会坏
    for url in ("data:image/png;base64,iVBORw0KGgo=", "blob:https://example.com/uuid-1"):
        r = _FakeRoute(url, navigation=False)
        page.handler(r)
        check(r.continued and not r.aborted, f"inline subresource passes {url[:30]}…")

    # 但同样的 URL 作为顶层导航必须被拦（否则等于绕过 open 的 URL 校验）
    for url in ("data:text/html,<script>alert(1)</script>", "blob:https://example.com/x"):
        r = _FakeRoute(url, navigation=True)
        page.handler(r)
        check(r.aborted and not r.continued, f"top-level nav blocked {url[:30]}…")


def test_shell_gated_through_runner():
    """端到端：模型发出的 exec_shell_win 工具调用经 runner → 工具表 → 确认门。

    只测薄壳包装是不够的 —— 必须证明真实调用链上也拦得住。
    """
    print("[V2] 经 runner 的完整调用链同样受门控")
    import loop_agent_v2 as la

    _reset_confirm()
    conv = {"messages": [{"role": "system", "content": "s"}], "size": None}
    calls = la._xml_to_tool_calls([
        {"name": "exec_shell_win",
         "arguments": json.dumps({"shell_cmd": "echo PWNED_BY_INJECTION"})}
    ])
    emitted = []
    la._run_tools(calls, emitted.append, conv)

    results = [e for e in emitted if e["type"] == "tool_result"]
    check(len(results) == 1, "产生 1 条工具结果")
    if results:
        payload = results[0]["result"]
        check("PWNED_BY_INJECTION" not in payload,
              "未授权时注入的命令**没有真的执行**（输出里无回显）")
        check(json.loads(payload).get("denied") is True, "结果标记为 denied")
    _reset_confirm()


def test_browse_uses_hardened_validator():
    """防止回归：browse.py 里不得残留会覆盖 net_guard 的旧 `_validate_url`。

    实施过程中真的踩过这个坑 —— 残留的旧实现把模块级名字覆盖掉，
    表面上"修好了"，实际 `localtest.me` 仍能通过。
    """
    print("[V5] browse 实际生效的校验器必须是 net_guard 的版本")
    import loop_agent_core.browse as browse

    check(browse._validate_url.__module__ == "loop_agent_core.net_guard",
          f"browse._validate_url 来自 net_guard（实际 {browse._validate_url.__module__}）")

    # 行为验证：DNS 绕过域名必须被拦
    orig = net_guard.socket.getaddrinfo
    net_guard.socket.getaddrinfo = lambda h, p, **kw: [
        (2, 1, 6, "", ("127.0.0.1", p or 80))
    ]
    net_guard.clear_cache()
    try:
        try:
            browse._validate_url("http://localtest.me:19387/")
            check(False, "browse._validate_url 必须拦下 DNS 绕过域名")
        except Exception:
            check(True, "browse._validate_url 拦下 DNS 绕过域名")
    finally:
        net_guard.socket.getaddrinfo = orig
        net_guard.clear_cache()


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print()
    if _fails:
        print(f"{len(_fails)} FAILED:")
        for f in _fails:
            print(f"  - {f}")
        return 1
    print(f"all {len(tests)} security suites passed")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
