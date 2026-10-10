"""一键跑全部自检套件（纯逻辑，不触网、不依赖本地服务）。Run: python run_tests.py
退出码非 0 = 有套件失败，可直接接 CI / pre-commit。
"""
import subprocess
import sys

SUITES = [
    "test_code_index.py",
    "test_multi_agent.py",
    "test_interrupt.py",
    "test_vision_tool.py",
    "test_shell_tools.py",
    "test_tui_markdown.py",
    "test_security_hardening.py",
]


def main():
    failed = []
    for suite in SUITES:
        print(f"=== {suite} ===", flush=True)
        r = subprocess.run([sys.executable, suite])
        if r.returncode != 0:
            failed.append(suite)
        print()
    if failed:
        print(f"{len(failed)}/{len(SUITES)} suites FAILED: {failed}")
        sys.exit(1)
    print(f"all {len(SUITES)} suites passed")


if __name__ == "__main__":
    main()
