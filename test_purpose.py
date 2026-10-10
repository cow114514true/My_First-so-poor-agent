"""回归：敏感工具「操作目的」(purpose) 透传到确认门。

覆盖三件事：
1) 旧 3 参 `_ask` 仍可用（向后兼容，不因新增 purpose 破坏既有测试/宿主）；
2) 新 4 参 `_ask` 能收到模型自述的 purpose 字符串；
3) 模型未提供 purpose 时传空串（由界面显式标"未说明"，而不是静默省略）。
"""
from loop_agent_core import confirm

# 显式固定策略：环境里若设了 AGENT_CONFIRM_POLICY=allow_all/deny_all，request() 会在
# 调用 _ask 之前就短路返回，下面的断言会莫名其妙地失败（与本次改动无关的假失败）。
confirm.set_policy("ask")

# 1) 3 参旧签名：不应报错，且 purpose 被忽略而不是位置错位传成 is_worker
confirm.reset()
seen = {}
confirm._ask = lambda k, t, w: (seen.update(k=k, w=w) or "allow")
assert confirm.request("write_file", "/x", "修复确认框") is True
assert seen == {"k": "write_file", "w": False}, seen

# 2) 4 参新签名：purpose 原样送达
confirm.reset()
got = {}
confirm._ask = lambda k, t, w, p: (got.update(p=p) or "deny")
assert confirm.request("exec_shell_win", "[cmd] dir", "清理测试日志") is False
assert got["p"] == "清理测试日志", got

# 3) 未提供 purpose => 空串（界面据此显示"未说明"）
confirm.reset()
got2 = {}
confirm._ask = lambda k, t, w, p: (got2.update(p=p) or "deny")
assert confirm.request("write_file", "/y") is False
assert got2["p"] == "", got2

# 4) 会话放行仍按类别生效（不因新参数破坏门控语义）
confirm.reset()
count = {"n": 0}
confirm._ask = lambda k, t, w, p: (count.update(n=count["n"] + 1) or "allow_session")
assert confirm.request("edit_file", "/a", "改注释") is True
assert confirm.request("edit_file", "/b", "再改") is True   # 同类别已放行，不再问
assert count["n"] == 1, count
assert confirm.request("write_file", "/c", "写新文件") is True  # write 另一类别：仍会问
assert count["n"] == 2, count

confirm._ask = None
print("purpose plumbing OK")
