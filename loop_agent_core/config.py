"""后端配置：模型/base_url/client/WORK_DIR 锚点。"""
import os
import httpx
from openai import OpenAI

# headroom 默认从 HuggingFace 下载 tokenizer（SSL 失败 + 重试 5 次 ≈ 10s 卡顿）。
# 0 = 完全禁用网络下载，回退到本地 tiktoken 估算。设此值后首次 import headroom 即生效。
os.environ.setdefault("HEADROOM_HF_TOKENIZER_LOAD_TIMEOUT_SECS", "0")

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
                "reasoning_effort": "medium",
                "extra_body": {"thinking": {"type": "enabled"}},
            },
            "max_tool_rounds": 16,
            "ctx": 65536
        }


def _maybe_wrap_headroom(raw_client):
    """headroom-ai 接入点。HEADROOM_ENABLED=1（默认）启用；HEADROOM_MODE 默认 audit（只记录/分析，
    不改请求），设 optimize 才真正压缩上下文。任何失败静默回退原 client，不影响主流程。"""
    if os.environ.get("HEADROOM_ENABLED", "1") != "1":
        return raw_client
    try:
        from headroom.client import HeadroomClient
        from headroom.providers import OpenAICompatibleProvider
        mode = os.environ.get("HEADROOM_MODE", "audit")
        provider = OpenAICompatibleProvider(base_url=cfg["base_url"], api_key=cfg["api_key"],
                                            default_model=cfg["model"])
        # Windows 下默认 sqlite:///<tempdir> 会解析出畸形路径（\\C:\...），显式给正斜杠绝对路径。
        # 用 JSONL 而非 SQLite：JSONL 纯文件追加无线程约束，避免 worker 线程跨线程复用连接报错。
        db_path = os.path.join(WORK_DIR, "tmp", "headroom.jsonl").replace(os.sep, "/")
        wrapped = HeadroomClient(original_client=raw_client, provider=provider,
                                 default_mode=mode, store_url=f"jsonl://{db_path}",
                                 model_context_limits={cfg["model"]: cfg["ctx"]})
        print(f"[headroom] wrapped client: mode={mode}, model={cfg['model']}", flush=True)
        return wrapped
    except Exception as e:
        print(f"[headroom] init failed ({e!r}), using raw client", flush=True)
        return raw_client


cfg = _backend_config()
_current_raw = os.environ.get("MODEL_BACKEND", "<unset>")
print(f"[backend] MODEL_BACKEND={_current_raw!r} → model={cfg['model']} base_url={cfg['base_url']}", flush=True)
CURRENT_MODEL = cfg["model"]
# 流式连接 read 超时：120s 内无 chunk 即判定断流，避免 TCP 层无限死等
client = _maybe_wrap_headroom(OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"],
                                     timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)))
