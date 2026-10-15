"""生产路径的录制开关（BUILD §4.10）：入口建档、终点收尾、失败保留。

前一套 `test_trace.py` 验的是 trace 层**自己的**不变量（writer、checkpoint、回放），
它每一个用例都自己 `open_writer`。这里补的是另一个缺口：**生产入口自己会不会开**。
这两件事不会互相替代——机制全绿而入口从没接过，正是这套设施曾经的状态。

要验的三件事：

1. 开关开着时，真入口（`python loop_agent_v2.py` 与 `python tui.py` 的那两行）跑完
   必须留下一份自洽的 trace：run_start 第一条、run_end 最后一条、validate 零问题、
   meta.json 记着真 backend / model / session_id / trace_level。TUI 还要多验一条：
   agent 跑在子线程里，事件也必须落盘（contextvars 不自动跨线程）。
2. 开关关着时**一个文件都不产生**——默认状态不能有 IO 代价。
3. 失败现场不许消失：收尾照做、retention=failure、`tools.trace clean` 默认不清。

入口一律开**子进程**跑：开关是进程级事实（env + 模块级 writer），同进程连着跑
「开着 / 关着」会互相污染；而"真实会话录不录得下来"只有跨进程边界才算验过。
假后端（`llm.set_transport`）是唯一被替换的东西，原因是不许触网。
Run: python tests/test_prod_recording.py
"""
import json
import os
import shutil
import subprocess
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS = os.path.join(ROOT, "tests")
sys.path.insert(0, ROOT)
os.environ.setdefault("DS_KEY", "sk-selftest-not-a-real-key")
os.environ.setdefault("FILTERED_LOG", os.path.join(ROOT, "tmp", "_selftest_prodrec_filtered.log"))
os.environ.setdefault("CACHE_LOG", os.path.join(ROOT, "tmp", "_selftest_prodrec_cache.log"))

import loop_agent_core.trace as trace  # noqa: E402
import tools.trace as t_trace  # noqa: E402

# trace 必须落在 tmp/ 下：仓库根的 tmp/traces/** 是 CI 的拦截线，自检不能踩。
_TRACE_ROOT = os.path.join(ROOT, "tmp", "traces-prodtest")
_HEADROOM_JSONL = os.path.join(ROOT, "tmp", "headroom.jsonl")
_QUESTION = "用 get_date 工具告诉我今天是哪天"
_STDIN = _QUESTION + "\nexit\n"


# ── 工具 ────────────────────────────────────────────────────────────
def _fresh_root(tag: str) -> str:
    d = os.path.join(_TRACE_ROOT, tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


def _run_entry(entry: str, mode: str = "ok", *, trace_dir=None, enabled=True,
               stdin=_STDIN, timeout=180, level="assert", extra_argv=()):
    """在真入口上跑一次会话，返回 CompletedProcess。"""
    env = dict(os.environ)
    env["DS_KEY"] = "sk-selftest-not-a-real-key"
    env["MODEL_BACKEND"] = "deepseek"          # 让 manifest.backend 可断言
    env["TRACE_DIR"] = trace_dir or _TRACE_ROOT
    env["TRACE_LEVEL"] = level
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("HEADROOM_STORE_URL", None)        # 隔离：不许把遥测指到别处
    if enabled:
        env["TRACE_ENABLED"] = "1"
    else:
        env.pop("TRACE_ENABLED", None)
    cmd = [sys.executable, os.path.join(_TESTS, "_prod_recording_launcher.py"),
           entry, mode, *extra_argv]
    return subprocess.run(cmd, cwd=ROOT, env=env, input=stdin, text=True,
                          encoding="utf-8", errors="replace",
                          capture_output=True, timeout=timeout)


def _only_dir(root: str) -> str:
    names = [n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))]
    assert len(names) == 1, f"期望恰好一个会话目录，实到 {names}"
    return os.path.join(root, names[0])


def _hash(path: str):
    if not os.path.exists(path):
        return None
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _assert_wellformed(run_dir: str, *, expect_llm=True, level="assert"):
    """一份生产录像的硬形状：首尾、自洽、meta 字段。"""
    evs = trace.read_events(run_dir)
    problems = trace.validate_events(evs)
    assert not problems, problems
    assert evs[0]["type"] == "run_start", evs[0]
    assert evs[-1]["type"] == "run_end", [e["type"] for e in evs[-4:]]
    assert trace.assertion_ids(evs) == trace.assertion_ids(evs)  # 可重入、不抛
    man = trace.load_manifest(run_dir)
    assert man["session_id"] == os.path.basename(run_dir), man
    assert man["backend"] == "deepseek", man
    assert man["model"], man
    assert man["trace_level"] == level, man
    assert os.path.exists(os.path.join(run_dir, trace.TRACE_INDEX)), "trace.idx.json 缺失"
    if expect_llm:
        types_ = [e["type"] for e in evs]
        assert "llm_request" in types_, types_
        assert "llm_end" in types_, types_
        assert "turn_end" in types_, types_
    return evs


# ── 1. 开关开着：真入口必须录下来 ───────────────────────────────────
def test_cli_entry_records_trace():
    """`python loop_agent_v2.py` 那条路径：一问一答 + 一次工具调用。"""
    root = _fresh_root("cli")
    p = _run_entry("cli", trace_dir=root)
    assert p.returncode == 0, f"stderr={p.stderr[-2000:]}"
    run_dir = _only_dir(root)
    evs = _assert_wellformed(run_dir)
    types_ = [e["type"] for e in evs]
    assert "tool_call" in types_, types_
    assert "tool_result" in types_, types_
    assert "token_usage" in types_, types_
    ends = [e for e in evs if e["type"] == "turn_end"]
    assert ends and ends[-1]["reason"] == "answer", ends
    assert trace.load_manifest(run_dir)["entry"] == "cli"


def test_tui_entry_records_across_thread():
    """`python tui.py` 那条路径（headless）。agent 在子线程里跑，事件仍须落盘。"""
    root = _fresh_root("tui")
    p = _run_entry("tui", trace_dir=root)
    assert p.returncode == 0, f"stderr={p.stderr[-2000:]}"
    run_dir = _only_dir(root)
    evs = _assert_wellformed(run_dir)
    types_ = [e["type"] for e in evs]
    # 这三类正是 agent 线程里发出的：少了它们就说明录制上下文没带进线程，
    # 目录建了、run_start 写了，但真实请求一条都没录到。
    assert "tool_call" in types_, types_
    assert "tool_result" in types_, types_
    assert "turn_end" in types_, types_
    assert trace.load_manifest(run_dir)["entry"] == "tui"


# ── 2. 开关关着：一个字节都不产生 ───────────────────────────────────
def test_switch_off_writes_nothing():
    root = os.path.join(_TRACE_ROOT, "off")
    shutil.rmtree(root, ignore_errors=True)   # 故意**不**建目录：关着时它就不该出现
    p = _run_entry("cli", trace_dir=root, enabled=False)
    assert p.returncode == 0, f"stderr={p.stderr[-2000:]}"
    assert not os.path.exists(root), f"开关关着却建了 {sorted(os.listdir(root))}"


def test_switch_off_tui_writes_nothing():
    root = os.path.join(_TRACE_ROOT, "off-tui")
    shutil.rmtree(root, ignore_errors=True)
    p = _run_entry("tui", trace_dir=root, enabled=False)
    assert p.returncode == 0, f"stderr={p.stderr[-2000:]}"
    assert not os.path.exists(root), "TUI 关着开关却建了目录"


# ── 3. 失败 / 中断路径也要收尾 ──────────────────────────────────────
def test_backend_failure_still_closes_and_marks_failure():
    """传输层抛异常 → turn_end:reason=api_error，run_end 照落，标 failure。

    这里同时钉住一个**既有事实**：`chat()` 不吞 API 异常（runner 的 `_model_call`
    是"先记录、再原样上抛"），所以 CLI 循环会带着异常穿出录制壳，`run_end` 记成
    `exception`。录制壳不替它兜底——收尾归收尾，语义归语义。
    """
    root = _fresh_root("boom")
    p = _run_entry("cli", mode="boom", trace_dir=root)
    run_dir = _only_dir(root)
    evs = _assert_wellformed(run_dir)
    ids = trace.assertion_ids(evs)
    assert any(a.startswith("turn_end:reason=api_error") for a in ids), ids
    assert any(a.startswith("exception:RuntimeError@") for a in ids), ids
    assert evs[-1]["reason"] == "exception", evs[-1]
    man = trace.load_manifest(run_dir)
    assert man["retention"] == trace.RETENTION_FAILURE, man
    assert man["failure_assertions"], man
    assert man["end_reason"] == "exception", man


def test_ctrl_c_still_closes_as_interrupted():
    """第二句 input 抛 KeyboardInterrupt（= 用户 Ctrl-C）：run_end 必须落盘。"""
    root = _fresh_root("kb")
    p = _run_entry("cli", mode="kb", trace_dir=root)
    run_dir = _only_dir(root)
    evs = trace.read_events(run_dir)
    assert evs[-1]["type"] == "run_end", [e["type"] for e in evs[-4:]]
    assert evs[-1]["reason"] == "interrupted", evs[-1]
    assert trace.validate_events(evs) == []
    man = trace.load_manifest(run_dir)
    assert man["retention"] == trace.RETENTION_NORMAL, man
    # 第一问是成功的，中断的是第二问
    assert any(e["type"] == "turn_end" and e["reason"] == "answer" for e in evs), evs


def test_body_exception_still_closes_as_exception():
    """录制壳本身：{正文抛异常} → run_end:reason=exception，且不外抛覆盖原异常。"""
    root = _fresh_root("raise")
    old_dir = os.environ.get("TRACE_DIR")
    old_sw = os.environ.get("TRACE_ENABLED")
    os.environ["TRACE_ENABLED"] = "1"
    os.environ["TRACE_DIR"] = root
    run_dir = os.path.join(root, "raise-1")
    try:
        try:
            with trace.session_recording("raise-1", run_dir=run_dir):
                trace.emit_record({"type": "turn_end", "reason": "answer"})
                raise ValueError("正文故意炸")
        except ValueError:
            pass
    finally:
        _restore("TRACE_DIR", old_dir)
        _restore("TRACE_ENABLED", old_sw)
    evs = trace.read_events(run_dir)
    assert evs[-1]["type"] == "run_end" and evs[-1]["reason"] == "exception", evs[-1]
    assert os.path.exists(os.path.join(run_dir, trace.TRACE_INDEX))


# ── 3.5 档位：TRACE_LEVEL 必须从生产入口传下去 ──────────────────────
def test_stream_level_is_honoured_at_production_entry():
    """`TRACE_LEVEL=stream` 时 `response_chunk` / `thinking_chunk` 必须真的落盘。

    §4.4 的档位过滤只有一处用武之地：`_should_write()`（`trace.py:501-502`）对
    ≥ `LEVEL_STREAM` 的事件要求 writer 档位也是 stream，而这类事件只有
    `response_chunk` / `thinking_chunk` 两种（`trace.py:86-87`）——正是"模型到底
    一个字一个字吐了什么"的原始证据。

    这条用例补的缺口：此前每个生产入口用例都写死 `TRACE_LEVEL=assert`
    （`tests/test_prod_recording.py` 里 `env["TRACE_LEVEL"] = "assert"`），于是
    「环境变量能不能真的把档位升到 stream」从来没在任何真入口上验过——出事时最想
    要的那类证据，恰好是唯一没验过的那条路。
    """
    root = _fresh_root("stream")
    p = _run_entry("cli", trace_dir=root, level="stream")
    assert p.returncode == 0, f"stderr={p.stderr[-2000:]}"
    run_dir = _only_dir(root)
    evs = _assert_wellformed(run_dir, level="stream")
    types_ = [e["type"] for e in evs]
    # 假后端第 2 轮给的是正文，所以必然有 response_chunk。
    assert "response_chunk" in types_, types_
    chunk = next(e for e in evs if e["type"] == "response_chunk")
    assert chunk["chunk"], chunk
    # 升了档但没丢核心面：同一批事件仍在，run_start / limits 的位置不变。
    assert evs[0]["type"] == "run_start" and evs[0]["level"] == trace.LEVEL_STREAM, evs[0]
    assert "llm_request" in types_ and "turn_end" in types_, types_


# ── 3.6 TUI 的 Esc 中断：按键 → 中断标记 → runner 收尾 ──────────────
def test_tui_escape_interrupts_agent_and_closes_trace():
    """在真 TUI 里按 Esc（= `tui.py:1405 action_collapse_panel`）中断在飞的回合。

    此前 Ctrl-C 只接了 CLI（`kb` 模式），TUI 的中断路径一次都没跑过
    （`tui.py:1406-1408`：`_agent_busy` 时按 Esc 置 `interrupt_event`）。这条路径
    牵的不是一句提示语，而是 runner 的流式中断收尾：停流 → 已生成的部分正文落库
    → `turn_end:reason=interrupted`。中断标记在按键之后立刻被清掉
    （`tui.py:1919-1920`），所以"按键那一刻标记真的被置起了"只能由子进程当场记下来，
    事后从 trace 里读不出来。

    按键路由有一处 headless 专属的坑：焦点在 `#input-area`（TextArea）时，Textual
    8.2.8 的 headless 驱动没把 escape 冒泡到 App 级绑定，所以子进程先试 `pilot.press`，
    没生效再直接调那个绑定指向的动作。绑定关系本身在这里断言，动作内容由子进程验。
    """
    import tui as tui_mod

    bindings = {getattr(b, "key", None): getattr(b, "action", None)
                for b in tui_mod.AgentTUI.BINDINGS}
    assert bindings.get("escape") == "collapse_panel", bindings
    assert hasattr(tui_mod.AgentTUI, "action_collapse_panel"), "Esc 绑定的动作不存在"
    root = _fresh_root("tui-esc")
    probe = os.path.join(root, "esc_probe.json")
    os.makedirs(root, exist_ok=True)
    p = _run_entry("tui", mode="slow", trace_dir=root, extra_argv=(probe,))
    assert p.returncode == 0, f"stderr={p.stderr[-2000:]}"
    assert os.path.exists(probe), f"启动器没写 Esc 探针文件（stderr={p.stderr[-2000:]}）"
    with open(probe, encoding="utf-8") as fp:
        pr = json.load(fp)
    assert pr["busy_at_key"], pr                      # Esc 落在"agent 还在跑"的窗口里
    assert pr["set_after_key"], pr                    # 真的置起了中断标记，不是折叠面板

    run_dir = _only_dir(root)
    evs = _assert_wellformed(run_dir)
    turn = [e for e in evs if e["type"] == "turn_end"]
    assert turn, [e["type"] for e in evs]
    assert turn[-1]["reason"] == "interrupted", turn[-1]
    # 中断必须有痕迹：要么落了部分正文、要么说明"一个字都没来得及说"。
    codes = [e.get("code") for e in evs if e["type"] == "notice"]
    assert ("interrupted_partial" in codes) or ("interrupted" in codes), codes


# ── 4. 保留策略：失败留下、正常可清 ──────────────────────────────────
def test_clean_keeps_failure_scene_and_drops_normal():
    """`tools.trace clean` 默认清普通会话，但失败现场必须活着。"""
    root = _fresh_root("clean")
    old_dir = os.environ.get("TRACE_DIR")
    old_sw = os.environ.get("TRACE_ENABLED")
    os.environ["TRACE_ENABLED"] = "1"
    os.environ["TRACE_DIR"] = root
    try:
        good = os.path.join(root, "sess-good")
        bad = os.path.join(root, "sess-bad")
        r1 = trace.session_recording("sess-good", run_dir=good)
        r1.start()
        trace.emit_record({"type": "turn_end", "reason": "answer"})
        r1.finish()
        r2 = trace.session_recording("sess-bad", run_dir=bad)
        r2.start()
        trace.emit_record({"type": "tool_error", "name": "read_file", "code": "enoent"})
        r2.finish()
    finally:
        _restore("TRACE_DIR", old_dir)
        _restore("TRACE_ENABLED", old_sw)
    assert trace.load_manifest(good)["retention"] == trace.RETENTION_NORMAL
    assert trace.load_manifest(bad)["retention"] == trace.RETENTION_FAILURE

    args = types.SimpleNamespace(root=root, older_than="0s", dry_run=False,
                                 quiet=True, include_failures=False)
    assert t_trace.cmd_clean(args) == 0
    assert not os.path.exists(good), "普通会话过了保留期却没被清掉"
    assert os.path.isdir(bad), "失败现场被 clean 删掉了"
    assert os.path.exists(os.path.join(bad, trace.TRACE_EVENTS))

    # 显式 opt-in 才允许连失败现场一起清
    args2 = types.SimpleNamespace(root=root, older_than="0s", dry_run=False,
                                  quiet=True, include_failures=True)
    assert t_trace.cmd_clean(args2) == 0
    assert not os.path.exists(bad), "--include-failures 之后失败现场应当被清"


# ── 5. 开关只在起点求值一次 ─────────────────────────────────────────
def test_switch_is_evaluated_once_at_start():
    """起点之后改 TRACE_ENABLED 不改主意：半路开关比不录更难查。"""
    root = _fresh_root("once")
    old_dir = os.environ.get("TRACE_DIR")
    old_sw = os.environ.get("TRACE_ENABLED")
    os.environ["TRACE_DIR"] = root
    try:
        # (a) 起点关着 → 中途打开也不录
        os.environ.pop("TRACE_ENABLED", None)
        off = trace.session_recording("once-off", run_dir=os.path.join(root, "once-off"))
        off.start()
        assert off.writer is None and trace.current_writer() is None
        os.environ["TRACE_ENABLED"] = "1"
        trace.emit_record({"type": "turn_end", "reason": "answer"})   # 静默空操作
        off.finish()
        assert not os.path.exists(os.path.join(root, "once-off"))

        # (b) 起点开着 → 中途关掉也照录，且收尾照做
        on = trace.session_recording("once-on", run_dir=os.path.join(root, "once-on"))
        on.start()
        assert on.writer is not None and trace.current_writer() is not None
        os.environ.pop("TRACE_ENABLED", None)
        trace.emit_record({"type": "turn_end", "reason": "answer"})
        trace.emit_record({"type": "notice", "level": "warn", "code": "compacted",
                           "message": "起点之后关掉开关，事件仍须落盘"})
        on.finish()
        evs = trace.read_events(os.path.join(root, "once-on"))
        # limits 是 open_writer 在 run_start 之后自己发的一等公民事件（§4.7）。
        assert [e["type"] for e in evs] == ["run_start", "limits", "turn_end",
                                            "notice", "run_end"], evs
    finally:
        _restore("TRACE_DIR", old_dir)
        _restore("TRACE_ENABLED", old_sw)


def _restore(key, old):
    if old is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = old


# ── 6. 与 headroom 遥测隔离 ─────────────────────────────────────────
def test_recording_does_not_touch_headroom_telemetry():
    """录制侧不碰 HEADROOM_STORE_URL，也不往真遥测文件里灌假 usage（§3.1 / 验收 #32）。"""
    root = _fresh_root("headroom")
    before = _hash(_HEADROOM_JSONL)
    p = _run_entry("cli", trace_dir=root)
    assert p.returncode == 0, f"stderr={p.stderr[-2000:]}"
    after = _hash(_HEADROOM_JSONL)
    assert before == after, (
        f"录制把假 usage 写进了真遥测文件 {os.path.relpath(_HEADROOM_JSONL, ROOT)}")

    # 壳自己也不许改环境：HEADROOM_STORE_URL 是回放专用的旋钮，录制侧留给它空着。
    old_dir = os.environ.get("TRACE_DIR")
    old_sw = os.environ.get("TRACE_ENABLED")
    old_store = os.environ.get("HEADROOM_STORE_URL")
    os.environ.pop("HEADROOM_STORE_URL", None)
    os.environ["TRACE_ENABLED"] = "1"
    os.environ["TRACE_DIR"] = root
    try:
        rec = trace.session_recording("hr-env", run_dir=os.path.join(root, "hr-env"))
        rec.start()
        assert os.environ.get("HEADROOM_STORE_URL") is None, "录制壳改了 headroom store"
        rec.finish()
    finally:
        _restore("TRACE_DIR", old_dir)
        _restore("TRACE_ENABLED", old_sw)
        _restore("HEADROOM_STORE_URL", old_store)


def _run_all():
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok  {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    if passed == len(tests):
        # 全绿才清场：失败时保留现场，方便看子进程留下的 trace。
        shutil.rmtree(_TRACE_ROOT, ignore_errors=True)
    return passed == len(tests)


if __name__ == "__main__":
    for k in ("TRACE_ENABLED", "TRACE_DIR"):
        os.environ.pop(k, None)   # 套件自己决定开关，别继承外面的
    sys.exit(0 if _run_all() else 1)
