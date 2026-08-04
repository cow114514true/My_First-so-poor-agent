"""Token 估算：DeepSeek 官方 tokenizer / 本地 /tokenize / 启发式。"""
import json
import os

from .config import _is_local_backend, WORK_DIR

_TOKENIZER_CACHE = None


def _char_heuristic(text):
    """ASCII ≈ 1 token / 4 chars, CJK ≈ 1 token / char. Fallback for gate decisions."""
    cjk = sum(1 for ch in text if ord(ch) > 0x2E80)
    return cjk + (len(text) - cjk) // 4 + 1


def _load_ds_tokenizer():
    global _TOKENIZER_CACHE
    if _TOKENIZER_CACHE is None:
        from tokenizers import Tokenizer
        _TOKENIZER_CACHE = Tokenizer.from_file(os.path.join(WORK_DIR, "tokenizer.json"))
    return _TOKENIZER_CACHE


def _local_tokenize(text):
    """Exact token count from the local llama.cpp server's /tokenize endpoint."""
    base = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    server = base[:-3] if base.endswith("/v1") else base
    import urllib.request
    req = urllib.request.Request(
        server + "/tokenize",
        data=json.dumps({"content": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return len(data.get("tokens", []))


def estimate_tokens(text):
    """Token count: DeepSeek official tokenizer / local /tokenize; heuristic on failure."""
    if not text:
        return 0
    if _is_local_backend():
        try:
            return _local_tokenize(text)
        except Exception:
            return _char_heuristic(text)
    try:
        return len(_load_ds_tokenizer().encode(text).ids)
    except Exception:
        return _char_heuristic(text)


def _read_budget():
    """Per-backend read gate budget. Env-overridable."""
    if _is_local_backend():
        return int(os.environ.get("READ_BUDGET_LOCAL", "2000"))
    return int(os.environ.get("READ_BUDGET_DS", "6000"))
