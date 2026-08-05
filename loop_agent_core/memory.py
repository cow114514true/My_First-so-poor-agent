import sys
import os # 获取路径
import json # 格式解析
from datetime import datetime ,timezone # 记忆文件时间戳
from .config import WORK_DIR

_MEM_SYSTEM_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(
            os.path.abspath(__file__)),"..","memories","system.json"
        )
    )

_MEM_PROJECT_PATH = os.path.abspath(
    os.path.join(WORK_DIR,"memories","project.json")
)

def _load_memories(path) -> list:
    if not os.path.exists(path):
        return []
    else:
        try:
            res_list = json.load(open(path,"r",encoding="utf-8"))
            return res_list
        except Exception as e:
            sys.stderr.write(f"[Error] memories/{os.path.basename(path)} corrupted: {e}\n")
            return []
def _save_memories(path,records:list) -> None:
    os.makedirs(os.path.dirname(path),exist_ok=True)
    json.dump(records, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
def recall_mock(topic:str = "") -> str:
    if _worker_check and _worker_check():
        return "[Error] recall 在 sub-agent 内不可用。"
    
    hits = []
    try:
        system_records = _load_memories(_MEM_SYSTEM_PATH)
        project_records = _load_memories(_MEM_PROJECT_PATH)
        merge_record = system_records + project_records
    except Exception as e:
        return "暂无记忆。可用 remember(topic, content) 添加"
    if not merge_record:
        return "暂无记忆。可用 remember(topic, content) 添加"
    if not topic.strip():
        hits = merge_record
    else:
        for record in merge_record:
            if topic.lower() in (record["topic"] + "" + record["content"]).lower():
                hits.append(record)
    if not hits:
        return "无相关记忆。可用 remember(topic, content) 添加"
    hits = sorted(hits,key=lambda r: r.get("ts", ""), reverse=True)
    hits = hits[:5]
    system_g = [r for r in hits if r.get("level") == "system"]
    project_g = [r for r in hits if r.get("level") == "project"]

    lines = []
    if system_g:
        lines.append("[系统级记忆]")
        for r in system_g:
            lines.append(f"- {r['topic']}: {r['content']} ({r.get('ts','')[:10]})")
    if project_g:
        lines.append("[项目级记忆]")
        for r in project_g:
            lines.append(f"- {r['topic']}: {r['content']} ({r.get('ts','')[:10]})")
    return "\n".join(lines) if lines else "无相关记忆。"
def remember_mock(topic:str,content:str,level:str = "project") -> str:
    if _worker_check and _worker_check():
        return "[Error] remember 在 sub-agent 内不可用。将你的结果返回给主 agent 处理。"
    if topic.strip() == "" or content.strip() == "":
        return "[Error] topic 和 content 不能为空"
    label = ""
    action = ""
    mem_path = None
    if level.strip() == "system":
        mem_path = _MEM_SYSTEM_PATH
        label = "system"
    elif level.strip() == "project":
        mem_path = _MEM_PROJECT_PATH
        label = "project"
    else:
        return f"[ERROR] 不合理的level{level},只能取system 和 project"
    
    records = _load_memories(mem_path)

    for r in records:
        if r.get("topic") == topic.strip():
            r["content"] = content.strip()
            r["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            action = "已更新"
    if action == "已更新":
        _save_memories(mem_path,records)
        return f"{action} [{label}] {topic}"
    else:
        if action == "":
            new_content = {
                "topic": topic.strip(),
                "content": content.strip(),
                "level": level.strip(),
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")
            }
            records.append(new_content)
            _save_memories(mem_path,records)
            action = "已记住"
            return f"{action} [{label}] {topic}"
        else:
            return "[ERROR],action had some value not to be expected"

def build_memory_injection() -> str | None:
    system_records = _load_memories(_MEM_SYSTEM_PATH)
    project_records = _load_memories(_MEM_PROJECT_PATH)

    if not system_records and not project_records:
        return None

    lines = []
    lines.append("[长期记忆 — 以下是你跨会话记住的事实]")
    lines.append("")

    # 系统级：完整输出 content
    if system_records:
        lines.append("### 系统级记忆")
        for r in system_records:
            lines.append(f"- {r['topic']}: {r['content']}")

    # 项目级：只输出 topic 索引（按设计是斜杠分隔）
    if project_records:
        lines.append("### 项目级记忆索引")
        topics = " / ".join(r["topic"] for r in project_records)
        lines.append(topics)

    lines.append("")
    lines.append("---")
    lines.append("需要详情用 recall(主题) 查询。")

    return "\n".join(lines)
_worker_check = None