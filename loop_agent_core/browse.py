"""通用浏览器底座：browser_act 单步操作（专用线程 + 持久会话 + 元素清单）。

取代 browse_web。核心改动：
- 专用浏览器线程：所有 Playwright 调用经 ds_web._executor 归入单一线程，
  根治 "cannot switch to a different thread"（真实会话里反复出现）。
- goto 改 domcontentloaded + 短超时，不再死等 networkidle（大页 60s 超时）。
- 每次调用只做一个动作，返回页面状态 + 可交互元素清单（编号），模型按编号驱动下一步。
"""
import ipaddress
import json
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

# 会话缓存：key=profile（headless 只在首次创建生效）。全程只在 executor 线程内访问。
_browser_sessions = {}
_last_active = None  # 最近使用的会话 key：模型常漏传 profile/headless，用它回退避免碎片化


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


def _get_browser_page(profile: str, headless: bool):
    """Return (page, ctx, browser) for the given profile. Lazily creates and caches.
    MUST be called inside an _executor closure (owns the browser thread).
    模型常漏传 profile → 用 _last_active 回退到最近会话，避免碎片化。
    重建 context 时恢复 SPA 状态：init_script 回放 sessionStorage + 跳到上次 URL。
    用户手动关掉的窗口会使缓存会话失效——先做存活检测，死会话重建。"""
    global _last_active
    key = profile or (_last_active or "__default__")
    if key in _browser_sessions:
        sess = _browser_sessions[key]
        try:
            sess["page"].url  # 真实往返检测：页面/浏览器已死会抛异常（比 is_closed 可靠）
            _last_active = key
            return sess["page"], sess["ctx"], sess["browser"]
        except Exception:
            pass  # dead session
        _browser_sessions.pop(key, None)  # 用户关掉的死会话 → 下次调用重建

    browser = _get_playwright().chromium.launch(headless=headless)
    storage_path = os.path.join(_PROFILES_DIR, f"{key}.json") if key != "__default__" else None
    if storage_path and os.path.exists(storage_path):
        ctx = browser.new_context(storage_state=storage_path)
    else:
        ctx = browser.new_context()
    page = ctx.new_page()
    if key != "__default__":
        sess = _load_session(key)
        if sess:
            ss = sess.get("session_storage") or {}
            if ss:
                # 在页面脚本运行前回放 sessionStorage（SPA 常在加载时读它恢复会话）
                page.add_init_script(
                    "() => { const d = %s; for (const k in d) sessionStorage.setItem(k, d[k]); }"
                    % json.dumps(ss, ensure_ascii=False)
                )
            url = sess.get("url")
            if url:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
    _browser_sessions[key] = {"page": page, "ctx": ctx, "browser": browser}
    _last_active = key
    return page, ctx, browser


def _save_profile(profile: str, ctx, page=None) -> None:
    """Persist browser state: storage_state (cookies, localStorage) + SPA 恢复用 URL/sessionStorage."""
    if not profile or profile == "__default__":
        return
    os.makedirs(_PROFILES_DIR, exist_ok=True)
    storage_path = os.path.join(_PROFILES_DIR, f"{profile}.json")
    ctx.storage_state(path=storage_path)
    try:
        url = page.url if page is not None else ""
        ss = (page.evaluate("() => { const o = {}; for (const k of sessionStorage) o[k] = sessionStorage.getItem(k); return o; }")
              if page is not None else {})
        with open(os.path.join(_PROFILES_DIR, f"{profile}_session.json"), "w", encoding="utf-8") as f:
            json.dump({"url": url, "session_storage": ss}, f, ensure_ascii=False)
    except Exception:
        pass


def _load_session(profile: str):
    try:
        with open(os.path.join(_PROFILES_DIR, f"{profile}_session.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


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


def _css_path(el):
    """稳定的 CSS 路径：优先 #id，否则 tag:first-class:nth-of-type，逐级上溯（截断防超长）。"""
    try:
        return el.evaluate("""(e) => {
            const parts = [];
            let cur = e;
            while (cur && cur.nodeType === 1 && cur.tagName.toLowerCase() !== 'html' && parts.length < 6) {
                const tag = cur.tagName.toLowerCase();
                let sel = tag;
                if (cur.id) { sel = tag + '#' + cur.id; parts.unshift(sel); break; }
                if (cur.classList && cur.classList.length) sel += '.' + cur.classList[0];
                const parent = cur.parentElement;
                if (parent) {
                    const sameTag = [...parent.children].filter(c => c.tagName === cur.tagName);
                    if (sameTag.length > 1) sel += ':nth-of-type(' + (sameTag.indexOf(cur) + 1) + ')';
                }
                parts.unshift(sel);
                cur = parent;
            }
            return parts.join(' > ');
        }""")
    except Exception:
        return "?"


def _list_interactive_elements(page, limit=30):
    return [f"[{i+1}] {_describe(el)} — css={_css_path(el)}" for i, el in enumerate(_visible_elements(page)) if i < limit]


def _find_element(page, target):
    """target 支持：元素清单编号、css=<选择器>、xpath=<表达式>。返回 Playwright locator。"""
    if isinstance(target, str):
        low = target.strip()
        if low.startswith("css="):
            return page.locator(low[4:].strip()).first
        if low.startswith("xpath="):
            return page.locator("xpath=" + low[5:].strip()).first
    try:
        idx = int(target)
    except (TypeError, ValueError):
        raise ValueError(f"target 必须是元素清单编号、css=<选择器> 或 xpath=<表达式>，拿到: {target!r}")
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


def _do_browser(action, url, target, text, profile, headless):
    """执行单个浏览器动作；失败抛异常，由 browser_act_mock 统一格式化 / 死会话重试。"""
    global _last_active
    page, ctx, browser = _get_browser_page(profile, headless)
    out = [f"[browser_act] action: {action}"]
    if action not in ("open", "close") and page.url.strip().rstrip("/") in ("about:blank", ""):
        return "[Error] Browser page is still blank (about:blank). Call action='open' with a URL first."
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
    elif action == "press_key":
        key_name = str(text or "").strip()
        if not key_name:
            out.append("[Error] press_key needs text=<key name>, e.g. 'Space', 'Escape', 'Enter'")
        else:
            page.keyboard.press(key_name)
            page.wait_for_timeout(400)
            out.append(f"OK: pressed key {key_name}")
    elif action == "hover":
        _find_element(page, target).hover()
        page.wait_for_timeout(400)
        out.append(f"OK: hovered {target}")
    elif action == "select_option":
        _find_element(page, target).select_option(text)
        page.wait_for_timeout(400)
        out.append(f"OK: selected option '{text}' in {target}")
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
        key = profile or _last_active or "__default__"
        _browser_sessions.pop(key, None)
        if _last_active == key:
            _last_active = None
        browser.close()
        out.append("OK: closed browser session")
    else:
        raise ValueError(f"Unknown action: {action!r}. Valid: open, click, type, press_enter, press_key, hover, select_option, scroll, wait, back, screenshot, close")

    if action != "close":
        _save_profile(profile or _last_active or "__default__", ctx, page)
        out.append(_page_state(page))
    return "\n".join(out)


def browser_act_mock(action, url="", target=None, text="", profile="", headless=True):
    """单步浏览器操作：每次只做一个动作，返回页面状态 + 可交互元素清单。
    headless=False 会显示浏览器窗口（登录/验证码等需要人工操作时用）。
    死会话（用户关了窗口）自动丢弃并重建重试一次。"""
    def work():
        try:
            return _do_browser(action, url, target, text, profile, headless)
        except Exception as e:
            if "has been closed" in str(e):
                _browser_sessions.pop(profile or _last_active or "__default__", None)
                try:
                    return _do_browser(action, url, target, text, profile, headless)
                except Exception as e2:
                    return f"[Error] browser_act failed: {e2}"
            return f"[Error] browser_act failed: {e}"
    return _executor.call(work)
