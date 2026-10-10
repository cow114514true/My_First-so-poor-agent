"""Tavily 搜索/读页工具（stdlib urllib 实现，零新依赖）。"""
import os
import json
import urllib.request
import urllib.error

from .config import _TAVILY_API_KEY

_SEARCH_URL  = "https://api.tavily.com/search"
_EXTRACT_URL = "https://api.tavily.com/extract"


# ---------------------------------------------------------------------------
# 关键修复：显式构造 opener，禁用系统代理（或在显式设置了环境变量时使用它）
# ---------------------------------------------------------------------------
def _build_opener() -> urllib.request.OpenerDirector:
    proxy = (
        os.environ.get("https_proxy")
        or os.environ.get("http_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    # 空 dict = 禁用一切代理（包括 Windows 注册表里的 IE 代理）
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(url, payload, timeout=30):
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if payload.get("api_key"):
        headers["Authorization"] = f"Bearer {payload['api_key']}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = _build_opener()
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------
def search_web_mock(query, max_results=5, search_depth="basic"):
    """网页搜索，返回标题/URL/内容片段列表。"""
    if not _TAVILY_API_KEY:
        return "[Error] TAVILY_KEY 未设置。设置环境变量后重试。"

    max_results = min(max(1, int(max_results)), 20)  # 限制 1~20

    try:
        data = _post(_SEARCH_URL, {
            "api_key": _TAVILY_API_KEY,
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
        })
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            pass
        return f"[Error] 搜索失败 HTTP {e.code}: {detail}"
    except urllib.error.URLError as e:
        return f"[Error] 搜索失败 (网络): {e.reason}"
    except Exception as e:
        return f"[Error] 搜索失败: {e}"

    if not isinstance(data, dict):
        return f"[Error] 响应格式异常: {str(data)[:200]}"

    results = data.get("results", [])
    if not results:
        return "[Error] 搜索无结果，换个 query 或稍后再试。"

    return "搜索结果:\n" + "\n".join(
        f"[{i + 1}] {r.get('title', '')}\n"
        f"    {r.get('url', '')}\n"
        f"    {(r.get('content') or '')[:300]}"
        for i, r in enumerate(results)
    )


# ---------------------------------------------------------------------------
# 读页
# ---------------------------------------------------------------------------
def fetch_url_mock(url, max_chars=8000):
    """读指定网页正文（Tavily extract 清洗版）。"""
    if not _TAVILY_API_KEY:
        return "[Error] TAVILY_KEY 未设置。设置环境变量后重试。"

    try:
        data = _post(_EXTRACT_URL, {
            "api_key": _TAVILY_API_KEY,
            "urls": [url],
        })
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            pass
        return f"[Error] 读页失败 HTTP {e.code}: {detail}"
    except urllib.error.URLError as e:
        return f"[Error] 读页失败 (网络): {e.reason}"
    except Exception as e:
        return f"[Error] 读页失败: {e}"

    if not isinstance(data, dict):
        return f"[Error] 响应格式异常: {str(data)[:200]}"

    results = data.get("results", [])
    if not results:
        return f"[Error] extract 无返回: {url}"

    res = results[0]
    content = (res.get("content") or res.get("raw_content") or "").strip()
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n...(截断, 共 {len(content)} chars)"

    return f"--- {res.get('url')} ---\n{res.get('title', '')}\n{content}"