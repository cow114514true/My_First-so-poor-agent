"""Tavily 搜索/读页工具（stdlib urllib 实现，零新依赖）。"""
import json
import urllib.request

from .config import _TAVILY_API_KEY

_SEARCH_URL = "https://api.tavily.com/search"
_EXTRACT_URL = "https://api.tavily.com/extract"


def _post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def search_web_mock(query, max_results=5, search_depth="basic"):
    """网页搜索，返回标题/URL/内容片段列表。"""
    if not _TAVILY_API_KEY:
        return "[Error] TAVILY_KEY 未设置。设置环境变量后重试。"
    try:
        data = _post(_SEARCH_URL, {
            "api_key": _TAVILY_API_KEY, "query": query,
            "max_results": max_results, "search_depth": search_depth,
        })
    except Exception as e:
        return f"[Error] 搜索失败: {e}"
    results = data.get("results", [])
    if not results:
        return "[Error] 搜索无结果，换个 query 或稍后再试。"
    return "搜索结果:\n" + "\n".join(
        f"[{i+1}] {r.get('title', '')}\n    {r.get('url', '')}\n    {r.get('content', '')[:300]}"
        for i, r in enumerate(results))


def fetch_url_mock(url, max_chars=8000):
    """读指定网页正文（Tavily extract 清洗版）。"""
    if not _TAVILY_API_KEY:
        return "[Error] TAVILY_KEY 未设置。设置环境变量后重试。"
    try:
        data = _post(_EXTRACT_URL, {"api_key": _TAVILY_API_KEY, "urls": [url]})
    except Exception as e:
        return f"[Error] 读页失败: {e}"
    results = data.get("results", [])
    if not results:
        return f"[Error] extract 无返回: {url}"
    res = results[0]
    content = (res.get("content") or res.get("raw_content") or "").strip()
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n...(截断, 共 {len(content)} chars)"
    return f"--- {res.get('url')} ---\n{res.get('title', '')}\n{content}"
