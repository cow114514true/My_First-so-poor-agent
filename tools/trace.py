"""trace 工具箱（施工文档 §4.8 / §4.1 / §10）。

用法（在仓库根跑）：

    python -m tools.trace list
    python -m tools.trace validate tmp/traces/<sid>
    python -m tools.trace show tmp/traces/<sid> --type llm_request --limit 5
    python -m tools.trace show tmp/traces/<sid> --format otel
    python -m tools.trace extract-cassette tmp/traces/<sid> --out tests/fixtures/replay/<name>/ \
        --expected-assertion "turn_end:reason=api_error"
    python -m tools.trace replay tests/fixtures/replay/<name>/
    python -m tools.trace clean --older-than 30d --dry-run

设计取舍：CLI 只做**读取与裁剪**，从不修改已有 trace。`clean` 是唯一的删除入口，且只删
"确认是 trace 目录"的那些（有 trace.jsonl / meta.json），避免一个手滑的路径参数把别的
东西清掉。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loop_agent_core import cassette as cassette_mod  # noqa: E402
from loop_agent_core import replay as replay_mod      # noqa: E402
from loop_agent_core import trace as trace_mod        # noqa: E402


# ── 辅助 ────────────────────────────────────────────────────────────
def _resolve_dir(path: str) -> str:
    """把 `tmp/traces/<sid>` / `<sid>` / trace 文件路径都归一成目录。

    `list` 打印的是会话名（目录名），用户自然会把它原样喂回来，所以裸名字要去
    trace 根下再找一次——否则每次都得手抄一遍 tmp/traces 前缀。
    """
    p = os.path.abspath(path)
    if not os.path.exists(p):
        cand = os.path.join(trace_mod.trace_root(), path)
        if os.path.exists(cand):
            p = os.path.abspath(cand)
    if os.path.isfile(p):
        p = os.path.dirname(p)
    if not os.path.isdir(p):
        raise SystemExit(f"目录不存在：{p}")
    if not os.path.exists(os.path.join(p, trace_mod.TRACE_EVENTS)):
        raise SystemExit(f"{p} 里没有 {trace_mod.TRACE_EVENTS}，不是 trace 目录")
    return p


def _load(path: str):
    d = _resolve_dir(path)
    return d, trace_mod.read_events(d)


def _fmt_size(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.1f}{unit}" if unit != "B" else f"{int(v)}B"
        v /= 1024
    return f"{n}B"


def _parse_age(text: str) -> float:
    """`30d` / `12h` / `90m` / `45s` → 秒。"""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*", str(text or ""))
    if not m:
        raise SystemExit(f"无法解析时长：{text!r}（示例：30d / 12h / 90m）")
    n = float(m.group(1))
    unit = m.group(2) or "d"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]


def _is_trace_dir(path: str) -> bool:
    """clean 的安全闸：只认"像 trace 的目录"。"""
    if not os.path.isdir(path):
        return False
    if os.path.exists(os.path.join(path, trace_mod.TRACE_EVENTS)):
        return True
    man = os.path.join(path, trace_mod.TRACE_META)
    if os.path.exists(man):
        try:
            with open(man, "r", encoding="utf-8") as f:
                meta = json.load(f)
            return isinstance(meta, dict) and "session_id" in meta
        except (OSError, ValueError):
            return False
    return False


# ── 子命令 ──────────────────────────────────────────────────────────
def cmd_list(args) -> int:
    root = args.root or trace_mod.trace_root()
    runs = trace_mod.list_runs(root)
    if args.json:
        print(json.dumps(runs, ensure_ascii=False, indent=2))
        return 0
    if not runs:
        print(f"（{root} 下没有 trace）")
        return 0
    print(f"{'会话':<34} {'级别':<14} {'后端/模型':<28} {'大小':>9}  修改时间")
    for r in runs:
        man = r.get("manifest") or {}
        size = trace_mod.dir_size(r["dir"])
        flag = " ⚠" if size > trace_mod.SIZE_WARN_BYTES else ""
        level = {0: "assert", 1: "stream_summary", 2: "stream"}.get(man.get("trace_level"),
                                                                  man.get("trace_level", "?"))
        print(f"{r['run_id']:<34} {str(level):<14} "
              f"{str(man.get('backend', '?')) + '/' + str(man.get('model', '?')):<28} "
              f"{_fmt_size(size):>9}{flag}  "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(r['mtime'] or 0))}")
    return 0


def cmd_validate(args) -> int:
    _, events = _load(args.trace_dir)
    problems = trace_mod.validate_events(events, strict=args.strict)
    counts: dict = {}
    for e in events:
        counts[e.get("type")] = counts.get(e.get("type"), 0) + 1
    print(f"事件 {len(events)} 条，类型 {len(counts)} 种")
    if args.types:
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {k:<24} {v}")
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems[:50]:
            print(f"  - {p}")
        return 1
    print("结构自检通过")
    return 0


def _otel_view(events: list) -> list:
    """把自研事件映射成一棵 OTEL 风格的 span 树（派生视图，§10）。

    只映射有意义的边界：一次 llm_request→llm_end 是一个 span，一个工具调用是一个 span。
    trace 本身仍是唯一事实来源；这个视图是给已有的可观测后端看的，丢了也不影响回放。
    """
    spans = []
    open_req: dict = {}
    trace_id = None
    for e in events:
        t = e.get("type")
        if t == "run_start":
            trace_id = e.get("run_id")
            spans.append({"name": "agent.run", "trace_id": trace_id, "span_id": e.get("seq"),
                          "start": e.get("ts"), "attributes": {"session": e.get("session"),
                                                               "level": e.get("level")}})
        elif t == "llm_request":
            open_req[e.get("request_index")] = {
                "name": f"llm.{e.get('kind')}", "trace_id": trace_id, "span_id": e.get("seq"),
                "parent": "agent.run", "start": e.get("ts"), "turn": e.get("turn"),
                "attributes": {"model": e.get("model"), "kind": e.get("kind"),
                               "messages": len(e.get("messages") or []),
                               "tools": e.get("tools_count")}}
        elif t == "llm_end":
            span = open_req.pop(e.get("request_index"), None)
            if span is None:
                continue
            span["end"] = e.get("ts")
            span["duration_ms"] = e.get("latency_ms")
            span["attributes"].update({"finish_reason": e.get("finish_reason"),
                                       "usage": e.get("usage"),
                                       "interrupted": e.get("interrupted")})
            spans.append(span)
        elif t == "tool_call":
            spans.append({"name": f"tool.{e.get('name')}", "trace_id": trace_id,
                          "span_id": e.get("seq"), "start": e.get("ts"),
                          "attributes": {"call_id": e.get("call_id"), "class": e.get("class")}})
        elif t == "tool_result":
            spans.append({"name": f"tool.{e.get('name')}.result", "trace_id": trace_id,
                          "span_id": e.get("seq"), "start": e.get("ts"),
                          "attributes": {"call_id": e.get("call_id"),
                                         "error": e.get("error"),
                                         "chars": e.get("result_stored_chars")}})
        elif t in ("notice", "error", "turn_end"):
            spans.append({"name": f"event.{t}", "trace_id": trace_id, "span_id": e.get("seq"),
                          "start": e.get("ts"),
                          "attributes": {k: v for k, v in e.items()
                                         if k not in ("seq", "ts", "type", "messages",
                                                      "result_full", "result_stored",
                                                      "arguments_raw")}})
    return spans


def cmd_export(args) -> int:
    """派生视图导出（§10 已定项 4）：OTEL 不进 L0 存储层，只在这里生成。

    `--shareable` 是 L2 的对外口径（§8.4：抹正文/文件内容/路径/URL query）——本阶段
    只做 trace 自己的脱敏（密钥在写入时就已抹掉），额外抹正文的能力留在 L2。
    """
    d, events = _load(args.trace_dir)
    fmt = (args.format or "otel").lower()
    if fmt == "otel":
        payload = _otel_view(events)
        text = "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in payload)
    elif fmt == "json":
        text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    else:
        raise SystemExit(f"未知导出格式：{fmt}（支持 otel / json）")
    if args.out:
        out = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"已导出 {len(events)} 条事件（{fmt}）→ {out}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_show(args) -> int:
    _, events = _load(args.trace_dir)
    if args.type:
        events = [e for e in events if e.get("type") == args.type]
    if args.assertions:
        for aid in trace_mod.assertion_ids(events):
            print(aid)
        return 0
    if args.format == "otel":
        print(json.dumps(_otel_view(events), ensure_ascii=False, indent=2))
        return 0
    if args.format == "json":
        for e in events[:args.limit] if args.limit else events:
            print(json.dumps(e, ensure_ascii=False))
        return 0
    for e in (events[:args.limit] if args.limit else events):
        print(_one_line(e))
    return 0


def _one_line(e: dict) -> str:
    """一条事件压成一行人类可读文本（回放报告和排障都靠它）。"""
    t = e.get("type")
    ts = str(e.get("ts") or "")[11:23]
    head = f"[{e.get('seq'):>5}] {ts} {str(t):<22}"
    if t == "llm_request":
        return (f"{head} kind={e.get('kind')} model={e.get('model')} "
                f"msgs={len(e.get('messages') or [])} tools={e.get('tools_count')} "
                f"stream={e.get('stream')}")
    if t == "llm_end":
        return (f"{head} finish={e.get('finish_reason')} "
                f"usage={(e.get('usage') or {}).get('total_tokens')} "
                f"latency={e.get('latency_ms')}ms interrupted={e.get('interrupted')}")
    if t == "llm_chunk":
        bits = []
        if e.get("content"):
            bits.append(f"content={str(e['content'])[:40]!r}")
        if e.get("reasoning_content"):
            bits.append(f"thinking={str(e['reasoning_content'])[:40]!r}")
        if e.get("tool_calls"):
            bits.append(f"tool_calls={len(e['tool_calls'])}")
        return f"{head} ri={e.get('request_index')} " + " ".join(bits)
    if t in ("tool_call", "tool_result"):
        extra = (f"args={str(e.get('arguments_raw'))[:60]}"
                 if t == "tool_call" else
                 f"error={e.get('error_code')} chars={e.get('result_stored_chars')}")
        return (f"{head} {e.get('name')}({str(e.get('call_id'))[:12]}) "
                f"class={e.get('class')} {extra}")
    if t == "notice":
        return f"{head} {e.get('level')}/{e.get('code')} {str(e.get('message'))[:80]}"
    if t == "turn_end":
        return f"{head} reason={e.get('reason')} answer_chars={e.get('answer_chars')}"
    if t in ("limits",):
        knobs = e.get("knobs") or {}
        return f"{head} site={e.get('site')} " + ",".join(f"{k}={v}" for k, v in list(knobs.items())[:8])
    if t == "checkpoint":
        return (f"{head} kind={e.get('kind')} ri={e.get('request_index')} "
                f"msgs={e.get('messages_len')} hash={str(e.get('messages_hash'))[:12]}")
    if t == "error":
        return f"{head} {e.get('where')} {e.get('exc_type')}: {str(e.get('message'))[:80]}"
    rest = {k: v for k, v in e.items()
            if k not in ("seq", "ts", "type", "turn", "tool_round", "request_index", "session")}
    return f"{head} " + " ".join(f"{k}={str(v)[:40]}" for k, v in rest.items())


def cmd_assertions(args) -> int:
    _, events = _load(args.trace_dir)
    ids = trace_mod.assertion_ids(events)
    if args.json:
        print(json.dumps(ids, ensure_ascii=False, indent=2))
        return 0
    if not ids:
        print("（没有断言级事件：这份 trace 是纯成功路径，或级别太低）")
        return 0
    for aid in ids:
        print(aid)
    return 0


def cmd_extract(args) -> int:
    meta = cassette_mod.extract_cassette(
        _resolve_dir(args.trace_dir), args.out, from_request=args.from_request or 0,
        to_request=args.to_request, playback_mode=args.mode,
        derived_from=args.derived_from, expected_assertion=args.expected_assertion,
        workspace_root=args.workspace_root, force=args.force)
    check = meta.get("replay_check") or {}
    print(f"cassette 已写入 {os.path.abspath(args.out)}")
    print(f"  请求 {meta.get('request_count', '?')} 条 / checkpoint {meta.get('checkpoint_count', 0)} 份")
    print(f"  完整度 llm={((meta.get('completeness') or {}).get('llm'))} "
          f"tools={((meta.get('completeness') or {}).get('tools'))}")
    print(f"  断言面 {len(meta.get('assertion_ids') or [])} 条；"
          f"目标断言={meta.get('expected_assertion') or '（未声明）'}")
    if meta.get("expected_assertion"):
        print(f"  回放自检：{'已复现' if check.get('reproduced') else check.get('reason')}")
    return 0


def cmd_replay(args) -> int:
    report = replay_mod.run_replay(
        args.cassette_dir, question=args.question, mode=args.mode, strict=args.strict,
        window=args.window, tool_policy=args.tool_policy,
        overlay_root=args.overlay_root, overlay_read=args.overlay_read,
        exec_c=args.exec_c, keep_overlay=args.keep_overlay, exec_date=args.exec_date)
    print(report.text())
    if args.expected_assertion and report.expected_assertion != args.expected_assertion:
        print(f"✗ cassette 声明的目标断言 {report.expected_assertion!r} 与命令行给的 "
              f"{args.expected_assertion!r} 不一致")
        return 2
    if report.expected_assertion and not report.reproduced:
        return 1
    if args.max_skips is not None and report.skips_count > args.max_skips:
        print(f"✗ 跳过 {report.skips_count} 个请求，超过上限 {args.max_skips}")
        return 1
    return 0


def _retention_of(d):
    """读一份 trace 的保留判据（meta.json 的 retention）；读不出来返回 None。

    None 表示「录制壳之外产生的 trace」——手工脚本、迁移产物、老录像。这类没有失败
    标记可言，按普通会话走保留期，不给它开免死金牌。
    """
    try:
        return trace_mod.load_manifest(d).get("retention")
    except (OSError, trace_mod.TraceError):
        return None


def cmd_clean(args) -> int:
    """删掉超过保留期的会话目录（§4.1：默认 30 天）。

    安全闸：只删 trace 根目录下、且 `_is_trace_dir()` 认得的直接子目录。传进来的 root
    必须是一个存在的目录——不做任何"向上找"的猜测。

    第二条闸（§4.10）：录制壳标了 `retention=failure` 的会话**不随保留期消失**。失败
    现场正是这套设施存在的理由，自动删掉它等于把最贵的证据先扔了。要连它一起清必须
    显式 `--include-failures`。
    """
    root = os.path.abspath(args.root or trace_mod.trace_root())
    if not os.path.isdir(root):
        print(f"（{root} 不存在，无需清理）")
        return 0
    age = _parse_age(args.older_than)
    now = time.time()
    victims = []
    held = []
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not _is_trace_dir(d):
            if not args.quiet:
                print(f"  跳过（不是 trace 目录）：{name}")
            continue
        newest = 0.0
        for f in os.listdir(d):
            try:
                newest = max(newest, os.path.getmtime(os.path.join(d, f)))
            except OSError:
                pass
        if now - newest > age:
            if _retention_of(d) == trace_mod.RETENTION_FAILURE and not args.include_failures:
                held.append((d, now - newest))
                continue
            victims.append((d, trace_mod.dir_size(d), now - newest))
    total = sum(v[1] for v in victims)
    for d, size, age_s in victims:
        print(f"  {'将删除' if args.dry_run else '删除'} {os.path.relpath(d, root)}"
              f"（{_fmt_size(size)}，{age_s / 86400:.1f} 天未动）")
        if not args.dry_run:
            shutil.rmtree(d, ignore_errors=True)
    for d, age_s in held:
        print(f"  保留（失败现场，--include-failures 才清）{os.path.relpath(d, root)}"
              f"（{age_s / 86400:.1f} 天未动）")
    print(f"{'将清理' if args.dry_run else '已清理'} {len(victims)} 个会话，"
          f"{_fmt_size(total)}（保留期 {args.older_than}）"
          + (f"；另保留 {len(held)} 个失败现场" if held else ""))
    return 0


def cmd_size(args) -> int:
    root = args.root or trace_mod.trace_root()
    runs = trace_mod.list_runs(root)
    over = []
    fails = 0
    for r in runs:
        size = trace_mod.dir_size(r["dir"])
        failure = _retention_of(r["dir"]) == trace_mod.RETENTION_FAILURE
        fails += 1 if failure else 0
        if size > trace_mod.SIZE_WARN_BYTES or args.all:
            over.append((r["run_id"], size, failure))
    over.sort(key=lambda kv: -kv[1])
    if not over:
        print(f"{len(runs)} 个会话，均未超过 {_fmt_size(trace_mod.SIZE_WARN_BYTES)}"
              + (f"（失败现场 {fails} 个）" if fails else ""))
        return 0
    for rid, size, failure in over:
        mark = "⚠" if size > trace_mod.SIZE_WARN_BYTES else " "
        print(f"{mark} {rid:<40} {_fmt_size(size)}"
              + ("  失败现场（clean 默认保留）" if failure else ""))
    if fails:
        print(f"失败现场 {fails} 个：clean 默认保留，需 --include-failures 才清")
    return 0


# ── 解析 ────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tools.trace",
                                description="trace / cassette 工具箱（只读；唯一删除入口是 clean）")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("list", help="列出会话")
    q.add_argument("--root", default=None)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_list)

    q = sub.add_parser("validate", help="结构自检")
    q.add_argument("trace_dir")
    q.add_argument("--strict", action="store_true")
    q.add_argument("--types", action="store_true", help="同时打印事件类型分布")
    q.set_defaults(func=cmd_validate)

    q = sub.add_parser("show", help="查看事件（text/json/otel）")
    q.add_argument("trace_dir")
    q.add_argument("--type", default=None)
    q.add_argument("--limit", type=int, default=0)
    q.add_argument("--format", choices=("text", "json", "otel"), default="text")
    q.add_argument("--assertions", action="store_true", help="只列断言 id")
    q.set_defaults(func=cmd_show)

    q = sub.add_parser("assertions", help="列出断言 id（§3.4 词表）")
    q.add_argument("trace_dir")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_assertions)

    q = sub.add_parser("export", help="导出派生视图（§10：--format otel）")
    q.add_argument("trace_dir")
    q.add_argument("--format", choices=("otel", "json"), default="otel")
    q.add_argument("--out", default=None, help="输出文件（缺省打到 stdout）")
    q.add_argument("--shareable", action="store_true",
                   help="对外分享口径（正文抹除属 L2 §8.4 范围）")
    q.set_defaults(func=cmd_export)

    q = sub.add_parser("extract-cassette", help="抽取可独立回放的证据包（§4.8）")
    q.add_argument("trace_dir")
    q.add_argument("--out", required=True)
    q.add_argument("--from-request", type=int, default=0)
    q.add_argument("--to-request", type=int, default=None)
    q.add_argument("--mode", choices=cassette_mod.PLAYBACK_MODES,
                   default=cassette_mod.PLAYBACK_FINGERPRINT)
    q.add_argument("--derived-from", default=None)
    q.add_argument("--expected-assertion", default=None)
    q.add_argument("--workspace-root", default=None)
    q.add_argument("--force", action="store_true")
    q.set_defaults(func=cmd_extract)

    q = sub.add_parser("replay", help="回放一个 cassette（§6）")
    q.add_argument("cassette_dir")
    q.add_argument("--mode", choices=cassette_mod.PLAYBACK_MODES, default=None)
    q.add_argument("--strict", action="store_true")
    q.add_argument("--window", type=int, default=None)
    q.add_argument("--question", default=None)
    q.add_argument("--expected-assertion", default=None)
    q.add_argument("--max-skips", type=int, default=None)
    q.add_argument("--tool-policy", choices=("cassette", "class"), default="cassette")
    # L2（§8）只在 --tool-policy class 下生效；这些开关默认全关，回放的默认行为
    # 保持 L1：只吐录制结果、一个字节都不写到工作区。
    q.add_argument("--overlay-root", default=None,
                   help="overlay 根目录（默认 tmp/overlay；必须落在 WORK_DIR 之内）")
    q.add_argument("--overlay-read", action="store_true",
                   help="A 类读被本次回放写过的路径时，读 overlay 而非录制结果（§8.2.3）")
    q.add_argument("--exec-c", action="store_true",
                   help="C 类也真执行（默认只吐录制结果；开了就不是离线回放了）")
    q.add_argument("--keep-overlay", action="store_true",
                   help="回放结束保留 overlay 目录，便于人工核对")
    q.add_argument("--no-exec-date", dest="exec_date", action="store_false",
                   help="get_date 不注入 trace_epoch 的日期，改吐录制结果")
    q.set_defaults(func=cmd_replay, exec_date=True)

    q = sub.add_parser("clean", help="清理过期会话")
    q.add_argument("--older-than", default="30d")
    q.add_argument("--root", default=None)
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--quiet", action="store_true")
    q.add_argument("--include-failures", action="store_true",
                   help="连失败现场（retention=failure）一起清；默认保留")
    q.set_defaults(func=cmd_clean)

    q = sub.add_parser("size", help="会话体积与告警")
    q.add_argument("--root", default=None)
    q.add_argument("--all", action="store_true")
    q.set_defaults(func=cmd_size)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (cassette_mod.CassetteError, replay_mod.ReplayError) as exc:
        print(f"✗ {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except trace_mod.TraceError as exc:
        print(f"✗ TraceError: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
