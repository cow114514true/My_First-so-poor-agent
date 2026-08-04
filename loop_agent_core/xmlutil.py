"""DSML/XML 清洗与解析：剥 DSML 污染、Qwen3 格式归一化、工具调用解析、垃圾判定。"""
import json
import re

# -- ponytail: response quality guard, prevents DSML/garbage from polluting messages --
# 污染形式：标签的 `<` 后插入了全角竖线(U+FF5C)包裹的 DSML 标记，如 `<｜｜DSML｜｜tool_calls>`。
# 既有 `|DSML|`(ASCII) 与全角变体一并处理，大小写不敏感。
_DSML_RE = re.compile(r"[|｜]+DSML[|｜]+", re.I)


def _strip_dsml(text):
    """去掉全角/ASCII 竖线包裹的 DSML 标记，恢复标签为可解析形式。"""
    if not text:
        return text
    return _DSML_RE.sub("", text)


def _is_garbage_content(text: str) -> bool:
    """True if the response looks like DSML garbage / XML leakage."""
    if not text or not text.strip():
        return True
    if _DSML_RE.search(text):
        return True
    # 先剥掉工具调用 XML，避免把本地模型的合法 <tool_call> 误判为垃圾
    t = _strip_toolcall_xml(text).strip()
    if not t:
        return True  # 剥干净后没有正文 → 只是工具调用 XML，不能当回复显示
    if "<DSML" in t or "<dsml" in t or "|DSML|" in t:
        return True
    if t.startswith("<?xml"):
        return True
    angle_count = t.count("<") + t.count(">")
    if len(t) > 200 and angle_count / len(t) > 0.3:  # 阈值放宽，避免误杀正常回复
        return True
    return False


_XML_TOOLCALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.S)
_XML_FN_RE = re.compile(r"<function=([^>\n]+)>", re.S)
_XML_PARAM_RE = re.compile(r"<parameter=([^>\n]+)>(.*?)</parameter>", re.S)

# Qwen3 系列把工具调用写成 <tool_calls><invoke name=".."><parameter name=".." string="true">..</parameter></invoke></tool_calls>，
# 与旧 <tool_call><function=..><parameter=..> 不同。归一化成旧格式，让解析/剥离共用一套正则。
_QWEN_TOOLCALL_RE = re.compile(r"<tool_calls>(.*?)</tool_calls>", re.S)
_QWEN_INVOKE_RE = re.compile(r"<invoke\s+name\s*=\s*[\"']?([^\"'>\s]+)[\"']?\s*>", re.S)
_QWEN_PARAM_RE = re.compile(r"<parameter\s+name\s*=\s*[\"']?([^\"'>\s]+)[\"']?[^>]*>(.*?)</parameter>", re.S)


def _normalize_toolcall_xml(text):
    """把 Qwen3 的 <tool_calls><invoke> 格式归一化成 <tool_call><function=..><parameter=..>。"""
    if not text or "<tool_calls>" not in text:
        return text
    def _conv(m):
        block = m.group(1)
        name = _QWEN_INVOKE_RE.search(block)
        if not name:
            return ""  # 无 <invoke> 的块整体丢弃
        args = "".join(f"<parameter={k}>{v}</parameter>" for k, v in _QWEN_PARAM_RE.findall(block))
        return f"<tool_call><function={name.group(1)}>{args}</function></tool_call>"
    return _QWEN_TOOLCALL_RE.sub(_conv, text)


def _parse_xml_tool_calls(text):
    """Parse llama.cpp/Qwen `<tool_call>` XML blocks into OpenAI-style [{"name", "arguments"}].
    Qwen-family local models sometimes emit tool calls as XML (often inside reasoning_content)
    instead of JSON tool_calls; llama-server does not parse those out of the thinking block."""
    if not text:
        return []
    text = _strip_dsml(text)
    text = _normalize_toolcall_xml(text)
    calls = []
    for block in _XML_TOOLCALL_RE.findall(text):
        m = _XML_FN_RE.search(block)
        if not m:
            continue
        args = {}
        for k, v in _XML_PARAM_RE.findall(block):
            k = k.strip()
            v = v.strip()
            try:
                args[k] = json.loads(v)
            except (json.JSONDecodeError, ValueError):
                args[k] = v
        calls.append({"name": m.group(1).strip(),
                      "arguments": json.dumps(args, ensure_ascii=False)})
    return calls


def _strip_toolcall_xml(text):
    """从显示文本中剥掉 <tool_call> 块及零散标签，只留正文。未闭合的块整体丢弃。"""
    if not text:
        return ""
    text = _strip_dsml(text)
    text = _normalize_toolcall_xml(text)
    t = _XML_TOOLCALL_RE.sub("", text)  # 已闭合的块整体移除
    open_pos = t.find("<tool_call")
    if open_pos != -1 and "</tool_call>" not in t[open_pos:]:
        t = t[:open_pos]  # 未闭合的块（常被截断）→ 丢弃其全部残留
    return re.sub(r"</?tool_call\b[^>]*>|</?function\b[^>]*>|</?parameter\b[^>]*>", "", t)
