"""网络守卫：URL 校验 + DNS 解析（防 DNS rebinding / 域名绕过）+ 导航后复校验。

修复 SECURITY_AUDIT_REPORT V5/V6：
- 原 `_validate_url` 只比对字面量主机名，不做 DNS 解析 ——
  `localtest.me` / `127.0.0.1.nip.io` 这类"解析到环回地址的公网域名"可直接绕过。
- 原实现只有 `open` 一处校验；`click` / `back` / 会话恢复同样能改 URL 却完全不校验。

设计取舍：解析得出的 IP 只用于**判断**（不做 IP 直连替换），因此不改变 TLS SNI / Host 语义，
不会破坏 HTTPS 站点。DNS rebinding 的残余窗口由"每次导航后复校验实际落地 URL"覆盖。
"""
import ipaddress
import os
import socket
from urllib.parse import urlparse

# 显式放行的主机（本地开发用，如本机 llama.cpp / 调试站点）。
# 例：AGENT_ALLOW_HOSTS="127.0.0.1:8080,localhost:19387"
_ALLOW_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("AGENT_ALLOW_HOSTS", "").split(",")
    if h.strip()
}

# DNS 判定缓存：page.route 的拦截器对每个子资源请求都会调 validate_url，
# 不缓存的话每次都要 getaddrinfo，会明显拖慢页面加载。
_DNS_CACHE_MAX = 512
_DNS_CACHE: dict = {}


class BlockedURL(ValueError):
    """URL 被网络守卫拒绝。"""


def _host_is_explicitly_allowed(host: str, port) -> bool:
    if not _ALLOW_HOSTS:
        return False
    host = (host or "").lower()
    candidates = {host}
    if port:
        candidates.add(f"{host}:{port}")
    # 支持 "*.example.com" 通配
    for pat in _ALLOW_HOSTS:
        if pat in candidates:
            return True
        if pat.startswith("*.") and host.endswith(pat[1:]):
            return True
    return False


def _ip_is_blocked(ip) -> bool:
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _resolve_ips(host: str, port):
    """返回该主机解析出的全部 IP（去重）。解析失败返回空列表。带小缓存。"""
    key = (host.lower(), port)
    cached = _DNS_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        infos = socket.getaddrinfo(host, port or None, proto=socket.IPPROTO_TCP)
    except Exception:
        return []
    out = []
    for info in infos:
        addr = info[4][0]
        try:
            out.append(ipaddress.ip_address(addr))
        except ValueError:
            continue
    # 去重且保序
    seen, uniq = set(), []
    for ip in out:
        if ip not in seen:
            seen.add(ip)
            uniq.append(ip)
    if len(_DNS_CACHE) >= _DNS_CACHE_MAX:
        _DNS_CACHE.clear()
    _DNS_CACHE[key] = uniq
    return uniq


def clear_cache():
    """清空 DNS 判定缓存（测试/长会话用）。"""
    _DNS_CACHE.clear()


def validate_url(url: str) -> str:
    """校验 URL 允许访问；返回原 URL。被拒时抛 BlockedURL。

    规则：仅 http/https；显式放行主机优先；
    主机是字面量 IP → 必须为公网；主机是域名 → **解析出的每个 IP** 都必须是公网。
    """
    if not isinstance(url, str) or not url.strip():
        raise BlockedURL("empty URL")
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise BlockedURL(f"Only http/https allowed, got: {parsed.scheme!r}")

    host = (parsed.hostname or "").strip()
    if not host:
        raise BlockedURL(f"URL has no host: {url}")
    port = parsed.port

    if _host_is_explicitly_allowed(host, port):
        return url

    # 1) 字面量 IP：直接判定
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    else:
        literal_ip = ip
    if literal_ip is not None:
        if _ip_is_blocked(literal_ip):
            raise BlockedURL(f"Access denied: private/loopback network {host}")
        return url

    # 2) 域名：必须解析，且每个结果都必须是公网地址
    ips = _resolve_ips(host, port)
    if not ips:
        # 解析不了就不放行（原实现会把无法解析的域名放过去）
        raise BlockedURL(f"Access denied: cannot resolve host {host!r}")
    for ip in ips:
        if _ip_is_blocked(ip):
            raise BlockedURL(
                f"Access denied: {host} resolves to non-public address {ip}"
            )
    return url


def is_allowed(url: str) -> bool:
    """validate_url 的布尔版本。"""
    try:
        validate_url(url)
        return True
    except BlockedURL:
        return False


def _is_navigation(route) -> bool:
    """该请求是否为顶层文档导航（而非子资源加载）。"""
    try:
        return bool(route.request.is_navigation_request())
    except Exception:
        pass
    try:
        return route.request.resource_type == "document"
    except Exception:
        return False


def _classify(url: str, navigation: bool = False):
    """返回 'allow' | 'deny' | 'unknown'。

    需要区分"危险导航"和"无害子资源"：
    - `file:` 无论何时都必须拦（可读本地文件）。
    - `data:` / `blob:` 是普通页面的内联资源（图片/媒体），**子资源放行**，
      否则正常网页会被搞坏；但**顶层导航**必须拦（可在浏览器里执行任意脚本、绕过 open 校验）。
    - http/https：做 DNS 校验，非公网地址一律拦。
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "unknown"
    scheme = (parsed.scheme or "").lower()

    if scheme in ("file", "ftp", "ftps", "gopher", "vbscript"):
        return "deny"
    if scheme in ("javascript", "data", "blob"):
        return "deny" if navigation else "unknown"
    if scheme in ("http", "https"):
        return "allow" if is_allowed(url) else "deny"
    return "unknown"


def block_private_requests(page, log=None):
    """给 Playwright page 挂路由拦截：危险导航与指向非公网地址的请求一律 abort。

    这一层覆盖 `click` / 重定向 / 子资源等所有出网路径 —— 只校验 `open` 是不够的（V6）。
    必须在浏览器线程内调用。
    """

    def _handler(route):
        try:
            req_url = route.request.url
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass
            return

        verdict = _classify(req_url, navigation=_is_navigation(route))
        if verdict == "deny":
            if log:
                log(f"[net_guard] blocked request: {req_url}")
            try:
                route.abort()
            except Exception:
                pass
            return
        # allow（公网 http/https）与 unknown（内联 data:/blob: 等子资源）都放行
        try:
            route.continue_()
        except Exception:
            pass

    page.route("**/*", _handler)
    return page
