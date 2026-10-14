"""L1 trace 冒烟：用假 transport 驱动真循环**并**走 llm 网关，检查事件流是否自洽。

为什么不是直接换 `runner.client`（tests/test_runner_visibility.py 的做法）：那样会绕过
llm 网关，于是 llm_request / llm_chunk / llm_end 全部不产生——trace 里只剩半截事实。
这里把假的 transport 挂在网关**下面**（`llm.set_transport`），走的是和录制/回放完全
相同的路径；`runner.client` 保持为真网关。
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DS_KEY", "sk-selftest-not-a-real-key")
os.environ["TRACE_ENABLED"] = "1"
os.environ["TRACE_DIR"] = os.path.join(ROOT, "tmp", "traces-smoke")
os.environ["FILTERED_LOG"] = os.path.join(ROOT, "tmp", "_smoke_filtered.log")
os.environ["CACHE_LOG"] = os.path.join(ROOT, "tmp", "_smoke_cache.log")

import loop_agent_core.llm as llm  # noqa: E402
import loop_agent_core.trace as trace  # noqa: E402
import loop_agent_v2 as la  # noqa: E402


class _Delta:
    def __init__(self, reasoning=None, content=None, tool_calls=None):
        self.reasoning_content = reasoning
        self.content = content
        self.tool_calls = tool_calls


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, index, cid, name=None, arguments=None):
        self.index = index
        self.id = cid
        self.function = _Fn(name, arguments)


class _Choice:
    def __init__(self, delta=None, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason
        self.message = delta


class _Usage:
    prompt_tokens = 100
    completion_tokens = 5
    total_tokens = 105
    prompt_cache_hit_tokens = 40
    prompt_cache_miss_tokens = 60


class _Chunk:
    """模拟一个流式 chunk：choices 可能为空（尾块的 usage 包）。

    注意：真实 SDK 里带 finish_reason 的 chunk **仍有** delta 对象（只是字段为 None），
    runner 是直接 `chunk.choices[0].delta.content` 取的，所以不许给 delta=None。
    """

    def __init__(self, delta=None, finish_reason=None, usage=False, no_choices=False):
        if no_choices:
            self.choices = []
        else:
            self.choices = [_Choice(delta if delta is not None else _Delta(),
                                    finish_reason)]
        self.usage = _Usage() if usage else None


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks
        self._it = iter(chunks)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close(self):
        pass


class FakeTransport:
    """按调用次序吐预设批次；用完复用最后一批。"""

    def __init__(self, batches):
        self._batches = list(batches)
        self.calls = 0
        self.seen = []

    def create(self, kind, cfg, kwargs):
        self.seen.append({"kind": kind, "model": kwargs.get("model"),
                          "stream": kwargs.get("stream"),
                          "messages": kwargs.get("messages")})
        idx = min(self.calls, len(self._batches) - 1)
        self.calls += 1
        return _Stream(self._batches[idx])


def _run(batches, question="干活", session=None):
    session = session or ("smoke-%d" % (int(time.time()) % 100000))
    conv = {"messages": [{"role": "system", "content": "t"}], "size": None,
            "_archive": [], "_compact_state": {}, "_session_id": session}
    events = []
    transport = FakeTransport(batches)
    llm.set_transport(transport)
    # 每个场景一套干净的 trace 目录 + 干净的 checkpoint 存储缓存（同一个 session 名会
    # 让第二次 open_writer 续写上一次的事件，seek 就会命中旧现场）。
    from loop_agent_core import checkpoint as cp
    cp.close_store()
    cp.reset_counters()
    w = trace.open_writer(session, run_dir=os.path.join(os.environ["TRACE_DIR"], session),
                          manifest={"session_id": session, "backend": "fake",
                                    "model": "deepseek-v4-flash"})
    try:
        out = la.chat(question, on_event=events.append, _conv=conv)
    finally:
        llm.reset()
        trace.close_writer(w)
        cp.close_store()
    return out, events, conv, w.run_dir, transport


def _dump(run_dir):
    for line in open(os.path.join(run_dir, "trace.jsonl"), encoding="utf-8"):
        e = json.loads(line)
        print(f"  {e.get('seq'):>3} {e['type']:<16} ri={e.get('request_index')} "
              f"turn={e.get('turn')} tr={e.get('tool_round')} "
              f"{e.get('reason') or e.get('kind') or e.get('code') or ''}")


def _tool_round_batches():
    """一个工具轮的假后端：先要一次工具，再给最终答案。"""
    call = _Chunk(_Delta(tool_calls=[_ToolCall(0, "c1", "get_date", "{}")]))
    done = _Chunk(_Delta(content="今天是某天"), finish_reason="stop")
    return [[call, _Chunk(_Delta(), finish_reason="tool_calls"),
             _Chunk(usage=True, no_choices=True)],
            [done, _Chunk(usage=True, no_choices=True)]]


def main():
    scene = sys.argv[1] if len(sys.argv) > 1 else "all"

    if scene in ("all", "stuck"):
        thinking = [_Chunk(_Delta(reasoning="我再想想，先不调用工具")),
                    _Chunk(_Delta(), finish_reason="stop"),
                    _Chunk(usage=True, no_choices=True)]
        out, events, conv, run_dir, transport = _run([thinking])
        print("=== 场景1：只出思考（3 次 nudge → stuck）===")
        print("out:", out[:40].replace("\n", " "))
        print("transport calls:", transport.calls, "kinds:", [s["kind"] for s in transport.seen])
        print("screen notices:", [(e.get("level"), e.get("code"))
                                  for e in events if e.get("type") == "notice"])
        _dump(run_dir)
        evs = trace.read_events(run_dir)
        print("validate:", trace.validate_events(evs))
        reqs = [e for e in evs if e["type"] == "llm_request"]
        chunks = [e for e in evs if e["type"] == "llm_chunk"]
        print(f"llm_request={len(reqs)} llm_end="
              f"{len([e for e in evs if e['type'] == 'llm_end'])} llm_chunk={len(chunks)}")
        print("请求序号:", [e["request_index"] for e in reqs])
        print("chunk 序号:", sorted({e["request_index"] for e in chunks}))
        for e in reqs[:2]:
            print(f"  req{ e['request_index'] } messages 条数={len(e['messages'] or [])} "
                  f"tail_role={(e['messages'] or [{}])[-1].get('role')}")
        # ── checkpoint 落盘 / seek ──
        from loop_agent_core import checkpoint as cp
        store = cp.CheckpointStore(run_dir)
        print("frames:", [(f["group_id"], f["requests"]) for f in store.index["frames"]])
        for e in reqs:
            ri = e["request_index"]
            try:
                snap = store.find_for_request(ri, e["messages"])
            except Exception as exc:  # noqa: BLE001
                print(f"  seek ri={ri} 失败：{type(exc).__name__}: {exc}")
                continue
            print(f"  seek ri={ri} → snapshot ri={snap['request_index']} "
                  f"turn={snap['turn']} tr={snap['tool_round']} kind={snap['kind']} "
                  f"decompress={store.decompress_count()}")
        print("解压次数:", store.decompress_count(), "请求数:", len(reqs))

    if scene in ("all", "tool"):
        print("=== 场景2：一轮工具调用 ===")
        out, events, conv, run_dir, transport = _run(_tool_round_batches(), question="今天几号")
        print("out:", out[:60].replace("\n", " "))
        print("transport calls:", transport.calls)
        _dump(run_dir)
        evs = trace.read_events(run_dir)
        print("validate:", trace.validate_events(evs))
        tc = [e for e in evs if e["type"] == "tool_call"]
        tr = [e for e in evs if e["type"] == "tool_result"]
        print("tool_call:", [(e["tool_name"], e.get("tool_class"), e.get("call_id")) for e in tc])
        print("tool_result:", [(e["tool_name"], e.get("error"), e.get("result_chars")) for e in tr])
        reqs = [e for e in evs if e["type"] == "llm_request"]
        print("请求的 (turn, tr, ri):",
              [(e["turn"], e["tool_round"], e["request_index"]) for e in reqs])
        from loop_agent_core import checkpoint as cp
        store = cp.CheckpointStore(run_dir)
        for e in reqs:
            ri = e["request_index"]
            try:
                snap = store.find_for_request(ri, e["messages"])
                print(f"  seek ri={ri} → snap ri={snap['request_index']} "
                      f"turn={snap['turn']} tr={snap['tool_round']}")
            except Exception as exc:  # noqa: BLE001
                print(f"  seek ri={ri} 失败：{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
