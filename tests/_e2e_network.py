r"""真网络端到端实践测试（手动，不进 run_tests.py 的 SUITES）。

覆盖一条现有 fixture 覆盖不到的路径：真实云端 backend（deepseek-v4-flash）录一场
真实会话，再离线走完 录制 -> 抽 cassette -> 多轮回放 -> L3 轮级收缩 的整条链路。
入库 fixture 全是 backend=fake 的 _FakeTransport，证明不了真指纹/真工具轮/真
turn_start 在回放侧仍然成立。

用法（需要真实 DS_KEY，会走网络、消耗真实 token）：

    $env:DS_KEY = [Environment]::GetEnvironmentVariable('DS_KEY','User')
    # DSH 沙箱下 probe 子进程的 mkdtemp 会被拒，临时挂站点补丁（正常终端不需要）：
    $env:PYTHONPATH = '<repo>/tmp/sbxsite'
    python tests/_e2e_network.py

产物全在 tmp/e2e_net/（已 gitignore）：traces/ 原始 trace、cassettes/ 抽出的 cassette、
replays/ 回放 run_dir、report.json 结构化结论。默认 3 个用户轮；失败任一段会打印 JSON 并退出 1。
"""
import json
import os
import shutil
import sys
import time
import traceback

E2E = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(E2E, ".."))
ART = os.path.join(ROOT, "tmp", "e2e_net")
sys.path.insert(0, ROOT)

os.environ["TRACE_ENABLED"] = "1"
os.environ["TRACE_LEVEL"] = "assert"
os.environ["TRACE_DIR"] = os.path.join(ART, "traces")
os.environ["AGENT_CONFIRM_POLICY"] = "allow_all"
os.environ["FILTERED_LOG"] = os.path.join(ART, "debug_filtered.txt")
os.environ["CACHE_LOG"] = os.path.join(ART, "cache_events.jsonl")

report = {"stages": {}}


def stage(name, fn, **extra):
    t0 = time.time()
    try:
        out = fn()
        report["stages"][name] = {"ok": True, "elapsed": round(time.time() - t0, 1), **extra, **(out or {})}
    except Exception as exc:  # noqa: BLE001
        report["stages"][name] = {"ok": False, "elapsed": round(time.time() - t0, 1), **extra,
                                  "error": f"{type(exc).__name__}: {exc}",
                                  "trace": traceback.format_exc()[-1500:]}
    return report["stages"][name]


def finish(code):
    os.makedirs(ART, exist_ok=True)
    with open(os.path.join(ART, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(code)


if not os.environ.get("DS_KEY"):
    print(json.dumps({"error": "DS_KEY missing"}, ensure_ascii=False))
    sys.exit(2)

from loop_agent_core import cassette as cassette_mod          # noqa: E402
from loop_agent_core import checkpoint as cp                  # noqa: E402
from loop_agent_core import confirm, config, llm, trace       # noqa: E402
from loop_agent_core import replay as replay_mod              # noqa: E402
from loop_agent_core.prompts import system_prompt             # noqa: E402
import loop_agent_v2 as la                                    # noqa: E402
from tools import shrink as shrink_mod                        # noqa: E402

WORK = os.path.join(ART, "work")
TRACES = os.environ["TRACE_DIR"]
CASSETTES = os.path.join(ART, "cassettes")
HELLO = os.path.join(WORK, "hello.txt")
MARKER = "E2E-HELLO-42"
REL = os.path.relpath(HELLO, ROOT).replace("\\", "/")

QUESTIONS = [
    f"请用 read_file 工具读取 {REL}，然后原样告诉我文件内容。",
    f"再用 read_file 读一次同一个文件 {REL}，只回答文件内容。",
    "只回复 ok 两个字。",
]
EXPECTED = "turn_end:reason=answer"

report["env"] = {"backend": config.cfg["base_url"], "model": config.CURRENT_MODEL,
                 "root": ROOT, "has_key": bool(os.environ.get("DS_KEY"))}


def do_record():
    os.makedirs(WORK, exist_ok=True)
    with open(HELLO, "w", encoding="utf-8") as f:
        f.write(MARKER + "\n")
    run_dir = os.path.join(TRACES, "e2e-net-" + str(int(time.time())))
    os.makedirs(run_dir, exist_ok=True)
    conv = {"messages": [{"role": "system", "content": system_prompt}], "size": None,
            "_archive": [], "_compact_state": {}, "_session_id": "e2e-net"}
    prev_policy = confirm.get_policy()
    confirm.set_policy("allow_all")
    cp.close_store()
    cp.reset_counters()
    writer = trace.open_writer("e2e-net", run_dir=run_dir,
                               manifest={"session_id": "e2e-net", "backend": "deepseek",
                                         "model": config.CURRENT_MODEL})
    outs = []
    try:
        for q in QUESTIONS:
            outs.append(str(la.chat(q, _conv=conv))[:300])
    finally:
        try:
            writer.write_index()
        except Exception:  # noqa: BLE001
            pass
        trace.close_writer(writer)
        cp.close_store()
        confirm.set_policy(prev_policy)
        llm.reset()
    events = trace.read_events(run_dir)
    by_type = {}
    for e in events:
        by_type[e.get("type")] = by_type.get(e.get("type"), 0) + 1
    ids = trace.assertion_ids(events)
    report["run_dir"] = run_dir
    return {"run_dir": run_dir, "assistant": outs, "assertion_ids": ids,
            "event_types": by_type, "event_count": len(events),
            "messages": len(conv["messages"]), "expected_present": EXPECTED in ids}


rec = stage("record", do_record)
if not rec.get("ok"):
    finish(1)

RUN_DIR = rec["run_dir"]
OUT_DIR = os.path.join(CASSETTES, "e2e-net")


def do_extract():
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    meta = cassette_mod.extract_cassette(
        RUN_DIR, OUT_DIR, playback_mode="fingerprint", expected_assertion=EXPECTED,
        derived_from="tests/_e2e_network.py (real-network E2E)", workspace_root=ROOT, force=True)
    return {"meta": {k: meta.get(k) for k in ("playback_mode", "expected_assertion", "request_count",
                                              "completeness", "degraded_reasons", "assertion_ids",
                                              "checkpoint_count", "replay_check")}}


ext = stage("extract", do_extract)
if not ext.get("ok"):
    finish(1)


def do_replay():
    rdir = os.path.join(ART, "replays", "post-record")
    os.makedirs(rdir, exist_ok=True)
    rep = replay_mod.run_replay(OUT_DIR, run_dir=rdir, all_turns=True, check_env=False)
    return {"reproduced": rep.reproduced, "assertion_ids": rep.assertion_ids,
            "matches": len(rep.matches), "turns": getattr(rep, "turns", None),
            "all_turns": getattr(rep, "all_turns", None)}


stage("replay_all_turns", do_replay)


def do_shrink():
    sout = os.path.join(CASSETTES, "e2e-net-shrunk")
    shutil.rmtree(sout, ignore_errors=True)
    rep = shrink_mod.shrink_cassette(OUT_DIR, sout, expected_assertion=EXPECTED,
                                     max_replays=80, runs=1, timeout=600, force=True)
    return {"out_dir": sout, "converged": rep.get("converged"), "result": rep.get("result"),
            "replays": rep.get("replays"), "response_count_before": rep.get("response_count_before"),
            "response_count_after": rep.get("response_count_after"),
            "deleted_units": rep.get("deleted_units"), "deleted_events": rep.get("deleted_events"),
            "unconsumed": rep.get("unconsumed"), "checkpoint_b_patched": rep.get("checkpoint_b_patched"),
            "verify": rep.get("verify"), "substituted_assertion": rep.get("substituted_assertion")}


sh = stage("shrink_L3", do_shrink)

ok = all(s.get("ok") for s in report["stages"].values())
report["ok"] = ok
report["summary"] = {
    "recorded_requests": rec.get("event_types", {}).get("llm_request"),
    "recorded_turns": rec.get("event_types", {}).get("turn_start"),
    "tool_calls": rec.get("event_types", {}).get("tool_call"),
    "extract_reproduced": ext.get("meta", {}).get("replay_check", {}).get("reproduced"),
    "replay_all_turns_reproduced": report["stages"].get("replay_all_turns", {}).get("reproduced"),
    "replay_turns": report["stages"].get("replay_all_turns", {}).get("turns"),
    "shrink_converged": sh.get("converged"),
    "shrink_response": [sh.get("response_count_before"), sh.get("response_count_after")],
    "shrink_deleted_units": sh.get("deleted_units"),
}
finish(0 if ok else 1)
