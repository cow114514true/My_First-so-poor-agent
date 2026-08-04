"""通用浏览器：URL 校验、Playwright 会话、指令解析、截图、profile 持久化。"""
import ipaddress
import os
import re
from datetime import datetime
from urllib.parse import urlparse

from .config import _PROFILES_DIR, WORK_DIR
from .ds_web import _get_playwright


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


# ponytail: shared browser instances keyed by (profile, headed)
_browser_sessions = {}


def _get_browser_page(profile: str, headed: bool):
    """Return (page, ctx, browser) for the given profile. Lazily creates and caches."""
    key = (profile or "__default__", headed)
    if key in _browser_sessions:
        sess = _browser_sessions[key]
        try:
            # Return existing page (reuse tab)
            return sess["page"], sess["ctx"], sess["browser"]
        except Exception:
            pass  # stale session, recreate

    pw = _get_playwright()
    browser = pw.chromium.launch(headless=not headed)

    # Load storage state if profile exists
    storage_path = os.path.join(_PROFILES_DIR, f"{profile}.json") if profile else None
    if storage_path and os.path.exists(storage_path):
        ctx = browser.new_context(storage_state=storage_path)
    else:
        ctx = browser.new_context()

    page = ctx.new_page()
    _browser_sessions[key] = {"page": page, "ctx": ctx, "browser": browser, "playwright": pw}
    return page, ctx, browser


def _save_profile(profile: str, ctx) -> None:
    """Persist browser storage state (cookies, localStorage) to disk."""
    if not profile:
        return
    os.makedirs(_PROFILES_DIR, exist_ok=True)
    storage_path = os.path.join(_PROFILES_DIR, f"{profile}.json")
    ctx.storage_state(path=storage_path)


def _execute_instructions(page, instructions: str) -> str:
    """Parse instructions and execute browser actions. Returns a summary."""
    actions_done = []

    # ── click ──
    for m in re.finditer(
        r'click\s+(?:on\s+)?(?:the\s+)?["\']?(?P<target>.+?)["\']?\s*(?:button|link|element|tab)?\s*(?:$|[,;.]|\s+(?:and|then|after|to))',
        instructions, re.I,
    ):
        target = m.group("target").strip().rstrip('"\' ')
        if not target:
            continue
        try:
            # Click based on element type
            found = page.get_by_role("button", name=re.compile(target, re.I)).first
            if not found or not found.is_visible():
                found = page.get_by_role("link", name=re.compile(target, re.I)).first
            if not found or not found.is_visible():
                found = page.get_by_text(target).first
            if found and found.is_visible():
                found.click()
                actions_done.append(f"clicked '{target}'")
                page.wait_for_timeout(1500)
        except Exception as e:
            actions_done.append(f"click '{target}' failed: {e}")

    # ── type / fill ──
    for m in re.finditer(
        r'(?:type|fill|enter|input)\s+["\']?(?P<text>.+?)["\']?\s+(?:in|into|on)\s+(?:the\s+)?["\']?(?P<target>.+?)["\']?\s*(?:field|input|box|form)?\s*(?:$|[,;.]|\s+(?:and|then))',
        instructions, re.I,
    ):
        text = m.group("text").strip().rstrip('"\' ')
        target = m.group("target").strip().rstrip('"\' ')
        if not text or not target:
            continue
        try:
            # Find input by placeholder, label, or nearby text
            el = (
                page.get_by_placeholder(target).first
                or page.get_by_label(target).first
                or page.locator(f"input[name*='{target}']").first
            )
            if not el or not el.is_visible():
                # Fallback: find any visible input near the target text
                el = page.get_by_text(target).first
                if el and el.is_visible():
                    # Try to find input near this text
                    pass  # too complex for v1
            if el and el.is_visible():
                el.fill(text)
                actions_done.append(f"typed '{text}' into '{target}'")
                page.wait_for_timeout(500)
        except Exception as e:
            actions_done.append(f"type '{text}' failed: {e}")

    # ── search ──
    for m in re.finditer(
        r'search\s+(?:for\s+)?["\']?(?P<query>.+?)["\']?(?:\s*$|\s*(?:and|then|after|\.))',
        instructions, re.I,
    ):
        query = m.group("query").strip().rstrip('"\' ')
        if not query:
            continue
        try:
            # Find search input
            search_el = (
                page.get_by_role("searchbox").first
                or page.get_by_placeholder(re.compile(r"search|搜索", re.I)).first
                or page.locator("input[type='search']").first
                or page.locator("input[name='q']").first  # common search param
            )
            if search_el and search_el.is_visible():
                search_el.fill(query)
                search_el.press("Enter")
                actions_done.append(f"searched for '{query}'")
                page.wait_for_timeout(2000)
        except Exception as e:
            actions_done.append(f"search '{query}' failed: {e}")

    # ── scroll ──
    if re.search(r'scroll\s+down', instructions, re.I):
        page.keyboard.press("PageDown")
        actions_done.append("scrolled down")
        page.wait_for_timeout(500)
    if re.search(r'scroll\s+up', instructions, re.I):
        page.keyboard.press("PageUp")
        actions_done.append("scrolled up")
        page.wait_for_timeout(500)

    # ── wait ──
    wait_m = re.search(r'wait\s+(\d+)\s*(?:seconds?|s)', instructions, re.I)
    if wait_m:
        secs = min(int(wait_m.group(1)), 10)  # cap at 10s
        page.wait_for_timeout(secs * 1000)
        actions_done.append(f"waited {secs}s")

    return "; ".join(actions_done) if actions_done else "no actions executed"


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
        # Clean up excessive whitespace
        body_text = re.sub(r'\n{3,}', '\n\n', body_text).strip()
        return f"Title: {title}\n\n{body_text}"
    except Exception as e:
        return f"[extract text failed: {e}]"


def _take_screenshot(page) -> str:
    """Take screenshot, save to temp file, return path."""
    tmp_dir = os.path.join(WORK_DIR, "screenshots")
    os.makedirs(tmp_dir, exist_ok=True)
    path = os.path.join(tmp_dir, f"page_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
    page.screenshot(path=path, full_page=False)
    return path


def browse_web_mock(url, instructions, output="text", profile="", headed=False):
    """Browse any website and return content."""
    url = _validate_url(url)
    page, ctx, browser = _get_browser_page(profile, headed)

    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1000)  # extra settle time for SPAs

        action_summary = _execute_instructions(page, instructions)

        # Wait for any post-action navigation
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass  # timeout on networkidle is OK

        result_parts = [f"[browse_web] URL: {url}"]
        if action_summary:
            result_parts.append(f"Actions: {action_summary}")

        if output in ("text", "both", ""):
            text = _extract_page_text(page)
            # Truncate very long pages
            if len(text) > 8000:
                text = text[:8000] + f"\n\n... (truncated, {len(text)} chars total)"
            result_parts.append(f"--- Page Content ---\n{text}")

        if output in ("screenshot", "both"):
            screenshot_path = _take_screenshot(page)
            result_parts.append(f"--- Screenshot saved ---\n{screenshot_path}\n[Use use_ds_from_web with this path to analyze. Ask briefly: 'Describe this screenshot concisely in plain text. No fluff.']")

        # Persist profile state on each use
        _save_profile(profile, ctx)

        return "\n\n".join(result_parts)

    except Exception as e:
        return f"[Error] browse_web failed: {e}"
