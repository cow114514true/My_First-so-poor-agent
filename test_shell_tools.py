"""exec_shell_win 分支自检：cmd 默认 + powershell 都能跑、中文 UTF-8 不乱。"""
import json
from loop_agent_core.shell_tools import exec_shell_win_mock
from loop_agent_core.validation import validate_tool_result


def _assert(res, *needles):
    d = json.loads(res)
    assert d["returncode"] == 0, d
    for n in needles:
        assert n in d["stdout"], f"missing {n!r} in {d['stdout']!r}"


def test_cmd_default():
    _assert(exec_shell_win_mock("echo hello"), "hello")


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
    d2 = json.loads(exec_shell_win_mock("dir /b | more"))
    assert "note" in d2 and "汇总行" not in d2["note"], d2

    # 非管道：不加噪音
    assert "note" not in json.loads(exec_shell_win_mock("echo hi"))


if __name__ == "__main__":
    test_cmd_default()
    test_powershell_runs()
    test_powershell_utf8()
    test_shell_alias_powershell()
    test_nonzero_exit_not_pressure_retry()
    test_pipe_commands_get_a_note()
    print("OK")
