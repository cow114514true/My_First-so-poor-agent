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

from .debuglog import log_error

from .config import _PROFILES_DIR, WORK_DIR
from .ds_web import _executor, _get_playwright

_EVALUATE_TIMEOUT = 15000  # JS 求值超时：页面 JS 卡死时抛 TimeoutError 而非永久挂


def _evaluate(page, js, arg=None):
    """page.evaluate 带超时兜底：JS 死循环/超重 DOM 时不阻塞整个工具线程。
    统一用位置参数 arg（page/element/frame 的 evaluate 都支持 expression, arg），
    不传 timeout 关键字——element/frame 的 evaluate 没有该参数会 TypeError。"""
    return page.evaluate(js, arg)

# input 类选择器排前，避免被 button/a 淹没导致搜索框排在 30 之后（bilibili 搜索框曾排第 75）。
# _visible_elements 用 _css_path 去重，所以跨类别重叠（button 带 role=button 等）不会产生重复项。
_ELEMENT_SELECTORS = (
    "input:not([type='hidden'])", "textarea", "select",
    "[contenteditable]", "[role='search']", "[role='searchbox']", "[role='combobox']",
    "button", "a[href]", "[role='button']", "[role='link']", "[role='textbox']",
    "label", "summary",
    "[role='checkbox']", "[role='radio']", "[role='switch']", "[role='tab']", "[role='menuitem']",
    "video", "audio", "[onclick]",
    "[tabindex]:not([tabindex='-1'])",
    ".video-toolbar-left-item",  # bilibili 视频操作栏（点赞/投币/收藏）是 div 不是 button，无文本
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
                except Exception as e:
                    log_error("browse restore goto", e)
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
        ss = (_evaluate(page, "() => { const o = {}; for (const k of sessionStorage) o[k] = sessionStorage.getItem(k); return o; }")
              if page is not None else {})
        with open(os.path.join(_PROFILES_DIR, f"{profile}_session.json"), "w", encoding="utf-8") as f:
            json.dump({"url": url, "session_storage": ss}, f, ensure_ascii=False)
    except Exception as e:
        log_error("save_profile session", e)


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
        body_text = _evaluate(page, """() => {
            const clone = document.body.cloneNode(true);
            clone.querySelectorAll('script, style, noscript, nav, footer, [role="navigation"]').forEach(el => el.remove());
            return clone.textContent || '';
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


# 补充扫描：cursor:pointer 的"裸可点击"元素（如登录按钮是 <span>，无 role/onclick）。
# 标准类别已覆盖的（a/button/input 及其内部）用 closest 过滤，避免重复灌入。
# 扫描加了 节点预算(3500) + 时间预算(2s)，且用廉价预筛（可见叶子+有文本）把候选砍小，
# 最后才对少数候选查 getComputedStyle——否则大页（视频页 DOM 数千节点）会卡死（实测挂 >90s）。
_POINTER_SCAN_JS = r"""() => {
    const out = [];
    const start = performance.now();
    const COVER = 'a, button, input, textarea, select, label, summary, video, audio, [contenteditable], [onclick], [role="button"], [role="link"], [role="textbox"], [role="search"], [role="searchbox"], [role="combobox"], [role="checkbox"], [role="radio"], [role="switch"], [role="tab"], [role="menuitem"]';
    const all = document.querySelectorAll('body *');
    let scanned = 0;
    for (const el of all) {
        if (++scanned > 3500 || performance.now() - start > 2000) break;
        const tag = el.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE') continue;
        if (el.offsetParent === null) continue;          // 隐藏（display:none）——廉价检查
        if (el.children.length > 0) continue;            // 只查叶子，容器交给叶子代表
        const t = (el.textContent || '').trim().replace(/\s+/g, ' ');
        if (!t && !el.getAttribute('aria-label')) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4 || r.top < 0 || r.left < 0) continue;
        if (el.closest(COVER)) continue;                 // 已被标准类别或其祖先覆盖
        if (getComputedStyle(el).cursor !== 'pointer') continue;  // 最后才查样式
        out.push({ path: uniq(el) });
        if (out.length >= 30) break;
    }
    return out;
    function uniq(el) {
        const parts = [];
        let n = el;
        while (n && n.nodeType === 1 && n.tagName.toLowerCase() !== 'body') {
            if (n.id) { parts.unshift('#' + CSS.escape(n.id)); break; }
            const kids = n.parentElement ? [...n.parentElement.children] : [];
            parts.unshift(n.tagName.toLowerCase() + ':nth-child(' + (kids.indexOf(n) + 1) + ')');
            n = n.parentElement;
        }
        return parts.join(' > ');
    }
}"""


def _pointer_leaves(page):
    """返回 cursor:pointer 且未被标准选择器覆盖的可点击元素（路径列表）。"""
    try:
        return _evaluate(page, _POINTER_SCAN_JS)
    except Exception:
        return []


_LINK_CAP = 40  # 链接类（a[href]/role=link）每页上限：防相关视频等被动链接洪水把交互控件挤出列表


# 批量求值 JS：一次 evaluate_all 返回所有匹配元素的描述/CSS路径/可见性。
# 逐元素 _visible_elements 在页面 JS 主线程繁忙时偶发 122s（实测 browse_timing.log），
# evaluate_all 每个选择器一次往返，全部选择器仅 0.08s——根治卡顿。
_BATCH_EL_JS = r"""(els) => els.map(e => {
    const tag = e.tagName.toLowerCase();
    const role = e.getAttribute('role');
    let label = (e.getAttribute('placeholder') || e.getAttribute('aria-label')
        || e.getAttribute('title')
        || (e.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 100)
        || e.getAttribute('name') || '');
    if (!label && (tag === 'video' || tag === 'audio')) label = tag + ' player';
    const cls = typeof e.className === 'string' ? e.className : '';
    if (!role) {
        if (/\bvideo-coin\b/.test(cls)) label = '投币 ' + label;
        else if (/\bvideo-like\b/.test(cls)) label = '点赞 ' + label;
        else if (/\bvideo-fav\b/.test(cls)) label = '收藏 ' + label;
        else if (/\bvideo-share\b/.test(cls)) label = '分享 ' + label;
    }
    if (!label || (label.length < 8 && /^[\d.,万kK%+ ]+$/.test(label))) {
        let p = e.parentElement;
        for (let i = 0; p && i < 3; i++, p = p.parentElement) {
            const t = p.getAttribute && (p.getAttribute('title') || p.getAttribute('aria-label'));
            if (t) { label = label ? t + ' ' + label : t; break; }
        }
    }
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    const visible = r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
    const box = { x: r.x, y: r.y, width: r.width, height: r.height };
    const parts = [];
    let cur = e;
    while (cur && cur.nodeType === 1 && cur.tagName.toLowerCase() !== 'html' && parts.length < 6) {
        const t2 = cur.tagName.toLowerCase();
        let sel = t2;
        if (cur.id) { sel = t2 + '#' + cur.id; parts.unshift(sel); break; }
        if (cur.classList && cur.classList.length) sel += '.' + cur.classList[0];
        const parent = cur.parentElement;
        if (parent) {
            const sameTag = [...parent.children].filter(c => c.tagName === cur.tagName);
            if (sameTag.length > 1) sel += ':nth-of-type(' + (sameTag.indexOf(cur) + 1) + ')';
        }
        parts.unshift(sel);
        cur = parent;
    }
    return { tag, role, label, css: parts.join(' > '), visible, box,
             is_link: tag === 'a' || role === 'link' };
})"""


def _visible_elements(page):
    """按固定选择器顺序收集可见元素——列表展示和编号点击必须用同一顺序。
    用 evaluate_all 批量求值（每个选择器一次往返），返回元素描述 dict 列表，
    由 _find_element 按编号重建 locator。跨 frame + 帧序去重 + 链接上限。"""
    els = []
    seen = set()
    link_count = 0
    for fi, frame in enumerate(page.frames):
        try:
            for sel in _ELEMENT_SELECTORS:
                try:
                    batch = frame.locator(sel).evaluate_all(_BATCH_EL_JS)
                except Exception:
                    continue  # 帧被移除/选择器无效：跳过
                for d in batch:
                    if not d or not d.get("visible"):
                        continue
                    if d.get("is_link") and link_count >= _LINK_CAP:
                        continue
                    p = d.get("css") or "?"
                    key = f"{fi}:{p}"
                    if key in seen:
                        continue
                    seen.add(key)
                    if d.get("is_link"):
                        link_count += 1
                    els.append({"fi": fi, "css": p, **d})
            # pointer_leaves 补充：cursor:pointer 的裸元素（登录 span 等），批量 evaluate_all 一次算
            try:
                for item in _pointer_leaves(frame):
                    try:
                        batch = frame.locator(item["path"]).evaluate_all(_BATCH_EL_JS)
                    except Exception:
                        continue
                    for d in batch:
                        if not d or not d.get("visible"):
                            continue
                        p = d.get("css") or "?"
                        key = f"{fi}:{p}"
                        if key in seen:
                            continue
                        seen.add(key)
                        els.append({"fi": fi, "css": p, **d})
            except Exception:
                continue
        except Exception:
            continue  # 帧被移除（Frame detached）/加载中：跳过整帧不崩
    return els


# 一次 evaluate 批量取 tag/role/label（含祖先 title 兜底），替代逐属性多次往返。
# 注意：用 textContent 而非 innerText——innerText 每次调用都强制全页布局，
# 视频页布局巨大时 78 个元素就是 60s+；textContent 是纯文本拼接，不触发布局。
_DESCRIBE_JS = r"""(e) => {
    const tag = e.tagName.toLowerCase();
    const role = e.getAttribute('role');
    let label = (e.getAttribute('placeholder') || e.getAttribute('aria-label')
        || e.getAttribute('title')
        || (e.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 100)
        || e.getAttribute('name') || '');
    if (!label && (tag === 'video' || tag === 'audio')) label = tag + ' player';
    // bilibili 视频操作栏：className 语义化（投币/点赞/收藏是 div，无 title/aria-label）
    const cls = typeof e.className === 'string' ? e.className : '';
    if (!role) {
        if (/\bvideo-coin\b/.test(cls)) label = '投币 ' + label;
        else if (/\bvideo-like\b/.test(cls)) label = '点赞 ' + label;
        else if (/\bvideo-fav\b/.test(cls)) label = '收藏 ' + label;
        else if (/\bvideo-share\b/.test(cls)) label = '分享 ' + label;
    }
    if (!label || (label.length < 8 && /^[\d.,万kK%+ ]+$/.test(label))) {
        let p = e.parentElement;
        for (let i = 0; p && i < 3; i++, p = p.parentElement) {
            const t = p.getAttribute && (p.getAttribute('title') || p.getAttribute('aria-label'));
            if (t) { label = label ? t + ' ' + label : t; break; }
        }
    }
    return { tag, role, label };
}"""


def _describe(el):
    try:
        d = el.evaluate(_DESCRIBE_JS)
        kind = d["role"] or {"a": "link", "input": "input", "textarea": "input",
                             "select": "select", "video": "video", "audio": "audio"}.get(d["tag"], d["tag"])
        return f"<{kind}> {(d['label'] or '').strip()[:60]}"
    except Exception:
        return "<el> ?"


def _css_path(el):
    """稳定的 CSS 路径：优先 #id，否则 tag:first-class:nth-of-type，逐级上溯（截断防超长）。"""
    try:
        return el.evaluate(_CSS_PATH_JS)
    except Exception:
        return "?"


# 与 _css_path 内联 JS 等价，独立出来供 _list_interactive_elements 批量复用
_CSS_PATH_JS = r"""(e) => {
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
}"""


_LIST_LIMIT = 120  # 列表上限：视频页可达 119 元素，80 会把互动栏（投币按钮 #110）截掉

def _list_interactive_elements(page, limit=_LIST_LIMIT):
    """基于 _visible_elements 的 dict 直接格式化（含可见性过滤、投币/点赞语义、CSS路径）。"""
    out = []
    for i, d in enumerate(_visible_elements(page)):
        if i >= limit:
            break
        kind = d.get("role") or {"a": "link", "input": "input", "textarea": "input",
                                 "select": "select", "video": "video", "audio": "audio"}.get(d.get("tag"), d.get("tag"))
        out.append(f"[{i+1}] <{kind}> {(d.get('label') or '').strip()[:60]} — css={d.get('css') or '?'}")
    return out


def _find_element(page, target):
    """target 支持：元素清单编号、css=<选择器>、xpath=<表达式>。返回 Playwright locator。
    编号按 _visible_elements 的顺序映射到 (frame, css_path)，重建 locator（跨帧可用）。"""
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
        d = els[idx - 1]
        frame = page.frames[d["fi"]] if d.get("fi") is not None and d["fi"] < len(page.frames) else page
        return frame.locator(d["css"]).first
    raise ValueError(f"元素编号 {idx} 超出范围 (1-{len(els)})。用最新返回的元素清单。")


def _pause_videos(page):
    """暂停页面上所有 <video>（含 iframe 内嵌播放器）——视频播放占满主线程会让
    每次 Playwright 往返从 5ms 飙到 50-100ms，元素列表构建从 4s 拖到 40s+。"""
    try:
        for f in page.frames:
            _evaluate(f, "() => { for (const v of document.querySelectorAll('video')) { v.pause(); } }")
    except Exception:
        pass  # 暂停失败不阻塞页面状态


def _page_state(page, locate=None):
    """返回 URL + Title + 元素清单 + 页面文本（截断 4000）。
    locate 非空时，把匹配的元素用 ★ 标注并置顶，模型不用翻完整清单找目标。
    分段计时每步实时写 tmp/browse_timing.log——卡在哪一步日志就停在哪步（先写后跑，不留死角）。"""
    import time as _t
    import os as _os
    _LOG_PATH = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tmp", "browse_timing.log")

    def _log(line):
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    _mark = _t.monotonic()
    _log("== _page_state start ==")

    _pause_videos(page)  # 先停视频，让后续往返恢复低延迟
    _log(f"  [_pause_videos] {_t.monotonic()-_mark:.1f}s")
    _mark = _t.monotonic()

    out = [f"URL: {page.url}", f"Title: {page.title() or ''}"]
    elements = _list_interactive_elements(page)
    _log(f"  [_list_interactive_elements] {_t.monotonic()-_mark:.1f}s ({len(elements)} els)")
    _mark = _t.monotonic()
    if elements:
        if locate:
            kw = locate.strip().lower()
            hits = [e for e in elements if kw in e.lower()]
            if hits:
                out.append(f"★ 命中『{locate}』({len(hits)}):")
                out.extend("★ " + e for e in hits)
        out.append("Elements:")
        out.extend(elements)
    body = _extract_page_text(page)
    _log(f"  [_extract_page_text] {_t.monotonic()-_mark:.1f}s")
    if len(body) > 4000:
        body = body[:4000] + f"\n... (truncated, {len(body)} chars total)"
    out.append(f"--- Page Content ---\n{body}")
    _log(f"== _page_state total {_t.monotonic()-_mark:.1f}s ==")
    return "\n".join(out)


def _do_browser(action, url, target, text, profile, headless, locate=None):
    """执行单个浏览器动作；失败抛异常，由 browser_act_mock 统一格式化 / 死会话重试。"""
    global _last_active
    page, ctx, browser = _get_browser_page(profile, headless)
    out = [f"[browser_act] action: {action}"]
    if action not in ("open", "close") and page.url.strip().rstrip("/") in ("about:blank", ""):
        return "[Error] Browser page is still blank (about:blank). Call action='open' with a URL first."
    if action == "open":
        target_url = _validate_url(url)
        # 归一化比较 scheme://netloc/path?query：bilibili 会把 /video/X 重定向成 /video/X/（尾部斜杠）
        def _norm(u):
            p = urlparse(u)
            return f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}?{p.query}"
        if page.url.strip() and _norm(page.url) == _norm(target_url):
            # 已在此 URL：跳过重复 goto（bilibili SPA 重复导航同一视频会卡 networkidle + 元素遍历 60s+）
            # 但残留会话的页面可能停在操作栏未渲染的状态，等它出现（同 goto 分支）
            try:
                page.wait_for_selector(".video-toolbar-left-item", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(300)
            out.append("OK: already on this URL")
        else:
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass  # networkidle 对长轮询页面是常态超时，不算错误，继续
            # 等视频操作栏（点赞/投币）渲染——bilibili 异步加载，过早返回只看到 2 个元素
            try:
                page.wait_for_selector(".video-toolbar-left-item", timeout=8000)
            except Exception:
                pass  # 某些页面无操作栏，不阻塞
            page.wait_for_timeout(300)
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
        # 用 JS 滚轮而非 PageDown：键盘焦点落在播放器时 PageDown 会触发视频切换快捷键
        # （bilibili 实测两次 scroll 就跳到下一视频），window.scrollBy 只滚动页面不受劫持。
        delta = -1 * 900 if str(text).lower() in ("up",) else 900
        _evaluate(page, f"window.scrollBy(0, {delta});")
        page.wait_for_timeout(300)
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
        out.append(_page_state(page, locate=locate))
    return "\n".join(out)


def browser_act_mock(action, url="", target=None, text="", profile="", headless=True, locate=None):
    """单步浏览器操作：每次只做一个动作，返回页面状态 + 可交互元素清单。
    headless=False 会显示浏览器窗口（登录/验证码等需要人工操作时用）。
    locate=<关键词> 时返回清单把匹配元素 ★ 置顶（找目标控件时用，省去翻完整清单）。
    死会话（用户关了窗口）自动丢弃并重建重试一次。"""
    def work():
        try:
            return _do_browser(action, url, target, text, profile, headless, locate=locate)
        except Exception as e:
            if "has been closed" in str(e):
                _browser_sessions.pop(profile or _last_active or "__default__", None)
                try:
                    return _do_browser(action, url, target, text, profile, headless, locate=locate)
                except Exception as e2:
                    return f"[Error] browser_act failed: {e2}"
            return f"[Error] browser_act failed: {e}"
    return _executor.call(work)
