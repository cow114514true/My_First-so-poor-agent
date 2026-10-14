"""工具分类表：A（只读/可吐回录制结果）/ B（有副作用，回放时真执行）/ C（外部世界，默认吐回）。

回放策略按这个表走（TRACE_REPLAY_BUILD.md §8.3）：
- A 类：回放直接吐 `result_full`，不触碰工作区。
- B 类：在 overlay 内真执行，按录制的发起顺序串行。
- C 类：默认吐录制结果；`opt_in` 显式开启时才真跑。

**未登记的工具一律抛错**（`class_of` / `assert_all_classified`）：
回放时不知道一个工具有没有副作用，就只能靠猜；猜错的代价是"回放篡改了工作区"
或者"回放结果无法复现"。所以宁可启动即失败，也不要静默归类。
"""
from __future__ import annotations

# class: "A" | "B" | "C"
# path_args: 入参里是**文件系统路径**的参数名（回放阶段用于正向重写到 overlay 根，§8.2）
# rewrite_scope: "structured"（逐参数替换）| "best_effort"（整串子串替换）| None
TOOL_CLASS: dict[str, dict] = {
    # ── A：只读 ────────────────────────────────────────────────
    "read_file":       {"class": "A", "path_args": ("path",), "rewrite_scope": "structured"},
    "count_tokens":    {"class": "A", "path_args": ("path",), "rewrite_scope": "structured"},
    "search_code":     {"class": "A", "path_args": ("path",), "rewrite_scope": "structured"},
    "code_index":      {"class": "A", "path_args": ("path",), "rewrite_scope": "structured"},
    "get_date":        {"class": "A", "path_args": (), "rewrite_scope": None},
    "get_weather":     {"class": "A", "path_args": (), "rewrite_scope": None},
    "recall":          {"class": "A", "path_args": (), "rewrite_scope": None},
    "recall_archive":  {"class": "A", "path_args": (), "rewrite_scope": None},
    "observe_browser": {"class": "A", "path_args": (), "rewrite_scope": None},

    # ── B：有副作用（回放时在 overlay 内真执行）──────────────────
    "write_file":      {"class": "B", "path_args": ("path",), "rewrite_scope": "structured"},
    "edit_file":       {"class": "B", "path_args": ("path",), "rewrite_scope": "structured"},
    "remember":        {"class": "B", "path_args": (), "rewrite_scope": None},
    "mark_coin":       {"class": "B", "path_args": (), "rewrite_scope": None},
    # 命令串里的路径只能做子串级替换：无法可靠解析 shell 语法
    "exec_shell_win":  {"class": "B", "path_args": (), "rewrite_scope": "best_effort"},

    # ── C：外部世界（默认不真跑）──────────────────────────────
    "browser_act":     {"class": "C", "path_args": (), "rewrite_scope": None},
    "visual_locate":   {"class": "C", "path_args": (), "rewrite_scope": None},
    "analyze_image":   {"class": "C", "path_args": ("path",), "rewrite_scope": "structured"},
    "search_web":      {"class": "C", "path_args": (), "rewrite_scope": None},
    "fetch_url":       {"class": "C", "path_args": (), "rewrite_scope": None},
    "use_ds_from_web": {"class": "C", "path_args": (), "rewrite_scope": None},
    "delegate_task":   {"class": "C", "path_args": (), "rewrite_scope": None},
    "qq_bot_send":     {"class": "C", "path_args": (), "rewrite_scope": None},
}

_CLASSES = ("A", "B", "C")


class UnclassifiedToolError(LookupError):
    """工具未登记在 TOOL_CLASS：分类未知即不可回放，必须显式登记。"""


def class_of(name: str) -> str:
    """工具类别；未登记 → 抛 UnclassifiedToolError。"""
    entry = TOOL_CLASS.get(name)
    if entry is None:
        raise UnclassifiedToolError(
            f"工具未登记在 TOOL_CLASS：{name!r}。"
            f"请先在 loop_agent_core/toolclass.py 里登记其类别（A/B/C）与 path_args。")
    return entry["class"]


def path_args_of(name: str) -> tuple:
    """该工具入参里属于文件系统路径的参数名；未登记 → 抛 UnclassifiedToolError。"""
    entry = TOOL_CLASS.get(name)
    if entry is None:
        raise UnclassifiedToolError(f"工具未登记在 TOOL_CLASS：{name!r}")
    return tuple(entry.get("path_args") or ())


def rewrite_scope_of(name: str) -> str | None:
    entry = TOOL_CLASS.get(name)
    if entry is None:
        raise UnclassifiedToolError(f"工具未登记在 TOOL_CLASS：{name!r}")
    return entry.get("rewrite_scope")


def registered_names() -> tuple:
    return tuple(TOOL_CLASS)


def unclassified_names(names) -> list:
    """names 里未登记的项（保持输入顺序，便于报错点名）。"""
    return [n for n in names if n not in TOOL_CLASS]


def assert_all_classified(names) -> None:
    """进程启动校验：实际注册的工具表里出现未登记工具即抛错并列出工具名。

    调用点见 loop_agent_v2（薄壳持有 TOOL_CALL_MAP 与 schemas.tools）。
    """
    import json
    seen = []
    for n in names:
        if isinstance(n, dict):  # schemas.tools 形态：{"type": "function", "function": {...}}
            try:
                n = n["function"]["name"]
            except (KeyError, TypeError):
                continue
        if n not in seen:
            seen.append(str(n))
    missing = unclassified_names(seen)
    if missing:
        raise UnclassifiedToolError(
            "以下工具未登记在 TOOL_CLASS，回放无法判定其副作用类别："
            + ", ".join(missing))


def assert_tool_class_wellformed() -> None:
    """TOOL_CLASS 自身的一致性校验（类别取值、path_args 与 rewrite_scope 匹配）。"""
    for name, entry in TOOL_CLASS.items():
        cls = entry.get("class")
        if cls not in _CLASSES:
            raise ValueError(f"TOOL_CLASS[{name!r}]['class'] 非法：{cls!r}（应为 A/B/C）")
        scope = entry.get("rewrite_scope")
        if scope not in (None, "structured", "best_effort"):
            raise ValueError(f"TOOL_CLASS[{name!r}]['rewrite_scope'] 非法：{scope!r}")
        if scope == "structured" and not entry.get("path_args"):
            raise ValueError(f"TOOL_CLASS[{name!r}] 声明 structured 却没有 path_args")
        for a in entry.get("path_args") or ():
            if not isinstance(a, str):
                raise ValueError(f"TOOL_CLASS[{name!r}]['path_args'] 含非字符串项：{a!r}")
