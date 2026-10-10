"""visual_locate v2：VLM 给粗包围框 → 代码吸附最近 DOM 元素 → 返回可点击编号。

v1（截图画编号让 VLM 选号）实测不可靠：目标常在 1-30 标注集外、密集编号读不准。
v2 换思路——只让 VLM 做它证明过的事（区域级定位，实测 20~40px），坐标数学交给
Playwright 的 bounding_box()：
  - 干净截图（不画框）→ 问 VLM 目标元素的包围框 x1 y1 x2 y2
  - 用中心距离 + 相交判定，吸附到最近的真实 _visible_elements 元素
  - 返回最近候选短名单（含文本描述），密集场景 agent 可借文本二次确认（粗到细）
"""
import base64
import math
import os
import re

from PIL import Image
from openai import OpenAI

from .browse import (_executor, _get_browser_page, _visible_elements, _take_screenshot,
                     _browser_sessions)
from .browser_observation import collect_observation
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
        try:
            msg = OpenAI(api_key=cfg.get("api_key", "sk_local"),
                         base_url=cfg["base_url"]).chat.completions.create(
                model=cfg["model"],
                messages=[{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": q},
                ]}],
                max_tokens=max_tokens,
            ).choices[0].message
        except Exception as e:
            # 连接失败/后端不可达：重试无意义，抛清晰错误由调用方转成友好提示
            raise RuntimeError(
                f"视觉后端请求失败：{e}。请确认视觉 server 已启动（start_local_llm.bat 选 3.5，"
                f"监听 {cfg.get('base_url')}）或已设置 VISION_BACKEND=luna + VISION_API_KEY。"
            )
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


def _iou(a, b):
    """两个包围框的 IoU（相交面积 / 并集面积）。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


# 幻觉/歧义闸门用：目标描述里的方位/泛词，先去掉再和吸附元素的文本比对
_NOISE = ("按钮", "网页", "页面", "顶栏", "侧边栏", "工具栏", "操作栏", "左上角", "右上角",
          "左下角", "右下角", "顶部", "底部", "那个", "这个", "的", " ")

# 阶段三：候选联合打分——分数只由 DOM 事实计算，不采信 VLM 自报置信度（会幻觉）。
# 权重：IoU 0.45（框准）+ 中心距离 0.25 + 文本/ARIA 匹配 0.30（语义对）。
# 去掉了文档 §7.4 里"视觉置信度×0.35"那一项——那是让会幻觉的模型自证清白，不可信。
_SCORE_W = {"iou": 0.45, "center": 0.25, "text": 0.30}
_CENTER_SCALE = 40.0  # VLM 区域级定位的实测误差尺度（20~40px）
# 门控阈值（初值，可用 probe_scoring 实测后校准）
_AUTO_SCORE = 0.45   # 低于此分 → 不自动指定
_AUTO_MARGIN = 0.12  # top1 与 top2 分差低于此 → 歧义，返回候选让 agent 二次判断


def _strip_noise(target):
    """去掉目标描述里的方位/泛词，得到可文本比对的核心词。"""
    tgt = target
    for w in _NOISE:
        tgt = tgt.replace(w, "")
    return tgt.strip()


def _text_score(target, label):
    """文本匹配打分：0=明确不匹配，1=明确匹配，0.5=无法用文本判断（目标或 label 为空/纯图标）。"""
    tgt = _strip_noise(target)
    label = (label or "").strip()
    if not tgt or not label:
        return 0.5
    if tgt in label or label in tgt:
        return 1.0
    return 0.0


def _target_label_mismatch(target, label):
    """吸附元素的文本与目标描述是否完全不匹配（编造/歧义闸门）。
    等价于 _text_score == 0.0；label 为空（纯图标）→ 0.5 → 不误伤图标目标。"""
    return _text_score(target, label) == 0.0


def _cand_scores(els, box, target=""):
    """对每个候选元素算可验证分数，返回 [(score, i)] 按分数降序。
    分数只由 DOM 事实算出（IoU/中心距离/文本匹配），VLM 只提供框、不给分。
    纯逻辑，不触浏览器——用 stub 元素即可回归。"""
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    out = []
    for i, d in enumerate(els):
        b = d.get("box")
        if not b or not (b.get("width") and b.get("height")):
            continue
        ex1, ey1 = b["x"], b["y"]
        ex2, ey2 = ex1 + b["width"], ey1 + b["height"]
        iou = _iou((x1, y1, x2, y2), (ex1, ey1, ex2, ey2))
        ecx, ecy = (ex1 + ex2) / 2, (ey1 + ey2) / 2
        dist = ((ecx - cx) ** 2 + (ecy - cy) ** 2) ** 0.5
        center = math.exp(-dist / _CENTER_SCALE)
        text = _text_score(target, d.get("label") or "")
        score = _SCORE_W["iou"] * iou + _SCORE_W["center"] * center + _SCORE_W["text"] * text
        out.append((score, i))
    out.sort(key=lambda t: -t[0])
    return out


def _decide(scores, els, target):
    """门控判定：根据候选分数返回 (verdict, reason)。
    verdict ∈ {'reject'(文本不匹配), 'ambiguous'(低分/小 margin/多文本命中), 'auto'}。
    纯逻辑，visual_locate_mock 与 probe_scoring 共用，保证判定口径一致。"""
    best_score, idx = scores[0]
    second = scores[1][0] if len(scores) > 1 else 0.0
    margin = best_score - second
    label = els[idx].get("label") or ""
    if _target_label_mismatch(target, label):
        return "reject", f"文本不匹配（吸附到 '{label}'）"
    text_hits = [i for _, i in scores if _text_score(target, els[i].get("label") or "") == 1.0]
    if best_score < _AUTO_SCORE:
        return "ambiguous", f"score={best_score:.2f} 低于阈值 {_AUTO_SCORE}"
    if margin < _AUTO_MARGIN:
        return "ambiguous", f"margin={margin:.2f} 低于阈值 {_AUTO_MARGIN}"
    if len(text_hits) > 1:
        return "ambiguous", f"{len(text_hits)} 个候选文本都命中『{target}』"
    return "auto", ""


def probe_grounding(url=None, targets=None, n=5):
    """接地实测（独立 probe，不改生产路径）：
    对每个目标连问 n 次包围框，量中心稳定性 + IoU 一致性 + 与真实 DOM 元素的中心误差，
    最后用不存在的目标测幻觉。视觉模型走 VISION_BASE_URL/VISION_MODEL/VISION_API_KEY 环境变量
    （指向 Luna 即为 Luna 实测）。
    """
    import statistics as _st
    url = url or os.environ.get("VISION_PROBE_URL", "https://www.bilibili.com")
    if targets is None:
        targets = ["投币", "点赞", "收藏"]
    ghost = "网页顶栏的打印按钮"  # bilibili 不存在、且不会撞上任何真实元素 → 幻觉测试

    def work():
        page, _ctx, _browser = _get_browser_page("", headless=True)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        shot = _take_screenshot(page)
        with Image.open(shot) as im:
            w, h = im.size
        with open(shot, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        els = _visible_elements(page)

        def find_el(key):
            k = key.replace("按钮", "").replace(" ", "")
            for i, d in enumerate(els):
                if k and k in (d.get("label") or "").replace(" ", ""):
                    return i, d
            return None, None

        cfg = _vision_cfg()
        print(f"[probe] model={cfg['model']} base_url={cfg['base_url']}")
        print(f"[probe] page={page.url} shot={w}x{h} interactive_elements={len(els)}")
        for t in targets:
            boxes = [b for b in (_ask_vlm_bbox(b64, t, (w, h)) for _ in range(n)) if b]
            if not boxes:
                print(f"[probe] '{t}': 连问 {n} 次全部解析失败 → 模型给框不可靠")
                continue
            cx = [(b[0] + b[2]) / 2 for b in boxes]
            cy = [(b[1] + b[3]) / 2 for b in boxes]
            ious = [_iou(a, b) for a in boxes for b in boxes]
            std_c = (_st.pstdev(cx) + _st.pstdev(cy)) / 2
            mb = [round(_st.mean(b[i] for b in boxes), 1) for i in range(4)]
            line = (f"[probe] '{t}': n={len(boxes)}/{n} 中心std={std_c:.1f}px "
                    f"IoU均值={_st.mean(ious):.2f} IoU最小={min(ious):.2f} 平均框={mb}")
            idx, d = find_el(t)
            if idx is not None:
                eb = d["box"]
                err = (((eb["x"] + eb["width"] / 2) - _st.mean(cx)) ** 2
                       + ((eb["y"] + eb["height"] / 2) - _st.mean(cy)) ** 2) ** 0.5
                line += f" | DOM真值[{idx + 1}] {_dict_desc(d)} 中心误差={err:.0f}px"
            else:
                line += " | (DOM 无匹配真值，仅报框一致性)"
            # 决定性判据：平均框跑现有吸附，看返回的编号对不对
            best, shortlist = _pick_element(els, mb)
            if best is not None:
                bid = best[2]
                snap_ok = (idx is not None and bid == idx)
                line += f" → 吸附[{bid + 1}] {_dict_desc(els[bid])} {'✓命中' if snap_ok else '✗错吸'}"
            print(line)
        g = _ask_vlm_bbox(b64, ghost, (w, h))
        if g:
            idx, d = find_el(ghost)
            print(f"[probe] 幻觉'{ghost}': 模型给了框 {g}，DOM 真值={d and _dict_desc(d) or '不存在 → 编造!'}")
        else:
            print(f"[probe] 幻觉'{ghost}': 模型返回 None ✓ 没有编造")
        print(f"[probe] 截图: {shot}")
        try:
            _browser.close()  # 进程退出前关浏览器，避免 node driver EPIPE
        except Exception:
            pass
        _browser_sessions.pop("__default__", None)
        return "probe done"
    try:
        return _executor.call(work)
    except RuntimeError as e:
        return f"[Error] {e}"


def probe_scoring(url=None, targets=None, n=5):
    """评分可靠性实测（阶段三）：对每个目标连问 n 次包围框，用 _cand_scores 打分，
    量 top-1 命中率 / 分数 / margin / 文本匹配 / 门控判定，最后用不存在的目标测幻觉闸门。
    视觉后端走 VISION_* 环境变量；URL/目标经命令行传（python -m loop_agent_core.visual_locate scoring <url> <t1> <t2>...）。
    只依赖 DOM 真值做命中判定，不采信模型自报置信度。"""
    url = url or os.environ.get("VISION_PROBE_URL", "https://www.bilibili.com")
    if targets is None:
        targets = ["投币", "点赞", "收藏"]
    ghost = "网页顶栏的打印按钮"

    def work():
        page, _ctx, _browser = _get_browser_page("", headless=True)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        obs = collect_observation(page, "", include_screenshot=True, include_dom=True, include_text=False)
        shot = obs.screenshot_path
        with Image.open(shot) as im:
            w, h = im.size
        with open(shot, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        els = obs.elements

        def find_el(key):
            k = key.replace("按钮", "").replace(" ", "")
            for i, d in enumerate(els):
                if k and k in (d.get("label") or "").replace(" ", ""):
                    return i, d
            return None, None

        cfg = _vision_cfg()
        print(f"[probe] model={cfg['model']} base_url={cfg['base_url']}")
        print(f"[probe] page={page.url} shot={w}x{h} interactive_elements={len(els)}")

        for t in targets:
            boxes = [b for b in (_ask_vlm_bbox(b64, t, (w, h)) for _ in range(n)) if b]
            if not boxes:
                print(f"[probe] '{t}': 连问 {n} 次全部解析失败 → 框不可靠")
                continue
            gidx, gd = find_el(t)
            match_count = sum(1 for d in els if _text_score(t, d.get("label") or "") == 1.0)
            hits, scores1, margins, texts = 0, [], [], []
            last_top1 = None
            for b in boxes:
                bx1, by1, bx2, by2 = b
                bx1, bx2 = sorted((max(0, min(bx1, w)), max(0, min(bx2, w))))
                by1, by2 = sorted((max(0, min(by1, h)), max(0, min(by2, h))))
                sc = _cand_scores(els, (bx1, by1, bx2, by2), t)
                if not sc:
                    continue
                s1, i1 = sc[0]
                s2 = sc[1][0] if len(sc) > 1 else 0.0
                scores1.append(s1)
                margins.append(s1 - s2)
                texts.append(_text_score(t, els[i1].get("label") or ""))
                last_top1 = i1
                if gidx is not None and i1 == gidx:
                    hits += 1

            def mean(x):
                return sum(x) / len(x) if x else 0.0

            line = (f"[probe] '{t}': n={len(boxes)}/{n} 命中={hits}/{len(boxes)} "
                    f"top1score均值={mean(scores1):.2f} margin均值={mean(margins):.2f} "
                    f"文本匹配均值={mean(texts):.2f} 文本命中候选={match_count}")
            if gidx is not None:
                line += f" | DOM真值[{gidx + 1}] {_dict_desc(gd)}"
            else:
                line += " | (DOM 无匹配真值，仅报分数一致性)"
            if last_top1 is not None:
                line += f" | 末次top1=[{last_top1 + 1}] {_dict_desc(els[last_top1])}"
            print(line)

        # 幻觉测试：不存在的目标，看门控（文本/分数/margin/多文本命中）是否拦截
        g = _ask_vlm_bbox(b64, ghost, (w, h))
        if g:
            sc = _cand_scores(els, g, ghost)
            if sc:
                verdict, reason = _decide(sc, els, ghost)
                s1, i1 = sc[0]
                mark = "✓拦截" if verdict != "auto" else "✗未拦截（会自动点，需人工确认）"
                print(f"[probe] 幻觉'{ghost}': 模型给了框，top1=[{i1 + 1}] {_dict_desc(els[i1])} "
                      f"score={s1:.2f} → {verdict}（{reason}）{mark}")
            else:
                print(f"[probe] 幻觉'{ghost}': 模型给了框但无元素可吸附")
        else:
            print(f"[probe] 幻觉'{ghost}': 模型返回 None ✓ 没有编造")
        print(f"[probe] 截图: {shot}")
        try:
            _browser.close()  # 进程退出前关浏览器，避免 node driver EPIPE
        except Exception:
            pass
        _browser_sessions.pop("__default__", None)
        return "probe done"
    try:
        return _executor.call(work)
    except RuntimeError as e:
        return f"[Error] {e}"


def visual_locate_mock(target, profile="", headless=True):
    """定位当前页面上描述的目标元素，返回编号 + 最近候选（供 browser_act click/type 用）。
    编号与 browser_act 元素清单一致。"""
    def work():
        page, _ctx, _browser = _get_browser_page(profile, headless)
        if page.url.strip().rstrip("/") in ("about:blank", ""):
            return "[Error] visual_locate: 浏览器是空白页，先 browser_act(open) 打开一个 URL。"
        # 复用统一 observation：截图与 DOM 同一采集周期，避免二者分属不同时刻错位
        obs = collect_observation(page, profile, include_screenshot=True,
                                  include_dom=True, include_text=False)
        shot = obs.screenshot_path
        if not shot:
            return "[Error] visual_locate: 截图失败。"
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
        els = obs.elements
        if not els:
            return "[Error] visual_locate: 页面没有可交互元素。"
        scores = _cand_scores(els, (x1, y1, x2, y2), target)
        if not scores:
            return f"[Error] visual_locate: 没有元素可吸附。截图: {shot}"
        verdict, reason = _decide(scores, els, target)
        best_score, idx = scores[0]
        desc = _dict_desc(els[idx])
        if verdict == "reject":
            return (f"[Error] visual_locate: VLM 框吸附到 [{idx+1}] ({desc})，"
                    f"但文本与目标『{target}』不一致——可能是幻觉或描述有歧义。截图: {shot}。"
                    f"换带位置的描述重试（如『操作栏的收藏按钮』）。")
        sl = ", ".join(f"[{i+1}] {_dict_desc(els[i])} (id={els[i].get('element_id') or '?'})" for _, i in scores[:5])
        eid = els[idx].get("element_id") or ""
        if verdict == "ambiguous":
            # 低分/小 margin/多文本命中：不自动点，把候选交给 agent 结合 DOM 二次判断
            return (f"[visual_locate] 『{target}』候选歧义（{reason}），未自动指定。"
                    f"请结合 DOM 上下文从候选中挑一个：{sl}。"
                    f"若都不符，换带方位的描述（如『视频操作栏的收藏按钮』）重试或请求用户确认。")
        margin = best_score - (scores[1][0] if len(scores) > 1 else 0.0)
        return (f"[visual_locate] 『{target}』 → 编号 [{idx+1}] ({desc}) id={eid} (score={best_score:.2f} margin={margin:.2f})。"
                f"最近候选: {sl}。用 browser_act(click, target={idx+1}) 或 target='{eid}'；若候选里另有更合适的，用它的编号。")
    try:
        return _executor.call(work)
    except RuntimeError as e:
        return f"[Error] {e}"


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        probe_grounding(url=sys.argv[2] if len(sys.argv) > 2 else None)
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "scoring":
        # python -m loop_agent_core.visual_locate scoring <url> <target1> [target2 ...]
        url = sys.argv[2] if len(sys.argv) > 2 else None
        targets = [t for t in sys.argv[3:] if t] or None
        probe_scoring(url=url, targets=targets)
        sys.exit(0)
    # 自检 1：_parse_box 解析（退化框/小数拆坏 → None）
    assert _parse_box(["436", "30", "645", "78"]) == [436.0, 30.0, 645.0, 78.0]
    assert _parse_box(re.findall(_NUM, "520.0 60.0 540.0 60.0")) is None, "小数 y2==y1 退化框应拒绝"
    assert _parse_box(["500", "60", "500", "60"]) is None, "点应拒绝"
    assert _parse_box(["500", "60"]) is None, "数字不足应拒绝"
    assert _parse_box(["1", "2", "3", "435", "28", "647", "83"], use_last=True) == [435.0, 28.0, 647.0, 83.0]
    print("visual_locate._parse_box self-check OK: degenerate/float-mangled boxes rejected")
    # 自检 2：_pick_element 吸附逻辑（不依赖浏览器/VLM）
    # 注意：_pick_element 消费 _visible_elements 的 dict（d.get("box")），用 dict 作 stub。
    # 模拟 bilibili 顶栏：几个导航链接 + 一个搜索框(#75)
    els = [{"box": {"x": 24, "y": 0, "width": 52, "height": 64}},   # 首页
           {"box": {"x": 86, "y": 0, "width": 28, "height": 64}},   # 番剧
           {"box": {"x": 557, "y": 23, "width": 215, "height": 20}}]  # 搜索框
    best, shortlist = _pick_element(els, (300, 30, 900, 100))     # VLM 粗框罩住搜索框
    assert best[2] == 2, best  # 吸附到搜索框（index 2，编号 3）
    assert shortlist and shortlist[0][2] == 2, shortlist
    # VLM 框偏到顶栏左侧 → 吸附到最近的导航链接
    best2, _ = _pick_element(els, (10, 0, 80, 60))
    assert best2[2] == 0, best2
    print("visual_locate._pick_element self-check OK: intersection+center-distance snapping works")
    # 自检 3：_target_label_mismatch 文本一致性闸门
    assert _target_label_mismatch("网页顶栏的打印按钮", "番剧"), "编造框吸附到无关元素应拒绝"
    assert _target_label_mismatch("投币按钮", "投币 投币（W）") is False, "真目标（误差小但文本对上）不应拒绝"
    assert _target_label_mismatch("收藏", "收藏（E）") is False, "歧义目标但文本对上不应拒绝（留给 prompt 处理）"
    assert _target_label_mismatch("播放按钮", "") is False, "纯图标按钮无 label 不应误伤"
    assert _target_label_mismatch("点赞", "点赞 点赞（Q）") is False, "正常目标不应拒绝"
    print("visual_locate._target_label_mismatch self-check OK: hallucination/ambiguity gate works")
    # 自检 4：_text_score / _cand_scores 联合打分（纯逻辑，不依赖浏览器/VLM）
    assert _text_score("投币按钮", "投币 投币（W）") == 1.0
    assert _text_score("播放按钮", "") == 0.5, "纯图标无 label → 中性 0.5"
    assert _text_score("打印", "番剧") == 0.0, "无关文本应 0"
    els4 = [
        {"box": {"x": 24, "y": 0, "width": 52, "height": 64}, "label": "首页"},
        {"box": {"x": 86, "y": 0, "width": 28, "height": 64}, "label": "番剧"},
        {"box": {"x": 557, "y": 23, "width": 215, "height": 20}, "label": "搜索框"},
    ]
    sc4 = _cand_scores(els4, (300, 30, 900, 100), "搜索框")
    assert sc4[0][1] == 2, sc4  # 目标元素得分最高
    assert sc4[0][0] > sc4[1][0], sc4  # 文本匹配拉开差距
    print("visual_locate._cand_scores self-check OK: code-computed scoring ranks target first")
    # 自检 5：_decide 门控判定（覆盖用户实测的"空 label + 低分 → ambiguous"与"多文本命中 → ambiguous"）
    # a) 文本不匹配 → reject
    elsa = [{"box": {"x": 0, "y": 0, "width": 60, "height": 60}, "label": "首页"}]
    va, ra = _decide(_cand_scores(elsa, (0, 0, 60, 60), "打印按钮"), elsa, "打印按钮")
    assert va == "reject", (va, ra)
    # b) 幻觉框离元素很远 → 低分 → ambiguous（复现 [24] <link> 空 label score=0.15）
    elsb = [{"box": {"x": 0, "y": 0, "width": 40, "height": 40}, "label": ""}]
    vb, rb = _decide(_cand_scores(elsb, (200, 200, 240, 240), "打印按钮"), elsb, "打印按钮")
    assert vb == "ambiguous", (vb, rb)  # 空 label 文本闸门不拒，但低分兜底 → 不自动点
    # c) 多个文本命中（页面上多个"收藏"）→ ambiguous
    elsc = [
        {"box": {"x": 100, "y": 100, "width": 60, "height": 60}, "label": "收藏 收藏（E）"},
        {"box": {"x": 200, "y": 100, "width": 60, "height": 60}, "label": "收藏夹"},
    ]
    vc, rc = _decide(_cand_scores(elsc, (100, 100, 160, 160), "收藏"), elsc, "收藏")
    assert vc == "ambiguous", (vc, rc)
    # d) 唯一文本命中 + 高分 + 大 margin → auto
    elsd = [
        {"box": {"x": 1160, "y": 200, "width": 60, "height": 60}, "label": "点赞 点赞（Q）"},
        {"box": {"x": 1160, "y": 280, "width": 60, "height": 60}, "label": "投币 投币（W）"},
    ]
    vd, rd = _decide(_cand_scores(elsd, (1160, 200, 1220, 260), "点赞"), elsd, "点赞")
    assert vd == "auto", (vd, rd)
    print("visual_locate._decide self-check OK: reject/ambiguous/auto verdicts correct")
