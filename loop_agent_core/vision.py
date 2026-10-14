"""本地视觉模型接口：analyze_image 工具（Qwen VLM via llama.cpp，OpenAI 兼容接口）。

请求出口走 loop_agent_core/llm.py 的 `llm.get().create(kind="vision", ...)`：
视觉调用也必须被 trace 记录 / 可被回放接管，否则回放时截图分析会偷偷真跑。
`_vision_cfg()` 仍然决定 base_url/model（本地 llama.cpp 或 luna 网关）。
"""
import base64
import os

from openai import OpenAI

from .config import WORK_DIR
from . import confirm
from . import llm

_MIME = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "bmp": "bmp"}
DEFAULT_Q = "描述这张图片的内容。"
_client_cache = None


def _is_real_sdk() -> bool:
    """`OpenAI` 还是真的 openai SDK 吗？测试会把它换成 _FakeOpenAI（见下面 _local_client）。"""
    return getattr(OpenAI, "__name__", "") == "OpenAI" and "openai" in getattr(OpenAI, "__module__", "")


def _local_client():
    """视觉后端的 OpenAI 客户端（惰性、缓存）。

    `vision.OpenAI` 是测试的替换点（tests/test_vision_tool.py 逐个用例换成 _FakeOpenAI）。
    被换成假对象时**不缓存**：缓存会把第一个用例的假客户端留给后面所有用例。
    """
    global _client_cache
    cfg = _vision_cfg()
    if not _is_real_sdk():
        return OpenAI(api_key=cfg.get("api_key", "sk_local"), base_url=cfg["base_url"])
    if _client_cache is None:
        _client_cache = OpenAI(api_key=cfg.get("api_key", "sk_local"), base_url=cfg["base_url"])
    return _client_cache


class _CfgTransport:
    """视觉出口的 transport：把 llm 网关的调用落到 `_local_client()` 上。

    `_local_client()` 是测试替换 `vision.OpenAI` 的注入点；默认配置里的 base_url/api_key
    由 `_vision_cfg()` 提供（llm.profile("vision") 不再重复决定）。
    """
    def create(self, kind, cfg, kwargs):
        kwargs = dict(kwargs)
        kwargs.pop("model", None)
        return _local_client().chat.completions.create(
            model=_vision_cfg()["model"], **kwargs)


_vision_llm = None


def _llm():
    global _vision_llm
    if _vision_llm is None:
        _vision_llm = llm.get(_CfgTransport())
    return _vision_llm



def _vision_cfg():
    """视觉后端配置。切换：VISION_BACKEND=local|luna（默认 local）。
    luna 模式默认走 ofox 网关，key 由 VISION_API_KEY 提供（setx 设一次即持久）。"""
    if os.environ.get("VISION_BACKEND") == "luna":
        key = os.environ.get("VISION_API_KEY", "")
        if not key:
            raise RuntimeError("VISION_BACKEND=luna 但 VISION_API_KEY 未设置。"
                               "先执行一次: setx VISION_API_KEY sk-of-...")
        return {
            "base_url": os.environ.get("VISION_BASE_URL", "https://api.ofox.io/v1"),
            "model": os.environ.get("VISION_MODEL", "openai/gpt-5.6-luna"),
            "api_key": key,
            "max_tokens": int(os.environ.get("VISION_MAX_TOKENS", "1024")),
        }
    return {
        "base_url": os.environ.get("VISION_BASE_URL", "http://127.0.0.1:8080/v1"),
        "model": os.environ.get("VISION_MODEL", "Qwen3.5-9B"),
        "max_tokens": int(os.environ.get("VISION_MAX_TOKENS", "1024")),
    }


def _resolve_image_path(path):
    # 模型常写 Windows 惯例路径 C:\Users\%USERNAME%\...，需展开环境变量
    path = os.path.expandvars(path)
    if os.path.isabs(path):
        return os.path.abspath(path)
    return os.path.abspath(os.path.join(WORK_DIR, path))


def analyze_image_mock(path, question=None, purpose=""):
    """读本地图片 → 本地视觉模型 → 返回文字分析。确认门把关（TUI 弹窗）。

    图片路径不走 _resolve_path 沙箱：图片通常在 WORK_DIR 外（截图/照片），
    由确认门而非路径白名单做安全边界。
    """
    resolved = _resolve_image_path(path)
    if not os.path.exists(resolved):
        return f"[Error] Image not found: {resolved}"
    if not confirm.request("analyze_image", resolved, purpose):
        return "[Denied by user]"
    ext = os.path.splitext(resolved)[1].lstrip(".").lower() or "png"
    mime = _MIME.get(ext, ext)
    try:
        with open(resolved, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except OSError as e:
        return f"[Error] Cannot read image: {e}"
    cfg = _vision_cfg()
    try:
        resp = _llm().create(
            kind="vision",
            model=cfg["model"],
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
                {"type": "text", "text": question or DEFAULT_Q},
            ]}],
            max_tokens=cfg["max_tokens"],
            stream=False,
        )
    except Exception as e:
        return (f"[Error] Vision request failed: {e}. "
                "Confirm the vision server is running (start_local_llm.bat, model 3.5).")
    content = resp.choices[0].message.content
    reasoning = getattr(resp.choices[0].message, "reasoning_content", None)
    return content or reasoning or "[Model returned no content]"
