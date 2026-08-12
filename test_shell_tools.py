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


if __name__ == "__main__":
    test_cmd_default()
    test_powershell_runs()
    test_powershell_utf8()
    print("OK")
