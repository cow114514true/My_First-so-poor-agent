"""exec_shell_win 分支自检：默认 pwsh、cmd 兼容层、UTF-8 不乱、管道提示。"""
import json
import os
import sys

# 本文件在 tests/ 下；直跑时仓库根不在 sys.path 里，故显式补上。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loop_agent_core.shell_tools import effective_shell, exec_shell_win_mock
from loop_agent_core.validation import validate_tool_result


def _assert(res, *needles):
    d = json.loads(res)
    assert d["returncode"] == 0, d
    for n in needles:
        assert n in d["stdout"], f"missing {n!r} in {d['stdout']!r}"


def test_default_shell_is_pwsh():
    """默认必须是 pwsh（不是 cmd）：`$PSVersionTable` 只有 PowerShell 认。"""
    d = json.loads(exec_shell_win_mock("$PSVersionTable.PSVersion.Major"))
    assert d["shell_used"] == "pwsh", d
    assert d["stdout"].strip().isdigit(), d


def test_cmd_syntax_is_routed_to_cmd_with_a_hint():
    """cmd 语法在 pwsh 下是**静默跑错**（`dir /b` → Get-ChildItem C:\\b），
    所以兼容层要改用 cmd 跑，并把 pwsh 写法教回去。"""
    eff, hint = effective_shell("dir /b", "pwsh")
    assert eff == "cmd" and hint and "Get-ChildItem" in hint[1], (eff, hint)

    d = json.loads(exec_shell_win_mock("dir /b"))
    assert d["shell_used"] == "cmd", d
    assert d["returncode"] == 0 and d["stdout"].strip(), d
    assert "cmd 语法" in d["note"] and "Get-ChildItem" in d["note"], d["note"]
    for cmd in ("cd /d %USERPROFILE%", "dir /b | more", "if exist x.txt echo y", "echo 1 2>nul"):
        assert effective_shell(cmd, "pwsh")[0] == "cmd", cmd


def test_powershell_syntax_is_never_rerouted():
    """带 PowerShell 特征的一律留在 pwsh（`-Format "%Y%m%d"` 不能被 %…% 规则误判）。"""
    for cmd in ('Get-Date -Format "%Y%m%d"',
                "Get-ChildItem $env:USERPROFILE -Recurse | Select-Object -First 3",
                "$PSVersionTable.PSVersion.Major",
                "Get-Content x.txt | Where-Object { $_ -match 'a' }"):
        assert effective_shell(cmd, "pwsh") == ("pwsh", None), cmd
    # 显式 cmd 时不做任何改道
    assert effective_shell("dir /b", "cmd") == ("cmd", None)


def test_powershell_errors_are_plain_text_not_clixml():
    """stderr 与 stdout 一旦合并（更准确说：一旦用 -EncodedCommand），pwsh 会把错误
    序列化成 CLIXML XML —— 实测模型看到那坨 XML 后断定"pwsh 坏了"、显式改回 cmd
    （chat_20260812_175615.json 第 11→13 条、第 17→19 条）。"""
    d = json.loads(exec_shell_win_mock("Get-ChildItem /b", shell="pwsh"))
    assert d["returncode"] != 0, d
    assert "CLIXML" not in d["stdout"], d["stdout"][:200]
    assert "<Objs" not in d["stdout"], d["stdout"][:200]
    assert "b" in d["stdout"], d["stdout"][:200]  # 至少能看清它去找了哪个路径


def test_pwsh_parse_error_is_readable():
    """纯语法错（比如 bash heredoc 的残留）必须回一句人话，而不是空输出或 XML。"""
    d = json.loads(exec_shell_win_mock("Write-Output ((("))
    assert d["returncode"] != 0, d
    assert "CLIXML" not in d["stdout"] and "<Objs" not in d["stdout"], d["stdout"][:200]
    assert "ParserError" in d["stdout"], d["stdout"][:300]


def test_cmdlet_error_sets_nonzero_exit():
    """pwsh 的 cmdlet 报错是**非终止**错误：不补一句退出码回填，脚本照样 rc=0。"""
    d = json.loads(exec_shell_win_mock("Get-ChildItem /definitely/not/a/switch"))
    assert d["returncode"] != 0, d
    assert d["success"] is False, d


def test_pwsh_temp_script_is_cleaned_up():
    """-File 模式的临时脚本不能留在 tmp/ 里。"""
    import glob
    before = set(glob.glob("tmp/_pwsh_*.ps1"))
    exec_shell_win_mock("Write-Output 'cleanup-check'")
    after = set(glob.glob("tmp/_pwsh_*.ps1"))
    assert after <= before, after - before


def test_powershell_runs():
    _assert(exec_shell_win_mock("Write-Output 'hello-ps'", shell="powershell"), "hello-ps")


def test_powershell_utf8():
    # PS 5.1 管道默认 OEM 代码页，中文必乱；前置 OutputEncoding 行后应正常
    _assert(exec_shell_win_mock("Write-Output '你好 PS'", shell="powershell"), "你好 PS")


def test_shell_alias_powershell():
    # 本地模型可能发 PowerShell/ps/pwsh，别名应仍走 PS 分支而非静默落 cmd
    _assert(exec_shell_win_mock("Write-Output 'alias-ok'", shell="PowerShell"), "alias-ok")
    _assert(exec_shell_win_mock("Write-Output 'ps-ok'", shell="ps"), "ps-ok")


def test_nonzero_exit_not_pressure_retry():
    # grep 无匹配 exit 1 是正常语义——不触发 Self-check，模型自己看 stdout 判断
    res = exec_shell_win_mock("cmd /c exit 1")
    is_valid, critique = validate_tool_result("exec_shell_win", res)
    assert is_valid  # 非零退出不再触发 Self-check 打断模型
    assert critique == ""


def test_pipe_commands_get_a_note():
    """管道命令必须自带提示：returncode 取自管道最后一个命令，不代表被测程序。

    实测（两次真实卡壳）：`python test_tui_markdown.py 2>&1 | findstr /C:"FAIL" …`
    三条 [FAIL] 却回报 "returncode": 0, "success": true —— 模型据此判断"没失败"，
    或者反复换 /C: 模式过滤（汇总行被滤掉，永远看不到结论），把工具轮次烧完。
    """
    d = json.loads(exec_shell_win_mock('python -c "print(1)" 2>&1 | findstr /C:"nope"'))
    assert "note" in d, d
    assert "最后一个命令" in d["note"]
    assert "汇总行" in d["note"]  # findstr 专属的一条

    # 普通管道：给通用提示即可，不硬扯 findstr
    d2 = json.loads(exec_shell_win_mock("Get-ChildItem | Select-Object -First 1"))
    assert "note" in d2 and "汇总行" not in d2["note"], d2

    # 非管道：不加噪音
    assert "note" not in json.loads(exec_shell_win_mock("echo hi"))


if __name__ == "__main__":
    test_default_shell_is_pwsh()
    test_cmd_syntax_is_routed_to_cmd_with_a_hint()
    test_powershell_syntax_is_never_rerouted()
    test_powershell_errors_are_plain_text_not_clixml()
    test_pwsh_parse_error_is_readable()
    test_cmdlet_error_sets_nonzero_exit()
    test_pwsh_temp_script_is_cleaned_up()
    test_powershell_runs()
    test_powershell_utf8()
    test_shell_alias_powershell()
    test_nonzero_exit_not_pressure_retry()
    test_pipe_commands_get_a_note()
    print("OK")
