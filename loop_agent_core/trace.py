"""L1 trace：一次运行的**事实流水**，可被回放引擎逐条消费（TRACE_REPLAY_BUILD.md §7）。

设计要点（每条都对应一个实测/推演过的失败模式）：

1. **追加写 + 每行一次 flush**。运行可能崩、可能被 Ctrl-C，trace 的价值恰恰在于
   事后还能解释"崩之前发生了什么"。缓冲写会把最后几条（也就是最关键的几条）丢掉。
   追加写 + trace 目录按 run_id 隔离，比"先把整个运行干完再落盘"更抗中断。
2. **事件分级**。`llm_chunk` 这类逐 token 事件量极大（一次 30k token 的回答能到上万行），
   但复盘**判定**几乎只用得到 request/response/tool_call/tool_result/turn_end。
   分级让默认产物保持"能读、能 diff"的规模，需要逐字复盘时再开严格档（§7.3/§10.4）。
3. **敏感信息在**写入端**脱敏，而不是读取端**。写在盘上的 key 就是泄漏了的 key；
   下游每次记得脱敏 = 迟早漏一次。
4. **异常 ID 从这里计算**，不在回放侧重算：两侧算法不一致时，回放会以为
   "录制里本来就没有这个异常"，从而静默跳过一条本该断言的路径（§5.3/§8.4）。

事件里始终带 `turn` / `tool_round`，回放可以按轮对齐；`request_index` 由
llm 网关在**请求真正发出前**记录（`begin_request`），chunk/usage 事件靠
contextvar 拿到同一个序号。
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import platform
import re
import threading
import time
import traceback
import uuid

# ── 事件级别定义（§4.4）────────────────────────────────────────────
# 三档，与 meta.json 里的 trace_level 同名（也接受旧整数）：
#   assert         0  判定必需，任何档位都写
#   stream_summary 1  每轮摘要（stream_summary 事件），默认档也写
#   stream         2  逐 chunk 文本（response_chunk / thinking_chunk），默认丢弃
LEVEL_CORE = 0
LEVEL_SUMMARY = 1
LEVEL_STREAM = 2
LEVEL_DIAG = LEVEL_SUMMARY  # 旧名，等价于 stream_summary

_LEVEL_NAMES = {LEVEL_CORE: "assert", LEVEL_SUMMARY: "stream_summary",
                LEVEL_STREAM: "stream"}
_LEVEL_FROM_NAME = {"assert": LEVEL_CORE, "core": LEVEL_CORE,
                    "stream_summary": LEVEL_SUMMARY, "summary": LEVEL_SUMMARY,
                    "diag": LEVEL_SUMMARY, "strict": LEVEL_SUMMARY,
                    "debug": LEVEL_SUMMARY, "full": LEVEL_STREAM,
                    "stream": LEVEL_STREAM}

# 事件类型 → 级别。未列出的事件默认按 CORE 处理（宁可多写，不可漏判定依据）。
# `llm_chunk` 是 **record** 级（§4.3）：它是回放"冻结响应"的载体，必须始终写；
# 逐 chunk 的**纯文本**视图另走 response_chunk / thinking_chunk（stream 级）。
EVENT_LEVEL = {
    "run_start": LEVEL_CORE,
    "run_end": LEVEL_CORE,
    "llm_request": LEVEL_CORE,
    "llm_end": LEVEL_CORE,
    "llm_param_retry": LEVEL_CORE,
    "llm_chunk": LEVEL_CORE,
    "headroom_transform": LEVEL_CORE,
    "tool_call": LEVEL_CORE,
    "tool_result": LEVEL_CORE,
    "tool_error": LEVEL_CORE,
    "tool_round_rollback": LEVEL_CORE,
    "turn_start": LEVEL_CORE,
    "turn_end": LEVEL_CORE,
    "nudge_injected": LEVEL_CORE,
    "notice:warn": LEVEL_CORE,
    "notice:info": LEVEL_SUMMARY,
    "limits": LEVEL_CORE,
    "prune": LEVEL_CORE,
    "compaction": LEVEL_CORE,
    "checkpoint": LEVEL_CORE,
    "interrupt": LEVEL_CORE,
    "worker_dispatch": LEVEL_CORE,
    "worker_settle": LEVEL_CORE,
    "xml_toolcall_retry": LEVEL_CORE,
    "thinking_only_retry": LEVEL_CORE,
    "api_error": LEVEL_CORE,
    "self_check": LEVEL_CORE,
    "error": LEVEL_CORE,
    "replay_skip": LEVEL_CORE,
    "override": LEVEL_CORE,
    "stream_summary": LEVEL_SUMMARY,
    "response_chunk": LEVEL_STREAM,
    "thinking_chunk": LEVEL_STREAM,
}

# 运行结束原因（turn_end.reason）闭集（§3.4）：回放按 reason 走不同断言，
# 出现枚举外的值即视为词表漂移——宁可当场失败，也不让回放去猜一个没见过的 reason。
TURN_END_REASONS = (
    "answer",            # 正常给出结论
    "stuck",             # 多轮无有效正文 / thinking-only 重试耗尽
    "garbled",           # 乱码过滤
    "interrupted",       # 用户 Esc
    "rounds_exhausted",  # 工具轮次上限（收尾轮也没拿到正文）
    "api_error",         # 调用失败
    "fallback",          # 兜底：循环走完仍没有可用正文
)

# ── 脱敏 ──────────────────────────────────────────────────────────
_SECRET_ENV = ("DS_KEY", "SUM_KEY", "VIS_KEY", "VISION_API_KEY", "TAVILY_API_KEY",
               "OPENAI_API_KEY", "QQ_BOT_TOKEN", "DEEPSEEK_API_KEY")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")
_B64_RE = re.compile(r"data:image/[a-zA-Z]+;base64,[A-Za-z0-9+/=]{64,}")


def redact(text: str) -> str:
    """抹掉凭据与内嵌图片（图片单独留指纹，见 redact_payload）。"""
    if not text:
        return text
    for name in _SECRET_ENV:
        v = os.environ.get(name)
        if v and len(v) >= 8:
            text = text.replace(v, f"<redacted:{name}>")
    text = _SK_RE.sub("sk-<redacted>", text)
    text = _B64_RE.sub("<image:base64-omitted>", text)
    return text


def redact_payload(obj):
    """递归脱敏，并把内嵌 base64 换成"指纹 + 长度"（既不留原文，也可校验漂移）。"""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_payload(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_payload(v) for v in obj]
    return obj


def digest(obj, n: int = 16) -> str:
    """结构化摘要（稳定键序，容忍不可序列化对象）。

    用途是"两边比一比是不是同一份"——不是加密，所以截断到 16 位足够避免碰撞且便于人眼比对。
    """
    blob = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:n]


_digest = digest  # 内部简写


def tools_sha256(tools) -> str | None:
    """一套工具 schema 的稳定摘要（§6.1：tools 只进指纹，不逐字进事件）。

    必须**只有这一份实现**：录制侧和回放侧都调它，两边的算法一旦分叉，指纹相等就
    不再意味着"同一套工具"，回放会拿另一套工具的响应去证明一个结论。
    """
    if not tools:
        return None
    return digest(tools, n=32)


# ── 异常 ID（§5.3）──────────────────────────────────────────────────
# 异常 ID 的口径：把异常映射成一段**结构描述**再哈希，这样同一个异常在不同时间、
# 不同进程、不同行号下都得到同一个 ID（行号特别容易改，绝不能进哈希）。
_EXC_NORMALIZE = (
    (re.compile(r"0x[0-9a-fA-F]+"), "<hex>"),
    (re.compile(r"line \d+"), "line <n>"),
    (re.compile(r"File \"[^\"]+\""), 'File "<f>"'),
    (re.compile(r"\b[0-9a-f]{32,}\b"), "<hash>"),
)


def exception_id(exc) -> str:
    """异常标识：类型 + 归一化后的消息摘要（前 12 位 sha1）。"""
    if exc is None:
        return "none"
    kind = type(exc).__name__
    msg = str(exc)
    for pat, rep in _EXC_NORMALIZE:
        msg = pat.sub(rep, msg)
    msg = " ".join(msg.split())[:300]
    digest = hashlib.sha1(f"{kind}:{msg}".encode("utf-8")).hexdigest()[:12]
    return f"{kind}#{digest}"


def assertion_id_for_error(text: str) -> str:
    """从错误文本算断言 ID（无异常对象时的兜底，如工具返回的 [Error] 文本）。"""
    body = " ".join(str(text or "").split())[:300]
    return "text#" + hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]


_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def exception_site(exc) -> str:
    """异常的项目内首个帧的**函数名**（§3.4）。

    只取函数名、绝不带行号：行号是最常被无关改动碰掉的东西，一旦进 ID，同一个 bug
    在两次提交里就是两个不同的断言 id，L3 的"复现同一个失败"判定会直接失效。
    """
    tb = getattr(exc, "__traceback__", None)
    frames = traceback.extract_tb(tb) if tb else []
    for fr in frames:
        try:
            path = os.path.abspath(fr.filename or "")
        except Exception:  # noqa: BLE001
            continue
        if path.startswith(_PROJECT_DIR):
            return fr.name or "<module>"
    return (frames[-1].name or "<unknown>") if frames else "<unknown>"


def exception_assertion_id(exc) -> str:
    """`exception:<ExcType>@<func>` —— 异常在断言面里的 id（§3.4）。"""
    if exc is None:
        return "exception:none@<unknown>"
    return f"exception:{type(exc).__name__}@{exception_site(exc)}"


def assertion_id(event: dict) -> str | None:
    """把一条 assert 级事件映射成断言 id（§3.4）。

    这是 L3 的最小化谓词与回放的"复现判据"共用的唯一入口：谓词必须能在**任意一次
    回放的 assert 级事件序列**里查找这个字符串，所以格式必须逐字稳定。
    """
    if not isinstance(event, dict):
        return None
    etype = event.get("type")
    if etype == "turn_end":
        reason = event.get("reason")
        return f"turn_end:reason={reason}" if reason else None
    if etype == "tool_error":
        tool = event.get("tool_name") or event.get("tool") or "?"
        code = event.get("code") or "unknown"
        return f"tool_error:{tool}:{code}"
    if etype == "notice":
        level = event.get("level") or "info"
        code = event.get("code")
        return f"notice:{level}:{code}" if code else None
    if etype in ("error", "exception", "api_error"):
        exc_type = event.get("exc_type")
        func = event.get("func") or event.get("site")
        if exc_type and func:
            return f"exception:{exc_type}@{func}"
        return None
    return None


def assertion_ids(events) -> list:
    """事件序列 → 断言 id 序列（保序，跳过非断言事件）。"""
    out = []
    for e in events:
        aid = assertion_id(e)
        if aid:
            out.append(aid)
    return out


_SENTINEL_STRIP = (
    re.compile(r"0x[0-9a-fA-F]+"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\b"),
    re.compile(r"\b\d+\.\d{4,}\b"),
)


def canonical_death_id(line: str) -> str:
    """致命错误字符串的规范化指纹（§5.1）：抹掉指针地址、时间戳、长浮点。"""
    s = str(line or "")
    for pat in _SENTINEL_STRIP:
        s = pat.sub("<v>", s)
    return " ".join(s.split())


# ── 路径 ──────────────────────────────────────────────────────────
# 落盘文件名是**对外契约**：cassette 抽取器、CLI、迁移器、回放器都按它们找文件，
# 所以集中在这里定义一次，别在别处再写字面量。
TRACE_EVENTS = "trace.jsonl"
TRACE_INDEX = "trace.idx.json"
TRACE_META = "meta.json"
TRACE_ERROR_LOG = "error.log"
# trace 的**格式**版本：字段增删都要 +1。回放器见到不认识的 schema_version 应当拒绝，
# 而不是把缺字段当默认值读下去（那会把"格式变了"伪装成"本来就没事"）。
SCHEMA_VERSION = 1
# 指纹 canon 的版本（§6.1）：canon 口径一旦变了，所有旧 cassette 都必须重抽——
# 因为指纹相等是"同一个请求"的唯一证据，口径变了旧指纹就是错的。
CANON_VERSION = 1
# 单会话 trace 目录的告警阈值（§4.1）：超过就记一条 error.log，提示 checkpoint 帧组
# 可能失控——trace 不该在用户不知情的情况下把磁盘吃掉几个 G。
SIZE_WARN_BYTES = 5 * 1024 * 1024


def trace_epoch(when=None) -> dict:
    """录制时的"时间锚"：回放要用它把 `$TS` 槽位重注入成录制当时的样子。

    `TRACE_EPOCH_ISO`（`YYYY-MM-DDTHH:MM:SS`）是给**入库 fixture 与自检**用的确定性钩子：
    录制基线一旦带上"今天几号"，就再也说不清下一次差异是代码改了还是日期翻了。
    生产录制不设这个变量；设了但解不出来就退回真实时钟，不因为一个环境变量判不出时间。
    """
    if when is None:
        pinned = os.environ.get("TRACE_EPOCH_ISO")
        if pinned:
            try:
                when = time.mktime(time.strptime(pinned, "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                when = None
    t = time.time() if when is None else when
    lt = time.localtime(t)
    return {"iso": time.strftime("%Y-%m-%dT%H:%M:%S", lt),
            "injected_date_str": time.strftime("%Y-%m-%d", lt)}


def default_trace_root() -> str:
    """默认落点：<仓库根>/tmp/traces。tmp/ 已在 .gitignore 覆盖范围内。"""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "tmp", "traces")


def trace_root() -> str:
    return os.environ.get("TRACE_DIR") or default_trace_root()


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def trace_level() -> int:
    """当前档位（§4.4）。默认 assert；TRACE_LEVEL 可给 assert/stream_summary/stream。"""
    v = (os.environ.get("TRACE_LEVEL") or "").strip().lower()
    return level_of(v) if v else LEVEL_CORE


def level_of(value) -> int:
    """档位的统一入口：接受 `assert`/`stream_summary`/`stream` 或旧整数，认不出按 assert。"""
    if isinstance(value, bool):
        return LEVEL_CORE
    if isinstance(value, int):
        return max(LEVEL_CORE, min(LEVEL_STREAM, value))
    return _LEVEL_FROM_NAME.get(str(value or "").strip().lower(), LEVEL_CORE)


def level_name(level) -> str:
    return _LEVEL_NAMES.get(level_of(level), "assert")


def tracing_enabled() -> bool:
    """是否写 trace。默认关闭：正常运行不为此付出 IO 代价；开回放/诊断时显式打开。"""
    v = (os.environ.get("TRACE_ENABLED") or "").strip().lower()
    return v in ("1", "true", "yes", "on", "strict")


# ── 当前运行上下文 ─────────────────────────────────────────────────
_writer: contextvars.ContextVar = contextvars.ContextVar("dsh_trace_writer", default=None)
_ctx: contextvars.ContextVar = contextvars.ContextVar("dsh_trace_ctx", default=None)


class TraceContext:
    """一次 chat() 回合的上下文：turn / tool_round / request_index。

    用不可变替换（dataclasses.replace 语义）而不是原地改字段：流式消费是生成器，
    跨线程时原地改会出现"chunk 事件挂到了下一个请求的序号上"这种串台。
    """
    __slots__ = ("turn", "tool_round", "request_index", "session")

    def __init__(self, turn=0, tool_round=0, request_index=0, session=""):
        self.turn = turn
        self.tool_round = tool_round
        self.request_index = request_index
        self.session = session

    def evolve(self, **kw):
        vals = {"turn": self.turn, "tool_round": self.tool_round,
                "request_index": self.request_index, "session": self.session}
        vals.update(kw)
        c = TraceContext.__new__(TraceContext)
        for k, v in vals.items():
            setattr(c, k, v)
        return c


def current_context() -> TraceContext:
    c = _ctx.get()
    if c is None:
        c = TraceContext()
        _ctx.set(c)
    return c


def set_context(ctx: TraceContext):
    """设置并返回 token，供 finally 里 reset_context 还原。"""
    return _ctx.set(ctx)


def reset_context(token) -> None:
    try:
        _ctx.reset(token)
    except (ValueError, LookupError):
        pass


def current_writer():
    return _writer.get()


def set_writer(writer):
    return _writer.set(writer)


def reset_writer(token) -> None:
    try:
        _writer.reset(token)
    except (ValueError, LookupError):
        pass


# ── Writer ────────────────────────────────────────────────────────
class TraceWriter:
    """trace 目录的唯一写者。线程安全（llm 流式消费可能落在别的线程）。"""

    def __init__(self, run_dir, run_id, manifest=None, level=None, on_event=None):
        self.run_dir = run_dir
        self.run_id = run_id
        # 观察者（§4.4）：收到的是**过滤之后**的事件，用不上时留 None，零开销。
        self.on_event = on_event
        # 落盘名字固定（§4.1）：trace.jsonl 是流水，trace.idx.json 是行偏移索引，
        # meta.json 是会话元信息。回放 / CLI / 迁移器都按这三个名字找文件。
        self.path = os.path.join(run_dir, "trace.jsonl")
        self.idx_path = os.path.join(run_dir, "trace.idx.json")
        self.manifest_path = os.path.join(run_dir, "meta.json")
        self.level = trace_level() if level is None else level_of(level)
        self._lock = threading.RLock()
        self._seq = 0
        self._last_seq = 0
        # 已分配的 request_index 数（只增，跨 turn/tool_round）。打开已有的 trace 目录时
        # 要从历史里**接着数**：同一个会话可能被多次打开（CLI 续跑、测试里连开两轮），
        # 从 0 重来会让 checkpoint 键位互相覆盖、回放定位到上一次的现场。
        self._requests = self._resume_request_count(run_dir)
        self._offsets: list = []
        self.start_ts = time.time()
        self.end_ts = None
        self._fp = None
        self._closed = False
        self._counts: dict = {}
        self._write_failures = 0
        os.makedirs(run_dir, exist_ok=True)
        self._fp = open(self.path, "a", encoding="utf-8", newline="\n")
        self.write_manifest(manifest or {})

    # -- manifest --
    def _resume_request_count(self, run_dir) -> int:
        """续写已有 trace 时，从历史事件里恢复 request_index 计数。

        读取失败（文件不存在/半截行）一律当作 0 处理：续不上只是键位可能撞上，
        而为了一个空目录在这里抛异常会让"打开 trace"变成一件会失败的事。
        """
        path = os.path.join(run_dir, "trace.jsonl")
        top = 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    n = e.get("request_index")
                    if isinstance(n, int) and n > top:
                        top = n
        except OSError:
            return 0
        return top

    def write_manifest(self, extra):
        m = {"run_id": self.run_id, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
             "trace_level": level_name(self.level), "trace_level_num": self.level,
             "schema_version": SCHEMA_VERSION, "canon_version": CANON_VERSION,
             "start_ts": self.start_ts, "end_ts": self.end_ts,
             "trace_epoch": trace_epoch(), "pid": os.getpid()}
        # 环境快照放最后、并且**不让它覆盖**调用方显式传进来的字段：回放要靠
        # headroom version/mode 判 CassetteExpired，靠 backend/model 判指纹，
        # 这些值一旦被"当前环境"覆盖，录像就变成了"现在"的自述。
        for k, v in _runtime_meta().items():
            m.setdefault(k, v)
        m.update(extra or {})
        # end_ts 由 writer 自己掌管：close() 是"读回旧 meta 合并后再写"，若让旧值覆盖，
        # 每次关闭都会把刚写下的结束时间抹回 None。
        m["end_ts"] = self.end_ts
        with self._lock:
            with open(self.manifest_path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(m, f, ensure_ascii=False, indent=2)
        return m

    def read_manifest(self) -> dict:
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    # -- 写 --
    def _should_write(self, etype, level_hint=None) -> bool:
        """档位过滤（§4.4）：assert 级始终写，stream_summary 默认写，stream 级默认丢弃。

        注意"stream_summary 默认写"是**字面**要求：默认档（assert）下逐轮摘要也要落盘，
        否则排障时最常用的"这一轮吐了多少字、首字多久"在默认档里全都查不到。所以只有
        stream 级（逐 chunk 文本视图）需要显式开档。
        """
        lvl = EVENT_LEVEL.get(etype, LEVEL_CORE) if level_hint is None else level_hint
        if lvl >= LEVEL_STREAM:
            return self.level >= LEVEL_STREAM
        return True

    def append(self, event: dict, level_hint=None) -> bool:
        """写一条事件。返回是否真的写入（便于测试断言分级行为）。"""
        if self._closed or not event:
            return False
        etype = event.get("type") or "unknown"
        if not self._should_write(etype, level_hint):
            return False
        ctx = current_context()
        with self._lock:
            # 先序列化、再占号：占号后写失败会在序号里留一个洞，之后**每一条**事件都会
            # 被 validate 报"seq 不连续"，把真正的失败点埋掉。
            seq = self._seq + 1
            rec = {
                "seq": seq,
                "ts": round(time.time(), 4),
                "type": etype,
                "turn": event.get("turn", ctx.turn),
                "tool_round": event.get("tool_round", ctx.tool_round),
                "request_index": event.get("request_index", ctx.request_index),
            }
            # 会话 id 与 turn / tool_round 一样按上下文补：写入器知道自己在录哪次会话，
            # 不该要求每个发射点自己记得带上它。缺了它，单看一行事件无法归因到哪次运行
            # （run_start 有、turn_end 没有的那种半截归属），而 trace 的意义正是"每一行
            # 都能独立归因"。
            if "session" not in event and ctx.session:
                rec["session"] = ctx.session
            for k, v in event.items():
                if k not in rec:
                    rec[k] = v
            rec = redact_payload(rec)
            try:
                text = json.dumps(rec, ensure_ascii=False) + "\n"
            except (TypeError, ValueError) as exc:
                self._write_failures += 1
                self._log_failure(etype, "serialize", exc)
                return False
            try:
                offset = self._fp.tell()
                self._fp.write(text)
                self._fp.flush()  # 每行 flush：崩溃前的事实必须留在盘上
            except OSError as exc:
                self._write_failures += 1
                self._log_failure(etype, "write", exc)
                return False
            self._seq = seq
            self._last_seq = seq
            self._offsets.append(offset)
            self._counts[etype] = self._counts.get(etype, 0) + 1
        # on_event 只转发**通过了同一套档位过滤**的事件（§4.4）：被档位丢掉的事件不能
        # 从这里漏出去，否则"默认档不显示逐 chunk 文本"这件事会被一个回调绕过。
        if self.on_event is not None:
            try:
                self.on_event(rec)
            except Exception:  # noqa: BLE001 —— 观察者的异常不能反过来弄坏 trace
                pass
        return True

    def _log_failure(self, etype: str, where: str, exc) -> None:
        """写入失败的旁路日志：trace 坏掉时这是唯一还能落盘的事实，必须留下。"""
        try:
            path = os.path.join(self.run_dir, "error.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{time.time():.4f}\t{etype}\t{where}\t{type(exc).__name__}: {exc}\n")
        except OSError:
            pass

    def write_failures(self) -> int:
        return self._write_failures

    def emit_record(self, record: dict) -> bool:
        """写一条**录制记录**（§4.3）：永远写，不受档位过滤。

        记录（llm_request / llm_chunk / llm_end / tool_call / tool_result / limits /
        checkpoint …）是回放的**输入**，缺一条就少一份可还原的事实；档位只能删
        "给人看的视图"（response_chunk），不能删事实。
        """
        return self.append(record, level_hint=LEVEL_CORE)

    def emit_event(self, event: dict) -> bool:
        """写一条**观察事件**：按档位过滤（stream_summary / stream 档会放行更多）。"""
        return self.append(event)

    def write_checkpoint(self, snap: dict) -> int:
        """写一份 checkpoint 快照，返回它挂接的 request_index（§4.7）。"""
        from . import checkpoint
        return checkpoint.write_snapshot(snap, run_dir=self.run_dir)

    def begin_request(self, kind: str, model=None) -> int:
        """给即将发出的请求分配 request_index（§7.2），**必须在请求真正发出前**调用。

        这个号是**全 trace 唯一且只增**的：`turn` 会重开、`tool_round` 会清零，但
        request_index 一旦分配就跟着这次调用走到底。回放按它定位请求、checkpoint 按它
        挂接，两者必须是同一个坐标系——早期实现把它做成"本轮内序号"，于是每个工具轮
        都从 0 重新开始，checkpoint 全撞在 request_index=0 上，seek 只能靠猜。
        """
        with self._lock:
            n = self._requests + 1
            self._requests = n
            return n

    def peek_request_index(self) -> int:
        """下一个将被分配的 request_index（不占用）。checkpoint 采在请求之前时用它。"""
        with self._lock:
            return self._requests + 1

    def request_count(self) -> int:
        with self._lock:
            return self._requests

    def last_seq(self) -> int:
        """最近一次真实写入的事件 seq（过滤掉的事件不占号）。"""
        return self._last_seq

    def offset_of(self, seq: int):
        """seq → 该行在文件里的**字节偏移**（O(1) 按序号跳转，§7.1）。"""
        if 1 <= seq <= len(self._offsets):
            return self._offsets[seq - 1]
        return None

    def write_index(self) -> str:
        """把行偏移索引落到磁盘（§4.2 第 5 条）。缺失时回放仍能工作，只是丢掉 O(1) 跳转。"""
        path = self.idx_path
        with self._lock:
            data = {"run_id": self.run_id, "file": os.path.basename(self.path),
                    "count": self._seq, "offsets": self._offsets}
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        return path

    def note(self, etype, **fields) -> bool:
        return self.append({"type": etype, **fields})

    def counts(self) -> dict:
        return dict(self._counts)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.end_ts = time.time()
            try:
                self.write_index()
            except OSError:
                pass
            try:
                if self._fp:
                    self._fp.flush()
                    self._fp.close()
            except OSError:
                pass
            try:
                # end_ts 要在文件关掉之后再写一次 meta。**必须把旧 meta 读回来合并**：
                # write_manifest 是"重写整份"，只传 {} 会把 session_id/backend/model 抹掉，
                # 而回放/CLI 正是靠这些字段认会话。
                self.write_manifest(self.read_manifest())
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _runtime_meta() -> dict:
    """meta.json 的环境快照（§4.1）。

    这里全部是**尽力而为**：取不到就不写这个键，绝不让"记录自己跑了什么环境"
    变成一件会抛异常的事——trace 是诊断设施，它不能成为新的故障源。
    """
    out = {"env_snapshot": {}}
    snap = out["env_snapshot"]
    for k in ("MODEL_BACKEND", "HEADROOM_ENABLED", "HEADROOM_MODE", "TRACE_LEVEL"):
        v = os.environ.get(k)
        if v is not None:
            snap[k] = v
    snap["python"] = platform.python_version()
    snap["platform"] = platform.platform()
    try:
        from importlib.metadata import PackageNotFoundError, version as _pkg_version
        # 发行名是 headroom-ai，导入名才是 headroom（§4.8 的 CassetteExpired 靠这个值）
        for _name in ("headroom-ai", "headroom"):
            try:
                out["headroom_version"] = _pkg_version(_name)
                break
            except PackageNotFoundError:
                continue
    except Exception:  # noqa: BLE001 —— 没装 headroom 的检出也要能录
        pass
    out["headroom_mode"] = os.environ.get("HEADROOM_MODE", "audit")
    try:
        from .config import _backend_config
        out["model_context_limits"] = {"ctx": _backend_config().get("ctx")}
    except Exception:  # noqa: BLE001
        pass
    return out


def _decode(raw) -> str:
    """子进程输出（bytes，编码随系统代码页）转成能写进 error.log 的一行。"""
    if not raw:
        return ""
    if isinstance(raw, bytes):
        for enc in ("utf-8", "mbcs", "gbk", "latin-1"):
            try:
                return raw.decode(enc).strip()
            except (UnicodeDecodeError, LookupError):
                continue
        return repr(raw)
    return str(raw).strip()


def harden_dir(path) -> bool:
    """把 trace 目录收紧到"只有当前用户能读"（§4.1）。

    trace 里有完整的对话、工具入参、工作区路径——它比聊天记录更敏感，因为它还带着
    文件内容与命令。POSIX 用 0o700；Windows 没有 mode 位，用 icacls 去掉继承并只授权
    当前用户。两者都是**尽力而为**：加固失败只写 error.log，不能让"跑一次 agent"因为
    权限收紧失败而失败（失败本身会让人干脆关掉 tracing）。

    Windows 上收紧权限**只**走 icacls，建目录绝不用 `mode=0o700`：Python 会把 0o700
    翻译成一条"只有属主"的 Windows DACL，而受限/沙箱进程的访问令牌对不上这条 DACL，
    结果是**自己刚建的目录自己都进不去**（实测 `os.makedirs(d, mode=0o700)` 之后立刻
    `os.listdir(d)` 就是 `PermissionError: [WinError 5]`，且此后 icacls 也无法读取该目录、
    error.log 都写不进去——整个 trace 目录彻底废掉）。建目录用不带 mode 的 makedirs，
    权限收敛交给 icacls；任何一步失败都只记 error.log 并把目录留在**可用**状态。

    `icacls` 的非零退出**必须**当成失败：在受限环境里它会打印 "Access is denied."
    却仍以 0 退出（实测），只看 `check=False` 就会把"根本没加固"报成加固成功。
    收紧后额外自查一次——当前用户必须仍能列出该目录，列不出说明权限已被收紧到自己
    都进不去，立刻尽力改回来，绝不把这种目录留给后续写入。
    """
    if os.environ.get("TRACE_NO_ACL") == "1":
        return False
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return False
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
            return True
        except OSError:
            return False
    try:
        import subprocess
        user = os.environ.get("USERNAME") or ""
        if not user:
            return False
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
                              capture_output=True, timeout=15, check=False, creationflags=flags)
        if proc.returncode != 0:
            log_problem(path, f"[trace] icacls 收紧权限失败（退出码 {proc.returncode}）："
                              f"{_decode(proc.stderr) or _decode(proc.stdout)}")
            return False
        try:
            os.listdir(path)  # 收紧后自己必须还进得去
        except OSError as e:
            _restore_inherited(path, flags)
            log_problem(path, f"[trace] 收紧权限后目录已不可读（{e}），已回退继承")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log_problem(path, f"[trace] 收紧权限异常：{e}")
        return False


def _restore_inherited(path, flags) -> None:
    """让目录重新继承父目录的权限（收紧把自己锁在外面时的最后手段）。

    `/reset` 会把继承 ACE 重新带回来，但父目录如果本身还带着 LOW 强制标签之类的
    约束，单靠它未必够；因此再补一次显式的当前用户完全控制授权。两步都是尽力而为。
    """
    import subprocess
    for args in (["icacls", path, "/reset"],
                 ["icacls", path, "/grant", f"{os.environ.get('USERNAME') or ''}:(OI)(CI)F"]):
        if not args[2]:
            continue
        try:
            subprocess.run(args, capture_output=True, timeout=15, check=False,
                           creationflags=flags)
        except Exception:  # noqa: BLE001
            pass


def dir_size(path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def log_problem(run_dir, message: str) -> None:
    """把一条问题追加到 <run_dir>/error.log（trace 自身的故障记录）。"""
    try:
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, TRACE_ERROR_LOG), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def check_size(run_dir, limit=None) -> int:
    """单会话目录超限就记一条告警（§4.1：>5MB）。返回目录字节数。"""
    limit = SIZE_WARN_BYTES if limit is None else limit
    size = dir_size(run_dir)
    if size > limit:
        log_problem(run_dir, f"[trace] 会话目录 {size} 字节，超过告警阈值 {limit}"
                            f"（checkpoint 帧组占大头；考虑调小 GROUP_REQUESTS 或关掉 checkpoint）")
    return size


# ── 模块级便捷入口 ─────────────────────────────────────────────────
def open_writer(session_id=None, *, level="assert", run_dir=None, manifest=None,
                run_id=None, on_event=None, **kw) -> TraceWriter:
    """新建（或继续）一次 trace，并把它设为当前 writer。

    `session_id` 同时充当目录名与 run_id；`level` 取 assert / stream_summary / stream。
    `on_event` 是事件观察者（§4.4），只收到通过档位过滤的事件。
    返回 writer；调用方负责 close（或用 with 语义的 open_run）。
    """
    root = trace_root()
    rid = run_id or session_id or new_run_id()
    rdir = run_dir or os.path.join(root, rid)
    harden_dir(rdir)
    man = dict(manifest or {})
    man.setdefault("session_id", rid)
    man.setdefault("schema_version", SCHEMA_VERSION)
    man.setdefault("canon_version", CANON_VERSION)
    man.setdefault("trace_epoch", trace_epoch())
    w = TraceWriter(rdir, rid, manifest=man, level=level, on_event=on_event)
    token = set_writer(w)
    w.context_token = token  # 便于 close_current 还原
    # 新 run 必须从干净的上下文起。这批字段是**进程级**的：不重置的话，同一进程里
    # 第二次运行会把上一轮末尾的 request_index 带进 run_start——"开跑之前"却挂着
    # 第 4 个请求的序号，于是"重录一次"和"第一次录"产出的 trace 不一样（入库 fixture
    # 的 --check 会因此假报漂移，回放侧也会看到一份自相矛盾的 trace）。
    set_context(TraceContext(session=man.get("session_id", "")))
    # run_start 必须是文件里的第一条：回放要靠它判断"这份 trace 是不是从头录的"，
    # 中途截断的 trace 缺这一条，回放应当直接拒绝而不是当成正常运行。
    w.append({"type": "run_start", "run_id": rid, "level": w.level,
              "session": (manifest or {}).get("session_id", "")})
    # 开场先记一条 limits：如果这次运行一个工具都没调用、也没触发压缩，limits 事件本来
    # 永远不会发，回放就查不到"当时 result_cap 是多少"。
    emit_limits(site="run_start", force=True)
    return w


def close_writer(writer=None, reason="normal") -> None:
    """收尾：补一条 run_end **再**关文件（run_end 是"运行正常结束"的唯一标志）。"""
    w = writer or current_writer()
    if w is None:
        return
    try:
        w.note("run_end", reason=reason, event_count=w.last_seq())
    except Exception:  # noqa: BLE001 —— 收尾失败不能反过来搞崩调用方
        pass
    try:
        from . import checkpoint
        checkpoint.close_store(w.run_dir)
    except Exception:  # noqa: BLE001
        pass
    tok = getattr(w, "context_token", None)
    if tok is not None:
        reset_writer(tok)
    w.close()
    check_size(w.run_dir)


# ── 会话级录制：生产入口的建档与收尾（§4.10） ─────────────────────
# 失败断言口径：一条断言 id 落进下面两类之一，这次会话就是「失败现场」，
# 保留策略不得删除（tools/trace.py clean 认 meta.json 的 retention 字段）。
#
# turn_end 的哪些理由算「正常」：
#   answer      —— 拿到正文，这是唯一的成功终局；
#   interrupted —— 用户自己按的 Esc，是人的意图，不是代码跑歪。
# 其余理由（stuck / garbled / rounds_exhausted / api_error / fallback）都是运行出了问题。
# notice 不进失败口径：它是诊断通道（提示/降级/跳过压缩），不是「这次会话坏了」。
TURN_END_NORMAL_REASONS = ("answer", "interrupted")
FAILURE_ASSERTION_PREFIXES = ("tool_error:", "exception:")
RETENTION_FAILURE = "failure"
RETENTION_NORMAL = "normal"


def is_failure_assertion(aid) -> bool:
    """一条断言 id 是否代表失败现场（判据见上面两段注释）。"""
    a = str(aid or "")
    if a.startswith(FAILURE_ASSERTION_PREFIXES):
        return True
    head = "turn_end:reason="
    if a.startswith(head):
        return a[len(head):] not in TURN_END_NORMAL_REASONS
    head = "run_end:reason="
    if a.startswith(head):
        return a[len(head):] not in TURN_END_NORMAL_REASONS
    return False


def failure_assertions(events) -> list:
    """事件序列 → 失败断言 id（保序）。口径复用 assertion_ids，不另造一套。

    额外补一条 `run_end:reason=exception`：会话是**从入口崩出去**的（例如传输层抛异常
    把 chat 拽崩），这时里面可能一条断言都没有，但这次运行显然不是正常收尾。
    """
    out = [a for a in assertion_ids(events) if is_failure_assertion(a)]
    for e in events:
        if e.get("type") == "run_end" and e.get("reason") == "exception":
            out.append("run_end:reason=exception")
            break
    return out


def failure_assertions_in(run_dir) -> list:
    """从落盘的 trace 里数失败断言。读不出来就当「没有失败」，绝不外抛。"""
    try:
        return failure_assertions(read_events(run_dir))
    except (OSError, TraceError):
        return []


def mark_retention(run_dir, reason="normal") -> dict:
    """把保留判据写进 meta.json：失败现场标 failure，其余标 normal（§4.10）。

    只**加字段**、不动事件流：回放器不读 retention，所以这里标错也不会污染录像本身；
    而 `tools.trace clean` 靠它决定谁不能删。原子替换，避免半截 meta 把会话弄坏。
    """
    man = load_manifest(run_dir)
    fails = failure_assertions_in(run_dir)
    man["retention"] = RETENTION_FAILURE if fails else RETENTION_NORMAL
    man["failure_assertions"] = fails
    man["end_reason"] = reason
    path = os.path.join(run_dir, TRACE_META)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    if fails:
        log_problem(run_dir, "[trace] 失败现场，已标记保留（clean 默认不清）："
                             + ", ".join(fails))
    return man


def session_backend() -> str:
    """本次会话真正在用的后端名（manifest.backend 的来源）。"""
    return os.environ.get("MODEL_BACKEND") or "deepseek"


def session_model() -> str:
    """本次会话真正在用的模型名。取不到就空串，不为一个字段让会话起不来。"""
    try:
        from .config import cfg
        return str(cfg.get("model") or "")
    except Exception:  # noqa: BLE001
        return ""


def current_session_id() -> str:
    """当前活跃 writer 的会话 id（没有在录就是空串）。

    供薄壳给 conv 打 `_session_id`：这样 turn_start / turn_end 里的 session
    与 run_start 里的 session 是同一个值，而不是各写各的。
    """
    w = current_writer()
    return str(getattr(w, "run_id", "") or "") if w is not None else ""


class SessionRecording:
    """一次会话的录制壳：起点建档、终点收尾（§4.10）。

    三条硬规矩：

    1. **开关只在起点求值一次**。进了 `start()` 之后再改 `TRACE_ENABLED` 也不改主意——
       半路开关会让 trace 同时缺前半段和后半段，比不录更难查。
    2. **收尾走 try/finally**（`finish()` 幂等）：正常退出、异常退出、用户中断都补
       `run_end` 与 `trace.idx.json`。
    3. **收尾失败不外抛**：录制是诊断设施，它不能成为「会话挂掉」的原因。
    """

    def __init__(self, session_id=None, *, entry="", backend=None, model=None,
                 run_dir=None, level=None, on_event=None, reason="normal"):
        self.entry = entry
        self.session_id = session_id or new_run_id(entry or "session")
        self.run_dir = run_dir
        # level=None 表示「档位交给 TRACE_LEVEL 决定」（默认 assert），不在这里钉死。
        self.level = level
        self.on_event = on_event
        self.backend = backend
        self.model = model
        self.reason = reason
        self.writer = None
        self.active = False
        self.retention = None

    def start(self):
        """求值开关并建档。关着时**一根文件都不产生**（不等价于建个空目录）。"""
        if self.active:
            return self
        if not tracing_enabled():
            return self
        man = {"session_id": self.session_id,
               "backend": self.backend or session_backend(),
               "model": self.model or session_model()}
        if self.entry:
            man["entry"] = self.entry
        self.writer = open_writer(self.session_id, level=self.level,
                                  run_dir=self.run_dir, manifest=man,
                                  on_event=self.on_event)
        self.run_dir = self.writer.run_dir
        self.active = True
        return self

    def finish(self, reason=None):
        """收尾（幂等）：补 run_end + 索引 + 保留判据。返回写入的 manifest。"""
        w = self.writer
        if w is None:
            return None
        self.writer = None
        self.active = False
        reason = reason or self.reason
        try:
            w.write_index()          # 关文件之前写：之后 writer 就不知道字节位置了
        except Exception:  # noqa: BLE001
            pass
        try:
            close_writer(w, reason=reason)
        except Exception as exc:  # noqa: BLE001
            log_problem(w.run_dir, f"[trace] 收尾失败（{type(exc).__name__}: {exc}）")
        try:
            self.retention = mark_retention(w.run_dir, reason=reason)
        except Exception as exc:  # noqa: BLE001 —— 标不上保留只是少一层保护，不该外抛
            log_problem(w.run_dir, f"[trace] 保留判据写入失败（{type(exc).__name__}: {exc}）")
        return self.retention

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.finish(reason=_exit_reason(exc_type))
        return False


def _exit_reason(exc_type) -> str:
    if exc_type is None:
        return "normal"
    if issubclass(exc_type, KeyboardInterrupt):
        return "interrupted"
    return "exception"


def session_recording(session_id=None, **kw) -> SessionRecording:
    """生产入口的会话录制壳（§4.10）。用法：

        with trace.session_recording(entry="tui"):
            run_one_session()

    开关关着时 `__enter__` 直接返回一个未激活的壳，没有任何 IO。
    """
    return SessionRecording(session_id, **kw)


def begin_turn(turn=0, session="") -> TraceContext:
    """开一轮：绑定 turn/session，清零 tool_round 与 request_index。"""
    ctx = current_context().evolve(turn=turn, session=session, tool_round=0,
                                   request_index=0)
    set_context(ctx)
    if current_writer() is not None:
        emit_event({"type": "turn_start", "turn": turn, "session": session})
    return ctx


def set_tool_round(n: int) -> TraceContext:
    """推进工具轮号（每个工具轮的动作事件都挂在它下面）。"""
    ctx = current_context().evolve(tool_round=n, request_index=0)
    set_context(ctx)
    return ctx


def emit_event(event: dict, level_hint=None) -> bool:
    """写一条**观察事件**；没有活跃 writer 时是**静默空操作**。

    这样 runner / compaction 里可以无脑插发射点，不必到处判"有没有在录"。
    """
    w = current_writer()
    if w is None:
        return False
    return w.append(event, level_hint=level_hint)


def emit_record(record: dict) -> bool:
    """写一条**录制记录**（§4.3）：永远写，不受档位过滤；无 writer 时空操作。

    与 emit_event 的分界不是"重要程度"而是**角色**：记录是回放的输入（llm_request /
    llm_chunk / llm_end / tool_call / tool_result / limits / checkpoint），事件是给人看的
    观察（response_chunk / turn_end / notice）。档位只允许削后者。
    """
    w = current_writer()
    if w is None:
        return False
    return w.append(record, level_hint=LEVEL_CORE)


def write_checkpoint(snap: dict) -> int:
    """把一份快照写进当前 trace 的 checkpoint 帧组（§4.7）。"""
    from . import checkpoint
    return checkpoint.write_snapshot(snap)


# ── limits：生效旋钮值（§4.5）──────────────────────────────────────
def limits_payload(conv=None) -> dict:
    """影响"上下文里到底存了什么"的**全部生效旋钮**，加当前记账计数。

    为什么必须记：回放要用**现在的**limits 重算 `truncate(result_full)`，再跟录制里的
    `result_stored` 比。如果某个旋钮被改过而 trace 里没有当时的原值，回放就没法判断
    "差异是回归还是配置不同"——只能记一条无法归因的 drift。
    全部惰性 import：trace 处在依赖图最底层，顶层 import config/context 会成环。
    """
    out = {"knobs": {}, "accounting": {}}
    try:
        from .config import _compact_k
        k = _compact_k()
        out["knobs"].update({
            "compact_max_tokens": k.get("max_tokens"),
            "compact_max_tokens_hard": k.get("max_tokens_hard"),
            "trigger_ratio": k.get("trigger_ratio"),
            "block_tokens": k.get("block_tokens"),
            "keep_rounds": k.get("keep_rounds"),
            "dedup_min_chars": k.get("dedup_min_chars"),
            "dedup_min_saved": k.get("dedup_min_saved"),
            "dedup_enabled": k.get("dedup_enabled"),
            "compact_enabled": k.get("compact_enabled"),
            "min_rounds": k.get("min_rounds"),
            "max_per_session": k.get("max_per_session"),
            "max_fails": k.get("max_fails"),
            "max_backoff": k.get("max_backoff"),
        })
    except Exception:  # noqa: BLE001 —— 记账失败不该影响主流程
        pass
    try:
        from .context import _prune_cap
        out["knobs"]["prune_cap"] = _prune_cap()
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import runner as _runner
        out["knobs"]["result_cap"] = 6000 if _is_local() else 10000
        out["knobs"]["store_tool_args_threshold"] = getattr(_runner, "_STORE_ARGS_THRESHOLD", None)
        out["knobs"]["tool_timeout"] = getattr(_runner, "_TOOL_TIMEOUT", None)
        out["knobs"]["worker_timeout"] = getattr(_runner, "_WORKER_TIMEOUT", None)
        out["knobs"]["sync_max"] = getattr(_runner, "_SYNC_MAX", None)
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import config as _config
        cache = getattr(_config, "_compact_k_cache", None) or {}
    except Exception:  # noqa: BLE001
        cache = {}
    acct = dict(cache)
    if conv is not None:
        # 会话自己的记账才是真值（config 缓存只是"旋钮"）。cstate(conv) 取的是
        # conv["_compact_state"]，两者在不同代码路径里被写过。
        try:
            from .compaction import cstate
            acct.update(cstate(conv) or {})
        except Exception:  # noqa: BLE001
            pass
    for key in ("_compact_count", "_compact_token", "_compact_last_turn", "_dedup_saved",
                "_compact_fails", "_compact_retry_turn"):
        if key in acct:
            out["accounting"][key.lstrip("_")] = acct[key]
    return out


def _is_local() -> bool:
    try:
        from .config import _is_local_backend
        return bool(_is_local_backend())
    except Exception:  # noqa: BLE001
        return False


_limits_seen: set = set()
# 组装 limits 载荷时会读 `_compact_k()`，而 `_compact_k()` 自己也会发一条 limits
# （site="compact_k"）——于是"记一次预算"变成记两条，而且内层先落盘。后果不只是
# 多一行日志：同一段历史跑两次，第一次两条、第二次一条（第二次 `_compact_k`
# 缓存已热、不再发），确定性用例直接红——同一件事有了两个真相。
_limits_in_progress = False


def emit_limits(site: str = "", force: bool = False, conv=None) -> dict | None:
    """在**生效值发生变化**时记一条 limits（同一 site 同一值只记一次）。

    不去重会把一次运行写成几万条相同的 limits（每次工具调用都过一次 cap），
    但完全不记又会丢掉"这次运行跑的是 6000 还是 8000"这个关键事实。
    """
    global _limits_in_progress
    w = current_writer()
    if w is None or _limits_in_progress:
        return None
    _limits_in_progress = True
    try:
        payload = limits_payload(conv)
        digest = hashlib.sha1(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
        key = (getattr(w, "run_dir", ""), site, digest)
        if key in _limits_seen and not force:
            return None
        _limits_seen.add(key)
        payload["site"] = site
        payload["digest"] = digest
        w.emit_record({"type": "limits", **payload})
        return payload
    finally:
        _limits_in_progress = False


def reset_limits_cache() -> None:
    _limits_seen.clear()


# ── headroom 管道扩展：headroom_transform（§4.3）───────────────────
class HeadroomTraceExtension:
    """把 headroom 压缩前后的 messages 记下来。

    为什么用扩展而不是包一层 client：压缩发生在 headroom 内部管道里，外面包一层只能看到
    送进去的 messages_pre，看不到它到底把上下文改成了什么；而"改成了什么"正是回放要比对的。
    扩展里**绝不允许抛异常**（headroom 会吞掉 handler 异常，但吞掉后我就再也看不见压缩了，
    所以这里自己 try 到底，并在失败时记一条 error 事件）。
    """

    def __init__(self):
        self._pre: dict = {}
        self._done: set = set()

    def on_pipeline_event(self, event):
        try:
            stage = getattr(event, "stage", "")
            stage = str(getattr(stage, "value", stage) or stage).lower()
            rid = str(getattr(event, "request_id", "") or "")
            msgs = getattr(event, "messages", None)
            if stage.endswith("input_received"):
                if msgs is not None:
                    self._pre[rid] = msgs
                self._done.discard(rid)
            elif stage.endswith("input_compressed"):
                self._record(rid, msgs, getattr(event, "metadata", None))
            elif stage.endswith("pre_send"):
                # audit 模式**不发** input_compressed（没有任何管道阶段真的跑），
                # 只发 input_received → pre_send。若只在 input_compressed 上记录，
                # 默认档（audit）就永远没有 headroom_transform，回放时看不出
                # "请求到底被 headroom 改过没有"——这恰恰是最要紧的一件事。
                # pre_send 的 messages 就是真正发出去的那份，任何模式下都存在。
                if rid not in self._done:
                    self._record(rid, msgs, getattr(event, "metadata", None))
                self._done.discard(rid)
                self._pre.pop(rid, None)
        except Exception as exc:  # noqa: BLE001
            try:
                emit_record({"type": "error", "where": "headroom_extension",
                             "exc_type": type(exc).__name__, "message": str(exc)[:400]})
            except Exception:  # noqa: BLE001
                pass
        return event

    def _record(self, rid, post, metadata=None):
        pre = self._pre.get(rid)
        self._done.add(rid)
        if pre is None and post is None:
            return
        w = current_writer()
        if w is None:
            return
        # 只记"变了没变"的摘要 + 两侧长度，不记全文：压缩前的上下文往往就是整场对话，
        # 全量落盘会让 trace 大小爆炸，而排查只需要知道动了哪些消息（digest 可定位）。
        def _d(msgs):
            if msgs is None:
                return None
            return {"count": len(msgs), "digest": _digest(msgs)}
        changed = _digest(pre) != _digest(post) if (pre is not None and post is not None) else None
        emit_record({
            "type": "headroom_transform",
            "headroom_request_id": rid,
            "changed": changed,
            "messages_pre": _d(pre),
            "messages_post": _d(post),
            "metadata": {k: v for k, v in (metadata or {}).items()
                         if isinstance(v, (str, int, float, bool, type(None)))} if metadata else None,
        })


def headroom_extensions() -> list:
    """给 HeadroomClient(config=HeadroomConfig(pipeline_extensions=[...])) 用。"""
    return [HeadroomTraceExtension()]


def current_run_dir():
    w = current_writer()
    return w.run_dir if w else None


# ── 读取 ──────────────────────────────────────────────────────────
class TraceError(RuntimeError):
    """trace 结构性错误（缺文件、坏行、schema 不合法）。"""


def read_events(run_dir) -> list:
    """读整个 trace.jsonl。坏行**不吞**：定位错误比"少了几条"重要得多。"""
    path = run_dir if run_dir.endswith(".jsonl") else os.path.join(run_dir, "trace.jsonl")
    if not os.path.exists(path):
        raise TraceError(f"trace 文件不存在：{path}")
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError as e:
                raise TraceError(f"{path}:{i} 行不是合法 JSON：{e}") from None
    return out


def iter_events(run_dir):
    path = run_dir if str(run_dir).endswith(".jsonl") else os.path.join(run_dir, "trace.jsonl")
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def list_runs(root=None) -> list:
    """列出现有 trace（新的在前）。返回 [{run_id, dir, manifest}]。"""
    root = root or trace_root()
    if not os.path.isdir(root):
        return []
    runs = []
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if not os.path.isdir(d) or not os.path.exists(os.path.join(d, "trace.jsonl")):
            continue
        man = {}
        try:
            with open(os.path.join(d, "meta.json"), "r", encoding="utf-8") as f:
                man = json.load(f)
        except (OSError, ValueError):
            pass
        runs.append({"run_id": name, "dir": d, "manifest": man,
                     "mtime": os.path.getmtime(os.path.join(d, "trace.jsonl"))})
    runs.sort(key=lambda r: r.get("mtime") or 0, reverse=True)
    return runs


# ── 结构自检 ───────────────────────────────────────────────────────
_REQUIRED_FIELDS = ("seq", "ts", "type", "turn", "tool_round", "request_index")

# request_index 允许缺省的顺序型事件（运行级、非请求级）
_ORDER_EXEMPT = {"run_start", "run_end", "limits", "checkpoint", "override", "compaction"}


def validate_events(events, strict=False) -> list:
    """结构自检，返回问题列表（空 = 没问题）。

    检查项：
    - 每个事件都有必需字段（缺失 → 回放无法对齐轮次）
    - seq 严格递增且从 1 开始（追加写 + 唯一写者应保证）
    - request_index 单调不减
    - 每条 llm_request 之后、下一条 llm_request 之前应出现对应的 llm_end
    - 出现的 notice:*/tool_error 的 code 字段是否合法（有 code 时）
    严格档（strict=True）额外要求：任何 level>=1 事件都必须在场。
    """
    problems = []
    if not events:
        return ["trace 为空"]
    if events[0].get("type") != "run_start":
        problems.append(f"第一条事件应为 run_start，实际是 {events[0].get('type')!r}")
    last_seq = 0
    last_req = -1
    pending_req = None
    for i, e in enumerate(events):
        for f in _REQUIRED_FIELDS:
            if f not in e:
                problems.append(f"#{i} seq={e.get('seq')} 缺字段 {f}")
        seq = e.get("seq")
        t = e.get("type")
        if isinstance(seq, int):
            if seq != last_seq + 1:
                problems.append(f"seq 不连续：{last_seq} → {seq}")
            last_seq = seq
        req = e.get("request_index")
        if isinstance(req, int) and t not in _ORDER_EXEMPT:
            # request_index 在**全 trace 内**只增（由 writer.begin_request 分配，
            # turn / tool_round 清零都不影响它）。回合级与运行级事件（turn_end /
            # run_end / checkpoint / limits …）不是"某次请求"，不参与这个单调性判定。
            if t == "turn_start":
                last_req = -1
            elif req < last_req:
                problems.append(f"seq={seq} request_index 回退：{last_req} → {req}")
            last_req = req
        if t == "turn_end":
            # reason 是闭集：回放按它选断言，出现没见过的值说明是**两头不一致**
            # （改了 runner 没改词表），必须当场报出来而不是等到回放时莫名失败。
            r = e.get("reason")
            if r not in TURN_END_REASONS:
                problems.append(f"seq={seq} turn_end.reason 不在词表：{r!r}")
        if t == "notice":
            try:
                from .runner import NOTICE_CODES
                vocab = {v[1] for v in NOTICE_CODES.values()}
            except Exception:  # noqa: BLE001 —— 词表拿不到就不判，别让自检自己崩
                vocab = set()
            c = e.get("code")
            if vocab and c not in vocab:
                problems.append(f"seq={seq} notice.code 不在词表：{c!r}")
        if t == "llm_request":
            if pending_req is not None:
                problems.append(f"seq={pending_req} 的 llm_request 没有对应 llm_end")
            pending_req = seq
        elif t == "llm_end":
            pending_req = None
        elif t not in _ORDER_EXEMPT and t not in ("run_start", "run_end"):
            if pending_req is not None and t in ("tool_call", "tool_result", "turn_end"):
                problems.append(f"seq={seq} 的 {t} 出现在 llm_end 之前（seq={pending_req}）")
    if pending_req is not None:
        problems.append(f"结尾的 llm_request(seq={pending_req}) 没有 llm_end")
    if events[-1].get("type") != "run_end" and not any(
            e.get("type") == "run_end" for e in events[-3:]):
        problems.append("末尾没有 run_end（运行可能非正常结束）")
    if strict:
        for i, e in enumerate(events):
            if e.get("type") == "llm_end" and "finish_reason" not in e:
                problems.append(f"#{i} 严格档下 llm_end 缺 finish_reason")
    return problems


def load_manifest(run_dir) -> dict:
    p = os.path.join(run_dir, "meta.json")
    if not os.path.exists(p):
        raise TraceError(f"manifest 不存在：{p}")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def format_exception(exc) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]
