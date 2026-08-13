"""visual_locate v2：VLM 给粗包围框 → 代码吸附最近 DOM 元素 → 返回可点击编号。

v1（截图画编号让 VLM 选号）实测不可靠：目标常在 1-30 标注集外、密集编号读不准。
v2 换思路——只让 VLM 做它证明过的事（区域级定位，实测 20~40px），坐标数学交给
Playwright 的 bounding_box()：
  - 干净截图（不画框）→ 问 VLM 目标元素的包围框 x1 y1 x2 y2
  - 用中心距离 + 相交判定，吸附到最近的真实 _visible_elements 元素
  - 返回最近候选短名单（含文本描述），密集场景 agent 可借文本二次确认（粗到细）
"""
import base64
import os
import re

from PIL import Image
from openai import OpenAI

from .browse import _executor, _get_browser_page, _visible_elements, _take_screenshot
from .vision import _vision_cfg


def _dict_desc(d):
    """从 _visible_elements 的 dict 生成 '<kind> label' 描述（browser_act 元素清单同格式）。"""
    kind = d.get("role") or {"a": "link", "input": "input", "textarea": "input",
                             "select": "select", "video": "video", "audio": "audio"}.get(d.get("tag"), d.get("tag"))
    return f"<{kind}> {(d.get('label') or '').strip()[:60]}"


_NUM = r"-?\d+(?:\.\d+)?"  # 浮点正则，避免 "520.0" 被 \d+ 拆成 [520, 0] → 退化框


def _parse_box(nums, use_last=False):
    """4 个数字 → 包围框 [x1,y1,x2,y2]；退化（宽或高 <2）返回 None。
    use_last=True 表示取末尾 4 个（reasoning 结尾的结论框）。"""
    if len(nums) < 4:
        return None
    nums = nums[-4:] if use_last else nums[:4]
    x1, y1, x2, y2 = (float(n) for n in nums)
    if x2 - x1 < 2 or y2 - y1 < 2:  # 退化框（如被小数点拆坏）
        return None
    return [x1, y1, x2, y2]


def _ask_vlm_bbox(b64, target, size, max_tokens=600, attempts=4):
    """问 VLM 目标的包围框，返回 [x1, y1, x2, y2]；解析失败返回 None。
    实测 Qwen 在 max_tokens 偏小时 content 常为空（CoT 吃光预算），故重试取到有效框为止；
    退化框 / 完全在截图外的框一律视为失败重试，不把垃圾坐标喂给吸附逻辑。"""
    cfg = _vision_cfg()
    w, h = size
    q = (f"这是一张浏览器页面截图，宽 {w}px 高 {h}px。"
         f"找出目标元素『{target}』的包围框：左上角坐标 (x1,y1) 和右下角坐标 (x2,y2)。"
         f"只输出 4 个数字，空格分隔：x1 y1 x2 y2。不要输出任何其它文字。")
    for _ in range(attempts):
        msg = OpenAI(api_key="sk_local", base_url=cfg["base_url"]).chat.completions.create(
            model=cfg["model"],
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": q},
            ]}],
            max_tokens=max_tokens,
        ).choices[0].message
        content = (msg.content or "").strip()
        if content:
            box = _parse_box(re.findall(_NUM, content))
            if box and not (box[2] < 0 or box[0] > w or box[3] < 0 or box[1] > h):  # 不在截图外
                return box
        # content 为空或框无效 → 取 reasoning 结尾的结论框兜底
        reasoning = (getattr(msg, "reasoning_content", None) or "").strip()
        box = _parse_box(re.findall(_NUM, reasoning), use_last=True)
        if box and not (box[2] < 0 or box[0] > w or box[3] < 0 or box[1] > h):
            return box
    return None


def _pick_element(els, box):
    """按 VLM 包围框吸附最近真实元素。
    els 是 _visible_elements 返回的 dict 列表（每项含 box={x,y,width,height}）。
    返回 (best, shortlist)；best=(inter, dist, i)，shortlist 是同结构的候选前 5。
    纯逻辑，不触浏览器——用 stub 元素即可回归。
    """
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    cands = []
    for i, d in enumerate(els):
        b = d.get("box")
        if not b or not (b.get("width") and b.get("height")):
            continue
        ex1, ey1 = b["x"], b["y"]
        ex2, ey2 = ex1 + b["width"], ey1 + b["height"]
        ecx, ecy = (ex1 + ex2) / 2, (ey1 + ey2) / 2
        dist = ((ecx - cx) ** 2 + (ecy - cy) ** 2) ** 0.5
        inter = not (ex2 < x1 or ex1 > x2 or ey2 < y1 or ey1 > y2)
        cands.append((inter, dist, i))
    if not cands:
        return None, []
    cands.sort(key=lambda c: (not c[0], c[1]))  # 与 box 相交优先，再按中心距离
    best = cands[0]
    inter_cands = [c for c in cands if c[0]]
    shortlist = sorted(inter_cands, key=lambda c: c[1])[:5] or cands[:5]
    return best, shortlist


def visual_locate_mock(target, profile="", headless=True):
    """定位当前页面上描述的目标元素，返回编号 + 最近候选（供 browser_act click/type 用）。
    编号与 browser_act 元素清单一致。"""
    def work():
        page, _ctx, _browser = _get_browser_page(profile, headless)
        if page.url.strip().rstrip("/") in ("about:blank", ""):
            return "[Error] visual_locate: 浏览器是空白页，先 browser_act(open) 打开一个 URL。"
        shot = _take_screenshot(page)
        with Image.open(shot) as im:
            w, h = im.size
        with open(shot, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        box = _ask_vlm_bbox(b64, target, (w, h))
        if box is None:
            return f"[Error] visual_locate: VLM 未给出有效包围框。截图: {shot}"
        x1, y1, x2, y2 = box
        x1, x2 = sorted((max(0, min(x1, w)), max(0, min(x2, w))))
        y1, y2 = sorted((max(0, min(y1, h)), max(0, min(y2, h))))
        if x2 - x1 < 2 or y2 - y1 < 2:
            return f"[Error] visual_locate: VLM 包围框过小 {box}。截图: {shot}"
        els = _visible_elements(page)
        if not els:
            return "[Error] visual_locate: 页面没有可交互元素。"
        best, shortlist = _pick_element(els, (x1, y1, x2, y2))
        if best is None:
            return f"[Error] visual_locate: 没有元素可吸附。截图: {shot}"
        idx = best[2]
        desc = _dict_desc(els[idx])
        sl = ", ".join(f"[{c[2]+1}] {_dict_desc(els[c[2]])}" for c in shortlist)
        return (f"[visual_locate] 『{target}』 → 编号 [{idx+1}] ({desc})。"
                f"最近候选: {sl}。用 browser_act(click, target={idx+1})；若候选里另有更合适的，用它的编号。")
    return _executor.call(work)


if __name__ == "__main__":
    # 自检 1：_parse_box 解析（退化框/小数拆坏 → None）
    assert _parse_box(["436", "30", "645", "78"]) == [436.0, 30.0, 645.0, 78.0]
    assert _parse_box(re.findall(_NUM, "520.0 60.0 540.0 60.0")) is None, "小数 y2==y1 退化框应拒绝"
    assert _parse_box(["500", "60", "500", "60"]) is None, "点应拒绝"
    assert _parse_box(["500", "60"]) is None, "数字不足应拒绝"
    assert _parse_box(["1", "2", "3", "435", "28", "647", "83"], use_last=True) == [435.0, 28.0, 647.0, 83.0]
    print("visual_locate._parse_box self-check OK: degenerate/float-mangled boxes rejected")
    # 自检 2：_pick_element 吸附逻辑（不依赖浏览器/VLM）
    class Stub:
        def __init__(self, box):
            self._box = box

        def bounding_box(self):
            return self._box

    # 模拟 bilibili 顶栏：几个导航链接 + 一个搜索框(#75)
    els = [Stub({"x": 24, "y": 0, "width": 52, "height": 64}),   # 首页
           Stub({"x": 86, "y": 0, "width": 28, "height": 64}),   # 番剧
           Stub({"x": 557, "y": 23, "width": 215, "height": 20})]  # 搜索框
    best, shortlist = _pick_element(els, (300, 30, 900, 100))     # VLM 粗框罩住搜索框
    assert best[2] == 2, best  # 吸附到搜索框（index 2，编号 3）
    assert shortlist and shortlist[0][2] == 2, shortlist
    # VLM 框偏到顶栏左侧 → 吸附到最近的导航链接
    best2, _ = _pick_element(els, (10, 0, 80, 60))
    assert best2[2] == 0, best2
    print("visual_locate._pick_element self-check OK: intersection+center-distance snapping works")
