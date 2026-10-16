"""L1 回放（施工文档 §6）。

回放要回答的问题只有一个：**"把这次运行重放一遍，那个失败还会不会出现"**。为了让它成为
证据而不是表演，这个模块做了三件事：

1. **请求匹配**（§6.1/§6.3）：给每个现场请求找一个录制请求，靠指纹（canon 归一后的
   sha256）而不是位置。位置在真实会话里根本不稳——一次重试、一个被跳过的心跳都会让
   序列整体错位，而错位后的"回放"仍然能跑完，只是证明了别的东西。
2. **差异归因**（§6.2/§6.5）：指纹不相等时，把差异**逐槽位**解释清楚：能重注入的
   （时间、记忆、nudge、裁剪提示、工作区路径）说明为什么不等，能耗时的（墙钟耗时）
   ​记进报告放行，剩下无法归类的一律失败——"说不清的差异"就是回归。
3. **工具与预算闭环**（§4.5）：工具结果按录制回吐，并用**当时记下的 limits** 重算
   `truncate(result_full)`，与录制的 `result_stored` 比对；预算被改过就点名旋钮，
   没有预算记录就拒绝归因（`Unattributable`），不许含糊过去。

不在本阶段范围：L2 overlay 的真实工具执行（§8）、L3 最小化（§7）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from types import SimpleNamespace

from . import cassette as cassette_mod
from . import trace as trace_mod
from .cassette import Cassette, PlaybackModeMismatch  # noqa: F401 —— 对外一并导出
from .toolclass import class_of, path_args_of, UnclassifiedToolError

POSITIONAL_BANNER = "按位回放（跳过请求指纹比对，仅保证响应序列）"
DEFAULT_WINDOW = 8


# ── 错误 ────────────────────────────────────────────────────────────
class ReplayError(RuntimeError):
    """回放相关错误的基类。"""


class NoMatchInWindow(ReplayError):
    """滑窗内找不到任何匹配的录制请求（两侧 diff 会附在消息里）。"""


class UnclassifiedDiff(ReplayError):
    """存在无法归类到 §6.2 槽位的差异：strict 模式下直接失败。"""


class ReinjectNotReproducible(ReplayError):
    """reinject 槽位的两侧取值不同，说明这个槽位在本次回放里复现不了。"""


class TruncationBudgetChanged(ReplayError):
    """`result_stored` 对不上，且当时的截断预算与现在不同——点名旋钮。"""


class Unattributable(ReplayError):
    """对不上，且录制里没有 limits 记录，无法判断差异从哪来。"""


class ReplayToolMissing(ReplayError):
    """回放时遇到录制里没有的工具调用结果。"""


class LiveCall(SimpleNamespace):
    """一次"回放时真执行"的工具调用（§8.3 的 B 类 / opt_in 的 C 类）。

    `kind` 是**唯一的判定口径**：`record` / `stub` / `injected_date` 都是"吐内容"，
    `live` 是"真执行"。分类（A/B/C）只解释"为什么这么判"，不参与判断。

    `run_arguments` 是**回放中真正送给工具**的那份入参 JSON（overlay 重写后）；`arguments_raw`
    式的原始逻辑入参留在 `args` 里。两份并存的理由：trace 的 `tool_call` 事件在**执行之前**
    就发出去（它记的是"模型发起了什么调用"），所以重写必须发生在那之前，那份证据才有地方挂。
    """

    def __init__(self, *, name, call_id, cls, kind, args=None):
        super().__init__(name=name, call_id=call_id, cls=cls, kind=kind, args=dict(args or {}),
                         rewrite=None, run_arguments=None)


# ── §6.4 路径归一化 ─────────────────────────────────────────────────
class PathRoots:
    """一组 (前缀, 记号) 映射，替换时**最长前缀优先**。

    最长前缀优先不是洁癖：`_PROFILES_DIR` 常常就在 `WORK_DIR` 之下，先替换短的那个
    会把长路径拆成 `$WORKSPACE/../profiles/...` 这种两个签出都能算出来但含义不同的串。
    """

    def __init__(self, pairs=None):
        self.pairs = []
        for prefix, token in (pairs or []):
            if prefix:
                self.pairs.append((os.path.abspath(str(prefix)), token))
        self.pairs.sort(key=lambda p: len(p[0]), reverse=True)

    def add(self, prefix, token):
        if prefix:
            self.pairs.append((os.path.abspath(str(prefix)), token))
            self.pairs.sort(key=lambda p: len(p[0]), reverse=True)

    def rewrite(self, text):
        if not isinstance(text, str) or not text:
            return text
        out = text
        for prefix, token in self.pairs:
            if prefix in out:
                out = out.replace(prefix, token)
            alt = prefix.replace("\\", "/")
            if alt != prefix and alt in out:
                out = out.replace(alt, token)
        return out

    def rewrite_obj(self, obj):
        """递归重写（dict / list / str）。只改字符串叶子，不动结构。"""
        if isinstance(obj, str):
            return self.rewrite(obj)
        if isinstance(obj, dict):
            return {k: self.rewrite_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.rewrite_obj(v) for v in obj]
        return obj


def record_roots(workspace_root: str | None = None) -> PathRoots:
    """录制侧根表：`WORK_DIR → $WORKSPACE`、`_PROFILES_DIR → $PROFILES`（§6.4）。"""
    from .config import WORK_DIR, _PROFILES_DIR
    roots = PathRoots()
    roots.add(workspace_root or WORK_DIR, "$WORKSPACE")
    try:
        roots.add(_PROFILES_DIR, "$PROFILES")
    except Exception:  # noqa: BLE001
        pass
    return roots


def replay_roots(overlay_root: str | None = None) -> PathRoots:
    """回放侧根表：`overlay_root → $WORKSPACE`（L1 没有 overlay 时退化成 WORK_DIR）。"""
    from .config import WORK_DIR
    roots = PathRoots()
    roots.add(overlay_root or WORK_DIR, "$WORKSPACE")
    return roots


# ── §6.2 volatile 槽位 ──────────────────────────────────────────────
# 槽位表是**闭集**：多一个槽位就多一种"差异可以合法消失"的理由，而每一个理由都必须
# 能说清"为什么这次回放复现不了它、以及复现不了是否可接受"。
POLICY_REINJECT = "reinject"   # 值不等 = 复现失败（strict 直接判失败）
POLICY_TOLERATE = "tolerate"   # 值不等 = 记进报告，仍算通过

SLOTS = {
    "ts": POLICY_REINJECT,
    "memory": POLICY_REINJECT,
    "nudge": POLICY_REINJECT,
    "trim_notice": POLICY_REINJECT,
    "workspace_path": POLICY_REINJECT,
    "timing": POLICY_TOLERATE,
}
REINJECT_SLOTS = tuple(k for k, v in SLOTS.items() if v == POLICY_REINJECT)
TOLERATE_SLOTS = tuple(k for k, v in SLOTS.items() if v == POLICY_TOLERATE)

_TS_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b")
_CLOCK_RE = re.compile(r"\b\d{2}:\d{2}:\d{2}\b")
_MS_RE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|毫秒|s\b|秒)\b")
_ID_RE = re.compile(r"\b(?:session|request|message|msg|call)_?id\b\s*[:=]\s*[\w\-]+", re.I)
_MEMORY_MARK = "[长期记忆"
_TRIM_RE = re.compile(r"\[Context trimmed[^\]]*\]")


def _nudge_texts() -> tuple:
    try:
        from .runner import _EMPTY_NUDGE, _WRAPUP_NUDGE
        return tuple(t for t in (_EMPTY_NUDGE, _WRAPUP_NUDGE) if t)
    except Exception:  # noqa: BLE001
        return ()


def _memory_prefix(text: str) -> str | None:
    """取出记忆注入块（`[长期记忆` 开头那一段）。"""
    if not isinstance(text, str) or _MEMORY_MARK not in text:
        return None
    idx = text.find(_MEMORY_MARK)
    return text[idx:]


def _text_slot_values(text: str) -> dict:
    """抽出这段文本里各槽位的取值列表（用于比较"两侧取值是否相同"）。"""
    if not isinstance(text, str):
        return {}
    vals = {"ts": _TS_RE.findall(text) + _CLOCK_RE.findall(text)}
    nudges = []
    for t in _nudge_texts():
        if t and t in text:
            nudges.append(t)
    if nudges:
        vals["nudge"] = nudges
    mem = _memory_prefix(text)
    if mem:
        vals["memory"] = [mem]
    trims = _TRIM_RE.findall(text)
    if trims:
        vals["trim_notice"] = trims
    ms = _MS_RE.findall(text)
    if ms:
        vals["timing"] = ms
    return vals


def normalize_text(text: str, slots, roots: PathRoots | None = None):
    """把指定槽位的取值换成记号（只用于指纹与差异归因，不改送入模型的内容）。"""
    if not isinstance(text, str):
        return text
    out = text
    if roots is not None:
        out = roots.rewrite(out)
    if "ts" in slots:
        out = _TS_RE.sub("$TS", out)
        out = _CLOCK_RE.sub("$TMP", out)
    if "timing" in slots:
        out = _MS_RE.sub("$MS", out)
    if "trim_notice" in slots:
        out = _TRIM_RE.sub("$TRIM_NOTICE", out)
    if "nudge" in slots:
        for t in _nudge_texts():
            if t:
                out = out.replace(t, "$NUDGE")
    if "memory" in slots:
        out = _replace_memory(out)
    if "ids" in slots:
        out = _ID_RE.sub("<id>", out)
    return out


def _replace_memory(text: str) -> str:
    if _MEMORY_MARK not in text:
        return text
    idx = text.find(_MEMORY_MARK)
    # 记忆注入总是"注入块 + \\n\\n + 用户正文"（runner.py:724-728），正文要保留
    tail = text[idx:]
    sep = tail.find("\n\n")
    if sep < 0:
        return text[:idx] + "$MEMORY"
    return text[:idx] + "$MEMORY" + tail[sep:]


def _normalize_obj(obj, slots, roots: PathRoots | None = None):
    if isinstance(obj, str):
        return normalize_text(obj, slots, roots)
    if isinstance(obj, dict):
        return {k: _normalize_obj(v, slots, roots) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalize_obj(v, slots, roots) for v in obj]
    return obj


def _slot_values(msgs) -> dict:
    """收集整段 messages 里每个槽位出现的**取值**（保序，便于报告逐条列出）。"""
    agg: dict = {}
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        for k, v in _text_slot_values(str(m.get("content") or "")).items():
            agg.setdefault(k, []).extend(v)
        for tc in (m.get("tool_calls") or []):
            args = str(((tc or {}).get("function") or {}).get("arguments") or "")
            for k, v in _text_slot_values(args).items():
                agg.setdefault(k, []).extend(v)
    return agg


# ── §6.1 指纹 ───────────────────────────────────────────────────────
# params 只取**语义项**：temperature 变了就是不同请求，而 stream 这种传输细节不是。
PARAM_KEYS = ("temperature", "top_p", "max_tokens", "reasoning_effort")


def _tools_sha256(tools) -> str | None:
    """工具 schema 摘要：与录制侧共用 trace.tools_sha256（分叉则指纹失去意义）。"""
    return trace_mod.tools_sha256(tools)


def _recorded_params(req: dict) -> dict:
    """录制请求的 params + 顶层 tools_sha256 合并成 canon_params 认得的形状。

    录制事件把 `tools_sha256` 放在**事件顶层**（它和 params 平级，人读起来才是"这次
    调用用了哪套工具"），而 canon_params 从 params 里读；不合并的话录制侧算出来的指纹
    永远缺 tools_sha256，于是每个请求都判"对不上"（实测：现场带工具、录制不带）。
    """
    params = dict(req.get("params") or {})
    if req.get("tools_sha256") and not params.get("tools_sha256"):
        params["tools_sha256"] = req["tools_sha256"]
    return params


def canon_params(params: dict | None, tools=None) -> dict:
    params = params or {}
    out = {}
    for k in PARAM_KEYS:
        if k in params and params[k] is not None:
            out[k] = params[k]
    extra = params.get("extra_body") or {}
    thinking = (extra.get("thinking") or {}) if isinstance(extra, dict) else {}
    if isinstance(thinking, dict) and thinking.get("type"):
        out["thinking_type"] = thinking["type"]
    # 录制事件里 tools 只留摘要（tools_sha256），现场侧才拿得到完整 schema：
    # 先看参数里的摘要（录制侧），再退化到现场计算（回放侧），两者算法同源。
    sha = params.get("tools_sha256") or _tools_sha256(tools)
    if sha:
        out["tools_sha256"] = sha
    return out


def _canon_tool_calls(tcs) -> list:
    out = []
    for tc in (tcs or []):
        fn = (tc or {}).get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {"_raw": args}
        out.append({"name": fn.get("name"), "arguments": args})
    # 按 name 排序：工具调用的**顺序**在并发工具轮里不稳定，但集合是同一批
    return sorted(out, key=lambda x: str(x.get("name")))


def canon_messages(messages, roots: PathRoots | None = None) -> list:
    """messages 的 canon 形（§6.1）：role + 规范化 content + tool_calls + tool_call_id。"""
    out = []
    for m in (messages or []):
        if not isinstance(m, dict):
            continue
        item = {"role": m.get("role")}
        content = m.get("content")
        if isinstance(content, str):
            item["content"] = normalize_text(content, set(REINJECT_SLOTS) | {"ids"}, roots)
        elif content is None:
            item["content"] = None
        else:
            item["content"] = _normalize_obj(content, set(REINJECT_SLOTS) | {"ids"}, roots)
        if m.get("tool_calls"):
            item["tool_calls"] = _canon_tool_calls(m["tool_calls"])
        if m.get("tool_call_id"):
            item["tool_call_id"] = m["tool_call_id"]
        if m.get("name"):
            item["name"] = m["name"]
        out.append(item)
    return out


def _fingerprint(core: dict) -> str:
    blob = json.dumps(core, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def loose_fingerprint(kind, model, params, messages, *, roots=None, tools=None) -> str:
    """canon 指纹：能把"同一逻辑请求"在两次运行里对上（§6.1）。"""
    core = {"kind": kind, "model": model, "params": canon_params(params, tools),
            "messages": canon_messages(messages, roots)}
    return _fingerprint(core)


def strict_fingerprint(kind, model, params, messages, *, roots=None, tools=None) -> str:
    """raw 指纹：逐字相等才通过（§6.1），只作预检用——它相等就能跳过差异归因。

    tools 摘要必须**只出现一次**且位置固定：录制侧它被并进 params（`_recorded_params`），
    现场侧只能从 tools 现算。若把两边原样塞进 core，同一套工具会算出两个不同的 raw 指纹，
    预检于是永不命中——strict 每次都退化成"跑一遍结构化 diff"，预检名存实亡。
    """
    p = dict(params or {})
    sha = _tools_sha256(tools) or p.get("tools_sha256")
    p.pop("tools_sha256", None)
    core = {"kind": kind, "model": model,
            "params": _normalize_obj(p, set(), roots),
            "messages": _normalize_obj(messages, set(), roots),
            "tools_sha256": sha}
    return _fingerprint(core)


def request_signature(kind) -> str:
    """请求签名：把调用流按「可互换的响应来源」分桶（§6.3 分桶游标）。

    签名只取 `kind`，不取 model / params / messages：同一个 kind 的调用在真实会话里
    本来就是**同一条响应流**（chat 的多轮、summary 的多次压缩尝试），而不同 kind 的调用
    在录制里会**交错出现**。用全局单一游标时，一次 kind 交错就整体错位——明明每条都有
    对应录制，却因为不在同一个窗口里而报「漂移」。

    model / params / messages 是否真的变了仍由 `loose` 指纹判定，分桶不会放过它们：
    桶只决定「从哪条流里按 FIFO 取」，取到之后照旧逐条比 loose。
    """
    return str(kind or "")


# ── §6.5 差异归因 ───────────────────────────────────────────────────
class DiffExplanation:
    """一次指纹不等的完整解释：哪些槽位解释了差异、值是否相等、剩下什么。"""

    def __init__(self):
        self.explaining: list = []      # 单独归零后能让两侧相等的槽位
        self.unequal_reinject: dict = {}  # 槽位 -> (recorded_vals, live_vals)
        self.tolerated: dict = {}       # 槽位 -> (recorded_vals, live_vals)
        self.unclassified: str | None = None  # 全部槽位归零后仍存在的差异

    @property
    def ok(self) -> bool:
        return self.unclassified is None and not self.unequal_reinject

    def summary(self) -> str:
        parts = []
        if self.explaining:
            parts.append("可解释槽位=" + ",".join(self.explaining))
        if self.unequal_reinject:
            parts.append("reinject 值不等=" + ",".join(sorted(self.unequal_reinject)))
        if self.tolerated:
            parts.append("容忍槽位=" + ",".join(sorted(self.tolerated)))
        if self.unclassified:
            parts.append("未归类差异=" + self.unclassified[:400])
        return "；".join(parts) or "无差异"


def _clip(obj, n: int = 160) -> str:
    """把任意结构压成一行短摘要，供差异报告点名"具体是哪条消息"。"""
    try:
        s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        s = repr(obj)
    return s if len(s) <= n else s[:n] + "…"


def _first_diff(a, b, path="$") -> str:
    """找出两棵结构的第一个差异点（报告里给出"具体哪条消息的哪个字段"）。"""
    if type(a) is not type(b):
        return f"{path}: 类型 {type(a).__name__} != {type(b).__name__}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                return f"{path}.{k}: 只存在于现场侧"
            if k not in b:
                return f"{path}.{k}: 只存在于录制侧"
            if a[k] != b[k]:
                return _first_diff(a[k], b[k], f"{path}.{k}")
        return f"{path}: 相等"
    if isinstance(a, list):
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                return _first_diff(x, y, f"{path}[{i}]")
        if len(a) != len(b):
            # 先看共同前缀（上面已比完），长度不同再指出多出来/少了的那一条**具体是什么**：
            # 只报"长度 3 != 1"会让人以为差异在长度上，而真正要找的是那条多出来的消息。
            longer, side = (b, "现场侧") if len(b) > len(a) else (a, "录制侧")
            return (f"{path}[{min(len(a), len(b))}]: 只在{side}存在："
                    f"{_clip(longer[min(len(a), len(b))])}")
        return f"{path}: 相等"
    sa, sb = str(a), str(b)
    if len(sa) > 120 or len(sb) > 120:
        import difflib
        sm = difflib.SequenceMatcher(None, sb, sa)
        op = next((o for o in sm.get_opcodes() if o[0] != "equal"), None)
        if op:
            _, i1, i2, j1, j2 = op
            return (f"{path}: 录制={sb[max(0, i1 - 20):i2 + 20]!r} 现场={sa[max(0, j1 - 20):j2 + 20]!r}")
    return f"{path}: 录制={sb[:120]!r} 现场={sa[:120]!r}"


def explain_diff(recorded_msgs, live_msgs, *, roots=None, live_roots=None) -> DiffExplanation:
    """按 §6.5 的第 2–5 步解释 messages 的差异。

    两侧各用自己的根表归一化：录制侧消息里是录制那台机器的绝对路径，现场侧是这一台的，
    只拿一把根表去套两边，跨检出回放时路径差异会全被算成"未归类"。

    关键认识：**归一化只会让两侧更相等，不会更不等**。所以"某个槽位单独归一化就相等"这种
    情形根本不存在；能出现的只有两种：
      1) 用全部槽位归一化后相等 → 差异全部由已知槽位造成，通过（值不等本身是正常的：
         日期天天在变、记忆块每轮重注入）。
      2) 归一化后仍不等 → 剩下的是槽位解释不了的差异。这时再看它是不是"槽位结构对不上"
         （例如录制里有 1 处 nudge、现场 0 处）：是 → reinject 槽位不可复现，失败；
         否 → 未归类差异，失败。
    """
    ex = DiffExplanation()
    all_slots = set(SLOTS) | {"ids"}
    rec_n = _normalize_obj(recorded_msgs, all_slots, roots)
    live_n = _normalize_obj(live_msgs, all_slots, live_roots or roots)
    rec_vals = _slot_values(recorded_msgs)
    live_vals = _slot_values(live_msgs)
    # 归一化时真的被替换掉的槽位（两侧原始取值不同）＝这次回放用到了 reinject
    for slot in SLOTS:
        rv, lv = rec_vals.get(slot) or [], live_vals.get(slot) or []
        if rv == lv:
            continue
        if SLOTS[slot] == POLICY_REINJECT:
            ex.explaining.append(slot)
            ex.unequal_reinject[slot] = (rv, lv)
        else:
            ex.tolerated[slot] = (rv, lv)
    if rec_n == live_n:
        # 结构一致：reinject 槽位"值不同但可替换"，属于 §6.5 第 4 步的通过情形
        ex.unequal_reinject = {}
        return ex

    # 结构已经对不上：先判断是不是某个 reinject 槽位的出现次数/位置对不上
    for slot in REINJECT_SLOTS:
        rv, lv = rec_vals.get(slot) or [], live_vals.get(slot) or []
        if len(rv) != len(lv):
            ex.unequal_reinject.setdefault(slot, (rv, lv))
            ex.unclassified = (
                f"槽位 {slot} 不可复现：录制 {len(rv)} 处、现场 {len(lv)} 处")
            return ex
    ex.unclassified = _first_diff(live_n, rec_n)
    if ex.unequal_reinject:
        slots = ",".join(sorted(ex.unequal_reinject))
        ex.unclassified = f"{ex.unclassified}（同时 reinject 槽位 {slots} 取值不等）"
    return ex


# ── 报告 ────────────────────────────────────────────────────────────
class ReplayReport:
    """回放报告：匹配、跳过、容忍、漂移、断言面。测试的通过与否由它决定。"""

    def __init__(self, cassette: Cassette, mode: str, strict: bool = False):
        self.cassette = cassette
        self.mode = mode
        self.strict = strict
        self.matches: list = []       # {"position","request_index","exact"}
        self.skips: list = []         # {"from_position","to_position","recorded_index","live_digest"}
        self.tolerated: list = []
        self.drifts: list = []
        self.tool_notes: list = []
        self.banner: str | None = None
        self.assertion_ids: list = []
        self.expected_assertion: str | None = None
        self.reproduced: bool | None = None
        self.mismatch: str | None = None
        self.overlay = None
        # §6.3 分桶游标：录制里从未被现场消费的请求（代码合法删调用时可见），
        # 由 ReplaySession 退出时从 transport 收上来。
        self.unconsumed: list = []
        # 多轮回放（run_replay(all_turns=True)）：按 turn_start 切出的用户轮。
        # 单轮时为空，报告不打印这一段。
        self.turns: list = []
        self.all_turns = False
        # strict 预检记账：strict_hash 相等的请求直接放行（跳过结构化 diff），
        # 不等的才交给 explain_diff 归类。两个数分开记，才能证明"相等确实跳过了"。
        self.strict_equal = 0
        self.strict_diffed = 0
        self.t0 = time.time()

    def add_match(self, position, request_index, exact):
        self.matches.append({"position": position, "request_index": request_index, "exact": exact})

    def add_skip(self, frm, to, recorded_index, note=""):
        self.skips.append({"from_position": frm, "to_position": to,
                           "recorded_index": recorded_index, "note": note})

    @property
    def skips_count(self) -> int:
        return len(self.skips)

    def finalize(self, live_events) -> "ReplayReport":
        self.assertion_ids = trace_mod.assertion_ids(live_events)
        exp = self.cassette.expected_assertion()
        self.expected_assertion = exp
        if exp:
            self.reproduced = exp in self.assertion_ids
        return self

    def lines(self) -> list:
        out = []
        if self.banner:
            out.append(self.banner)
        if self.mode == "positional":
            # 断言面测试要能一眼读到"这份报告没有做请求指纹比对"：按位回放只保证
            # 响应序列，任何"请求侧也一致"的推论都超出了它的证据范围。
            out.append("未做请求指纹比对：按位消费响应序列，判定依据 cassette_meta.expected_assertion")
        out.append(f"回放模式={self.mode} strict={self.strict} "
                   f"cassette={os.path.basename(self.cassette.dir)}")
        out.append(f"请求匹配 {len(self.matches)} 个 / 录制 {self.cassette.request_count()} 个；"
                   f"跳过 {self.skips_count} 个")
        if self.turns:
            out.append(f"多轮回放 {len(self.turns)} 轮：")
            for t in self.turns:
                q = str(t.get("question") or "")[:40]
                out.append(f"  turn#{t['index']} 请求 {len(t.get('request_indices') or [])} 个"
                           f" 问题={q!r}")
        for s in self.skips:
            out.append(f"  skip {s['from_position']}..{s['to_position']} "
                       f"(request_index={s['recorded_index']}) {s['note']}")
        if self.unconsumed:
            detail = "、".join(f"{u['request_index']}({u.get('kind')})"
                                   for u in self.unconsumed[:6])
            out.append(f"  未消费录制请求 {len(self.unconsumed)} 条：{detail}")
        for t in self.tolerated:
            out.append(f"  容忍槽位 {t['slot']}：录制={str(t['recorded'])[:80]} "
                       f"现场={str(t['live'])[:80]}")
        if self.overlay is not None:
            # B 类真执行是"回放动了工作区"的唯一合法形式，它执行了哪些调用必须进报告：
            # 只看"回放通过"这一个字，读报告的人无法区分"没有副作用"和"副作用被隔离了"。
            out.append(f"overlay {self.overlay.root}")
            out.append(f"  overlay {self.overlay.describe()}")
            for call_id, name in self.overlay.live_calls:
                out.append(f"  真执行 {name}({call_id})")
            for name, logical, _hit in self.overlay.reads_from_overlay:
                out.append(f"  overlay 重读 {name} {logical}")
        for d in self.drifts:
            out.append(f"  漂移 {d}")
        if self.expected_assertion:
            out.append(f"目标断言 {self.expected_assertion} → "
                       f"{'已复现' if self.reproduced else '未复现'}")
        else:
            out.append(f"现场断言面 {len(self.assertion_ids)} 条：{self.assertion_ids[:6]}")
        if self.mismatch:
            out.append(f"失败：{self.mismatch}")
        return out

    def text(self) -> str:
        return "\n".join(self.lines())


# ── 假响应对象 ──────────────────────────────────────────────────────
class _Delta(SimpleNamespace):
    pass


def chunk_from_record(rec: dict):
    """把一条录制的 `llm_chunk` 还原成"看起来像 SDK chunk"的对象。

    形状必须和真 SDK 一致：runner 会读 `chunk.choices[0].delta.content`，也会在
    `choices == []` 的尾块上读 `chunk.usage`——回放器给出别的形状，就等于在回放一条
    现实中不存在的响应。
    """
    usage = rec.get("usage")
    usage_obj = SimpleNamespace(**usage) if isinstance(usage, dict) else usage
    has_payload = any(rec.get(k) is not None for k in
                      ("content", "reasoning_content", "tool_calls", "finish_reason"))
    if rec.get("choices_empty") or not has_payload:
        # usage-only 尾块：真 SDK 就是 choices=[]，runner 也先看 usage 再看 choices
        return SimpleNamespace(choices=[], usage=usage_obj, id=rec.get("id"))
    tcs = []
    for tc in (rec.get("tool_calls") or []):
        tcs.append(SimpleNamespace(
            index=tc.get("index"), id=tc.get("id"),
            function=SimpleNamespace(name=tc.get("name"), arguments=tc.get("arguments"))))
    delta = _Delta(content=rec.get("content"), reasoning_content=rec.get("reasoning_content"),
                   tool_calls=tcs or None, role="assistant")
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=rec.get("finish_reason"))
    return SimpleNamespace(choices=[choice], usage=usage_obj, id=rec.get("id"))


class ReplayStream:
    """一次回放的"响应流"：按录制顺序吐 chunk。

    usage 已经由 `Cassette.chunks_for` 从 llm_end 合并到尾块上——runner 只在尾块读它，
    所以顺序不能改：先内容块，最后才是 usage 帧。
    """

    def __init__(self, chunks: list):
        self._chunks = list(chunks)
        self._i = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._i >= len(self._chunks):
            raise StopIteration
        rec = self._chunks[self._i]
        self._i += 1
        return chunk_from_record(rec)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def response_from_record(end: dict | None, rec: dict | None = None):
    """把一条录制的 `llm_end.message` 还原成非流式响应对象。

    summary / vision 出口都是 `stream=False`：它们的正文只记在 llm_end 里（没有
    llm_chunk）。回放这两个出口时必须给回和录制时同形状的响应，否则回放要么崩，
    要么静悄悄地用一个空正文继续跑，把"复现"变成另一种行为的复现。
    """
    end = end or {}
    msg = dict(end.get("message") or {})
    usage = end.get("usage")
    usage_obj = SimpleNamespace(**usage) if isinstance(usage, dict) else usage
    tcs = []
    for tc in (msg.get("tool_calls") or []):
        tcs.append(SimpleNamespace(
            index=tc.get("index"), id=tc.get("id"),
            function=SimpleNamespace(name=tc.get("name"), arguments=tc.get("arguments"))))
    message = SimpleNamespace(content=msg.get("content"), reasoning_content=msg.get("reasoning_content"),
                              tool_calls=tcs or None, role="assistant")
    choice = SimpleNamespace(index=0, message=message, finish_reason=end.get("finish_reason"))
    return SimpleNamespace(choices=[choice], usage=usage_obj, id=(rec or {}).get("id"))


# ── 回放 transport（§6.6）────────────────────────────────────────────
class ReplayTransport:
    """从 cassette 供给 LLM 响应。

    `mode="fingerprint"`：走 §6.1–6.5 全流程（默认）。
    `mode="positional"`：按位消费，跳过指纹比对——只保证响应序列，不保证请求内容。

    直调时注意：指纹模式要求**调用方带上与录制时同源的 `tools`**。cassette 里只存得下
    `tools_sha256`（整份 schema 每轮 2–4KB，不值当登记版本库），所以现场不传 tools 时
    算出的指纹一定与录制不同、必报 NoMatchInWindow——这不是 bug，而是"工具集变了就不算
    同一段历史"。正常路径（`run_replay` 跑真 runner）会自动带上 tools。
    """

    def __init__(self, cassette_dir, *, mode=None, strict=False, window=None,
                 cassette=None, report=None, check_env=True):
        self.cassette = cassette or Cassette(cassette_dir, mode=mode, require_llm=True,
                                             check_env=check_env)
        self.mode = self.cassette.mode
        self.strict = bool(strict)
        self.window = 0 if self.strict else (DEFAULT_WINDOW if window is None else int(window))
        self.cursor = 0
        self.report = report
        self.roots = record_roots(self.cassette.meta.get("workspace_root"))
        self.live_roots = replay_roots()
        self.calls: list = []
        # §6.3 签名分桶 FIFO 游标：每个 kind 一条独立响应流，各自按录制顺序消费。
        # 全局 cursor 只留给按位模式（positional）；指纹模式的走位由 bucket_at 决定。
        # 桶键只取 kind（request_signature）：混 kind 会话里一次交错就让全局游标整体
        # 错位，而松散指纹本来就保证「只有同 kind 才可能匹配」，分桶不会放过真变化。
        self.buckets: dict = {}      # signature -> [录制位置, ...]（升序）
        self.bucket_at: dict = {}    # signature -> 该桶已消费到的下标
        if self.mode == cassette_mod.PLAYBACK_POSITIONAL:
            if self.report is not None:
                self.report.banner = POSITIONAL_BANNER
        else:
            for pos in range(self.cassette.request_count()):
                rec = self.cassette.request_at(pos)
                self.buckets.setdefault(request_signature(rec.get("kind")), []).append(pos)
            self.bucket_at = {sig: 0 for sig in self.buckets}

    # ── 匹配 ──────────────────────────────────────────────────────
    def _recorded_core(self, req: dict):
        return loose_fingerprint(req.get("kind"), req.get("model"),
                                 _recorded_params(req),
                                 req.get("messages") or [],
                                 roots=self.roots,
                                 tools=req.get("tools"))

    def _match_fingerprint(self, live):
        """在该现场请求的**签名桶**里，从桶游标起按 FIFO 找 loose 命中（§6.3）。

        桶按 request_signature(kind) 分，桶内 FIFO：同 kind 的调用按录制顺序消费，
        不同 kind 在录制里怎么交错、现场又怎么重排，都不会互相错位。窗口 W 的语义
        从「全局后 W 条」变成「本桶后 W 条」——这才让「同一段历史重跑一次都不该跳」
        在混 kind 会话里也成立。返回 (position, exact, rec)。
        """
        sig = request_signature(live.get("kind"))
        bucket = self.buckets.get(sig) or []
        start = self.bucket_at.get(sig, 0)
        hi = min(len(bucket) - 1, start + self.window)
        diffs = []
        for bi in range(start, hi + 1):
            pos = bucket[bi]
            rec = self.cassette.request_at(pos)
            rec_loose = self._recorded_core(rec)
            if rec_loose != live["loose"]:
                diffs.append((pos, rec))
                continue
            exact = True
            if self.strict:
                exact = strict_fingerprint(rec.get("kind"), rec.get("model"),
                                           _recorded_params(rec),
                                           rec.get("messages") or [],
                                           roots=self.roots,
                                           tools=rec.get("tools")) == live["strict"]
                if exact:
                    # §6.5 Step 1：strict_hash 相等 → 什么都不用再比。
                    if self.report is not None:
                        self.report.strict_equal += 1
                else:
                    if self.report is not None:
                        self.report.strict_diffed += 1
                    ex = explain_diff(rec.get("messages") or [], live["messages"],
                                      roots=self.roots, live_roots=self.live_roots)
                    if self.report is not None:
                        for slot, (rv, lv) in ex.tolerated.items():
                            self.report.tolerated.append({"slot": slot, "recorded": rv, "live": lv})
                        for slot, (rv, lv) in ex.unequal_reinject.items():
                            raise ReinjectNotReproducible(
                                f"strict 不适用：{slot} 不可复现（录制={str(rv)[:120]} "
                                f"现场={str(lv)[:120]}）")
                    if ex.unclassified:
                        raise UnclassifiedDiff(
                            f"strict 差异未归类：{ex.unclassified}（录制 {rec.get('request_index')}）")
            if bi > start and self.report is not None:
                # 每个 skip 都要能解释「为什么跳过了这几条」（§9 #10）：只写「前移了」
                # 等于把差异藏起来——而跳过恰恰是最可能掩盖真实不匹配的地方。
                # 给出**被跳过的那条录制请求**与现场的首个分叉 message。
                skipped_pos = bucket[start]
                skipped = self.cassette.request_at(skipped_pos)
                note = _first_diff(
                    canon_messages(skipped.get("messages") or [], self.roots),
                    live["canon_messages"])
                self.report.add_skip(skipped_pos, bucket[bi - 1], rec.get("request_index"),
                                     note=note)
            self.bucket_at[sig] = bi + 1
            return pos, exact, rec
        detail = "\n".join(
            f"  position={p} request_index={r.get('request_index')} "
            f"{_first_diff(live['canon_messages'], canon_messages(r.get('messages') or [], self.roots))}"
            for p, r in diffs[:3])
        raise NoMatchInWindow(
            f"签名 {sig or '<none>'} 的桶 [{start}, {start + self.window}] 内没有匹配的录制请求"
            f"（该 kind 共 {len(bucket)} 条，已消费 {start} 条）。\n"
            f"现场 loose={live['loose'][:16]} model={live['model']} kind={live['kind']}\n"
            + (detail or "  （本桶内没有剩余请求——现场比录制多发了这个 kind 的调用？）"))
    def create(self, kind=None, cfg=None, kwargs=None, **extra):
        """Transport 协议入口。

        llm.py 以 `(kind, cfg, kwargs)` 三参调用；但 §6.6 把 transport 的入口写成
        `create(self, *, kind, **kwargs)`（与 openai 同签名），直接按那种写法调用也得能用，
        否则"按文档写一段回放脚本"会得到一个把 model/messages 全丢掉的静默错误结果。
        """
        if isinstance(kind, str) is False and kwargs is None:
            kind, cfg, kwargs = None, None, None
        if kwargs is None and cfg is None and extra:
            kwargs, extra = extra, {}
        kwargs = dict(kwargs or {})
        if extra and kwargs:
            kwargs.update(extra)
        # tools 不入 params：录制侧就把它折成 tools_sha256 了，现场侧若把整份 schema 塞进
        # params，strict 指纹会**永远**不等——那会让"严格回放"变成"从来没能通过的回放"。
        params = {k: v for k, v in kwargs.items() if k not in ("messages", "model", "tools")}
        live = {
            "kind": kind,
            "model": kwargs.get("model"),
            "messages": kwargs.get("messages") or [],
            "params": params,
            "tools": kwargs.get("tools"),
        }
        live["canon_messages"] = canon_messages(live["messages"], self.live_roots)
        live["loose"] = loose_fingerprint(live["kind"], live["model"], params,
                                         live["messages"], roots=self.live_roots,
                                         tools=live["tools"])
        live["strict"] = strict_fingerprint(live["kind"], live["model"], params,
                                            live["messages"], roots=self.live_roots,
                                            tools=live["tools"])
        self.calls.append({k: v for k, v in live.items() if k != "messages"})

        if self.mode == cassette_mod.PLAYBACK_POSITIONAL:
            pos = self.cursor
            if pos >= self.cassette.request_count():
                raise NoMatchInWindow(
                    f"按位回放已用完 cassette 的 {self.cassette.request_count()} 条请求")
            rec = self.cassette.request_at(pos)
            self.cursor = pos + 1
            if self.report is not None:
                self.report.add_match(pos, rec.get("request_index"), exact=None)
            return self._respond(rec)

        pos, exact, rec = self._match_fingerprint(live)
        self.cursor = pos + 1
        if self.report is not None:
            self.report.add_match(pos, rec.get("request_index"), exact=exact)
        return self._respond(rec)

    def _respond(self, rec: dict):
        """按录制时的形状回吐响应（流式 → 分片流；非流式 → 整条响应对象）。"""
        ri = rec.get("request_index")
        if rec.get("stream") is False:
            return response_from_record(self.cassette.end_for(ri), rec)
        return ReplayStream(self.cassette.chunks_for(ri))

    def finished(self) -> bool:
        if self.mode == cassette_mod.PLAYBACK_POSITIONAL:
            return self.cursor >= self.cassette.request_count()
        # 指纹模式：全部签名桶都消费到桶尾才算跑完（桶内 FIFO）。
        return all(self.bucket_at.get(sig, 0) >= len(b) for sig, b in self.buckets.items())

    def unconsumed(self) -> list:
        """录制里没有被任何现场请求消费的请求（§6.3：代码合法删调用时可见）。

        分桶之后「删掉一个调用」不再让后续整体错位，代价是那条录制的响应**没人取**。
        它必须显式进报告，否则「回放通过」会把「录制有 5 条、现场只用了 3 条」这种
        真实差异掩盖成绿色。
        """
        if self.mode == cassette_mod.PLAYBACK_POSITIONAL:
            rest = list(range(self.cursor, self.cassette.request_count()))
        else:
            rest = []
            for sig, bucket in self.buckets.items():
                rest.extend(bucket[self.bucket_at.get(sig, 0):])
        out = []
        for pos in sorted(rest):
            rec = self.cassette.request_at(pos)
            out.append({"position": pos, "request_index": rec.get("request_index"),
                        "kind": rec.get("kind")})
        return out


# ── §4.5 工具结果的归因 ─────────────────────────────────────────────
def attribute_truncation(result_full: str, result_stored: str, recorded: dict,
                         limits_now: dict | None, limits_recorded: dict | None,
                         report: ReplayReport | None = None) -> str:
    """回放侧重算截断，返回 "ok" / "drift"，异常情况抛错（§4.5）。

    三种结局必须分开：预算被改过（点名旋钮，人能立刻去改回来）、根本没记预算
    （不可归因，谁都说不清）、以及预算没变却对不上（真回归，必须失败）。
    """
    cap_now = ((limits_now or {}).get("knobs") or {}).get("result_cap")
    cap_rec = ((limits_recorded or {}).get("knobs") or {}).get("result_cap")
    trunc = (recorded or {}).get("truncation") or {}
    cap_rec = trunc.get("cap", cap_rec)
    stored_now = result_full
    if cap_now is not None and len(stored_now) > cap_now:
        stored_now = stored_now[:cap_now] + (f"\n...(stored truncated, {len(result_full)} chars "
                                            f"total; re-read via read_file if needed)")
    if stored_now == result_stored:
        return "ok"
    if limits_recorded is None:
        raise Unattributable(
            f"result_stored 对不上且录制里没有 limits 记录（call_id={recorded.get('call_id')}）"
            f"：无法判断差异来自预算变化还是代码回归")
    if cap_now is not None and cap_rec is not None and cap_now != cap_rec:
        raise TruncationBudgetChanged(
            f"截断预算变了：result_cap 录制={cap_rec} 现在={cap_now}"
            f"（call_id={recorded.get('call_id')}）；要么恢复旋钮，要么重录 cassette")
    if report is not None:
        report.drifts.append(
            f"tool_result {recorded.get('call_id')} 截断结果不一致（预算未变：{cap_rec}）")
    return "drift"


# ── 一次回放的全局面 ────────────────────────────────────────────────
class ReplayToolBox:
    """按 §8.3 决定每个工具调用"吐录制结果"还是"在 overlay 里真执行"。

    `policy="cassette"`（默认）：所有工具都回吐录制结果。L1 回放因此完全离线、不改工作区。
    `policy="class"`：A 吐录制、B 真执行、C 吐录制（`exec_c` 显式打开才真跑）。
    真执行会写文件，隔离由 L2 overlay 负责（`overlay.Overlay`）。

    决定结果是**四元组**（`LiveCall`）而不是 `record|None`：`None` 一个值同时意味着
    "B 类要真跑"和"录制里根本没有这个调用"，runner 只能靠猜——猜错的代价是要么漏掉
    真执行，要么拿一个凭空现跑的结果去糊弄"复现"。
    """

    # `policy="class"` 时由 `ReplaySession` 填上（§8.1）；L1 回放保持 None。
    # 放类属性是为了让"这个箱子有没有 overlay"在任何时候都能被安全地读，而不是
    # 靠 `hasattr` 试探一个只在某条分支里才被赋值的实例属性。
    overlay = None

    def __init__(self, cassette: Cassette, *, policy="cassette", exec_c: bool = False,
                 exec_date: bool = False):
        self.cassette = cassette
        self.policy = policy
        self.exec_c = bool(exec_c)
        self.exec_date = bool(exec_date)
        self.served: list = []
        self.live: list = []
        # 真执行调用的账本（§8.3）：L2 的 B 类结果**本来就不等于录制结果**，这份账
        # 是"哪些调用真的跑了、跑出来多大"的唯一记录；不记的话报告里只剩一句"通过了"。
        self.live_detail: list = []

    # ── §8.3 的另一条：日期注入 ─────────────────────────────────
    def injected_date(self) -> str | None:
        """`trace_epoch.injected_date_str`：`get_date` 回放时该返回的那一天。"""
        epoch = (self.cassette.meta or {}).get("trace_epoch") or {}
        return epoch.get("injected_date_str") or None

    def recorded_text(self, call_id) -> str:
        """录制的 `result_full`（缺失时退到 `result_stored`）。"""
        rec = self.cassette.tool_result(call_id) or {}
        full = rec.get("result_full")
        return str(full if full is not None else (rec.get("result_stored") or ""))

    def decision(self, name: str, call_id):
        """这个调用该怎么供给：`LiveCall`（真执行，§8.3 的 B 类）或 **str**（要吐的内容）。

        返回 str 而不是再包一层 dict：runner 那一侧的判定必须是"拿到文本"还是"拿到
        一个要真跑的调用"，中间再加一层 `record|None` 只会让"没录到"和"要真跑"重新
        长回同一个面孔（`None` 曾经就是这个坑）。
        """
        rec = self.cassette.tool_result(call_id)
        try:
            cls = class_of(name)
        except UnclassifiedToolError:
            # 未登记工具 fail-closed：真跑等于在"不知道有没有副作用"的前提下动工作区。
            raise ReplayToolMissing(
                f"回放遇到未登记在 TOOL_CLASS 的工具：{name!r}（call_id={call_id}）。"
                f"请先登记类别 A/B/C 再回放。")
        kind = "record"
        if name == "get_date" and self.exec_date and self.injected_date():
            kind = "injected_date"
        elif name == "delegate_task":
            # §8.3 另一条：子 agent 整体 stub——子树有自己的 trace，父侧只吐录制结果。
            kind = "stub"
        elif self.policy == "class" and cls == "B":
            kind = "live"
        elif self.policy == "class" and cls == "C" and self.exec_c:
            kind = "live"
        if kind == "live":
            return LiveCall(name=name, call_id=call_id, cls=cls, kind=kind)
        if rec is None:
            raise ReplayToolMissing(
                f"回放时遇到没有录制结果的工具调用：{name}({call_id})")
        if kind == "injected_date":
            # §8.3 另一条：日期按录制时的 meta 注入，不是吐 get_date 自己的录制结果——
            # 录制结果里是"当时那一天"，而断言与提示词里引用的是注入日，两者必须同源。
            return str(self.injected_date())
        return self.recorded_text(call_id)

    def result_for(self, name: str, call_id):
        """回放侧取一次工具供给。真执行的调用返回 None（由 runner 去跑）。"""
        d = self.decision(name, call_id)
        self.served.append({"call_id": call_id, "name": name,
                            "class": getattr(d, "cls", None),
                            "kind": getattr(d, "kind", "record")})
        if isinstance(d, LiveCall):
            self.live.append({"call_id": call_id, "name": name, "class": d.cls})
            return None
        return {"call_id": call_id, "name": name,
                "class": self.served[-1]["class"], "kind": self.served[-1]["kind"],
                "result_full": d}

    def wants_live(self, name: str, call_id) -> bool:
        return isinstance(self.decision(name, call_id), LiveCall)


class ReplaySession:
    """一次回放的活动现场：transport + 工具供给 + overlay + 报告 + 事件收集。

    overlay 与工具供给**同生**：`policy="cassette"`（L1）时不建 overlay——那时没有任何
    工具会真跑，建一个空目录副本只会让"回放没碰工作区"这件事变得需要额外证明。
    """

    _active = None

    def __init__(self, cassette_dir, *, mode=None, strict=False, window=None,
                 tool_policy="cassette", check_env=True, run_dir=None,
                 overlay_root=None, overlay_read=False, exec_c=False, keep_overlay=False,
                 exec_date=False):
        from . import overlay as overlay_mod
        self.report = ReplayReport(None, mode or "fingerprint", strict=strict)
        self.cassette = Cassette(cassette_dir, mode=mode, require_llm=True, check_env=check_env)
        self.report.cassette = self.cassette
        self.report.mode = self.cassette.mode
        self.transport = ReplayTransport(cassette_dir, mode=mode, strict=strict, window=window,
                                        cassette=self.cassette, report=self.report,
                                        check_env=check_env)
        self.tools = ReplayToolBox(self.cassette, policy=tool_policy, exec_c=exec_c,
                                   exec_date=exec_date)
        self.run_dir = run_dir
        self._prev_session = None
        self.overlay = None
        if tool_policy == "class":
            sid = str(self.cassette.meta.get("session_id")
                      or os.path.basename(os.path.abspath(cassette_dir)))
            self.overlay = overlay_mod.Overlay(
                sid, root=overlay_root, read_from_overlay=overlay_read, keep=keep_overlay)
            self.tools.overlay = self.overlay
        self.report.overlay = self.overlay

    # ── 工具供给（runner 侧调用）────────────────────────────────
    def tool_result(self, name, call_id):
        return self.tools.result_for(name, call_id)

    def tool_decision(self, name, call_id):
        """runner 侧：这个调用要吐内容还是真执行（真执行时带 overlay 里的改写入参）。"""
        return self.tools.decision(name, call_id)

    # ── 生命周期 ─────────────────────────────────────────────────
    def __enter__(self):
        from . import llm as llm_mod
        self._prev_session = ReplaySession._active
        ReplaySession._active = self
        # set_transport 返回旧值，还原时原样放回（None 表示"回到默认惰性构建"）
        self._prev_transport = llm_mod.set_transport(self.transport)
        if self.overlay is not None:
            # 副本按**录制入参**建（§8.1），不是按现场将要发起的调用建：现场调用可能
            # 因为回放本身跑偏而完全不同，照着它拷会漏掉真正要重放的路径。
            self.overlay.prepare(self.cassette)
        return self

    def __exit__(self, *exc):
        from . import llm as llm_mod
        llm_mod.set_transport(self._prev_transport)
        ReplaySession._active = self._prev_session
        if self.report is not None:
            # §6.3：分桶游标跑完后，录制里没被取用的请求必须显式留下，
            # 否则「现场少发了调用」会被当成绿色通过。
            self.report.unconsumed = self.transport.unconsumed()
        if self.overlay is not None and self.overlay.delete_on_exit:
            # overlay 是**这次回放的**副作用现场，不留到下一次：残留会让下一次回放
            # 从"上一次跑完的状态"起跑，隔离承诺和可复现性同时失效。
            self.overlay.cleanup()
        return False


def active_session():
    """当前活动回放（没有回放时返回 None）。"""
    return ReplaySession._active


def decision_for(name, call_id):
    """runner 的钩子入口：回放中返回 `LiveCall`（真执行）或 `str`（要吐的内容）。

    非回放返回 None。
    """
    s = active_session()
    if s is None:
        return None
    return s.tool_decision(name, call_id)


def tool_result_for(name, call_id):
    """兼容旧入口：回放中返回录制结果，否则 None。"""
    s = active_session()
    if s is None:
        return None
    return s.tool_result(name, call_id)


def conversation_from_cassette(cassette, question=None, request_index=None):
    """从 cassette 还原"开始回放时的会话"（§4.7 A 组 + §4.8 checkpoints 的用途）。

    回放必须在**录制时的那个会话**上重跑。用 loop_agent_v2 的模块级全局会话会带着
    别人的历史，第一个请求就与录制指纹不符（实测：录制 2 条 vs 现场 17 条），于是
    指纹匹配永远失败、回放变成"跑了一遍但什么都没对上"。

    会话内容优先取 pre_request 快照的 messages_inline（真录制内容）；没有快照时退回
    第一条 llm_request 的 messages。两条路径都要**去掉刚刚追加的用户问题**——那是
    chat_fn 的入参，回放时会被重新追加一次。
    """
    import copy
    reqs = cassette.requests
    if not reqs:
        raise ReplayError("cassette 里没有 llm_request，无法还原会话")
    # 默认从第一条请求起步（单轮回放）；给了 request_index 就从那一轮的第一条请求
    # 起步（多轮 reseed：把会话重置到该轮开始时的快照）。
    first = None
    if request_index is not None:
        for r in reqs:
            if r.get("request_index") == request_index:
                first = r
                break
        if first is None:
            raise ReplayError(f"cassette 里没有 request_index={request_index} 的请求")
    else:
        first = reqs[0]
    q = cassette.initial_question() if question is None else question
    snap = None
    ri = first.get("request_index", 0)
    try:
        snap = cassette.load_for_request(ri, kind="pre_request")
    except Exception:  # noqa: BLE001 —— 没有快照不算致命，退回 llm_request.messages
        snap = None
    if snap is not None:
        messages = snap.get("messages_inline")
        if messages is None:
            if snap.get("body_missing"):
                raise ReplayError(
                    f"checkpoint request_index={ri} 的 messages 只有 hash（body_missing）："
                    "无法还原会话，回放会得到看似通过的错误结论")
            messages = snap.get("messages")
    else:
        messages = first.get("messages")
    messages = copy.deepcopy(list(messages or []))
    if messages and q is not None and messages[-1].get("role") == "user" \
            and messages[-1].get("content") == q:
        messages.pop()
    conv = {"messages": messages, "size": None}
    if snap is not None:
        shell = snap.get("C") or {}
        if isinstance(shell.get("_archive"), list):
            conv["_archive"] = copy.deepcopy(shell["_archive"])
        if isinstance(shell.get("_compact_state"), dict):
            conv["_compact_state"] = copy.deepcopy(shell["_compact_state"])
    return conv


def run_replay(cassette_dir, *, chat_fn=None, question=None, conv=None, mode=None,
               strict=False, window=None, tool_policy="cassette", run_dir=None,
               on_event=None, trace_level="assert", check_env=True,
               overlay_root=None, overlay_read=False, exec_c=False, keep_overlay=False,
               exec_date=False, all_turns=False, reseed=False) -> ReplayReport:
    """跑一次完整回放并返回报告。

    现场自己带一个 trace writer：断言面必须从**这次回放**的事件流里读，而不是从
    cassette 里读——从 cassette 里读的话，"复现了"永远是自证的。

    `tool_policy="class"` 打开 §8.3：A 吐录制、B 在 overlay 里真执行、C 吐录制
    （`exec_c=True` 才真跑）。真执行只写 `tmp/overlay/<sid>/`，真实 WORK_DIR 的
    mtime/sha256 不变（§8.1/§8.2）。

    `all_turns=True` 打开 §4.7 多轮：按 cassette 的 turn_start 逐轮调 chat_fn，会话
    自然累积（上一轮的 assistant 回复留在 conv 里进入下一轮），复现整段会话而不是
    只有第一个用户轮。任一解析不出问题就报错，**不静默少跑一轮**（少跑会伪装成通过）。
    `reseed=True` 是逃生口：每轮开始前把会话重置到该轮第一条请求的 pre_request 快照，
    用于"上一轮回放偏了、但想单独定位某轮"的场合。
    """
    # 注意：`loop_agent_v2` 是仓库根的顶层模块（不是 loop_agent_core 的子模块），
    # 早先这里写成 `from . import loop_agent_v2` 会 ImportError，把回放的默认入口整条打断。
    import loop_agent_v2  # noqa: F401 —— 保证 runner 的出口已装配
    if chat_fn is None:
        chat_fn = loop_agent_v2.chat
    session = ReplaySession(cassette_dir, mode=mode, strict=strict, window=window,
                            tool_policy=tool_policy, check_env=check_env, run_dir=run_dir,
                            overlay_root=overlay_root, overlay_read=overlay_read,
                            exec_c=exec_c, keep_overlay=keep_overlay, exec_date=exec_date)
    result = None
    writer = None
    rid = trace_mod.new_run_id("replay")
    events: list = []
    try:
        writer = trace_mod.open_writer(rid, run_dir=run_dir, level=trace_level,
                                       manifest={"backend": "replay",
                                                 "model": session.cassette.meta.get("model"),
                                                 "cassette": os.path.basename(cassette_dir)})
        with session:
            # 注意：这里**不能**调 llm.reset()——它会把刚装上的回放 transport 清成 None，
            # 下一次调用就会惰性构建出真网关，回放静悄悄地打到了线上。
            if conv is None:
                conv = conversation_from_cassette(session.cassette, question=question)
            turns = session.cassette.turns()
            if all_turns and len(turns) > 1:
                # 多轮：逐轮驱动。任一问题解析不出就报错——静默跳过一轮会让"复现了"
                # 变成一种假象（现场只跑了一半却报绿）。
                for t in turns:
                    if not t.get("question"):
                        raise ReplayError(
                            f"第 {t['index']} 轮解析不出用户问题，拒绝静默少跑一轮")
                session.report.all_turns = True
                session.report.turns = [
                    {"index": t["index"], "question": t["question"],
                     "request_indices": t["request_indices"]}
                    for t in turns
                ]
                for t in turns:
                    if reseed:
                        # 逃生口：每轮重置到该轮开始时的快照，隔离上一轮的偏差。
                        conv = conversation_from_cassette(
                            session.cassette, question=t["question"],
                            request_index=t["request_indices"][0])
                    result = chat_fn(t["question"], on_event=on_event, _conv=conv)
            else:
                if question is None:
                    question = session.cassette.initial_question()
                result = chat_fn(question, on_event=on_event, _conv=conv)
    finally:
        try:
            if writer is not None:
                trace_mod.close_writer(writer, reason="replay")
                events = trace_mod.read_events(
                    os.path.join(writer.run_dir, trace_mod.TRACE_EVENTS))
        except Exception:  # noqa: BLE001
            events = []
    if result is None:
        raise ReplayError("回放没有拿到结果（chat_fn 抛错？）")
    session.report.finalize(events)
    session.report.result = result
    return session.report
