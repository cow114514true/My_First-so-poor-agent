"""后端配置：模型/base_url/client/WORK_DIR 锚点。"""
import os
import httpx
from openai import OpenAI

WORK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # agent/（包上一层，即项目根）
_PROFILES_DIR = os.path.join(WORK_DIR, "profiles")  # 浏览器登录态 profile 持久化目录
_TAVILY_API_KEY = os.environ.get("TAVILY_KEY", "")

def _is_local_backend():
    return os.environ.get("MODEL_BACKEND", "").startswith("local")


def check_local_llm(timeout=1.0):
    """检测本地 llama.cpp 服务端口是否可达。返回 (reachable: bool, detail: str)。
    非 local 后端直接返回 (True, 'cloud')。TCP 连接即够——端口通即认为服务在（模型是否加载好由后续 API 调用判断）。"""
    if not _is_local_backend():
        return True, "cloud"
    from urllib.parse import urlparse
    import socket
    base_url = cfg["base_url"].rstrip("/")
    root = base_url[:-3] if base_url.endswith("/v1") else base_url  # 去掉 OpenAI 兼容 /v1 前缀
    try:
        p = urlparse(root)
        socket.create_connection((p.hostname or "127.0.0.1", p.port or 8080), timeout=timeout).close()
        return True, "alive"
    except Exception:
        return False, "unreachable"


def _backend_config():
    if os.environ.get("MODEL_BACKEND") == "local-3.5":
        return {
            "api_key": "sk_local",
            "base_url": os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
            "model": os.environ.get("LLM_MODEL", "qwen-3.5-9B"),
            "extra_kwargs": {},
            "max_tool_rounds": 12,
            "ctx": 16384
        }
    elif os.environ.get("MODEL_BACKEND") == "local-3.6":
        return {
            "api_key": "sk_local",
            "base_url": os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
            "model": os.environ.get("LLM_MODEL", "qwen-3.6-35B"),
            "extra_kwargs": {},
            "max_tool_rounds": 12,
            "ctx": 8192
        }
    else:
        return {
            "api_key": os.environ.get("DS_KEY"),
            "base_url": os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
            "model": "deepseek-v4-flash",
            "extra_kwargs": {
                "reasoning_effort": "high",
                "extra_body": {"thinking": {"type": "enabled"}},
            },
            "max_tool_rounds": 12,
            "ctx": 65536
        }


cfg = _backend_config()
_current_raw = os.environ.get("MODEL_BACKEND", "<unset>")
print(f"[backend] MODEL_BACKEND={_current_raw!r} → model={cfg['model']} base_url={cfg['base_url']}", flush=True)
CURRENT_MODEL = cfg["model"]
# 流式连接 read 超时：120s 内无 chunk 即判定断流，避免 TCP 层无限死等
client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"],
                timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0))
