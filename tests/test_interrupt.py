"""Self-check for the interrupt feature (shared event + shell kill). Run: python test_interrupt.py"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import loop_agent_v2 as la
from loop_agent_core.control import interrupt_event
from loop_agent_core.shell_tools import exec_shell_win_mock


def test_event_reexport():
    assert hasattr(la, "interrupt_event")
    assert interrupt_event is la.interrupt_event


def test_shell_killed_on_interrupt():
    interrupt_event.set()
    try:
        res = json.loads(exec_shell_win_mock("ping -n 10 127.0.0.1"))
        assert res["interrupted"] is True, res
        assert res["returncode"] != 0, res
    finally:
        interrupt_event.clear()


def test_shell_normal_no_interrupt():
    interrupt_event.clear()
    res = json.loads(exec_shell_win_mock("echo hello"))
    assert res["interrupted"] is False, res
    assert res["success"] is True


def test_tools_list_has_hover_select():
    names = {t["function"]["name"]: t["function"] for t in la.tools}
    browser = names["browser_act"]
    actions = browser["parameters"]["properties"]["action"]["enum"]
    assert "hover" in actions and "select_option" in actions
    target_type = browser["parameters"]["properties"]["target"]["type"]
    assert target_type in (["integer", "string"], "string"), target_type


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
