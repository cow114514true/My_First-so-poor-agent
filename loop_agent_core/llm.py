"""LLM 出口归一：所有大模型调用（对话 / 摘要 / 视觉）都从这里走。

为什么要归一（TRACE_REPLAY_BUILD.md 阶段 0）：
1. 回放引擎只需要替换**一处** transport，就能接管全部模型调用；如果调用散落在
   runner / compaction / vision 各处各自建 OpenAI client，回放就得逐处打补丁，
   漏一处就变成"回放时偷偷真调模型"，结论立刻失效。
2. trace 需要在**请求真正发出的那一刻**记下 model/messages/tools/params，
   而不是靠调用方自觉上报——记在网关里才不会有遗漏。

对外两组入口：
- `get()`  → LLM 对象。`llm.create(kind="chat", **kwargs)` 发一次请求。
- `get().chat.completions.create(**kwargs)` → `runner.client` 的兼容外观，
  让运行循环把它当 OpenAI client 用（也便于测试整体替换 transport）。

transport 是唯一需要被回放替换的层：`set_transport()`。
"""
from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

# kind → (base_url 环境变量, key 环境变量, model 环境变量, 默认 model)
# 三类出口各自独立配置：摘要用小模型、视觉用多模态模型、对话用主模型。
_PROFILES = {
    "chat":    ("DS_BASE_URL", "DS_KEY", "DS_MODEL", "deepseek-chat"),
    "summary": ("SUM_BASE_URL", "SUM_KEY", "SUM_MODEL", ""),
    "vision":  ("VIS_BASE_URL", "VIS_KEY", "VIS_MODEL", ""),
}

VALID_KINDS = tuple(_PROFILES)

_lock = threading.RLock()
# `_transport` 是**全局覆盖**（录制/回放引擎装的那一个），`_default_transport` 才是不装
# 覆盖时惰性构建的真网关。两者必须分开存：从前 `_get_transport()` 会把默认实现写回
# `_transport`，于是"有没有装过覆盖"再也分不出来——回放装了 ReplayTransport 之后，
# 只要有一次默认构建发生，后面的调用就会悄悄走到真网关上，回放打成线上。
_transport = None
_default_transport = None
_instances: dict = {}


class LLMError(RuntimeError):
    """网关自身的问题（kind 非法、缺凭据等），与模型返回的错误区分开。"""


def _env(name, default=None):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v


def profile(kind: str) -> dict:
    """某一类出口的生效配置（key 一律不回显明文）。"""
    if kind not in _PROFILES:
        raise LLMError(f"未知的 LLM 出口类别：{kind!r}（应为 {VALID_KINDS}）")
    base_env, key_env, model_env, default_model = _PROFILES[kind]
    base = _env(base_env) or _env("DS_BASE_URL")
    key = _env(key_env) or _env("DS_KEY")
    model = _env(model_env) or default_model
    if kind in ("summary", "vision") and not _env(model_env):
        # 未单独配置就回落主模型：保留旧行为（compaction/vision 原先就这样）
        model = _env("DS_MODEL") or default_model
    return {"kind": kind, "base_url": base, "api_key": key, "model": model,
            "has_key": bool(key), "model_env": model_env, "base_env": base_env}


def set_transport(transport):
    """安装**全局覆盖** transport（回放引擎在此接管）。返回旧值，便于还原。"""
    global _transport
    with _lock:
        old = _transport
        _transport = transport
    return old


def get_transport():
    """当前安装的覆盖 transport（没有装过就是 None，不代表"真网关不可用"）。"""
    return _transport


def _note_error(kind, exc):
    """模块级错误留痕：传输层（含 headroom 包裹层）在 LLM 之外也要能记。"""
    try:
        from . import trace
        trace.emit_event({"type": "error", "kind": kind, "phase": "transport",
                          "exception_id": trace.exception_id(exc),
                          "exc_type": type(exc).__name__,
                          "func": trace.exception_site(exc),
                          "error": f"{type(exc).__name__}: {str(exc)[:500]}"},
                         level_hint=trace.LEVEL_CORE)
    except Exception:  # noqa: BLE001 —— 记录失败不能反过来弄坏调用
        pass


class _HeadroomTransport:
    """headroom 包裹层：套在**传输层**外面（§2「包裹对象为 transport」/§3.2）。

    为什么套在传输层而不是套客户端：`HeadroomClient` 的 `.chat.completions.create`
    是挂在**实例**上的（`__init__` 里 `self.chat = type("Chat", ...)()`），`dir()`
    看不到它，`isinstance` 也认不出；把它当 OpenAI 客户端塞进 `llm` 网关的客户端位，
    一旦 headroom 版本换了实例属性名，失败方式是**静默退回未包裹**而不是报错。
    走传输层就只有一个接缝：这里的 `create(kind, cfg, kwargs)`。

    用它**公开**的 `chat.completions.create`（而不是私有的 `_create`）：公开面是
    版本升级承诺保持的，私有面不是。任何异常都退回内层传输——包裹层负责优化与观测，
    它挂掉不该让 agent 停止说话。
    """

    def __init__(self, inner, layer):
        self._inner = inner
        self._layer = layer

    def create(self, kind, cfg, kwargs):
        if kind != "chat" or self._layer is None:
            return self._inner.create(kind, cfg, kwargs)
        kw = dict(kwargs)
        model = kw.pop("model", None) or cfg.get("model")
        messages = kw.pop("messages", None) or []
        try:
            return self._layer.chat.completions.create(
                model=model, messages=messages, **kw)
        except Exception as exc:  # noqa: BLE001 —— 观测层故障不得升级成"对话失败"
            _note_error("headroom", exc)
            return self._inner.create(kind, cfg, kwargs)


def _wrap_with_headroom(inner):
    """包一层 headroom（只包 chat）。装不上或没启用就原样返回。"""
    if os.environ.get("HEADROOM_ENABLED", "1") != "1":
        return inner
    try:
        from .config import headroom_layer
        layer = headroom_layer()
    except Exception:  # noqa: BLE001
        return inner
    if layer is None:
        return inner
    return _HeadroomTransport(inner, layer)


def _build_default_transport():
    """默认 transport：真·OpenAI SDK；惰性导入，测试环境无 key 也不炸。"""
    from openai import OpenAI

    class _DefaultTransport:
        def __init__(self):
            self._clients = {}

        def client_for(self, kind, cfg):
            key = (kind, cfg.get("base_url") or "")
            if key not in self._clients:
                if not cfg.get("api_key"):
                    raise LLMError(
                        f"缺少 {kind} 出口的凭据（环境变量 {_PROFILES[kind][1]} 或 DS_KEY）")
                kwargs = {"api_key": cfg["api_key"]}
                if cfg.get("base_url"):
                    kwargs["base_url"] = cfg["base_url"]
                self._clients[key] = OpenAI(**kwargs)
            return self._clients[key]

        def create(self, kind, cfg, kwargs):
            return self.client_for(kind, cfg).chat.completions.create(**kwargs)

    # headroom 包裹层套在**传输层**外（§2）：只包 chat，summary/vision 走各自后端，
    # 用主后端的上下文限制去包它们会把 A 模型的预算套到 B 模型上。
    return _wrap_with_headroom(_DefaultTransport())


def _get_transport():
    """默认 transport（真网关），惰性构建并缓存——绝**不**写进 `_transport` 覆盖位。"""
    global _default_transport
    with _lock:
        if _default_transport is None:
            _default_transport = _build_default_transport()
        return _default_transport


def is_retryable_param_error(exc) -> bool:
    """是否是"参数不被支持"这类可去参重试的 400（如 thinking）。"""
    text = str(exc)
    code = getattr(exc, "status_code", None)
    if code != 400 and "BadRequest" not in type(exc).__name__ and "400" not in text:
        return False
    lowered = text.lower()
    return any(t in lowered for t in ("thinking", "unknown parameter", "unrecognized",
                                     "unsupported", "invalid_request_error"))


RETRY_STRIP_PARAMS = ("thinking", "reasoning_effort")


def _usage_dict(usage):
    """把各家形态不一的 usage 统一成 dict（DeepSeek / OpenAI 兼容 / 已是 dict）。"""
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    out = {}
    for f in ("prompt_tokens", "completion_tokens", "total_tokens"):
        v = getattr(usage, f, None)
        if v is not None:
            out[f] = v
    for f in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cached_tokens"):
        v = getattr(usage, f, None)
        if v is not None:
            out[f] = v
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", None)
        if cached is None and isinstance(details, dict):
            cached = details.get("cached_tokens")
        if cached is not None:
            out["cached_tokens"] = cached
    return out or None


def _response_payload(resp) -> dict:
    """非流式响应的**内容**（流式走 llm_chunk，这里只有非流式需要）。

    只记形状（finish_reason/usage）对回放是不够的：summary / vision 出口都是
    `stream=False`，回放它们时必须能把当时的正文重新吐出来，否则 cassette 里
    "LLM 记录完整"是假的。
    """
    out: dict = {}
    try:
        ch = resp.choices[0]
    except (AttributeError, IndexError, TypeError):
        return out
    msg = getattr(ch, "message", None)
    if msg is None:
        return out
    for key in ("content", "reasoning_content"):
        v = getattr(msg, key, None)
        if v:
            out[key] = v
    tcs = getattr(msg, "tool_calls", None)
    if tcs:
        out["tool_calls"] = [
            {"index": getattr(t, "index", None), "id": getattr(t, "id", None),
             "name": getattr(getattr(t, "function", None), "name", None),
             "arguments": getattr(getattr(t, "function", None), "arguments", None)}
            for t in tcs]
    return out


def _chunk_payload(chunk) -> dict:
    """把一个流式 chunk 摊平成可回放的字典。

    只记"形状"（index/finish_reason）是不够的：回放时必须把**同样的 chunk** 重新吐回
    runner，否则正文和工具调用分片就丢了。所以要连 content / reasoning_content /
    tool_calls 分片一起记；choices 为空（尾块的 usage 包）也要显式记下来，回放才知道
    该吐一个 choices=[] 的 chunk。
    """
    out = {"choices_empty": not getattr(chunk, "choices", None)}
    try:
        ch = chunk.choices[0]
    except (AttributeError, IndexError, TypeError):
        return out
    try:
        out["finish_reason"] = getattr(ch, "finish_reason", None)
    except (AttributeError, TypeError):
        pass
    delta = getattr(ch, "delta", None) or getattr(ch, "message", None)
    if delta is not None:
        if getattr(delta, "content", None):
            out["content"] = delta.content
        if getattr(delta, "reasoning_content", None):
            out["reasoning_content"] = delta.reasoning_content
        tcs = getattr(delta, "tool_calls", None)
        if tcs:
            out["tool_calls"] = [
                {"index": getattr(t, "index", None), "id": getattr(t, "id", None),
                 "name": getattr(getattr(t, "function", None), "name", None),
                 "arguments": getattr(getattr(t, "function", None), "arguments", None)}
                for t in tcs]
    return out


class _TracedStream:
    """流式响应包装：逐 chunk 记 trace（严格档），并在流结束时补 llm_end。

    为什么包在这里而不是让 runner 自己记：runner 有**三条**流式出口
    （主循环 / 收尾轮 / 工具轮），任何一处忘了记，回放就少一段事实。
    包在网关里，出口只有一处。
    """

    def __init__(self, inner, kind, ctx_token=None, request_index=None):
        self._inner = inner
        self._kind = kind
        self._ctx_token = ctx_token
        self._request_index = request_index
        self._chunks = 0
        self._usage = None
        self._finish = None
        self._closed = False
        self._exhausted = False
        self._t0 = time.time()
        self._think_chars = 0
        self._resp_chars = 0
        self._emitted_chars = 0
        self._first_token_ms = None

    @property
    def _stream_summary(self) -> dict:
        return {"kind": self._kind,
                "chunk_count": self._chunks,
                "thinking_chars": self._think_chars,
                "response_chars": self._resp_chars,
                "emitted_response_chars": self._emitted_chars,
                "first_token_ms": self._first_token_ms,
                "latency_ms": int((time.time() - self._t0) * 1000),
                "finish_reason": self._finish,
                "interrupted": not self._exhausted}

    def __iter__(self):
        return self

    def __next__(self):
        from . import trace
        try:
            chunk = next(self._inner)
        except StopIteration:
            self._exhausted = True
            self._finish_stream()
            raise
        except Exception as exc:
            trace.emit_event({"type": "error", "kind": self._kind, "phase": "llm_stream",
                              "exception_id": trace.exception_id(exc),
                              "exc_type": type(exc).__name__,
                              "func": trace.exception_site(exc),
                              "error": f"{type(exc).__name__}: {str(exc)[:500]}"},
                             level_hint=trace.LEVEL_CORE)
            raise
        self._chunks += 1
        if self._first_token_ms is None:
            self._first_token_ms = int((time.time() - self._t0) * 1000)
        u = _usage_dict(getattr(chunk, "usage", None))
        if u:
            self._usage = u
            # 不再单独发 llm_usage：usage 已经带在 llm_chunk / llm_end 上（§4.3 的事件表
            # 是**闭集**，多出来的事件名会让回放器与断言面各自维护一套口径）。
        try:
            if chunk.choices:
                r = getattr(chunk.choices[0], "finish_reason", None)
                if r:
                    self._finish = r
        except (AttributeError, IndexError, TypeError):
            pass
        payload = _chunk_payload(chunk)
        if payload.get("reasoning_content"):
            self._think_chars += len(payload["reasoning_content"])
        if payload.get("content"):
            self._resp_chars += len(payload["content"])
        # llm_chunk 是**录制记录**（§4.3）：回放要靠它重放同一个 chunk，任何档位都写。
        # `chunk_seq` 是 §4.3 说的 chunk 内序号；事件顶层的 `seq` 是写入器的全局行号
        # （trace.idx.json 靠它做 O(1) 跳转），两者不能共用一个字段名。
        trace.emit_record({"type": "llm_chunk", "kind": self._kind,
                           "index": self._chunks,
                           "chunk_seq": self._chunks,
                           "request_index": self._request_index,
                           "finish_reason": self._finish,
                           "usage": u,
                           **payload})
        # 人看的逐字视图另走 stream 档：默认档下上万行 chunk 没人读，只会把 trace 撑爆。
        if payload.get("reasoning_content"):
            trace.emit_event({"type": "thinking_chunk", "kind": self._kind,
                              "index": self._chunks,
                              "chunk": payload["reasoning_content"]},
                             level_hint=trace.LEVEL_STREAM)
        if payload.get("content"):
            trace.emit_event({"type": "response_chunk", "kind": self._kind,
                              "index": self._chunks, "chunk": payload["content"]},
                             level_hint=trace.LEVEL_STREAM)
        return chunk

    def note_emitted(self, n: int) -> None:
        """runner 真正**吐给用户**的正文长度（与模型产出长度区分开：可能被过滤/截断）。"""
        self._emitted_chars += int(n or 0)

    def _finish_stream(self):
        from . import trace
        if self._closed:
            return
        self._closed = True
        summary = self._stream_summary
        trace.emit_record({"type": "llm_end", "kind": self._kind,
                           "request_index": self._request_index,
                           "finish_reason": self._finish,
                           "chunk_count": self._chunks,
                           "usage": self._usage,
                           "latency_ms": summary["latency_ms"],
                           "interrupted": summary["interrupted"]})
        trace.emit_event({"type": "stream_summary", "request_index": self._request_index,
                          **summary}, level_hint=trace.LEVEL_SUMMARY)
        if self._ctx_token is not None:
            trace.reset_context(self._ctx_token)

    def close(self):
        inner = getattr(self._inner, "close", None)
        if callable(inner):
            try:
                inner()
            except Exception:
                pass
        self._finish_stream()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __getattr__(self, name):  # response.model 之类的透传
        return getattr(self._inner, name)



class LLM:
    """单一出口。每次 create 都过 transport，并在发出前记 trace 请求事件。"""

    def __init__(self, transport=None):
        self._transport = transport
        self._last_request_index = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._compat_create))

    # ── 内部：选 transport + 记录请求 ─────────────────────────
    def _transport_for(self):
        """选 transport：**全局覆盖优先于实例自带**。

        覆盖必须能压过实例 transport，否则回放只接管得了 runner 的对话出口，
        而摘要（`loop_agent_v2._SummaryTransport`）和视觉（`vision._CfgTransport`）
        各有自己的实例 transport——它们会在回放中真的打到线上，回放结论立刻作废。
        """
        override = get_transport()
        if override is not None:
            return override
        if self._transport is not None:
            return self._transport
        return _get_transport()

    def _emit_request(self, kind, cfg, kwargs):
        """记录**发出前**的请求事实，并分配/绑定 request_index（§7.2）。

        请求事件必须在同一轮里携带 request_index：chunk / usage / response 都靠它
        归到同一次调用上；如果让写出后才补序号，流式 chunk 会挂到下一个请求上。

        request_index 由 writer **分配**（全 trace 只增），不能取自 ctx：
        `begin_turn` / `set_tool_round` 会把 ctx 里的值清零，取 ctx 会让每个工具轮的
        第一个请求都拿到 0——checkpoint 按 request_index 挂接，于是全部撞在 0 上。
        """
        from . import trace
        w = trace.current_writer()
        ctx = trace.current_context()
        if w is not None:
            idx = w.begin_request(kind, kwargs.get("model") or cfg.get("model"))
        else:
            idx = ctx.request_index
        self._last_request_index = idx
        trace.emit_record({
            "type": "llm_request",
            "request_index": idx,
            "kind": kind,
            "model": kwargs.get("model") or cfg.get("model"),
            # tools 的**内容**不落盘（每次请求 2–4KB，且它在仓库里、跨机器一致）：指纹只需要
            # 一个能判断"是不是同一套工具"的摘要；完整 schema 由 tools_sha256 唯一确定。
            "params": {k: v for k, v in kwargs.items()
                       if k not in ("messages", "model", "tools")},
            "messages": kwargs.get("messages"),
            "tools_count": len(kwargs.get("tools") or []),
            "tools_sha256": trace.tools_sha256(kwargs.get("tools")),
            "stream": bool(kwargs.get("stream")),
        })
        # 请求发出后的 chunk / usage / response 都归到这个号上；下一请求由
        # begin_request 重新分配，不靠 ctx 自增。
        ctx = ctx.evolve(request_index=idx)
        trace.set_context(ctx)
        return ctx

    def _note_response(self, kind, resp, latency_ms=None, request_index=None):
        """记录一次非流式调用的结果形状（内容由调用方自行发射时会重复，这里只记元数据）。"""
        from . import trace
        reason = None
        usage = None
        try:
            ch = resp.choices[0]
            reason = getattr(ch, "finish_reason", None)
        except (AttributeError, IndexError, TypeError):
            pass
        usage = getattr(resp, "usage", None)
        trace.emit_record({"type": "llm_end", "kind": kind,
                           "request_index": request_index,
                           "finish_reason": reason,
                           "usage": _usage_dict(usage),
                           "latency_ms": latency_ms,
                           "interrupted": False,
                           # 非流式没有 llm_chunk，正文只能记在这里（回放要用）
                           "message": _response_payload(resp)})

    def create(self, kind="chat", **kwargs):
        """发一次请求。kind ∈ chat|summary|vision；kwargs 原样传给 OpenAI SDK。"""
        if kind not in _PROFILES:
            raise LLMError(f"未知的 LLM 出口类别：{kind!r}（应为 {VALID_KINDS}）")
        cfg = profile(kind)
        if not kwargs.get("model"):
            kwargs["model"] = cfg.get("model")
        kwargs.setdefault("stream", True)
        transport = self._transport_for()
        t0 = time.time()
        self._emit_request(kind, cfg, kwargs)
        try:
            resp = transport.create(kind, cfg, kwargs)
        except Exception as exc:  # 参数不支持 → 去参重试一次
            if not is_retryable_param_error(exc):
                self._note_failure(kind, exc)
                self._note_error(kind, exc)
                raise
            stripped = {k: v for k, v in kwargs.items() if k not in RETRY_STRIP_PARAMS}
            if stripped == kwargs:
                self._note_failure(kind, exc)
                self._note_error(kind, exc)
                raise
            from . import trace
            trace.emit_event({"type": "llm_param_retry", "kind": kind,
                              "dropped": [p for p in RETRY_STRIP_PARAMS if p in kwargs],
                              "error": str(exc)[:500]}, level_hint=trace.LEVEL_CORE)
            self._emit_request(kind, cfg, stripped)
            try:
                resp = transport.create(kind, cfg, stripped)
            except Exception as exc2:
                self._note_failure(kind, exc2)
                self._note_error(kind, exc2)
                raise
        if not kwargs.get("stream"):
            self._note_response(kind, resp, latency_ms=int((time.time() - t0) * 1000),
                                request_index=self._last_request_index)
            return resp
        from . import trace
        if trace.current_writer() is None:
            return resp
        return _TracedStream(resp, kind, request_index=trace.current_context().request_index)

    def _note_error(self, kind, exc):
        from . import trace
        trace.emit_event({"type": "error", "kind": kind, "phase": "llm_request",
                          "exception_id": trace.exception_id(exc),
                          "exc_type": type(exc).__name__,
                          "func": trace.exception_site(exc),
                          "error": f"{type(exc).__name__}: {str(exc)[:500]}"},
                         level_hint=trace.LEVEL_CORE)

    def _note_failure(self, kind, exc):
        """失败的调用也要闭合 llm_request → llm_end。

        没有这条，失败录像里每一条 llm_request 都缺对端，`trace.validate_events` 会把
        整份现场判成不自洽——而失败现场恰恰是这套设施最贵的产物（§4.10 失败保留）。
        与 `_note_response` 的区别是带 `error` 且没有正文：回放宁可按"这次调用没有结果"
        还原，也不该去猜一个不存在响应。
        """
        from . import trace
        trace.emit_record({"type": "llm_end", "kind": kind,
                           "request_index": self._last_request_index,
                           "finish_reason": None,
                           "usage": None,
                           "chunk_count": 0,
                           "latency_ms": None,
                           "interrupted": False,
                           "error": f"{type(exc).__name__}: {str(exc)[:500]}"})

    # ── 兼容外观：当成 OpenAI client 用 ──────────────────────
    def _compat_create(self, **kwargs):
        return self.create(kind=kwargs.pop("_kind", "chat"), **kwargs)


def get(transport=None) -> LLM:
    """取（并缓存）LLM 单例；传 transport 则返回一个绑定该 transport 的新实例。"""
    if transport is not None:
        return LLM(transport)
    with _lock:
        if "default" not in _instances:
            _instances["default"] = LLM()
        return _instances["default"]


def reset():
    """清空缓存实例、全局覆盖与惰性默认 transport（测试用）。"""
    global _transport, _default_transport
    with _lock:
        _transport = None
        _default_transport = None
        _instances.clear()
