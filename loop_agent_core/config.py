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


_compact_k_cache = None


def _compact_k():
    """上下文压缩旋钮（惰性求值 + 缓存）。

    **必须惰性**：测试在 import 之后才改环境变量，import 时定死会读不到。
    同时保证"不新增第二套 ctx 计算"：阈值口径一律是 context._prune_cap() * trigger_ratio。
    """
    global _compact_k_cache
    if _compact_k_cache is not None:
        return _compact_k_cache
    local = _is_local_backend()

    def _envi(key, default):
        v = os.environ.get(key)
        return int(v) if v not in (None, "") else default

    def _envf(key, default):
        v = os.environ.get(key)
        try:
            return float(v) if v not in (None, "") else default
        except ValueError:
            return default

    _compact_k_cache = {
        # L0 去重
        "dedup_enabled": _envi("DEDUP_ENABLED", 1),
        "dedup_min_chars": _envi("DEDUP_MIN_CHARS", 1000),
        "dedup_min_saved": _envi("DEDUP_MIN_SAVED", 200),
        # L1 摘要
        "compact_enabled": _envi("COMPACT_ENABLED", 1),
        # 0.5 而非 0.7：阈值越晚，单次摘要输入越大、保留率越低（arXiv 2605.23296：96k 输入仅保留 ~3%）
        "trigger_ratio": _envf("COMPACT_TRIGGER_RATIO", 0.5),
        # 单次摘要输入的硬上限；本地窗口小，取一半口径
        "block_tokens": _envi("COMPACT_BLOCK_TOKENS", 4000 if local else 8000),
        "keep_rounds": _envi("COMPACT_KEEP_ROUNDS", 2 if local else 3),
        # 输出规模靠结构性控制（max_tokens + 块数），不靠 prompt 请求"写详细点"。
        # 1500 实测不够：一次 ~7.6k token 的真实块，DeepSeek 产出 1761（含 717 思考）就被
        # finish_reason=length 截断，缺 3 个小标题 → validate_summary 不过 → 整次压缩作废。
        # 论文（arXiv 2605.23296）说摘要**输出几乎不随输入变化**，所以这个预算不能按块大小缩放，
        # 必须按"不变输出量 + 思考开销"给足。
        "max_tokens": _envi("COMPACT_MAX_TOKENS", 1500 if local else 3000),
        # 截断时的单次升级上限（只升一次）：宁可多花一次调用，也不要整次压缩白费
        "max_tokens_hard": _envi("COMPACT_MAX_TOKENS_HARD", 2000 if local else 6000),
        "min_reclaim": _envf("COMPACT_MIN_RECLAIM", 0.15),
        # 频率预算：同步压缩可占端到端耗时最高 62%，本地后端尤其不能频繁压
        "min_rounds": _envi("COMPACT_MIN_ROUNDS", 3),
        "max_per_session": _envi("COMPACT_MAX_PER_SESSION", 20),
        # 失败退避：连续失败 max_fails 次即本会话停用 L1（避免"每次工具调用重试一次"的
        # 刷屏 + 空烧 token；实测踩到）。退避轮数 = min_rounds * 2^(失败次数-1)，封顶 max_backoff。
        "max_fails": _envi("COMPACT_MAX_FAILS", 3),
        "max_backoff": _envi("COMPACT_MAX_BACKOFF", 32),
        "archive_max_chars": _envi("COMPACT_ARCHIVE_MAX_CHARS", 200000),
    }
    try:
        from . import trace
        # limits 事件在这里发（§4.3）：压缩旋钮是"上下文里到底存了什么"的主要决定者，
        # 回放重算 result_stored / 摘要块时用的就是这些值。
        trace.emit_limits(site="compact_k")
    except Exception:  # noqa: BLE001
        pass
    return _compact_k_cache


def reset_compact_k_cache() -> None:
    """清掉压缩旋钮缓存（测试改环境变量后必须调，否则读到的还是上一个用例的值）。"""
    global _compact_k_cache
    _compact_k_cache = None


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
            # DS_KEY 缺失不在这里报错，而是在模块末尾构造 OpenAI() 时抛
            # OpenAIError: Missing credentials —— 于是"只想 import 模块跑纯逻辑自检"
            # 也会失败。跑 run_tests.py / tests/ 下任意套件前先给个假值即可（不触网）：
            #   set DS_KEY=sk-dummy   /   $env:DS_KEY="sk-dummy"
            "api_key": os.environ.get("DS_KEY"),
            "base_url": os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
            "model": "deepseek-v4-flash",
            "extra_kwargs": {
                "reasoning_effort": "medium",
                "extra_body": {"thinking": {"type": "enabled"}},
            },
            # 摘要器专用：deepseek-v4-flash **默认就开思考**（实测 reasoning_tokens 680~717，
            # 白吃掉近一半输出预算且随时可能把摘要截断）。摘要是有界的机械压缩任务，思考无收益。
            # 只在云端后端下发：本地 llama.cpp 不认这个参数。后端若拒绝，薄壳会退回不带它的调用。
            "summary_extra_kwargs": {"extra_body": {"thinking": {"type": "disabled"}}},
            "max_tool_rounds": 16,
            "ctx": 65536
        }


def _maybe_wrap_headroom(raw_client):
    """headroom-ai 接入点。HEADROOM_ENABLED=1（默认）启用；HEADROOM_MODE 默认 audit（只记录/分析，
    不改请求），设 optimize 才真正压缩上下文。任何失败静默回退原 client，不影响主流程。"""
    if os.environ.get("HEADROOM_ENABLED", "1") != "1":
        return raw_client
    try:
        from headroom.client import HeadroomClient, HeadroomConfig, HeadroomMode
        from headroom.providers import OpenAICompatibleProvider
        mode = os.environ.get("HEADROOM_MODE", "audit")
        provider = OpenAICompatibleProvider(base_url=cfg["base_url"], api_key=cfg["api_key"],
                                            default_model=cfg["model"])
        # Windows 下默认 sqlite:///<tempdir> 会解析出畸形路径（\\C:\...），显式给正斜杠绝对路径。
        # 用 JSONL 而非 SQLite：JSONL 纯文件追加无线程约束，避免 worker 线程跨线程复用连接报错。
        # 可用 HEADROOM_STORE_URL 整体覆盖（测试要隔离 headroom 落盘，不能污染真实会话）。
        db_path = os.path.join(WORK_DIR, "tmp", "headroom.jsonl").replace(os.sep, "/")
        store_url = os.environ.get("HEADROOM_STORE_URL") or f"jsonl://{db_path}"
        try:
            from . import trace
            extensions = trace.headroom_extensions()
        except Exception:  # noqa: BLE001
            extensions = []
        hcfg = HeadroomConfig(pipeline_extensions=extensions)
        # HeadroomClient 只要收到 config，就**完全忽略** default_mode/store_url 这两个
        # 形参（源码里 `self._default_mode = config.default_mode` 无条件覆盖）。所以
        # 必须写进 config，否则 HEADROOM_MODE=optimize 会被静默降级成 audit：
        # 看上去在跑优化，实际一次压缩都没做，回放时才发现请求侧什么都没有。
        hcfg.default_mode = HeadroomMode(mode)
        hcfg.store_url = store_url
        wrapped = HeadroomClient(original_client=raw_client, provider=provider,
                                 default_mode=mode, store_url=store_url,
                                 model_context_limits={cfg["model"]: cfg["ctx"]},
                                 config=hcfg)
        # §2：包裹层的**包裹对象是传输层**，由 llm 网关取走并套在 chat 出口上。
        # 挂成模块级而不是让 llm 自己造：上下文限制/模式/store_url 只能有一处真相，
        # 两处各造一份会让"回放时 headroom 行为不一致"这种问题无处可查。
        global _HEADROOM_LAYER
        _HEADROOM_LAYER = wrapped
        print(f"[headroom] wrapped client: mode={mode}, model={cfg['model']}", flush=True)
        return wrapped
    except Exception as e:
        print(f"[headroom] init failed ({e!r}), using raw client", flush=True)
        return raw_client


_HEADROOM_LAYER = None


def headroom_layer():
    """当前会话的 headroom 包裹层（None = 未启用/装不上）。供 llm 网关的传输层使用。"""
    return _HEADROOM_LAYER


cfg = _backend_config()
_current_raw = os.environ.get("MODEL_BACKEND", "<unset>")
print(f"[backend] MODEL_BACKEND={_current_raw!r} → model={cfg['model']} base_url={cfg['base_url']}", flush=True)
CURRENT_MODEL = cfg["model"]
# 流式连接 read 超时：120s 内无 chunk 即判定断流，避免 TCP 层无限死等
client = _maybe_wrap_headroom(OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"],
                                     timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)))
