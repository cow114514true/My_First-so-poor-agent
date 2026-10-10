"""统一浏览器观察对象（阶段二）：截图 + DOM + 文本 + 滚动在同一观察周期采集。

解决文档 §3.5 的问题——截图、DOM 清单、页面文本若分属不同时刻，会出现
"截图有按钮但 DOM 没更新 / DOM 有元素但截图已旧"的错位。这里把一次采集
固化成一个 BrowserObservation，供 observe_browser 返回、供 visual_locate 复用。

线程约束：collect_observation 只接受已就绪的 Playwright page，所有调用必须发生在
浏览器专用线程内（即 _executor.call 闭包中）；observe_browser_mock 已做好这层包装。
"""
import hashlib
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List

from .browse import (_pause_videos, _take_screenshot, _extract_page_text,
                     _visible_elements, _evaluate, _format_elements, _locate_hits,
                     _LIST_LIMIT, _get_browser_page, _executor)


@dataclass
class BrowserObservation:
    """一次统一观察的结构化结果。elements 是 _visible_elements 的原始 dict 列表，
    编号只在本 observation 内有效；screenshot_hash 兼作页面内容身份。"""
    profile: str = "default"
    url: str = ""
    title: str = ""
    viewport: dict = field(default_factory=dict)      # {width, height}
    scroll: dict = field(default_factory=dict)        # {x, y}
    screenshot_path: str = ""
    screenshot_hash: str = ""
    page_text: str = ""
    elements: List[dict] = field(default_factory=list)
    timestamp: str = ""

    def to_dict(self):
        return asdict(self)


def _sha256_file(path):
    """文件 sha256 前 16 位 hex，作为截图内容身份（失败返回空串）。"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except Exception:
        return ""


def collect_observation(page, profile="", include_screenshot=True, include_dom=True,
                        include_text=True):
    """在同一观察周期内采集截图/DOM/文本/滚动，返回 BrowserObservation。
    必须在浏览器线程内调用。"""
    _pause_videos(page)  # 先停视频，保证后续往返低延迟
    shot, shot_hash = "", ""
    if include_screenshot:
        shot = _take_screenshot(page)
        shot_hash = _sha256_file(shot)
    viewport, scroll = {}, {}
    try:
        vp = page.viewport_size
        if vp:
            viewport = {"width": vp.get("width"), "height": vp.get("height")}
    except Exception:
        pass
    try:
        s = _evaluate(page, "() => ({x: window.scrollX, y: window.scrollY})")
        scroll = {"x": int(s.get("x") or 0), "y": int(s.get("y") or 0)}
    except Exception:
        pass
    elements = _visible_elements(page) if include_dom else []
    text = _extract_page_text(page) if include_text else ""
    return BrowserObservation(
        profile=profile or "default",
        url=page.url,
        title=(page.title() or "").strip(),
        viewport=viewport,
        scroll=scroll,
        screenshot_path=shot,
        screenshot_hash=shot_hash,
        page_text=text,
        elements=elements,
        timestamp=datetime.now().isoformat(timespec="seconds"),
    )


def format_observation(obs, focus=None, limit=_LIST_LIMIT, text_limit=4000):
    """把 BrowserObservation 转成给模型看的人类可读文本（与 browser_act 同编号格式）。
    focus 非空时把命中元素 ★ 置顶。"""
    out = [f"URL: {obs.url}", f"Title: {obs.title or ''}"]
    if obs.viewport:
        vp = obs.viewport
        sc = obs.scroll
        out.append(f"Viewport: {vp.get('width')}x{vp.get('height')}  scroll=({sc.get('x', 0)},{sc.get('y', 0)})")
    if obs.screenshot_path:
        out.append(f"Screenshot: {obs.screenshot_path} (sha256={obs.screenshot_hash})")
    lines = _format_elements(obs.elements, limit)
    if lines:
        if focus:
            hits = _locate_hits(obs.elements, focus, limit)
            if hits:
                out.append(f"★ 命中『{focus}』({len(hits)}):")
                out.extend(hits)
        out.append("Elements:")
        out.extend(lines)
    if obs.page_text:
        body = obs.page_text
        if len(body) > text_limit:
            body = body[:text_limit] + f"\n... (truncated, {len(body)} chars total)"
        out.append(f"--- Page Content ---\n{body}")
    out.append(f"timestamp: {obs.timestamp}")
    return "\n".join(out)


def observe_browser_mock(profile="", include_screenshot=True, include_dom=True,
                         include_text=True, focus=None, headless=True):
    """只观察、不执行动作：一次调用返回统一采集的 URL/标题/视口/滚动/截图/DOM/文本。
    include_* 按需裁剪；focus=<关键词> 把命中元素 ★ 置顶。
    用途：复杂任务开始前拿统一状态；visual_locate 复用同一份截图+DOM；调试页面状态。
    别用 browser_act(action='screenshot') 兼作观察——那不会带回 DOM/文本。"""
    def work():
        page, _ctx, _browser = _get_browser_page(profile, headless)
        if page.url.strip().rstrip("/") in ("about:blank", ""):
            return "[Error] observe_browser: 浏览器是空白页，先 browser_act(open) 打开一个 URL。"
        obs = collect_observation(page, profile,
                                  include_screenshot=include_screenshot,
                                  include_dom=include_dom,
                                  include_text=include_text)
        return format_observation(obs, focus=focus)
    return _executor.call(work)
