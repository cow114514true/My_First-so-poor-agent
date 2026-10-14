"""L1 checkpoint：把"请求发出前的那一瞬间"完整冻下来（TRACE_REPLAY_BUILD.md §4.6/§4.7）。

为什么必须冻**这一瞬间**：回放的失败几乎都在模型这一侧，而模型看到的**唯一**东西
就是这一次请求的 messages。事后重建（"用最终会话历史去掉后面那些消息"）看起来等价，
其实不等价——压缩、去重、渲染过的裁剪通知都在历史里留下了不可逆的痕迹，重建出来的
messages 和当时发出去的那份会差在几个字符上，于是回放对不上、又查不出为什么。

分组（§4.7）：
  A conv        会话消息（hash 指针；大 messages 走 messages_inline 降级）
  B 压缩记账    cstate(conv) 与 loop_agent_v2._session_state 两处计数
  C 薄壳        archive / compact 状态（summary / _compact_state）
  D 进程级      interrupt_event / config 的压缩配置缓存 / headroom 会话态
  E 工作区      agent 触碰过的文件路径（小文件带 sha256，大文件只留 mtime+size）
  F 前缀哈希    stable_prefix_hash（用于校验"前缀没被动过"）

本模块只负责**采集 + 校验**；落盘（zstd 帧组）与 seek 在 §4.7 的第二段实现，
即 `seal()` / `seek()`——它们依赖 zstandard，缺失时降级成"只写不压"。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

# 一份 checkpoint 里最大的 inline messages（超过就只留 hash + 长度）。
# 为什么留 inline：messages 是回放的**输入**，丢了它 checkpoint 就只是个指纹；
# 但一次 30k token 的对话能到几百 KB，全量 inline 会让 trace 目录失去可读性。
INLINE_MESSAGES_LIMIT = 64 * 1024

# 小文件才记内容哈希；大文件记 mtime+size（读了也不便宜，且回放不依赖内容）
SMALL_FILE_LIMIT = 256 * 1024

_lock = threading.RLock()
_captures = 0
_last_error = None
_stores: dict = {}
_next_request = {}  # run_dir -> 快照序号游标（只用于让同一请求下的多份快照各有各的键位）


def _next_snapshot_seq(run_dir, req_index: int) -> int:
    """快照的独立顺序号（只增），仅用于区分键位。

    `request_index` 由 trace writer 统一分配（全 trace 只增），快照的键位里必须带上
    它；但同一请求下会采两份快照（pre_request / post_turn），键位不能撞——于是再带一个
    只增的序号。序号先看**这个请求下已经有多少份快照**，同一会话被多次打开时就不会
    从 0 重来盖掉旧的那份。
    """
    base = 0
    try:
        base = len(_stores[run_dir].keys_for_request(req_index))
    except Exception:  # noqa: BLE001 —— 数不出来就从 0 起，最坏只是键位重复
        base = 0
    with _lock:
        nxt = max(_next_request.get(run_dir, 0), base)
        _next_request[run_dir] = nxt + 1
        return nxt


def _sha256(text) -> str:
    if isinstance(text, str):
        data = text.encode("utf-8", "replace")
    elif isinstance(text, bytes):
        data = text
    else:
        data = json.dumps(text, ensure_ascii=False, sort_keys=True).encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()


def messages_digest(messages) -> str:
    """messages 的结构化摘要。

    用 `json.dumps(..., sort_keys=True)` 而不是直接 str()：dict 的键序在不同
    代码路径下会变（尤其是从 SDK 对象反序列化回来的），而内容其实一样。
    """
    return _sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str))


def _stable_prefix_hash(messages) -> str:
    """前缀哈希：只覆盖 system + 最早的一段，用来发现"前缀被动了"。

    回放的 fingerprint 比对已经能发现差异，前缀哈希是**更早、更便宜**的预警：
    前缀动了说明 memory / workspace 注入变了，通常还没轮到模型就已经错了。
    """
    head = [m for m in (messages or [])[:3]]
    return _sha256(json.dumps(head, ensure_ascii=False, sort_keys=True, default=str))


def workspace_manifest(paths, root=None) -> list:
    """E 组：agent 触碰过的文件清单（只含给定路径，不扫全目录）。

    为什么只记"触碰过的"：扫全目录既慢又没意义（回放并不需要仓库全貌），
    真正影响判定的是"这次运行改过哪些文件"。
    """
    root = root or os.getcwd()
    out = []
    for p in sorted(set(paths or [])):
        if not p:
            continue
        ap = p if os.path.isabs(p) else os.path.join(root, p)
        rel = os.path.relpath(ap, root).replace("\\", "/")
        entry = {"path": rel, "exists": os.path.exists(ap)}
        try:
            st = os.stat(ap)
            entry["size"] = st.st_size
            entry["mtime"] = round(st.st_mtime, 3)
            if entry["exists"] and st.st_size <= SMALL_FILE_LIMIT:
                with open(ap, "rb") as f:
                    entry["sha256"] = hashlib.sha256(f.read()).hexdigest()
        except OSError as e:
            entry["error"] = f"{type(e).__name__}: {e}"
        out.append(entry)
    return out


def touched_paths(conv) -> list:
    """从会话历史里提取工具入参中出现过的文件路径（E 组输入）。"""
    from .toolclass import path_args_of
    paths = []
    for msg in (conv or {}).get("messages", []):
        for tc in (msg.get("tool_calls") or []):
            fn = (tc or {}).get("function") or {}
            name = fn.get("name")
            raw = fn.get("arguments")
            if not name or not raw:
                continue
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                continue
            if isinstance(args, dict):
                try:
                    keys = path_args_of(name)
                except Exception:  # noqa: BLE001 —— 未登记工具在这里不该拖垮 trace
                    keys = ()
                for key in keys:
                    v = args.get(key)
                    if isinstance(v, str) and v:
                        paths.append(v)
    return paths


def _compact_state(conv) -> dict:
    """B 组：压缩记账（与 limits.accounting 对账的那几个计数器）。"""
    try:
        from .compaction import cstate
        st = cstate(conv)
    except Exception:  # noqa: BLE001 —— 缺 compaction 时不该拖垮 trace
        st = conv or {}
    keys = ("_compact_count", "_compact_fails", "_compact_last_turn", "_compact_retry_turn",
            "_dedup_saved", "_compact_token")
    out = {k: st.get(k) for k in keys if k in st}
    # 运行期缓存（不跨进程，但跨同一进程的多次请求）
    try:
        from . import config
        cache = getattr(config, "_compact_k_cache", None)
        if isinstance(cache, dict):
            out["_compact_k_cache"] = {k: v for k, v in cache.items()}
    except Exception:  # noqa: BLE001
        pass
    return out


def shell_state() -> dict:
    """C 组：薄壳状态。薄壳在仓库根层的 loop_agent_v2 里，取不到就留空。"""
    out = {}
    try:
        import loop_agent_v2 as shell  # noqa: PLC0415 —— 循环导入，必须惰性
    except Exception:  # noqa: BLE001
        return out
    for attr in ("_session_state", "messages"):
        try:
            v = getattr(shell, attr, None)
        except Exception:  # noqa: BLE001
            continue
        if v is None:
            continue
        if attr == "messages":
            out["messages_len"] = len(v)
        else:
            out["_session_state_keys"] = sorted(v.keys()) if isinstance(v, dict) else type(v).__name__
            if isinstance(v, dict):
                for k in ("_archive", "_compact_state"):
                    if k in v:
                        sub = v[k]
                        out[k] = (sorted(sub.keys()) if isinstance(sub, dict) else type(sub).__name__)
    return out


def process_state() -> dict:
    """D 组：进程级状态（中断标志 / headroom 会话态 / 流式开关）。"""
    out = {}
    try:
        from .control import interrupt_event
        out["interrupt_event"] = bool(interrupt_event.is_set())
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import events
        out["headless_streaming"] = bool(getattr(events, "_headless_streaming", False))
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import config
        out["headroom_mode"] = os.environ.get("HEADROOM_MODE")
        out["headroom_enabled"] = os.environ.get("HEADROOM_ENABLED", "1")
    except Exception:  # noqa: BLE001
        pass
    return out


def write_snapshot(snap, run_dir=None) -> int:
    """把一份**已经组装好的**快照写进帧组，返回它挂接的 request_index（§4.7）。

    与 capture 的分工：capture 负责"从活着的进程里把状态抠出来"，这里只负责落盘。
    外部（tools/trace.py 迁移器、回放自检）已经有快照 dict 时不应当再去读一遍进程。
    """
    from . import trace
    d = run_dir or trace.current_run_dir()
    if not d:
        raise CheckpointStoreError("没有可写的 trace 目录（既没给 run_dir 也没有活跃 writer）")
    store_for(d).append(snap)
    return int(snap.get("request_index") or 0)


def capture(conv, q_idx=None, kind="pre_request") -> dict | None:
    """采集一份 checkpoint 并写入 trace。

    没有活跃 trace 时直接返回 None（**不采集**）：正常运行不该为回放付 IO 代价，
    而 checkpoint 的成本主要在序列化 messages 与读文件哈希上。
    """
    from . import trace
    w = trace.current_writer()
    if w is None:
        return None
    ctx = trace.current_context()
    # pre_request 采在 llm_request **之前**，此刻 request_index 还没被分配——取 writer
    # 的"下一个号"，它马上就会是这次请求的号。post_turn 采在请求之后，写的自然是下一个。
    # 两者都必须用 writer 的号，不能用 ctx：ctx 会被 begin_turn/set_tool_round 清零。
    req_index = w.peek_request_index()
    store = store_for(w.run_dir)
    messages = (conv or {}).get("messages") or []
    payload = json.dumps(messages, ensure_ascii=False, default=str)
    snap = {
        "kind": kind,
        "request_index": req_index,
        "_snapshot_seq": _next_snapshot_seq(w.run_dir, req_index),
        "turn": ctx.turn,
        "tool_round": ctx.tool_round,
        "q_idx": q_idx,
        "messages_hash": messages_digest(messages),
        "messages_len": len(messages),
        "messages_bytes": len(payload.encode("utf-8", "replace")),
        "B": _compact_state(conv),
        "C": shell_state(),
        "D": process_state(),
        "E": workspace_manifest(touched_paths(conv)),
        "F": _stable_prefix_hash(messages),
    }
    if snap["messages_bytes"] <= INLINE_MESSAGES_LIMIT:
        snap["messages_inline"] = messages
    else:
        # 降级：只留 hash + 长度。回放时若需要内容会显式报 body_missing，
        # 而不是悄悄用一个不完整的 messages 继续跑（那会得到"能跑但结论错"）。
        snap["body_missing"] = True
    snap["_key"] = checkpoint_key(kind, snap["request_index"], ctx.turn, ctx.tool_round,
                                  snap["_snapshot_seq"])
    trace.emit_event({"type": "checkpoint",
                      **{k: v for k, v in snap.items() if k not in ("_key", "_snapshot_seq")}},
                     level_hint=trace.LEVEL_CORE)
    try:
        store.append(snap)
    except Exception as e:  # noqa: BLE001 —— 落盘失败不能拖垮正在跑的 agent
        snap["_store_error"] = f"{type(e).__name__}: {e}"
    global _captures
    with _lock:
        _captures += 1
    return snap


def store_for(run_dir) -> CheckpointStore:
    """按 trace 目录取（并缓存）一个存储实例；跨线程共用同一份索引。"""
    with _lock:
        st = _stores.get(run_dir)
        if st is None:
            st = CheckpointStore(run_dir)
            _stores[run_dir] = st
        return st


def close_store(run_dir=None) -> None:
    """收尾：flush 索引（trace 关闭前调用）。"""
    with _lock:
        items = list(_stores.items()) if run_dir is None else [(run_dir, _stores.get(run_dir))]
        for rd, st in items:
            if st is not None:
                try:
                    st.close()
                except Exception:  # noqa: BLE001
                    pass
            _stores.pop(rd, None)
            _next_request.pop(rd, None)


def capture_count() -> int:
    return _captures


def reset_counters() -> None:
    global _captures
    with _lock:
        _captures = 0


def verify_restore(snap: dict, messages) -> list:
    """restore 后的自检：返回问题列表（空 = 一致）。§4.7 的 seek 流程要用。"""
    problems = []
    if not snap:
        return ["checkpoint 为空"]
    want = snap.get("messages_hash")
    got = messages_digest(messages)
    if want != got:
        problems.append(f"messages_hash 不一致：录制 {want} ≠ 恢复 {got}")
    if snap.get("body_missing") and not snap.get("messages_inline"):
        problems.append("body_missing：checkpoint 只有指纹，没有 messages 正文")
    return problems


def compare_accounting(snap: dict, limits_recorded: dict | None) -> list:
    """B 组记账 vs 录制时的 limits.accounting（§4.7 的 seek 自检第二步）。

    这两个数是**同一件事的两种记法**：limits 是每次请求写下的生效旋钮+记账快照，B 组是
    快照时刻从 conv/config 里抠出来的实时值。对不上说明"恢复出来的压缩状态"与"那次请求
    真正看到的预算"不是一回事——继续跑下去，后面每一轮的压缩决策都建在一个错的前提上。
    """
    problems = []
    b = snap.get("B") or {}
    acc = (limits_recorded or {}).get("accounting") or {}
    for key, rec_val in acc.items():
        k = key if key.startswith("_") else f"_{key}"
        if k not in b:
            continue  # limits 记的旋钮在 B 组里没有对应项（例如 _compact_token 未曾写入）
        if b.get(k) != rec_val:
            problems.append(f"B 组记账不一致：{k} 录制 limits={rec_val} ≠ checkpoint={b.get(k)}")
    return problems


def restore(snap: dict, conv=None, *, messages=None, limits_recorded: dict | None = None,
            apply=True) -> dict:
    """把一份 checkpoint 恢复成"可以继续跑"的进程状态（§4.7 的 seek 收尾）。

    `apply=False` 只做自检不写回（用于"这条 trace 我到底能不能跳进来"的预检）。
    `messages` 传**录制侧的 messages**（来自 llm_request）：自检要比的是"录到的那段历史"
    与"快照声称的那段历史"，拿恢复后的现场去比就成了自己证明自己。

    返回 `{"problems": [...], "applied": {...}}`；problems 非空即恢复不可信，
    调用方必须把它当失败，而不是"带病继续跑"。
    """
    problems = verify_restore(snap, messages if messages is not None
                             else (snap.get("messages_inline") or []))
    problems += compare_accounting(snap, limits_recorded)
    applied = {}
    if not apply or problems:
        return {"problems": problems, "applied": applied}

    # 1) 进程级缓存：_compact_k_cache 是**上一段历史**算出来的旋钮，留着会让恢复后的
    #    压缩按旧环境做决策（旋钮可能来自不同 env / 不同 backend）。
    try:
        from . import config
        if hasattr(config, "reset_compact_k_cache"):
            config.reset_compact_k_cache()
            applied["reset_compact_k_cache"] = True
    except Exception as e:  # noqa: BLE001
        problems.append(f"清理 _compact_k_cache 失败：{type(e).__name__}: {e}")

    # 2) 中断标志：录到 D 组里的中断状态是"那一刻"的事实，恢复时按它还原；
    #    但**清掉**比置位更常见——上一轮按过 ESC 不该让恢复后的这一轮立刻自杀。
    try:
        from .control import interrupt_event
        if (snap.get("D") or {}).get("interrupt_event"):
            interrupt_event.set()
        else:
            interrupt_event.clear()
        applied["interrupt_event"] = bool(interrupt_event.is_set())
    except Exception as e:  # noqa: BLE001
        problems.append(f"恢复中断标志失败：{type(e).__name__}: {e}")

    # 3) B 组：写回 conv 的压缩记账（cstate 落在 conv["_compact_state"] 或 conv 本身）
    b = dict(snap.get("B") or {})
    b.pop("_compact_k_cache", None)
    if conv is not None and b:
        target = conv.setdefault("_compact_state", {})
        target.update(b)
        applied["B"] = sorted(b.keys())

    # 4) D 组：流式开关等（不还原 headroom 模式——那是 env 决定的，改它等于改运行环境）
    d = snap.get("D") or {}
    if "headless_streaming" in d:
        try:
            from . import events
            events._headless_streaming = bool(d["headless_streaming"])
            applied["headless_streaming"] = bool(d["headless_streaming"])
        except Exception as e:  # noqa: BLE001
            problems.append(f"恢复流式开关失败：{type(e).__name__}: {e}")

    # 5) C 组只做**记录**不做写回：薄壳的 _session_state 是活对象（含线程、句柄），
    #    把录制时的一份 dict 盖回去只会造出一个半死状态。回放路径本来就不用薄壳全局态。
    applied["C_keys"] = sorted((snap.get("C") or {}).keys())
    applied["E_paths"] = len(snap.get("E") or [])
    applied["F"] = snap.get("F")
    return {"problems": problems, "applied": applied}


def seek(run_dir: str, request_index: int, messages, *, kind: str = "pre_request",
         conv=None, apply: bool = True) -> dict:
    """按 request_index 定位 checkpoint 并恢复（§4.7 的 seek 流程一行封装）。

    messages 必须**来自录制的 llm_request**：seek 的语义是"回到那次请求之前的状态"，
    如果现场 messages 与录制的不一致，恢复出来的就不是那次请求的前提。
    """
    store = store_for(run_dir)
    snap = store.find_for_request(request_index, messages, kind=kind)
    rec = {"snap": snap, "verify": verify_restore(snap, messages),
           "recommended_messages": messages}
    limits_rec = None
    try:
        from . import trace
        evs = trace.read_events(run_dir)
        for e in evs:
            if e.get("type") == "limits":
                limits_rec = {"knobs": e.get("knobs") or {}, "accounting": e.get("accounting") or {}}
    except Exception:  # noqa: BLE001 —— 没有 limits 时 B 组对账跳过，不静默编一个
        limits_rec = None
    rec.update(restore(snap, conv, messages=messages, limits_recorded=limits_rec, apply=apply))
    return rec


# ── §4.7 帧组存储：checkpoints/<group>.zst + checkpoints/index.json ──────────
#
# 为什么是"帧组"而不是"每份快照一个文件"：一份 checkpoint 里 messages 是**重复度极高**
# 的内容（相邻两次请求只差几条消息），zstd 帧内共享字典能把它们压到 1/10 以下；而独立
# 文件既没有共享字典、又会产生成千上万个 inode。反过来，也**不能**把整个会话压成一个
# blob：那样 seek 到第 3 个请求要把整场会话解压一遍，seek 成本随 N 增长——这正是
# §4.7 明确要避免的（"解压次数 ≤ 帧组大小 K，与 N 无关"）。
#
# 于是：帧组按"5 个请求"切分，seek 只需要打开**包含目标请求的那一个** frame，
# 记录号 → (frame, offset, comp_len) 存 index.json，O(1) 找到字节位置。

GROUP_REQUESTS = 5  # 一个帧组覆盖多少个 request_index（§4.7：用户轮或每 5 请求）


class CheckpointStoreError(RuntimeError):
    pass


class CheckpointRestoreMismatch(CheckpointStoreError):
    """恢复出来的 messages 与录制时的 llm_request 不一致。"""


class _ZstdMissing(CheckpointStoreError):
    pass


def _zstd():
    try:
        import zstandard  # noqa: PLC0415 —— 缺失时降级，不在导入期就炸
    except ImportError as e:  # pragma: no cover - 依赖缺失路径
        raise _ZstdMissing("zstandard 未安装：checkpoint 无法解压（pip install zstandard）") from e
    return zstandard


def checkpoint_key(kind: str, request_index: int, turn: int = 0, tool_round: int = 0,
                   seq: int | None = None) -> str:
    """快照在索引里的稳定键：`kind@request_index#turn.tool_round~seq`。

    seq 必须带上：同一请求下会有多份快照（pre_request / post_turn，重跑还会追加），
    没有它就互相覆盖——§4.7 的"snapshots 按 hash 去重、多索引指向同一 blob"会退化成
    "只剩最后一份"，回放 seek 到较早的请求就再也找不回当时的现场。
    """
    base = f"{kind}@{int(request_index)}#{int(turn)}.{int(tool_round)}"
    return base if seq is None else f"{base}~{int(seq)}"


def kind_of_key(key: str) -> str:
    """从索引键里取出快照种类（pre_request / post_turn）。"""
    return (key or "").split("@", 1)[0]


def request_of_key(key: str):
    """从索引键里取出 request_index（解析不出来返回 None）。"""
    try:
        return int((key or "").split("@", 1)[1].split("#", 1)[0])
    except (IndexError, ValueError):
        return None


class CheckpointStore:
    """一个 trace 目录的 checkpoint 存储（写入端与读取端共用同一份索引格式）。"""

    def __init__(self, run_dir, *, group_requests: int = GROUP_REQUESTS):
        self.run_dir = run_dir
        self.dir = os.path.join(run_dir, "checkpoints")
        self.index_path = os.path.join(self.dir, "index.json")
        self.group_requests = max(1, int(group_requests))
        self._lock = threading.RLock()
        self.index = self._load_index()
        self._group_id = None
        self._group_cursor = 0
        self._frame_fp = None
        self._decompresses = 0

    # -- 索引 --
    def _load_index(self) -> dict:
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "frames" in data:
                data.setdefault("snapshots", {})
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "group_requests": self.group_requests,
                "frames": [], "snapshots": {}, "by_request": {}}

    def _save_index(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(self.index, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, self.index_path)

    # -- 写 --
    def _group_of(self, request_index: int) -> int:
        return int(request_index) // self.group_requests

    def _find_frame(self, group_id: int):
        for fr in self.index["frames"]:
            if fr.get("group_id") == group_id:
                return fr
        return None

    def _open_group(self, group_id: int):
        if self._frame_fp is not None and self._group_id == group_id:
            return self._frame_fp
        self._flush()
        path = os.path.join(self.dir, f"g{group_id:05d}.zst")
        self._frame_fp = open(path, "ab")
        self._group_id = group_id
        self._group_cursor = os.path.getsize(path)
        frame = self._find_frame(group_id)
        if frame is None:
            frame = {"group_id": group_id, "file": f"g{group_id:05d}.zst",
                     "requests": [None, None]}
            self.index["frames"].append(frame)
        return self._frame_fp

    def append(self, snap: dict) -> dict:
        """把一份快照追加进当前帧组，返回它的索引条目。"""
        zstandard = _zstd()
        req = int(snap.get("request_index") or 0)
        key = snap.get("_key") or checkpoint_key(
            snap.get("kind") or "pre_request", req,
            snap.get("turn") or 0, snap.get("tool_round") or 0,
            snap.get("_snapshot_seq"))
        with self._lock:
            gid = self._group_of(req)
            fp = self._open_group(gid)
            frame = self._find_frame(gid)
            body = json.dumps(snap, ensure_ascii=False, default=str).encode("utf-8")
            blob = zstandard.ZstdCompressor().compress(body)
            offset = fp.tell()
            fp.write(blob)
            fp.flush()
            entry = {"key": key, "frame": gid, "offset": offset,
                     "comp_len": len(blob), "raw_len": len(body), "request_index": req,
                     "kind": snap.get("kind"), "messages_hash": snap.get("messages_hash")}
            self.index["snapshots"][key] = entry
            self.index["by_request"].setdefault(str(req), []).append(key)
            lo, hi = frame["requests"]
            frame["requests"] = [req if lo is None else min(lo, req),
                                 req if hi is None else max(hi, req)]
            self._save_index()
            return entry

    def write(self, snap: dict) -> str:
        return self.append(snap)["key"]

    def _flush(self) -> None:
        if self._frame_fp is not None:
            try:
                self._frame_fp.flush()
                self._frame_fp.close()
            except OSError:
                pass
            self._frame_fp = None
        self._group_id = None
        self._save_index()

    def close(self) -> None:
        with self._lock:
            self._flush()

    # -- 读 --
    def keys_for_request(self, request_index: int) -> list:
        return list(self.index["by_request"].get(str(int(request_index)), []))

    def entry(self, key: str):
        return self.index["snapshots"].get(key)

    def load(self, key: str) -> dict:
        e = self.entry(key)
        if not e:
            raise CheckpointStoreError(f"checkpoint 不存在：{key}")
        zstandard = _zstd()
        path = os.path.join(self.dir, f"g{int(e['frame']):05d}.zst")
        with open(path, "rb") as f:
            f.seek(int(e["offset"]))
            blob = f.read(int(e["comp_len"]))
        body = zstandard.ZstdDecompressor().decompress(blob, max_output_size=int(e["raw_len"]))
        self._decompresses += 1
        return json.loads(body.decode("utf-8"))

    def load_for_request(self, request_index: int, kind: str | None = None) -> dict:
        keys = self.keys_for_request(request_index)
        if kind:
            keys = [k for k in keys if kind_of_key(k) == kind]
        if not keys:
            raise CheckpointStoreError(f"request_index={request_index} 没有 {kind or ''} 快照")
        return self.load(keys[-1])

    def find_for_request(self, request_index: int, messages, kind: str = "pre_request"):
        """按 request_index 找回**与录制 messages 对得上**的那份快照（§4.7 seek）。

        为什么不能直接取最后一份：一个 request_index 下可能挂着多份快照（同一轮里
        pre_request 之后还有 post_turn，迁移或重跑也会追加）。索引虽然记了
        messages_hash，但"哪一份才是这次请求看到的"只有**比对 messages** 才能确定——
        于是这里逐份验哈希，验中的那份才返回。全都对不上就抛
        CheckpointRestoreMismatch，而不是拿一份差不多的快照继续跑。
        """
        keys = self.keys_for_request(request_index)
        want = messages_digest(messages) if messages is not None else None
        candidates = [k for k in keys if kind_of_key(k) == kind] or list(keys)
        if not candidates:
            raise CheckpointStoreError(f"request_index={request_index} 没有 {kind} 快照")
        seen = []
        for key in candidates:
            try:
                snap = self.load(key)
            except CheckpointStoreError:
                continue
            seen.append(key)
            if want is None or snap.get("messages_hash") == want:
                return snap
        raise CheckpointRestoreMismatch(
            f"request_index={request_index} 的 {kind} 快照都与录制 messages 不一致"
            f"（候选 {len(seen)} 份：{seen[:4]}）")

    def decompress_count(self) -> int:
        """自检用：本实例一共解压过几次（seek 成本与 N 无关的断言靠它）。"""
        return self._decompresses
