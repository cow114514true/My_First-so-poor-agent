"""通用浏览器底座：browser_act 单步操作（专用线程 + 持久会话 + 元素清单）。

取代 browse_web。核心改动：
- 专用浏览器线程：所有 Playwright 调用经 ds_web._executor 归入单一线程，
  根治 "cannot switch to a different thread"（真实会话里反复出现）。
- goto 改 domcontentloaded + 短超时，不再死等 networkidle（大页 60s 超时）。
- 每次调用只做一个动作，返回页面状态 + 可交互元素清单（编号），模型按编号驱动下一步。
"""
import hashlib
import json
import os
import re
from datetime import datetime
from urllib.parse import urlparse

from .debuglog import log_error

from .config import _PROFILES_DIR, WORK_DIR
from .ds_web import _executor, _get_playwright
from .net_guard import BlockedURL, validate_url as _validate_url

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


# 注意：URL 校验统一由 net_guard 提供（见文件顶部 import 的 `_validate_url`）。
# 这里**不要**再定义同名的 `_validate_url` —— 曾因残留旧实现把模块级名字覆盖掉，
# 导致 DNS 绕过域名（localtest.me 等）重新放行。回归测试见 test_security_hardening.py。


def _guard_page(page):
    """给新建页面挂网络守卫：所有出网请求（含 click 跳转/重定向/子资源）都过 DNS 校验。
    见 SECURITY_AUDIT_REPORT V5/V6 —— 仅校验 open 的参数是不够的。"""
    from .net_guard import block_private_requests
    try:
        return block_private_requests(page, log=lambda m: log_error("net_guard", m))
    except Exception as e:
        log_error("guard_page", e)
        return page


def _reconcile_pages(sess):
    """把会话的活跃页同步到 context 当前页列表：新开的标签页自动聚焦、关掉的页移除。
    返回当前活跃 page；所有页都关闭或 context 已死返回 None。必须在浏览器线程内调用。"""
    ctx = sess.get("ctx")
    try:
        live = [p for p in ctx.pages if not p.is_closed()]  # 只保留存活页，防御性过滤
    except Exception:
        return None
    if not live:
        return None
    seen = sess.setdefault("seen_pages", [])
    seen_ids = {id(p) for p in seen}
    new = [p for p in live if id(p) not in seen_ids]
    for p in new:
        seen.append(p)
    sess["seen_pages"] = [p for p in seen if not p.is_closed()]  # 清理已关闭的引用
    active = sess.get("active")
    if new:
        active = new[-1]                       # 新打开的标签页自动聚焦（target=_blank / 手动新开）
    elif active is None or active.is_closed():
        active = live[-1]                      # 活跃页已关：退回最后一个
    sess["active"] = active
    sess["page"] = active                      # 保持 sess["page"] 与活跃页一致
    return active


def _get_browser_page(profile: str, headless: bool):
    """Return (page, ctx, browser) for the given profile. Lazily creates and caches.
    MUST be called inside an _executor closure (owns the browser thread).
    支持多标签页：每次调用把活跃页同步到 context.pages，新开的标签页自动聚焦；
    用 browser_act 的 tabs / switch_tab 动作查看与切换。模型常漏传 profile → _last_active 回退。
    重建 context 时恢复 SPA 状态：init_script 回放 sessionStorage + 跳到上次 URL。
    用户手动关掉的窗口会使缓存会话失效——先做存活检测，死会话重建。"""
    global _last_active
    key = profile or (_last_active or "__default__")
    if key in _browser_sessions:
        sess = _browser_sessions[key]
        try:
            active = _reconcile_pages(sess)
            if active is None:
                raise Exception("all pages closed")
            _last_active = key
            return active, sess["ctx"], sess["browser"]
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
    _guard_page(page)  # 所有导航/子资源请求都过网络守卫（V5/V6）
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
                    # 会话恢复的 URL 来自磁盘上的 profiles/<name>_session.json，
                    # 属于可被篡改的持久化状态 → 恢复前必须校验（V6）。
                    _validate_url(url)
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    log_error("browse restore goto", e)
    _browser_sessions[key] = {"page": page, "ctx": ctx, "browser": browser,
                              "seen_pages": [page], "active": page}
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


# ── 动作后验证（阶段一）────────────────────────────────────────────────
# 目标：把"Playwright 点击成功（输入事件已发送）"与"业务动作成功（页面真的变了）"分离。
# 原则：验证只走低成本非视觉信号（URL / 标题 / 交互元素计数与标签集合 / 预期文本），
#      绝不重新逐元素往返——每 frame 一次 evaluate 的轻量指纹即可，避免重蹈历史性能坑。

_DOM_SIG_JS = r"""() => {
    const SELS = "input:not([type='hidden']), textarea, select, [contenteditable], [role='search'], [role='searchbox'], [role='combobox'], button, a[href], [role='button'], [role='link'], [role='textbox'], label, summary, [role='checkbox'], [role='radio'], [role='switch'], [role='tab'], [role='menuitem'], video, audio, [onclick], [tabindex]:not([tabindex='-1'])";
    let count = 0;
    const labels = [];
    const els = document.querySelectorAll(SELS);
    for (let i = 0; i < els.length && count < 500; i++) {
        const el = els[i];
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const cs = getComputedStyle(el);
        if (cs.visibility === 'hidden' || cs.display === 'none') continue;
        count++;
        let t = (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') || '').trim();
        if (!t) t = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 30);
        if (t && labels.length < 80) labels.push(t.toLowerCase());
    }
    return { count, labels };
}"""


def _dom_sig(page):
    """廉价 DOM 指纹：可见交互元素计数 + 短标签集合（每 frame 一次 evaluate）。
    够用即可——只用于检测"菜单展开了 / 列表变了 / 页面无反应"，不用于展示。"""
    total = 0
    labels = []
    for f in page.frames:
        try:
            r = _evaluate(f, _DOM_SIG_JS)
        except Exception:
            continue
        total += int(r.get("count") or 0)
        labels.extend(r.get("labels") or [])
    return {"count": total, "labels": sorted(labels)}


def _snapshot(page):
    """动作前后的一致性快照：URL + 标题 + 轻量 DOM 指纹。"""
    try:
        return {
            "url": page.url,
            "title": (page.title() or "").strip(),
            "sig": _dom_sig(page),
        }
    except Exception:
        return {"url": "", "title": "", "sig": {"count": -1, "labels": []}}


_VERIFY_ACTIONS = ("click", "type", "press_enter", "select_option")


def _verify_change(before, after, verify_mode, expected=""):
    """对比 before/after 快照，返回 (status, signals)。
    status ∈ {success, uncertain, not_checked}。语义级"真的失败"（页面报错/点错目标）
    留给更高成本的视觉验证（阶段四），阶段一只做确定性的低成本判定。
    verify_mode: none/url/dom/auto。"""
    if not verify_mode or verify_mode == "none":
        return "not_checked", []
    signals = []
    url_changed = bool(before["url"]) and before["url"] != after["url"] and bool(after["url"])
    title_changed = bool(before["title"]) and before["title"] != after["title"] and bool(after["title"])
    if url_changed:
        signals.append(f"URL changed ({before['url']} → {after['url']})")
    if title_changed:
        signals.append(f"title changed ({before['title'][:40]!r} → {after['title'][:40]!r})")
    b, a = before["sig"], after["sig"]
    count_changed = a.get("count", -1) >= 0 and b.get("count", -1) != a.get("count")
    bl, al = set(b.get("labels") or []), set(a.get("labels") or [])
    added, removed = al - bl, bl - al
    dom_changed = count_changed or bool(added or removed)
    if count_changed:
        signals.append(f"interactive elements {b.get('count')} → {a.get('count')}")
    if added:
        signals.append("new elements: " + ", ".join(sorted(added)[:5]))
    if removed:
        signals.append("removed elements: " + ", ".join(sorted(removed)[:5]))
    exp_hit = False
    if expected and expected.strip():
        exp = expected.strip().lower()
        hay = (after["title"] + " " + " ".join(a.get("labels") or [])).lower()
        exp_hit = exp in hay
        if exp_hit:
            signals.append(f"expected '{expected}' appeared")
    if verify_mode == "url":
        status = "success" if url_changed else "uncertain"
    elif verify_mode == "dom":
        status = "success" if (dom_changed or exp_hit) else "uncertain"
    else:  # auto：URL/标题/DOM/预期任一变化即视为成功
        status = "success" if (url_changed or title_changed or dom_changed or exp_hit) else "uncertain"
    return status, signals


def _verify_line(status, signals, expected=""):
    """把验证结果转成一行可读提示，喂给模型。"""
    if status == "not_checked":
        return ""
    if status == "success":
        return f"[verify] success: {'; '.join(signals) or 'page state changed'}"
    hint = f"；期望『{expected}』未出现" if expected and expected.strip() else ""
    return (f"[verify] uncertain: 页面未见明显变化{hint}。"
            f"可能是无效点击、目标被遮挡或异步加载未完成。"
            f"重新观察页面状态，确认目标后改用 css=/xpath= 或 visual_locate 重试，不要盲目重复相同动作。")


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


def _norm_url(u):
    """规范化 URL：去 fragment + 去尾部斜杠，保留 query。用作 element_id 的页面身份。"""
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}?{p.query}"


def _element_id(page_url, fi, d):
    """稳定元素身份：规范化 URL + frame + tag + role + label + css 的 sha1 前 6 位。
    同页重渲染（元素未移动）→ id 不变；导航到新 URL → id 失效，不静默复用。"""
    key = "|".join([_norm_url(page_url), str(fi), d.get("tag") or "", d.get("role") or "",
                    (d.get("label") or "").strip(), d.get("css") or ""])
    return "element:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:6]


def _visible_elements(page):
    """按固定选择器顺序收集可见元素——列表展示和编号点击必须用同一顺序。
    用 evaluate_all 批量求值（每个选择器一次往返），返回元素描述 dict 列表，
    由 _find_element 按编号重建 locator。跨 frame + 帧序去重 + 链接上限。"""
    els = []
    seen = set()
    link_count = 0
    page_url = page.url
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
                    els.append({"fi": fi, "css": p, "element_id": _element_id(page_url, fi, d), **d})
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
                        els.append({"fi": fi, "css": p, "element_id": _element_id(page_url, fi, d), **d})
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

# 元素 dict → 展示行。browser_act / observe_browser 共用，保证编号与格式一致。
_ELEMENT_KIND = {"a": "link", "input": "input", "textarea": "input",
                 "select": "select", "video": "video", "audio": "audio"}


def _format_element_line(i, d, show_id=False):
    """单条元素展示行：编号 + kind + label + css（可选附 element_id）。"""
    kind = d.get("role") or _ELEMENT_KIND.get(d.get("tag"), d.get("tag"))
    line = f"[{i+1}] <{kind}> {(d.get('label') or '').strip()[:60]} — css={d.get('css') or '?'}"
    if show_id and d.get("element_id"):
        line += f" (id={d['element_id']})"
    return line


def _format_elements(elements, limit=_LIST_LIMIT):
    """把 _visible_elements 的 dict 列表格式化为编号清单（纯函数，不触浏览器）。"""
    out = []
    for i, d in enumerate(elements):
        if i >= limit:
            break
        out.append(_format_element_line(i, d))
    return out


def _locate_hits(elements, kw, limit=_LIST_LIMIT):
    """返回命中关键词的元素展示行（★ 前缀 + 附 element_id），供 locate/focus 置顶。"""
    kw = (kw or "").strip().lower()
    hits = []
    for i, d in enumerate(elements):
        if i >= limit:
            break
        if kw and kw in _format_element_line(i, d).lower():
            hits.append("★ " + _format_element_line(i, d, show_id=True))
    return hits


def _list_interactive_elements(page, limit=_LIST_LIMIT):
    """基于 _visible_elements 的 dict 直接格式化（含可见性过滤、投币/点赞语义、CSS路径）。"""
    return _format_elements(_visible_elements(page), limit)


def _find_element(page, target):
    """target 支持：元素清单编号、element=<id>、css=<选择器>、xpath=<表达式>。返回 Playwright locator。
    编号按 _visible_elements 的顺序映射到 (frame, css_path)，重建 locator（跨帧可用）。
    element_id 跨调用稳定，但页面导航后旧 id 失效（id 含 URL）。"""
    if isinstance(target, str):
        low = target.strip()
        if low.startswith("css="):
            return page.locator(low[4:].strip()).first
        if low.startswith("xpath="):
            return page.locator("xpath=" + low[5:].strip()).first
        if low.startswith("element:"):
            eid = low[len("element:"):]
            for d in _visible_elements(page):
                if d.get("element_id") == "element:" + eid:
                    frame = page.frames[d["fi"]] if d.get("fi") is not None and d["fi"] < len(page.frames) else page
                    return frame.locator(d["css"]).first
            raise ValueError(f"element_id '{eid}' 在当前页面找不到——可能页面已导航，旧 id 失效。用最新元素清单的编号或 css= 重新定位。")
    try:
        idx = int(target)
    except (TypeError, ValueError):
        raise ValueError(f"target 必须是元素清单编号、element=<id>、css=<选择器> 或 xpath=<表达式>，拿到: {target!r}")
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
    els = _visible_elements(page)
    elements = _format_elements(els)
    _log(f"  [_visible_elements] {_t.monotonic()-_mark:.1f}s ({len(elements)} els)")
    _mark = _t.monotonic()
    if elements:
        if locate:
            hits = _locate_hits(els, locate)
            if hits:
                out.append(f"★ 命中『{locate}』({len(hits)}):")
                out.extend(hits)
        out.append("Elements:")
        out.extend(elements)
    body = _extract_page_text(page)
    _log(f"  [_extract_page_text] {_t.monotonic()-_mark:.1f}s")
    if len(body) > 4000:
        body = body[:4000] + f"\n... (truncated, {len(body)} chars total)"
    out.append(f"--- Page Content ---\n{body}")
    _log(f"== _page_state total {_t.monotonic()-_mark:.1f}s ==")
    return "\n".join(out)


def _do_browser(action, url, target, text, profile, headless, locate=None, verify="auto", expected=""):
    """执行单个浏览器动作；失败抛异常，由 browser_act_mock 统一格式化 / 死会话重试。
    verify=none|url|dom|auto 对状态变更动作做前后快照验证；expected 为期望出现的文本/标题。"""
    global _last_active
    page, ctx, browser = _get_browser_page(profile, headless)
    out = [f"[browser_act] action: {action}"]
    if action not in ("open", "close", "tabs", "switch_tab") and page.url.strip().rstrip("/") in ("about:blank", ""):
        return "[Error] Browser page is still blank (about:blank). Call action='open' with a URL first."
    need_verify = action in _VERIFY_ACTIONS and verify not in (None, "", "none")
    before = _snapshot(page) if need_verify else None
    if action == "open":
        target_url = _validate_url(url)
        # 归一化比较 scheme://netloc/path?query：bilibili 会把 /video/X 重定向成 /video/X/（尾部斜杠）
        if page.url.strip() and _norm_url(page.url) == _norm_url(target_url):
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
    elif action == "tabs":
        pages = list(ctx.pages)
        out.append(f"OK: {len(pages)} tab(s) open")
        for i, p in enumerate(pages):
            mark = " (active)" if p is page else ""
            try:
                u, t = p.url, (p.title() or "")
            except Exception:
                u, t = "(closed)", ""
            out.append(f"[{i+1}]{mark} URL: {u} — Title: {t}")
    elif action == "switch_tab":
        pages = list(ctx.pages)
        try:
            idx = int(target)
        except (TypeError, ValueError):
            idx = None
            out.append("[Error] switch_tab needs target=<tab index>, from the 'tabs' list")
        if idx is not None:
            if not (1 <= idx <= len(pages)):
                out.append(f"[Error] switch_tab: tab index {idx} out of range (1-{len(pages)})")
            else:
                sess = _browser_sessions.get(profile or _last_active or "__default__")
                if sess:
                    sess["active"] = pages[idx - 1]
                    sess["page"] = pages[idx - 1]
                page = pages[idx - 1]
                out.append(f"OK: switched to tab #{idx}")
    elif action == "close":
        key = profile or _last_active or "__default__"
        _browser_sessions.pop(key, None)
        if _last_active == key:
            _last_active = None
        browser.close()
        out.append("OK: closed browser session")
    else:
        raise ValueError(f"Unknown action: {action!r}. Valid: open, click, type, press_enter, press_key, hover, select_option, scroll, wait, back, screenshot, tabs, switch_tab, close")

    if action != "close":
        # 导航后复校验实际落地 URL：click/back/重定向都可能把页面带到被禁地址。
        # 路由拦截已 abort 这类请求，这里再确认一次并如实告知模型（V5/V6）。
        try:
            landed = (page.url or "").strip()
            if landed and landed.split(":", 1)[0] in ("http", "https"):
                _validate_url(landed)
        except BlockedURL as e:
            out.append(f"[net_guard] 页面被带到被禁止的地址，已停止后续观察：{e}")
            return "\n".join(out)
        except Exception:
            pass

        # 动作可能打开了新标签页（target=_blank）→ 重新同步活跃页，让本调用返回的页面状态反映新页
        sess = _browser_sessions.get(profile or _last_active or "__default__")
        if sess:
            new_active = _reconcile_pages(sess)
            if new_active is not None:
                page = new_active
        _save_profile(profile or _last_active or "__default__", ctx, page)
        if need_verify:
            status, signals = _verify_change(before, _snapshot(page), verify, expected)
            line = _verify_line(status, signals, expected)
            if line:
                out.append(line)
        out.append(_page_state(page, locate=locate))
    return "\n".join(out)


def browser_act_mock(action, url="", target=None, text="", profile="", headless=True, locate=None, verify="auto", expected=""):
    """单步浏览器操作：每次只做一个动作，返回页面状态 + 可交互元素清单。
    headless=False 会显示浏览器窗口（登录/验证码等需要人工操作时用）。
    locate=<关键词> 时返回清单把匹配元素 ★ 置顶（找目标控件时用，省去翻完整清单）。
    verify=none|url|dom|auto（默认 auto）：对 click/type/press_enter/select_option 做前后快照验证，
      返回 [verify] success/uncertain；expected= 期望出现的文本/标题（命中计为 success）。
    死会话（用户关了窗口）自动丢弃并重建重试一次。"""
    def work():
        try:
            return _do_browser(action, url, target, text, profile, headless, locate=locate, verify=verify, expected=expected)
        except Exception as e:
            if "has been closed" in str(e):
                _browser_sessions.pop(profile or _last_active or "__default__", None)
                try:
                    return _do_browser(action, url, target, text, profile, headless, locate=locate, verify=verify, expected=expected)
                except Exception as e2:
                    return f"[Error] browser_act failed: {e2}"
            return f"[Error] browser_act failed: {e}"
    return _executor.call(work)
