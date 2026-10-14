"""trace → cassette 的抽取与加载（施工文档 §4.8）。

cassette 是**能独立回放的最小证据包**：一次 trace 里绝大多数事件只是给人看的诊断，
回放真正需要的只有「请求指纹 + 冻结的响应 + 工具结果 + 复现用的 checkpoint + 断言面」。
这个模块只做两件事：

* `extract_cassette()`：从 trace 目录里把上述内容抽到一个自包含目录；
* `Cassette`：加载并**按 §4.8 的规则校验**，校验不过就抛错，绝不"降级读下去"。

为什么校验要 fail-closed：cassette 的全部价值是"回放能证明这次失败真的复现了"。
如果加载时把缺失的 playback_mode 当成默认值、把不完整的 llm 序列当成随便跑跑，
回放就会**通过**——而它证明不了任何事，这比直接失败危险得多。
"""

from __future__ import annotations

import json
import os
import shutil
import time

from . import trace as trace_mod
from .checkpoint import CheckpointStore

CASSETTE_FORMAT = 1
CASSETTE_EVENTS = "cassette.jsonl"
CASSETTE_META = "meta.json"
CASSETTE_CHECKPOINTS = "checkpoints"

PLAYBACK_FINGERPRINT = "fingerprint"
PLAYBACK_POSITIONAL = "positional"
PLAYBACK_MODES = (PLAYBACK_FINGERPRINT, PLAYBACK_POSITIONAL)

# 收录范围（§4.8）：assert 级事件 + 复现数据 + 寻址数据 + 元信息。
# stream_summary / stream / 逐行 chunk 的"人看视图"（thinking_chunk/response_chunk）留在 trace。
EXCLUDED_TYPES = {"thinking_chunk", "response_chunk"}
# 这些类型在 `stream_summary` 档才会出现，同样不属于 assert 面。
DIAG_TYPES = {"stream_summary"}


class CassetteError(RuntimeError):
    """cassette 相关错误的基类。"""


class CassetteFormatError(CassetteError):
    """cassette 本身格式不对（缺字段、版本不认识）。"""


class PlaybackModeMismatch(CassetteError):
    """调用方要求的回放模式与 cassette 声明的不一致。"""


class CassetteIncomplete(CassetteError):
    """cassette 缺少走这条回放路径所必需的数据。"""


class CassetteExpired(CassetteError):
    """cassette 录制时的 headroom 版本/模式与当前运行环境不一致，指纹不可比。"""


# ── cassette_format 与词表迁移（§3.4）─────────────────────────────────
# 断言 id 的词表会随代码演进（notice code 改名、turn_end reason 增删）。旧 cassette 里的
# 事件是按旧词表写的：**静默读下去**只会导出一个当前词表里不存在的断言 id，于是
# 「目标断言没复现」——错的是词表，报出来的却是回放失败。所以这一层要显式拦：
# 先按 cassette_format 跑 converter，converter 跑完词表仍不认识 → CassetteFormatError。
_VOCAB_CONVERTERS: dict = {}


def register_converter(frm: int, fn) -> None:
    """注册 `cassette_format == frm` → `frm+1` 的迁移函数 `fn(meta, events)`。

    就地修改并返回 `(meta, events)`；抛异常即视为迁移失败（加载随即抛错）。
    """
    _VOCAB_CONVERTERS[int(frm)] = fn


def convert_vocab(meta: dict, events: list):
    """把 cassette 从它自己的 `cassette_format` 逐级升到当前版本。"""
    ver = meta.get("cassette_format")
    if ver is None:
        raise CassetteFormatError("cassette_meta 缺 cassette_format（无法判断词表版本）")
    try:
        ver = int(ver)
    except (TypeError, ValueError) as exc:
        raise CassetteFormatError(f"cassette_format 不是整数：{meta.get('cassette_format')!r}") from exc
    if ver > CASSETTE_FORMAT:
        raise CassetteFormatError(
            f"cassette_format={ver} 比本代码支持的 {CASSETTE_FORMAT} 新（需升级代码，不能往下读）")
    while ver < CASSETTE_FORMAT:
        fn = _VOCAB_CONVERTERS.get(ver)
        if fn is None:
            raise CassetteFormatError(
                f"cassette_format={ver} 没有可用的 converter（当前 {CASSETTE_FORMAT}）；"
                f"词表变更需注册 register_converter({ver}, fn) 或重抽 cassette")
        out = fn(meta, events)
        if isinstance(out, tuple) and len(out) == 2:
            meta, events = out
        meta["cassette_format"] = ver + 1
        ver += 1
    return meta, events


def _current_vocab() -> dict:
    """当前代码认识的断言词表（惰性 import：trace 不认识 runner，硬依赖会成环）。"""
    vocab = {"notice_codes": set(), "turn_end_reasons": set(trace_mod.TURN_END_REASONS)}
    try:
        from .runner import NOTICE_CODES
        vocab["notice_codes"] = {v[1] for v in NOTICE_CODES.values()}
    except Exception:  # noqa: BLE001 — 拿不到词表就不拦（不能因为导不进来就把 cassette 判死）
        pass
    return vocab


def stale_vocab(events: list) -> list:
    """返回事件里用了**当前词表不认识**的 id 的说明（空列表 = 全部认识）。"""
    vocab = _current_vocab()
    bad = []
    for e in events:
        t = e.get("type")
        if t == "notice" and vocab["notice_codes"]:
            code = e.get("code")
            if code and code not in vocab["notice_codes"]:
                bad.append(f"notice.code={code!r}（seq={e.get('seq')}）")
        elif t == "turn_end":
            reason = e.get("reason")
            if reason and reason not in vocab["turn_end_reasons"]:
                bad.append(f"turn_end.reason={reason!r}（seq={e.get('seq')}）")
    return bad


class CassetteNotFound(CassetteError):
    """目录里没有 cassette。"""


# ── 抽取 ────────────────────────────────────────────────────────────
def _read_jsonl(path: str) -> list:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise CassetteFormatError(f"{path}:{i} 不是合法 JSON：{exc}") from exc
    return out


def select_events(events: list, *, from_request: int = 0, to_request: int | None = None,
                  include_types=None) -> list:
    """按 request_index 区间裁剪事件序列。

    为什么按 request_index 而不是行号：一次会话里前面的请求可能来自完全不同的问题
    （用户改口、重跑），只有 request_index 能表达"从这个请求开始的那一段"。
    区间前的前言（run_start / 第一批 limits / checkpoint）要保留，否则回放缺元信息。
    """
    lo = int(from_request or 0)
    hi = None if to_request is None else int(to_request)
    keep = []
    started = lo <= 0
    for e in events:
        etype = e.get("type")
        if include_types is not None and etype not in include_types:
            continue
        ri = e.get("request_index")
        if etype == "llm_request":
            if not started and isinstance(ri, int) and ri >= lo:
                started = True
                keep.append(e)
                continue
            if started and hi is not None and isinstance(ri, int) and ri > hi:
                break
            if started:
                keep.append(e)
            continue
        if not started:
            # 开头的前言：run_start / limits / checkpoint / turn_start 这类没有请求归属的事件
            if etype in ("run_start", "limits", "checkpoint", "turn_start", "override"):
                keep.append(e)
            continue
        if hi is not None and isinstance(ri, int) and ri > hi:
            break
        keep.append(e)
    return keep


def completeness(events: list) -> dict:
    """完整度：llm 序列有没有 llm_end、每个 tool_call 有没有 tool_result。"""
    reqs = [e for e in events if e.get("type") == "llm_request"]
    ends = {e.get("request_index") for e in events if e.get("type") == "llm_end"}
    missing_llm = [r.get("request_index") for r in reqs if r.get("request_index") not in ends]
    calls = {e.get("call_id") for e in events if e.get("type") == "tool_call"}
    results = {e.get("call_id") for e in events if e.get("type") == "tool_result"}
    missing_tools = sorted(c for c in calls if c and c not in results)
    if not reqs:
        llm = "missing"
    elif missing_llm:
        llm = "partial"
    else:
        llm = "full"
    if not calls:
        tools = "full" if reqs else "missing"
    elif missing_tools:
        tools = "partial"
    else:
        tools = "full"
    return {"llm": llm, "tools": tools,
            "missing_llm_end": missing_llm, "missing_tool_results": missing_tools}


def headroom_fingerprint(meta: dict | None = None) -> dict:
    """当前运行环境的 headroom 版本/模式（§4.8 的 CassetteExpired 判据）。"""
    version = None
    try:
        from headroom import __version__ as version  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        version = None
    mode = os.environ.get("HEADROOM_MODE", "audit")
    enabled = os.environ.get("HEADROOM_ENABLED", "1") == "1"
    limits = None
    try:
        from .config import cfg  # noqa: PLC0415
        limits = {cfg["model"]: cfg["ctx"]}
    except Exception:  # noqa: BLE001
        limits = None
    return {"version": version, "mode": mode if enabled else None,
            "enabled": enabled, "model_context_limits": limits}


def build_meta(trace_dir: str, events: list, *, playback_mode: str = PLAYBACK_FINGERPRINT,
               derived_from: str | None = None, expected_assertion: str | None = None,
               workspace_root: str | None = None) -> dict:
    """组装 cassette_meta（§4.8）。"""
    src = trace_mod.load_manifest(trace_dir) or {}
    hr = headroom_fingerprint()
    comp = completeness(events)
    degraded = []
    if comp["missing_llm_end"]:
        degraded.append(f"缺少 llm_end 的请求：{comp['missing_llm_end'][:8]}")
    if comp["missing_tool_results"]:
        degraded.append(f"缺少 tool_result 的调用：{comp['missing_tool_results'][:8]}")
    return {
        "cassette_format": CASSETTE_FORMAT,
        "canon_version": trace_mod.CANON_VERSION,
        "workspace_root": workspace_root or src.get("workspace_root") or _repo_root(),
        "playback_mode": playback_mode,
        "completeness": {"llm": comp["llm"], "tools": comp["tools"]},
        "degraded_reasons": degraded,
        "headroom": {
            "version": src.get("headroom_version", hr.get("version")),
            "mode": src.get("headroom_mode", hr.get("mode")),
            "model_context_limits": src.get("model_context_limits") or hr.get("model_context_limits"),
        },
        "trace_epoch": src.get("trace_epoch") or trace_mod.trace_epoch(),
        "derived_from": derived_from,
        "expected_assertion": expected_assertion,
        "session_id": src.get("session_id"),
        "model": src.get("model"),
        "backend": src.get("backend"),
        "extracted_at": trace_mod.trace_epoch()["iso"],
    }


def _repo_root() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return here


def _remove_tree(path: str, attempts: int = 5, delay: float = 0.15) -> bool:
    """删除目录，返回是否真的删掉了。

    为什么要重试：在 Windows 上刚创建/刚被扫描过的目录可能短暂被占住，
    `shutil.rmtree(..., ignore_errors=True)` 会**静默地什么都不做**。抽取器随后要
    `os.replace(tmp, out_dir)`，而 Windows 不能用 replace 覆盖一个**已存在的目录**，
    于是失败形态是一句 `WinError 5 拒绝访问`——看起来像权限问题，实际是"目标没删干净"。
    """
    for i in range(max(1, attempts)):
        if not os.path.exists(path):
            return True
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return True
        if i + 1 < attempts:
            time.sleep(delay)
    return not os.path.exists(path)


def _atomic_move_dir(src: str, dst: str, attempts: int = 6, delay: float = 0.2) -> None:
    """把抽好的临时目录原子换名成目标目录。

    Windows 上 `os.replace(目录)` 偶发 `WinError 5`（杀软/索引器正在扫刚写出的
    `*.zst` 与 jsonl 时就会这样），同一份代码有时成功有时失败。这类失败是**环境时序**，
    不是逻辑错误，所以重试几次；真的一直失败就抛自己的异常，而不是把一个裸 OSError
    留给调用方——裸 OSError 会让人以为是权限配置问题，去查完全是错的方向。
    """
    last = None
    for i in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:  # noqa: PERF203 —— 见 docstring
            last = exc
            if i + 1 < attempts:
                time.sleep(delay)
    raise CassetteError(
        f"无法把 {src} 落盘到 {dst}（{last}）：目录可能被其它进程占用，抽取已中止，"
        "没有留下半成品")


def copy_checkpoints(trace_dir: str, out_dir: str, events: list) -> int:
    """把被选中的 checkpoint 快照**重新写进**新目录的帧组，返回快照数。

    不直接复制 .zst 帧文件：一个帧文件里混着被裁掉的那些请求的快照，整份拷过来会让
    cassette 依赖自己不需要的数据（也就失去了"能独立回放的最小证据包"这个性质）。
    """
    src = CheckpointStore(trace_dir)
    dst_dir = os.path.join(out_dir, CASSETTE_CHECKPOINTS)
    os.makedirs(dst_dir, exist_ok=True)
    dst = CheckpointStore(out_dir)
    wanted = sorted({e.get("request_index") for e in events
                     if e.get("type") == "llm_request" and isinstance(e.get("request_index"), int)})
    new_keys = {}
    n = 0
    for ri in wanted:
        for key in src.keys_for_request(ri):
            try:
                snap = src.load(key)
            except Exception:  # noqa: BLE001 —— 单个快照坏了不该阻止其余快照被抽出
                continue
            old = snap.pop("_key", None)
            nk = dst.append(snap)
            new_keys[old or key] = nk
            n += 1
    # 事件里引用的 checkpoint 记录带着新 key，回放才能用 cassette 自己的索引 seek
    for e in events:
        if e.get("type") == "checkpoint" and e.get("_key") in new_keys:
            e["_key"] = new_keys[e["_key"]]
    try:
        dst.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        src.close()
    except Exception:  # noqa: BLE001
        pass
    return n


def verify_replay(cassette_dir: str, expected_assertion: str | None = None, *,
                  run_dir: str | None = None) -> dict:
    """§4.8：抽取后**立刻回放一次**，确认这份 cassette 真能复现目标失败。

    这一步是整个抽取流程里唯一有价值的环节：没有它，cassette 只是"一条录下来的 trace"；
    有了它，才敢说"这个失败用这份证据就能重放"。所以它在**写进目标目录之前**跑，失败就
    整份丢弃——一份不能复现的 cassette 留在 tests/fixtures 里，比没有更坏。

    返回 {"reproduced": bool|None, "report": str, "reason": str|None}。
    """
    from . import replay
    cas = Cassette(cassette_dir, check_env=False)
    if cas.completeness["llm"] == "missing":
        return {"reproduced": None, "report": "",
                "reason": f"cassette 没有 LLM 记录（{cas.meta.get('degraded_reasons')}），无法回放验证"}
    question = cas.initial_question()
    # 自检用的运行目录必须**在 cassette 之外**：抽取器随后会把 tmp 目录原子换名成目标
    # 目录，而自检留在 tmp 里的 trace.jsonl / checkpoint 帧文件句柄不一定已经关掉。
    # Windows 下这表现为 `os.replace` 抛 WinError 5（拒绝访问），而且**只在某些时序下
    # 出现**——同一份代码有时通过有时失败，比稳定失败更难查。
    check_dir = run_dir or (cassette_dir.rstrip("\\/") + ".check")
    try:
        report = replay.run_replay(cassette_dir, question=question, run_dir=check_dir,
                                   check_env=False)
    finally:
        from . import checkpoint as _cp
        _cp.close_store(check_dir)
        shutil.rmtree(check_dir, ignore_errors=True)
    text = report.text()
    if expected_assertion and report.expected_assertion != expected_assertion:
        return {"reproduced": None, "report": text,
                "reason": (f"回放读到的目标断言是 {report.expected_assertion!r}，"
                           f"抽取时声明的是 {expected_assertion!r}")}
    if expected_assertion and not report.reproduced:
        return {"reproduced": False, "report": text,
                "reason": (f"回放没能复现 {expected_assertion}（现场断言面："
                           f"{report.assertion_ids[:8]}）")}
    return {"reproduced": bool(report.reproduced), "report": text, "reason": None}


def extract_cassette(trace_dir: str, out_dir: str, *, from_request: int = 0,
                     to_request: int | None = None, playback_mode: str = PLAYBACK_FINGERPRINT,
                     derived_from: str | None = None, expected_assertion: str | None = None,
                     workspace_root: str | None = None, force: bool = False) -> dict:
    """把 trace 抽成 cassette。返回 cassette_meta。

    抽取器**先写临时目录、全部成功后再原子换名**：半成品 cassette 比没有 cassette 更坏，
    因为它看起来是完整的。
    """
    trace_dir = os.path.abspath(trace_dir)
    out_dir = os.path.abspath(out_dir)
    if playback_mode not in PLAYBACK_MODES:
        raise CassetteFormatError(f"playback_mode 必须是 {PLAYBACK_MODES} 之一，收到 {playback_mode!r}")
    if not os.path.exists(os.path.join(trace_dir, trace_mod.TRACE_EVENTS)):
        raise CassetteNotFound(f"{trace_dir} 里没有 {trace_mod.TRACE_EVENTS}")
    if playback_mode == PLAYBACK_POSITIONAL and not (derived_from and expected_assertion):
        raise CassetteFormatError("positional cassette 必须带 derived_from 与 expected_assertion")
    if os.path.exists(out_dir) and not force:
        raise CassetteError(f"{out_dir} 已存在（加 force=True 覆盖）")

    events = trace_mod.read_events(trace_dir)
    picked = select_events(events, from_request=from_request, to_request=to_request)
    picked = [dict(e) for e in picked
              if e.get("type") not in EXCLUDED_TYPES and e.get("type") not in DIAG_TYPES]
    assert_ids = trace_mod.assertion_ids(picked)
    meta = build_meta(trace_dir, picked, playback_mode=playback_mode,
                      derived_from=derived_from, expected_assertion=expected_assertion,
                      workspace_root=workspace_root)
    meta["assertion_ids"] = assert_ids

    tmp = out_dir + ".tmp"
    _remove_tree(tmp)
    os.makedirs(tmp, exist_ok=True)
    try:
        n_snaps = copy_checkpoints(trace_dir, tmp, picked)
        with open(os.path.join(tmp, CASSETTE_EVENTS), "w", encoding="utf-8", newline="\n") as f:
            for e in picked:
                f.write(json.dumps(trace_mod.redact_payload(e), ensure_ascii=False) + "\n")
        meta["checkpoint_count"] = n_snaps
        with open(os.path.join(tmp, CASSETTE_META), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        # 抽完立刻自检一遍：加载校验 + **真回放一次**（§4.8）。不合格就**不许**落到目标目录：
        # 一份不能复现目标失败的 cassette 留在 fixtures 里，会被当成"已有证据"，比没有更坏。
        Cassette(tmp, check_env=False)
        check = verify_replay(tmp, expected_assertion)
        if expected_assertion and check["reproduced"] is not True:
            raise CassetteError(f"抽取的 cassette 未能复现 {expected_assertion}：{check['reason']}\n"
                                f"{check['report']}")
        meta["replay_check"] = {"reproduced": check["reproduced"], "reason": check["reason"]}
        # meta.json 要重写一次：回放验证要能 load（先得有 meta.json），而验证结论又要进 meta
        with open(os.path.join(tmp, CASSETTE_META), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        if os.path.exists(out_dir) and not _remove_tree(out_dir):
            raise CassetteError(
                f"{out_dir} 清不掉（可能被其它进程占用），抽取中止："
                "宁可没有 cassette，也不要留下一份看起来完整的半成品")
        # 落盘前把 tmp 里自己开过的 checkpoint 帧句柄关掉：Windows 上带着打开的
        # 文件句柄换名目录会失败，而这时失败留下的是一份**看起来完整**的半成品。
        from . import checkpoint as _cp2
        _cp2.close_store(tmp)
        _atomic_move_dir(tmp, out_dir)
    except Exception:
        _remove_tree(tmp)
        raise
    return meta


# ── 加载与校验 ──────────────────────────────────────────────────────
class Cassette:
    """只读的 cassette 视图 + §4.8 的加载校验。"""

    def __init__(self, cassette_dir: str, *, mode: str | None = None,
                 require_llm: bool = False, check_env: bool = True):
        self.dir = os.path.abspath(cassette_dir)
        meta_path = os.path.join(self.dir, CASSETTE_META)
        if not os.path.exists(meta_path):
            raise CassetteNotFound(f"{self.dir} 里没有 {CASSETTE_META}")
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                self.meta = json.load(f)
        except json.JSONDecodeError as exc:
            raise CassetteFormatError(f"{meta_path} 不是合法 JSON：{exc}") from exc
        if not isinstance(self.meta, dict):
            raise CassetteFormatError(f"{meta_path} 顶层不是对象")

        # 先把事件读进来再校验：词表检查看的是事件，不是 meta。
        raw_events = _read_jsonl(os.path.join(self.dir, CASSETTE_EVENTS))
        self.meta, self.events = convert_vocab(self.meta, raw_events)
        bad = stale_vocab(self.events)
        if bad:
            raise CassetteFormatError(
                "cassette 用了当前词表不认识的断言 id（词表变更需 converter 迁移或重抽）："
                + "；".join(bad[:6]))

        declared = self.meta.get("playback_mode")
        if declared not in PLAYBACK_MODES:
            raise CassetteFormatError(
                f"cassette_meta.playback_mode 缺失或非法：{declared!r}（必须是 {PLAYBACK_MODES}）")
        self.declared_mode = declared
        # mode 显式传入时**必须**与声明一致：回放器走指纹匹配而 cassette 是按位录的，
        # 两者对"哪条响应配哪个请求"的假设不同，混用会得到一份看似通过的错误结论。
        if mode is not None and mode != declared:
            raise PlaybackModeMismatch(
                f"要求 {mode} 回放，但 cassette 声明 playback_mode={declared}")
        self.mode = declared
        if declared == PLAYBACK_POSITIONAL:
            if not self.meta.get("derived_from") or not self.meta.get("expected_assertion"):
                raise CassetteFormatError(
                    "positional cassette 缺 derived_from / expected_assertion，无法判定复现")
        comp = self.meta.get("completeness") or {}
        self.completeness = {"llm": comp.get("llm", "missing"), "tools": comp.get("tools", "missing")}
        if require_llm and self.completeness["llm"] == "missing":
            raise CassetteIncomplete(
                f"cassette 的 LLM 记录为空（completeness.llm=missing），不能用于 LLM 回放："
                f"{self.meta.get('degraded_reasons')}")

        self._by_type: dict = {}
        for e in self.events:
            self._by_type.setdefault(e.get("type"), []).append(e)
        self._requests = [e for e in self._by_type.get("llm_request", [])]
        self._chunks: dict = {}
        for e in self._by_type.get("llm_chunk", []):
            self._chunks.setdefault(e.get("request_index"), []).append(e)
        self._ends = {e.get("request_index"): e for e in self._by_type.get("llm_end", [])}
        self._tool_calls = {e.get("call_id"): e for e in self._by_type.get("tool_call", [])}
        self._tool_results = {e.get("call_id"): e for e in self._by_type.get("tool_result", [])}
        self._assert_ids = trace_mod.assertion_ids(self.events)
        if check_env:
            self._check_headroom()
        self._store = None

    # ── 校验 ──────────────────────────────────────────────────────
    def _check_headroom(self) -> None:
        """headroom 版本/模式变过 → 请求被改写的方式也变过，旧 cassette 的指纹不可比。

        只在**录制侧真的用了 headroom**（meta 里有 mode/version）时才判：一份从没开过
        headroom 的 cassette 不应该因为本机装了别的 headroom 版本而失效。
        """
        rec = self.meta.get("headroom") or {}
        if not rec.get("mode") and not rec.get("version"):
            return
        now = headroom_fingerprint()
        if not now.get("enabled"):
            if rec.get("mode"):
                raise CassetteExpired(
                    f"cassette 录制时 headroom mode={rec.get('mode')}，当前 HEADROOM_ENABLED=0")
            return
        if rec.get("version") and now.get("version") and rec["version"] != now["version"]:
            raise CassetteExpired(
                f"headroom 版本不一致：cassette={rec['version']} 当前={now['version']}")
        if (rec.get("mode") or None) != (now.get("mode") or None):
            raise CassetteExpired(
                f"headroom mode 不一致：cassette={rec.get('mode')} 当前={now.get('mode')}")

    def check_canon_version(self) -> None:
        """canon 口径变了 → 旧指纹全部作废（§6.1 的 canon_version）。"""
        ver = self.meta.get("canon_version")
        if ver != trace_mod.CANON_VERSION:
            raise CassetteFormatError(
                f"canon_version 不一致：cassette={ver} 当前={trace_mod.CANON_VERSION}（需重抽）")

    # ── 访问 ──────────────────────────────────────────────────────
    @property
    def requests(self) -> list:
        return list(self._requests)

    def request_count(self) -> int:
        return len(self._requests)

    def request_at(self, index: int) -> dict:
        return self._requests[index]

    def request_index_at(self, position: int):
        return self._requests[position].get("request_index")

    def chunks_for(self, request_index) -> list:
        """该请求的响应分片，并把 llm_end 上的 usage 合并到最后一块（§4.3）。

        chunk 记录刻意不带 usage：usage 是**请求结束**的记账，记一次比每块抄一遍更省、
        也更容易核对；回放时在这里合并回去，才能让 runner 看到和录制时一样的尾块。
        """
        chunks = [dict(c) for c in self._chunks.get(request_index, [])]
        end = self._ends.get(request_index) or {}
        usage = end.get("usage")
        if usage:
            if chunks:
                chunks[-1].setdefault("usage", usage)
            else:
                # 非流式响应：没有分片，只有 llm_end.message；补一个 usage 帧让回放侧
                # 至少能看到记账（正文由 end_for 的 message 提供）。
                chunks.append({"choices_empty": True, "usage": usage,
                               "request_index": request_index})
        return chunks

    def end_for(self, request_index):
        return self._ends.get(request_index)

    def tool_calls(self) -> list:
        return list(self._tool_calls.values())

    def tool_call(self, call_id) -> dict | None:
        return self._tool_calls.get(call_id)

    def tool_result(self, call_id) -> dict | None:
        return self._tool_results.get(call_id)

    def assert_ids(self) -> list:
        return list(self._assert_ids)

    def has_assertion(self, assertion_id: str) -> bool:
        return assertion_id in self._assert_ids

    def expected_assertion(self):
        return self.meta.get("expected_assertion")

    def initial_question(self):
        """从第一条请求里还原用户最初的问题。

        回放要"重放这一段对话"，就必须有起点。起点只能从录制的第一条请求里取：
        靠调用方另传一个问题，回放跑的就不是同一段历史了。
        """
        for req in self._requests:
            for m in (req.get("messages") or []):
                if not isinstance(m, dict) or m.get("role") != "user":
                    continue
                content = m.get("content")
                if isinstance(content, str) and content.strip():
                    return content
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            return part.get("text") or ""
        return None

    def store(self) -> CheckpointStore:
        """cassette 自己的 checkpoint 帧组（懒加载）。"""
        if self._store is None:
            self._store = CheckpointStore(self.dir)
        return self._store

    def load_for_request(self, request_index, kind: str = "pre_request"):
        return self.store().load_for_request(request_index, kind=kind)

    def limits_for(self, request_index=None, nearest_before=None) -> dict | None:
        """取某请求**之前最近**的一条 limits（回放重算 result_stored 用，§4.5）。"""
        best = None
        for e in self._by_type.get("limits", []):
            ri = e.get("request_index")
            if request_index is not None and isinstance(ri, int) and isinstance(request_index, int):
                if ri <= request_index:
                    best = e
                elif best is None:
                    best = e
            else:
                best = e
        return best

    def describe(self) -> str:
        m = self.meta
        return (f"cassette {os.path.basename(self.dir)}: mode={self.mode} "
                f"requests={len(self._requests)} llm={self.completeness['llm']} "
                f"tools={self.completeness['tools']}")
