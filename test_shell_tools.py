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


if __name__ == "__main__":
    test_cmd_default()
    test_powershell_runs()
    test_powershell_utf8()
    test_shell_alias_powershell()
    test_nonzero_exit_not_pressure_retry()
    print("OK")
