"""一键跑全部自检套件（纯逻辑，不触网、不依赖本地服务）。Run: python run_tests.py
退出码非 0 = 有套件失败，可直接接 CI / pre-commit。
套件在 tests/ 下；新增套件要加进 SUITES，否则不会被跑到。

前置：需要 DS_KEY 有值（config.py 在 import 时构造 client，缺 key 会 OpenAIError）。
不真的调用 API，随便给个假值即可，例如：
    set DS_KEY=sk-dummy   (cmd)   /   $env:DS_KEY="sk-dummy"   (PowerShell)
"""
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.join(_HERE, "tests")

# 清单是唯一权威。--purpose / --all 仍然可用（向后兼容旧文档与肌肉记忆）。
SUITES = [
    "test_code_index.py",
    "test_multi_agent.py",
    "test_interrupt.py",
    "test_vision_tool.py",
    "test_shell_tools.py",
    "test_tui_markdown.py",
    "test_security_hardening.py",
    "test_purpose.py",
    "test_compaction.py",
    "test_runner_visibility.py",
    "test_trace.py",
    "test_replay.py",
    "test_migrate.py",
]


def _run(suite):
    """跑单个套件。cwd 固定为仓库根，与文档里 `python test_x.py` 的口径一致。"""
    print(f"=== {suite} ===", flush=True)
    return subprocess.run([sys.executable, os.path.join(_TESTS, suite)], cwd=_HERE).returncode


def main():
    args = sys.argv[1:]
    if args:
        suites = [a for a in args if a != "--all"]
        if not suites or "--all" in args:
            suites = SUITES
    else:
        suites = SUITES

    failed = [s for s in suites if _run(s) != 0]
    print()
    if failed:
        print(f"{len(failed)}/{len(suites)} suites FAILED: {failed}")
        sys.exit(1)
    print(f"all {len(suites)} suites passed")


if __name__ == "__main__":
    main()
