"""Self-check for analyze_image + confirm gate. Run: python test_vision_tool.py"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import loop_agent_v2 as la
import loop_agent_core.confirm as confirm
import loop_agent_core.vision as vision


def _reset_confirm():
    confirm._ask = None
    confirm._worker_check = None
    confirm.reset()
    # 测试环境：显式声明"无人值守自动放行"。
    # 修复前这里依赖 confirm 的隐式默认放行（_ask=None => 放行）；现在确认门是
    # fail-closed，放行必须是显式决定（SECURITY_AUDIT_REPORT V1）。
    # 需要验证"无人可问 => 拒绝"的用例在 test_security_hardening.py 中单独覆盖。
    confirm.set_policy("allow_all")


def _reset_confirm_ask():
    """把确认门重置为安全策略（ask），供门控相关用例使用，避免共用默认值。"""
    _reset_confirm()
    confirm.set_policy("ask")


class _FakeResp:
    def __init__(self, content):
        self.choices = [types.SimpleNamespace(
            message=types.SimpleNamespace(content=content, reasoning_content=None))]


class _FakeOpenAI:
    def __init__(self, *a, **k):
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(
            create=lambda **kw: _FakeResp("画面里有三个按钮和一个输入框")))


def _patch_openai():
    old = vision.OpenAI
    vision.OpenAI = _FakeOpenAI
    return old


# -- analyze_image --

def test_analyze_image_ok():
    _reset_confirm()
    tmp = os.path.join(la.WORK_DIR, "_selftest_img.png")
    with open(tmp, "wb") as f:
        f.write(b"\x89PNG fake bytes")
    old = _patch_openai()
    try:
        res = la.analyze_image_mock("_selftest_img.png", "有哪些元素?")
        assert "三个按钮" in res, res
    finally:
        vision.OpenAI = old
        os.remove(tmp)


def test_analyze_image_not_found():
    _reset_confirm()
    res = la.analyze_image_mock("_no_such_img.png")
    assert "[Error] Image not found" in res, res


def test_analyze_image_denied():
    _reset_confirm_ask()  # 必须用安全策略：allow_all 会直接放行，测不出拒绝路径
    tmp = os.path.join(la.WORK_DIR, "_selftest_img.png")
    with open(tmp, "wb") as f:
        f.write(b"x")
    confirm._ask = lambda kind, target, is_worker: "deny"
    old = _patch_openai()
    try:
        res = la.analyze_image_mock("_selftest_img.png")
        assert res == "[Denied by user]", res
    finally:
        vision.OpenAI = old
        os.remove(tmp)


def test_expandvars_resolution():
    _reset_confirm()
    raw = r"C:\Users\%USERNAME%\Pictures\Screenshots\X.png"
    expanded = vision._resolve_image_path(raw)
    assert "%USERNAME%" not in expanded, expanded
    assert "Pictures\\Screenshots" in expanded


def test_analyze_image_expandvars_path():
    # 模型常写 C:\Users\%USERNAME%\...（Windows 惯例），工具必须展开环境变量
    _reset_confirm()
    tmp = os.path.join(os.environ.get("TEMP", la.WORK_DIR), "_selftest_expand.png")
    with open(tmp, "wb") as f:
        f.write(b"x")
    old = _patch_openai()
    try:
        user = os.environ.get("USERNAME", "")
        disguised = vision._resolve_image_path(tmp).replace(user, "%USERNAME%")
        res = la.analyze_image_mock(disguised)
        assert "三个按钮" in res, res
    finally:
        vision.OpenAI = old
        os.remove(tmp)


def test_analyze_image_absolute_path():
    # 图片不走 WORK_DIR 沙箱：绝对路径（WORK_DIR 外）可读，确认门才是边界
    _reset_confirm()
    tmp = os.path.join(os.environ.get("TEMP", la.WORK_DIR), "_selftest_abs.png")
    with open(tmp, "wb") as f:
        f.write(b"x")
    old = _patch_openai()
    try:
        res = la.analyze_image_mock(tmp)
        assert "三个按钮" in res, res
    finally:
        vision.OpenAI = old
        os.remove(tmp)


# -- confirm gate --

def test_confirm_headless_fail_closed():
    """无人可问（_ask=None）时必须拒绝 —— 这是 V1 的核心回归测试。"""
    _reset_confirm()
    confirm.set_policy("ask")
    assert confirm.request("write_file", "/x") is False


def test_confirm_policy_allow_all_is_explicit():
    """非交互场景要放行必须显式声明策略，而不是因为'没人可问'被默认放行。"""
    _reset_confirm()
    confirm.set_policy("allow_all")
    try:
        assert confirm.request("write_file", "/x") is True
    finally:
        confirm.set_policy("ask")


def test_confirm_policy_deny_all():
    _reset_confirm()
    confirm.set_policy("deny_all")
    try:
        confirm._ask = lambda k, t, w: "allow"
        assert confirm.request("write_file", "/x") is False
    finally:
        confirm.set_policy("ask")


def test_confirm_deny_allow():
    _reset_confirm()
    confirm.set_policy("ask")
    confirm._ask = lambda k, t, w: "deny"
    assert confirm.request("analyze_image", "/x") is False
    confirm._ask = lambda k, t, w: "allow"
    assert confirm.request("analyze_image", "/x") is True


def test_confirm_session_is_kind_scoped():
    """'本会话放行'只对当前类别生效，不得顺带放行其它类别 —— V8 回归测试。"""
    _reset_confirm()
    confirm.set_policy("ask")
    calls = []
    confirm._ask = lambda k, t, w: (calls.append(k) or "allow_session")
    assert confirm.request("write_file", "/x") is True
    assert confirm.request("write_file", "/y") is True   # 同类别：已放行，不再问
    assert calls == ["write_file"]
    # 不同类别（命令执行）必须重新询问，而不是继承 write 的放行
    confirm._ask = lambda k, t, w: "deny"
    assert confirm.request("exec_shell_win", "whoami") is False
    assert calls == ["write_file"]


def test_confirm_session_revocable():
    """会话放行必须可撤销 —— V8 回归测试。"""
    _reset_confirm()
    confirm.set_policy("ask")
    confirm._ask = lambda k, t, w: "allow_session"
    assert confirm.request("write_file", "/x") is True
    confirm.revoke()
    confirm._ask = lambda k, t, w: "deny"
    assert confirm.request("write_file", "/y") is False


def test_confirm_worker_scope_isolated():
    """主会话的放行不得外溢到 worker 子 agent —— V8 回归测试。"""
    _reset_confirm()
    confirm.set_policy("ask")
    confirm._ask = lambda k, t, w: "allow_session"
    assert confirm.request("write_file", "/main") is True   # main scope 放行
    confirm._ask = lambda k, t, w: "deny"
    confirm._worker_check = lambda: True                    # 切到 worker scope
    assert confirm.request("write_file", "/worker") is False


def test_confirm_worker_label():
    _reset_confirm()
    confirm.set_policy("ask")
    seen = {}
    confirm._worker_check = lambda: True
    confirm._ask = lambda k, t, w: (seen.update(w=w) or "allow")
    confirm.request("edit_file", "/z")
    assert seen["w"] is True


# -- write_file / edit_file gating --

def test_write_file_gated():
    _reset_confirm_ask()  # 必须用安全策略，否则 allow_all 会绕过门控
    tmp = os.path.join(la.WORK_DIR, "_selftest_write.txt")
    if os.path.exists(tmp):
        os.remove(tmp)
    confirm._ask = lambda k, t, w: "deny"
    assert la.write_file_mock("_selftest_write.txt", "hi") == "[Denied by user]"
    assert not os.path.exists(tmp)
    confirm._ask = lambda k, t, w: "allow"
    assert "Written" in la.write_file_mock("_selftest_write.txt", "hi")
    try:
        assert open(tmp, encoding="utf-8").read() == "hi"
    finally:
        os.remove(tmp)


def test_edit_file_gated():
    _reset_confirm_ask()  # 必须用安全策略，否则 allow_all 会绕过门控
    tmp = os.path.join(la.WORK_DIR, "_selftest_edit.txt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("abc")
    confirm._ask = lambda k, t, w: "deny"
    try:
        assert la.edit_file_mock("_selftest_edit.txt", "abc", "xyz") == "[Denied by user]"
        assert open(tmp, encoding="utf-8").read() == "abc"
    finally:
        os.remove(tmp)


# -- registration --

def test_registration():
    _reset_confirm()
    assert "analyze_image" in la.TOOL_CALL_MAP
    names = {t["function"]["name"] for t in la.tools}
    assert "analyze_image" in names


def _run_all():
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {name}: {e}")
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
