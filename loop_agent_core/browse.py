"""通用浏览器底座：browser_act 单步操作（专用线程 + 持久会话 + 元素清单）。

取代 browse_web。核心改动：
- 专用浏览器线程：所有 Playwright 调用经 ds_web._executor 归入单一线程，
  根治 "cannot switch to a different thread"（真实会话里反复出现）。
- goto 改 domcontentloaded + 短超时，不再死等 networkidle（大页 60s 超时）。
- 每次调用只做一个动作，返回页面状态 + 可交互元素清单（编号），模型按编号驱动下一步。
"""
import ipaddress
import os
import re
from datetime import datetime
from urllib.parse import urlparse

from .config import _PROFILES_DIR, WORK_DIR
from .ds_web import _executor, _get_playwright

_ELEMENT_SELECTORS = (
    "button", "a[href]", "input:not([type='hidden'])",
    "textarea", "select", "[role='button']", "[role='link']", "[role='textbox']",
)

# 会话缓存：key=(profile, headed)。全程只在 executor 线程内访问。
_browser_sessions = {}


def _validate_url(url: str) -> str:
    """Block localhost, private IPs, file://. Return normalized URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only http/https allowed, got: {parsed.scheme}")
    host = parsed.hostname or ""
    host_lower = host.lower()
    if host_lower in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"):
        raise ValueError(f"Access denied: {host}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass  # not an IP address, allow hostname
    else:
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError(f"Access denied: private/loopback network {host}")
    return url


def _get_browser_page(profile: str, headed: bool):
    """Return (page, ctx, browser) for the given profile. Lazily creates and caches.
    MUST be called inside an _executor closure (owns the browser thread)."""
    key = (profile or "__default__", headed)
    if key in _browser_sessions:
        sess = _browser_sessions[key]
        try:
            return sess["page"], sess["ctx"], sess["browser"]
        except Exception:
            pass  # stale session, recreate

    browser = _get_playwright().chromium.launch(headless=not headed)
    storage_path = os.path.join(_PROFILES_DIR, f"{profile}.json") if profile else None
    if storage_path and os.path.exists(storage_path):
        ctx = browser.new_context(storage_state=storage_path)
    else:
        ctx = browser.new_context()
    page = ctx.new_page()
    _browser_sessions[key] = {"page": page, "ctx": ctx, "browser": browser}
    return page, ctx, browser


def _save_profile(profile: str, ctx) -> None:
    """Persist browser storage state (cookies, localStorage) to disk."""
    if not profile:
        return
    os.makedirs(_PROFILES_DIR, exist_ok=True)
    storage_path = os.path.join(_PROFILES_DIR, f"{profile}.json")
    ctx.storage_state(path=storage_path)


def _extract_page_text(page) -> str:
    """Extract readable text from the page."""
    try:
        title = page.title() or ""
        # Remove script/style content for cleaner text
        body_text = page.evaluate("""() => {
            const clone = document.body.cloneNode(true);
            clone.querySelectorAll('script, style, noscript, nav, footer, [role="navigation"]').forEach(el => el.remove());
            return clone.innerText || '';
        }""")
        body_text = re.sub(r'\n{3,}', '\n\n', body_text).strip()
        return f"Title: {title}\n\n{body_text}"
    except Exception as e:
        return f"[extract text failed: {e}]"


def _take_screenshot(page) -> str:
    """Take screenshot, save to screenshots/, return path."""
    tmp_dir = os.path.join(WORK_DIR, "screenshots")
    os.makedirs(tmp_dir, exist_ok=True)
    path = os.path.join(tmp_dir, f"page_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
    page.screenshot(path=path, full_page=False)
    return path


def _visible_elements(page):
    """按固定选择器顺序收集可见元素——列表展示和编号点击必须用同一顺序。"""
    els = []
    for sel in _ELEMENT_SELECTORS:
        for el in page.locator(sel).all():
            try:
                if el.is_visible():
                    els.append(el)
            except Exception:
                continue
    return els


def _describe(el):
    try:
        tag = el.evaluate("e => e.tagName.toLowerCase()") or "el"
        role = el.get_attribute("role")
        kind = role or {"a": "link", "input": "input", "textarea": "input",
                        "select": "select"}.get(tag, tag)
        label = (el.get_attribute("placeholder") or el.get_attribute("aria-label")
                 or el.get_attribute("title")
                 or (el.inner_text() or "").strip().replace("\n", " ")
                 or (el.get_attribute("name") or ""))
        return f"<{kind}> {label.strip()[:60]}"
    except Exception:
        return "<el> ?"


def _list_interactive_elements(page, limit=30):
    return [f"[{i+1}] {_describe(el)}" for i, el in enumerate(_visible_elements(page)) if i < limit]


def _find_element(page, target):
    try:
        idx = int(target)
    except (TypeError, ValueError):
        raise ValueError(f"target 必须是元素清单里的编号，拿到: {target!r}")
    els = _visible_elements(page)
    if 1 <= idx <= len(els):
        return els[idx - 1]
    raise ValueError(f"元素编号 {idx} 超出范围 (1-{len(els)})。用最新返回的元素清单。")


def _page_state(page):
    """返回 URL + Title + 元素清单 + 页面文本（截断 4000）。"""
    out = [f"URL: {page.url}", f"Title: {page.title() or ''}"]
    elements = _list_interactive_elements(page)
    if elements:
        out.append("Elements:")
        out.extend(elements)
    body = _extract_page_text(page)
    if len(body) > 4000:
        body = body[:4000] + f"\n... (truncated, {len(body)} chars total)"
    out.append(f"--- Page Content ---\n{body}")
    return "\n".join(out)


def browser_act_mock(action, url="", target=None, text="", profile="", headed=False):
    """单步浏览器操作：每次只做一个动作，返回页面状态 + 可交互元素清单。"""
    def work():
        page, ctx, browser = _get_browser_page(profile, headed)
        out = [f"[browser_act] action: {action}"]
        try:
            if action == "open":
                page.goto(_validate_url(url), wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(500)
                out.append("OK: navigated")
            elif action == "click":
                _find_element(page, target).click()
                page.wait_for_timeout(800)
                out.append(f"OK: clicked element #{target}")
            elif action == "type":
                _find_element(page, target).fill(text)
                page.wait_for_timeout(500)
                out.append(f"OK: typed into element #{target}")
            elif action == "press_enter":
                page.keyboard.press("Enter")
                page.wait_for_timeout(800)
                out.append("OK: pressed Enter")
            elif action == "scroll":
                page.keyboard.press("PageDown" if str(text).lower() in ("", "down") else "PageUp")
                out.append("OK: scrolled")
            elif action == "wait":
                secs = min(int(text or 1), 10)
                page.wait_for_timeout(secs * 1000)
                out.append(f"OK: waited {secs}s")
            elif action == "back":
                page.go_back(wait_until="domcontentloaded", timeout=30000)
                out.append("OK: went back")
            elif action == "screenshot":
                out.append(f"Screenshot saved: {_take_screenshot(page)}")
            elif action == "close":
                _browser_sessions.pop((profile or "__default__", headed), None)
                browser.close()
                out.append("OK: closed browser session")
            else:
                raise ValueError(f"Unknown action: {action!r}. Valid: open, click, type, press_enter, scroll, wait, back, screenshot, close")

            if action != "close":
                _save_profile(profile, ctx)
                out.append(_page_state(page))
            return "\n".join(out)
        except Exception as e:
            return f"[Error] browser_act failed: {e}"
    return _executor.call(work)
