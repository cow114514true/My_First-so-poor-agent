"""自检套件：TUI 聊天面板的 Markdown 渲染与健壮性。Run: python test_tui_markdown.py

不触网：用 dummy 凭据导入（OpenAI 客户端构造时不发请求），再以 Textual headless
harness 驱动真实的 AgentTUI，喂入 runner.py 实际发出的事件序列。

覆盖：
  1. 助手回复按 Markdown 渲染，且流式过程中不出现裸露的 ``` 或 ## 
  2. 用户输入逐字原样显示（不被 Markdown / Rich markup 解释）
  3. 270 tok/s 压力下不积压、不丢字
  4. 聊天面板自动跟随底部；用户上翻时不被抢走滚动位置
  5. PanelLog resize 防抖（拖拽窗口只重排一次）
  6. chat 面板双击展开/收起（含子控件转发与手势消抖）
  7. 思考面板首次输出延迟（慢速模型下不被阈值压住）
  8. 面板折行宽度与展开重排
  9. 中文禁则（标点不落行首）与整行折行、跨旧断点合并
  10. 思考面板 270 tok/s 压力（不积压、不丢字）
  11. 大文本量下的展开/收回：不可见的面板不重排、整段批量铺行
  12. 思考面板折叠（Ctrl+T）：折叠只改宽度不动内容、轮次徽标、展开态互斥
  13. 折叠的边角：焦点让位（0 宽控件）、跨问题轮次重数、/clear 归零
  14. 块级选择：j/k 选一轮、Enter/单击标题只展开那一轮（z/o 仍是整体操作）
退出码非 0 = 失败。
"""
import asyncio
import glob
import os
import random
import sys
import time

os.environ.setdefault("OPENAI_API_KEY", "sk-dummy-for-tests")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:1/v1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 上一次运行留下的 autosave 会在启动时弹出恢复对话框，盖住屏幕把后面的鼠标
# 命中测试全部带偏。测试自己造干净环境。
_SAVE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "conversations")
for _f in glob.glob(os.path.join(_SAVE_DIR, ".autosave*.json")):
    try:
        os.remove(_f)
    except OSError:
        pass

from textual.widgets import RichLog, Static, TextArea  # noqa: E402
from tui import AgentTUI, ChatTranscript, ChatMessage, PanelLog, wrap_cjk  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def screen_lines(widget) -> list:
    return ["".join(s.text for s in widget.render_line(y)).rstrip()
            for y in range(widget.size.height)]


_LEAD_PUNCT = ",.!?;:，。！？；：、）】》"


def fuzz_wrap() -> None:
    """wrap_cjk 的不变量（随机输入）：不丢字、不乱序、不超宽、不把标点放行首。

    行首禁则在下面几种情况下数学上无法成立，fuzz 按这些情况精确放行：
      - width < 8：腾挪空间不足，一行塞不下"一个字 + 一个中文标点"的组合；
      - 换行点两侧都是收尾标点（如 "容…。" + "，"）：不改变顺序就分不开它们；
      - 源文本自带硬换行 \\n 后紧跟标点——那是作者自己的断行，不是折行造成的。
    最后一条只在含 \\n 的那组里测，且只校验内容 / 顺序 / 宽度。
    """
    from rich.cells import cell_len as _cl
    from tui import wrap_cjk

    def cells(s):
        return sum(_cl(c) for c in s)

    core = (list("中文推理内容标点测试段落实现方式渲染宽度") * 6
            + ["abcXYZ_019", "min_width", "RichLog ", "用"]
            + ["，", "。", "、", "：", "！", "？", "（", "）"]
            + [". ", ", ", " ... ", " (", ") ", "? "]
            + [" ", "...", "——", "…", "🙂", "\t",
               "https://example.com/a/very/long/path?q=1"])
    widths = [2, 3, 4, 5, 6, 8, 12, 45, 46, 78, 133]
    cases = 4000
    bad_content = bad_width = bad_start = 0
    rnd = random.Random(1234)
    for _ in range(cases):
        text = "".join(rnd.choice(core) for _ in range(rnd.randint(0, 40)))
        width = rnd.choice(widths)
        lines = wrap_cjk(text, width)
        if "".join(lines) != text:
            bad_content += 1
        for i, ln in enumerate(lines):
            if cells(ln) > width:
                bad_width += 1
            if (width >= 8 and i < len(lines) - 1 and ln[:1] in _LEAD_PUNCT
                    and not (i == 0 and text.startswith(ln[:1]))
                    and lines[i - 1][-1] not in _LEAD_PUNCT):
                bad_start += 1

    # 硬换行单独测：含 \n 时只校验内容 / 顺序 / 宽度（行首标点由作者断行决定）
    bad_nl = 0
    rnd2 = random.Random(4321)
    for _ in range(1000):
        text = "".join(rnd2.choice(core + ["\n", "\n\n"]) for _ in range(rnd2.randint(0, 30)))
        width = rnd2.choice([4, 12, 45, 78])
        lines = wrap_cjk(text, width)
        if "".join(lines) != text.replace("\n", ""):
            bad_nl += 1
        elif any(cells(ln) > width for ln in lines):
            bad_nl += 1

    check("fuzz: 折行不丢字、不乱序", bad_content == 0, f"{bad_content}/{cases} 例不一致")
    check("fuzz: 没有超宽行", bad_width == 0, f"{bad_width} 行超宽")
    check("fuzz: 没有行首标点（width>=8）", bad_start == 0, f"{bad_start} 行以标点开头")
    check("fuzz: 含硬换行时内容与宽度仍正确", bad_nl == 0, f"{bad_nl}/1000 例异常")


async def run() -> None:
    fuzz_wrap()
    app = AgentTUI()
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        tr = app.query_one("#chat-panel", ChatTranscript)

        print("1. 助手 Markdown 渲染")
        check("chat 面板是 ChatTranscript", isinstance(tr, ChatTranscript))
        check("think 面板仍是 PanelLog",
              isinstance(app.query_one("#think-panel"), PanelLog))
        tr.start_assistant()
        md = "## Heading\n\nbody text\n\n```python\nx = 1\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        tr.append_chunk(md)
        await pilot.pause()
        joined = "\n".join(screen_lines(tr._live))
        check("不出现裸露 ```", "```" not in joined)
        check("不出现裸露 ##", "## Heading" not in joined)
        check("代码内容已渲染", "x = 1" in joined)
        check("标题文字保留", "Heading" in joined)
        check("表格已渲染（有分隔线）", "─" in joined)
        tr.finish_message()
        await pilot.pause()
        check("定型后 live 释放", tr._live is None)

        print("2. 用户输入原样显示")
        tricky = "[bold red]X[/bold red] **nb** `nc` [link]y[/link] ## nh"
        tr.add_user(tricky)
        await pilot.pause()
        um = tr.children[-1]
        ulines = " ".join(screen_lines(um))
        check("raw 精确保留", um.raw == tricky)
        check("字面保留 [bold red]", "[bold red]" in ulines)
        check("字面保留 **nb**", "**nb**" in ulines)
        check("字面保留 `nc`", "`nc`" in ulines)
        check("字面保留 ## nh（未被当标题）", "## nh" in ulines)

        print("3. 270 tok/s 压力")
        random.seed(0)
        toks = []
        for _ in range(270 * 30):            # 30 秒 @270 tok/s
            r = random.random()
            toks.append("思考" if r < 0.5 else
                        (" the " if r < 0.8 else
                         ("。" if r < 0.9 else "`c`")))
        src = "".join(toks)
        tr.clear_messages()
        app._status = "generating"
        tr.start_assistant()
        app._stream_buffer = ""
        app._stream_width = 0
        t0 = time.perf_counter()
        for tk in toks:
            app._handle_event({"type": "response_chunk", "content": tk})
        dur = time.perf_counter() - t0
        cap = len(toks) / dur
        check(f"处理能力 {cap:.0f} tok/s 远高于 270", cap > 270 * 3,
              f"{cap:.0f} tok/s, 余量 {cap/270:.1f}x")
        app._handle_event({"type": "__agent_done__"})
        await pilot.pause()
        final = tr.children[-1]
        check("端到端无丢字", final.raw == src,
              f"{len(final.raw)} vs {len(src)}")

        print("4. 滚动跟随（anchor 机制）")
        tr.clear_messages()
        await pilot.pause()
        for i in range(25):
            tr.add_user(f"turn {i}: " + "word " * 10)
            await pilot.pause()
        check("持续新内容时停在底部",
              tr.scroll_y >= tr.max_scroll_y - 1,
              f"y={tr.scroll_y} max={tr.max_scroll_y}")
        tr.scroll_to(y=0, animate=False)
        await pilot.pause()
        tr.add_user("while scrolled up")
        await pilot.pause()
        check("用户上翻时不被抢走位置", tr.scroll_y < 5, f"y={tr.scroll_y}")

        print("5. PanelLog resize 防抖")
        think = app.query_one("#think-panel", PanelLog)
        count = {"n": 0}
        orig = think.reflow

        def counted():
            count["n"] += 1
            orig()

        think.reflow = counted
        from textual.events import Resize
        from textual.geometry import Size
        for w in range(40, 70):              # 模拟拖拽窗口的连续 resize
            think._last_width = w - 1
            think.on_resize(Resize(Size(w, 20), Size(w - 1, 20), Size(w, 20)))
        check("resize 事件不立即重排", count["n"] == 0, f"立即触发 {count['n']} 次")
        await pilot.pause(0.4)
        check("防抖后只重排一次", count["n"] == 1, f"实际 {count['n']} 次")

        print("6. chat 面板双击展开/收起")
        # 聊天区几乎全被消息气泡覆盖，必须能从子控件上的双击触发
        app._restore_panels()
        await pilot.pause(0.3)
        tr.clear_messages()
        tr.add_user("click target message")
        await pilot.pause(0.3)

        # 取一个确定落在 chat 面板内、且可见的点
        vis = tr.scrollable_content_region
        x = vis.x + 2
        y = vis.y + max(1, vis.height // 2)
        hit = app.screen.get_widget_at(x, y)[0]
        check("点击目标位于 chat 面板内",
              isinstance(hit, (ChatTranscript, ChatMessage)),
              f"命中的是 {type(hit).__name__} @({x},{y})")

        async def dbl():
            await pilot.click(offset=(x, y))
            await pilot.pause(0.02)
            await pilot.click(offset=(x, y))
            await pilot.pause(0.3)
            return (app._expanded_panel, str(tr.styles.width))

        st = await dbl()
        check("在消息气泡上双击可展开 chat", st[0] == "chat-panel",
              f"expanded={st[0]!r} chat={st[1]}")
        check("展开时其它面板收缩为 0",
              str(app.query_one("#think-panel").styles.width) == "0")
        st = await dbl()
        check("再次双击可收起（不是单次 toggle 抵消）", st[0] == "",
              f"expanded={st[0]!r}")
        check("收起后三栏恢复等宽",
              str(tr.styles.width) == "1fr"
              and str(app.query_one("#think-panel").styles.width) == "1fr")

        print("7. 思考面板首次输出延迟")
        # 慢速模型 + 中文（每 token 2 字）下，阈值不能把首行压住太久
        think.clear()
        await pilot.pause()
        app._think_round = None
        slow = ["思考", "过程", "需要", "先读", "取文件", "然后", "分析"]
        t_start = time.monotonic()
        first_line_ms = None
        for tk in slow:                       # 8 tok/s，且不含换行与句末标点
            app._handle_event({"type": "thinking_chunk", "content": tk, "round": 1})
            await pilot.pause(0.12)
            if first_line_ms is None and len(think.lines) > 0:
                first_line_ms = (time.monotonic() - t_start) * 1000
        check("慢速模型 500ms 内出现首行思考内容",
              first_line_ms is not None and first_line_ms < 500,
              f"首行延迟 {first_line_ms:.0f} ms" if first_line_ms else "始终无输出")
        app._handle_event({"type": "__agent_done__"})
        await pilot.pause()
        shown = "".join("".join(s.text for s in ln) for ln in think.lines)
        check("思考内容无丢失", shown.replace(" ", "") == "".join(slow),
              f"{shown!r}")

        print("8. 面板折行宽度与展开重排")
        # RichLog.min_width 默认 78，会让文字永远按 78 列折行（无视面板实际宽度），
        # 表现为"只有 1/3 就换行"且展开后不重排。必须显式按内容宽度渲染。
        app._restore_panels()
        await pilot.pause(0.4)
        think.clear()
        await pilot.pause()
        panel_w = think.content_region.width
        check("think 面板窄于 min_width=78（否则测不出钳制）", panel_w < 78,
              f"width={panel_w}, min_width={think.min_width}")

        # 连续中文（无换行无标点），逐字喂入验证流式路径
        lengthy = "中文推理内容" * 30
        app._think_round = None
        for ch in lengthy:
            app._handle_event({"type": "thinking_chunk", "content": ch, "round": 1})
        app._flush_think()
        await pilot.pause()

        def think_line_lens():
            return [len("".join(s.text for s in think.lines[i]).rstrip())
                    for i in range(len(think.lines))]

        narrow_lens = think_line_lens()
        check("窄面板下按面板宽度折行（不是 78 列钳制的 39 字）",
              max(narrow_lens, default=0) <= panel_w,
              f"最长行 {max(narrow_lens, default=0)} vs 面板宽 {panel_w}")

        # 展开后必须重排：行变少（同样的字，更宽的行容纳更多）
        n_before = len(think.lines)
        app._expand_panel("think-panel")
        await pilot.pause(0.7)
        wide_w = think.content_region.width
        wide_lens = think_line_lens()
        n_after = len(think.lines)
        check("展开后确实重排（行数减少）", n_after < n_before,
              f"{n_before} -> {n_after} 行, 宽度 {panel_w} -> {wide_w}")
        check("展开后长行按新宽度折行",
              max(wide_lens, default=0) > max(narrow_lens, default=0),
              f"最长行 {max(narrow_lens, default=0)} -> {max(wide_lens, default=0)}")
        app._restore_panels()

        print("9. 中文禁则（标点不落行首）与整行折行")
        # Rich 的 divide_line/chop_cells 只按 cell 硬切，不管标点位置。面板宽度是
        # 奇数时，一行 22 个中文字恰好占 44 cell、剩 1 cell，而中文标点占 2 cell，
        # 于是每行的 "，" "。" 都会被挤到下一行行首。
        app._restore_panels()
        await pilot.pause(0.4)
        think.clear()
        await pilot.pause()
        pw = think.content_region.width
        check("面板宽度为奇数（最容易暴露行首标点）", pw % 2 == 1, f"width={pw}")

        sample = (
            "让我仔细想想这个问题。用户想要 Markdown 渲染效果，所以我需要先检查当前的实现方式。"
            "嗯，等一下，其实问题在于 RichLog 会把渲染宽度抬高到 min_width，也就是七十八列。"
            "所以文字是按三十九个中文字折行的，而不是按面板的实际宽度，"
            "这会让面板显得很空 ... 而且展开之后也不会重排。"
        )
        app._think_round = None
        for ch in sample:
            app._handle_event({"type": "thinking_chunk", "content": ch, "round": 1})
        app._flush_think()
        await pilot.pause()

        def think_rows():
            return ["".join(s.text for s in think.lines[i]).rstrip()
                    for i in range(len(think.lines))]

        rows = think_rows()
        cells = [sum(2 if ord(c) > 0x2E7F else 1 for c in r) for r in rows]
        bad = [(i, r[0]) for i, r in enumerate(rows)
               if r and r[0] in ",.!?;:，。！？；：、）】》"]
        check("没有行首标点", not bad, f"{bad}")
        # 禁则只允许挪换行点，不能改字符顺序（把标点提前到上一行会让连读时标点错位）
        check("禁则不丢字、不乱序",
              "".join(rows).replace(" ", "") == "".join(sample.split()),
              f"{''.join(rows).replace(' ', '')[:60]!r} vs {''.join(sample.split())[:60]!r}")
        check("没有超宽行", max(cells, default=0) <= pw, f"最宽 {max(cells, default=0)} vs {pw}")

        # 与"整段交给 Rich 原生折行"对比：Rich 按空白分词，整句中文会被当成一个
        # "词"而整句下移，那一行就白掉一大片；中文逐字折行应该明显更满。
        from rich.text import Text as _T
        native = [ln.plain.rstrip() for ln in _T(sample).wrap(think.app.console, pw)]
        ncells = [sum(2 if ord(c) > 0x2E7F else 1 for c in r) for r in native]
        check("行数不多于 Rich 原生折行", len(rows) <= len(native),
              f"禁则后 {len(rows)} 行 vs 原生 {len(native)} 行")

        def fill(seq):
            body = seq[:-1] or seq
            return sum(body) / len(body) / pw * 100

        check("填充率不低于 Rich 原生折行",
              fill(cells) >= fill(ncells) - 1.0,
              f"禁则后 {fill(cells):.1f}% vs 原生 {fill(ncells):.1f}%  (各行 {cells})")
        check("面板不空：填充率 >= 90%", fill(cells) >= 90,
              f"{fill(cells):.1f}%  (各行 {cells})")

        # 展开后必须能跨旧断点重新合并（不只是各自重排）
        n_before = len(rows)
        app._expand_panel("think-panel")
        await pilot.pause(0.7)
        wide_rows = think_rows()
        wide_cells = [sum(2 if ord(c) > 0x2E7F else 1 for c in r) for r in wide_rows]
        check("展开后跨旧断点合并（行数明显减少）", len(wide_rows) < n_before,
              f"{n_before} -> {len(wide_rows)} 行, 宽度 {pw} -> {think.content_region.width}")
        check("展开后最长行接近新的面板宽度",
              max(wide_cells, default=0) > max(cells, default=0),
              f"最长行 {max(cells, default=0)} -> {max(wide_cells, default=0)} cell")
        app._restore_panels()

        print("10. 思考面板 270 tok/s 压力（中英混排 + 标点）")
        # 思考面板的核心要求是"面对 270 tok/s 不能跟不上"。折行、禁则、残尾缓冲
        # 全在写入路径上，必须实测不能退化成瓶颈。
        app._restore_panels()
        await pilot.pause(0.4)
        think.clear()
        await pilot.pause()
        w_before = think.content_region.width
        app._think_round = None
        rnd2 = random.Random(7)
        heavy = []
        for _ in range(270 * 20):            # 20 秒 @270 tok/s
            r = rnd2.random()
            heavy.append("推理" if r < 0.45 else
                         (" min_width" if r < 0.70 else
                          ("，" if r < 0.85 else
                           ("。" if r < 0.95 else " ..."))))
        t0 = time.perf_counter()
        for tk in heavy:
            app._handle_event({"type": "thinking_chunk", "content": tk, "round": 1})
        app._flush_think()
        dur = time.perf_counter() - t0
        cap = len(heavy) / dur
        check(f"思考面板处理能力 {cap:.0f} tok/s 远高于 270", cap > 270 * 3,
              f"{cap:.0f} tok/s, 余量 {cap/270:.1f}x, 单 tick {dur/len(heavy)*1e6:.0f} µs")
        await pilot.pause()

        hrows = think_rows()
        hcells = [sum(2 if ord(c) > 0x2E7F else 1 for c in r) for r in hrows]
        shown = "".join(hrows).replace(" ", "")
        expect = "".join(heavy).replace(" ", "")
        check("思考内容无丢失（含标点）", shown == expect,
              f"{len(shown)} vs {len(expect)} 字")
        hbad = [r[0] for r in hrows if r and r[0] in _LEAD_PUNCT]
        check("高压下也没有行首标点", not hbad, f"{hbad[:8]}")
        check("高压下没有超宽行", max(hcells, default=0) <= w_before,
              f"最宽行 {max(hcells, default=0)} cell vs 折行宽度 {w_before}")
        now_w = think.content_region.width
        check("滚动条出现不改变折行宽度（否则会裁掉最后一格）",
              max(hcells, default=0) <= now_w,
              f"最宽行 {max(hcells, default=0)} vs 当前可见宽 {now_w}（折行时 {w_before}）")
        hfill = (sum(hcells[:-1]) / len(hcells[:-1]) / w_before * 100) if len(hcells) > 1 else 0
        check("高压下填充率 >= 90%", hfill >= 90, f"{hfill:.1f}%")

        print("11. 大文本量下的展开/收回（不重排看不见的面板 + 整段批量铺行）")
        app._restore_panels()
        await pilot.pause(0.4)
        think.clear()
        await pilot.pause()
        app._think_round = None
        big = "大文本量下的思考内容需要能整段重排" * 1200   # 约 2 万字
        for ch in big:
            app._handle_event({"type": "thinking_chunk", "content": ch, "round": 1})
        app._flush_think()
        await pilot.pause(0.3)

        def joined_rows():
            return "".join("".join(s.text for s in ln) for ln in think.lines).replace(" ", "")

        narrow_lines = len(think.lines)
        check("万字级内容已折行上屏", narrow_lines > 100, f"{narrow_lines} 行")

        # (a) 展开别的面板把本面板挤成 0 宽时，绝不能按回落宽度折行
        #     旧实现回落到 max(2, size.width)=2，1 万字会折成 2500 行（实测 20k 字
        #     要 750ms、60k 字要 2.4s、150k 字要 5.7s，UI 全程冻住）。
        reflows = {"n": 0}
        orig_reflow = think.reflow

        def counted_reflow():
            reflows["n"] += 1
            orig_reflow()

        think.reflow = counted_reflow
        try:
            app._expand_panel("chat-panel")
            await pilot.pause(0.5)
            check("不可见的面板不重排", reflows["n"] == 0, f"重排了 {reflows['n']} 次")
            check("不可见时不按 2 cell 折行", len(think.lines) == narrow_lines,
                  f"{narrow_lines} -> {len(think.lines)} 行")
            check("不可见时面板宽度确实是 0", think.content_region.width == 0,
                  f"width={think.content_region.width}")

            # (b) 隐藏期间继续流式写入：不上屏但必须记账
            #     这里**不能**再置 _think_round = None：轮次一变 _h_thinking_chunk 就会
            #     begin_block，而 begin_block 会把上一块自动折起 → 那 2 万字只剩标题 +
            #     尾部预览，(c) 的"内容没有丢"自然就对不上了。判据是"还是同一轮"，
            #     所以本节的 big + hidden 必须落在同一个块里（见 (c) 的块数断言）。
            hidden = "隐藏期间继续输出的思考内容也不会丢" * 20
            for ch in hidden:
                app._handle_event({"type": "thinking_chunk", "content": ch, "round": 1})
            app._flush_think()
            await pilot.pause(0.2)
            check("隐藏期间不上屏", len(think.lines) == narrow_lines,
                  f"{narrow_lines} -> {len(think.lines)} 行")

            # (c) 重新可见：一次性重排，隐藏期间的文字全部回来
            app._restore_panels()
            await pilot.pause(0.6)
            check("重新可见时重排一次", reflows["n"] == 1, f"{reflows['n']} 次")
            rows = joined_rows()
            check("隐藏期间的内容没有丢", rows == (big + hidden).replace(" ", ""),
                  f"{len(rows)} vs {len(big + hidden)} 字")
            # 重排结果必须与纯折行函数逐行一致：直接铺 Strip 绕开了 RichLog.write，
            # 少一行、多一行、或哪一行被截断了都要在这里暴露。
            # 这条等式只在"本节内容全在同一个无标题块里"时成立：块与块之间各自独立
            # wrap_cjk，块边界不会像旧单缓冲那样跨块合并，块数-1 行、再加每块的标题行
            # 都会让行数与单次 wrap_cjk 不一致。所以先钉住这个前提。
            check("本节内容在一个无标题块里（块边界不跨块合并折行）",
                  len(think._blocks) == 1 and not think._blocks[0].has_header,
                  f"{len(think._blocks)} 块, has_header="
                  f"{[b.has_header for b in think._blocks]}")
            expect = wrap_cjk(big + hidden, think.content_region.width)
            got = ["".join(s.text for s in ln) for ln in think.lines]
            check("重排逐行等于 wrap_cjk 的结果", got == expect,
                  f"{len(got)} 行 vs 期望 {len(expect)} 行")
            bad = [r for r in ("".join(s.text for s in ln).rstrip() for ln in think.lines)
                   if r and r[0] in _LEAD_PUNCT]
            check("重排后没有行首标点", not bad, f"{bad[:6]}")
        finally:
            think.reflow = orig_reflow

        # (d) 整段重排不能逐行调 RichLog.write：RichLog.write 每次都要跑一遍 Rich
        #     渲染 + 建 Strip + 写 virtual_size（reactive），2251 行要 42ms，而同样
        #     这些行自己建 Strip 只要 6ms。
        writes = {"n": 0}
        orig_write = RichLog.write

        def counted_write(self, *a, **kw):
            writes["n"] += 1
            return orig_write(self, *a, **kw)

        RichLog.write = counted_write
        try:
            t0 = time.perf_counter()
            think.reflow()
            dur = (time.perf_counter() - t0) * 1000
        finally:
            RichLog.write = orig_write
        check("整段重排不再逐行 write", writes["n"] <= 2 and len(think.lines) > 100,
              f"{len(think.lines)} 行只调了 {writes['n']} 次 RichLog.write")
        check("整段重排耗时 < 120ms（旧实现同量级要 45~110ms）", dur < 120,
              f"{dur:.0f} ms / {len(think.lines)} 行")

        # (e) 聊天面板被挤成 0 宽时，不能退化成逐字重渲染整段 Markdown（O(n²)）
        app._restore_panels()
        await pilot.pause(0.3)
        tr.clear_messages()
        app._expand_panel("think-panel")     # chat 面板宽度归 0
        await pilot.pause(0.4)
        check("chat 面板被挤成 0 宽", tr.content_region.width == 0,
              f"width={tr.content_region.width}")
        updates = {"n": 0}
        orig_update = ChatMessage.set_text

        def counted_update(self, *a, **kw):
            updates["n"] += 1
            return orig_update(self, *a, **kw)

        ChatMessage.set_text = counted_update
        try:
            app._status = "generating"
            tr.start_assistant()
            app._stream_buffer = ""
            app._stream_width = 0
            for _ in range(3000):
                app._handle_event({"type": "response_chunk", "content": "思"})
            app._flush_stream()
            await pilot.pause(0.2)
        finally:
            ChatMessage.set_text = orig_update
        check("隐藏的 chat 面板不逐字重渲染", updates["n"] <= 20,
              f"3000 字只重渲染了 {updates['n']} 次")
        check("隐藏期间回复内容不丢",
              tr.children[-1].raw == "思" * 3000,
              f"{len(tr.children[-1].raw)} 字")
        app._restore_panels()
        await pilot.pause(0.3)

        # (f) 长会话：重播历史可以远超面板容量（5000 行），只该重排还能上屏的尾部。
        #     实测 150 万字历史整段重排要 499ms，只折尾部后与历史长度无关。
        think.clear()
        think._history.clear()
        for r in range(240):                 # 约 120 万字，远超 5000 行
            think._history.append(("t", [f"第{r:03d}轮" + "中" * 4990]))
        import tui as _tui
        wrapped = {"chars": 0}
        orig_wrap = _tui.wrap_cjk

        def counting_wrap(text, width):
            wrapped["chars"] += len(text)
            return orig_wrap(text, width)

        _tui.wrap_cjk = counting_wrap
        try:
            t0 = time.perf_counter()
            think.reflow()
            dur = (time.perf_counter() - t0) * 1000
        finally:
            _tui.wrap_cjk = orig_wrap
        rows = ["".join(s.text for s in ln) for ln in think.lines]
        joined = "".join(rows)
        check("超长历史只保留面板容量内的行", len(think.lines) == think.max_lines,
              f"{len(think.lines)} 行（max_lines={think.max_lines}）")
        check("只折尾部，不折整段历史", wrapped["chars"] < 250000,
              f"120 万字历史只折了 {wrapped['chars']} 字")
        check("保留的是最新的尾部内容",
              "第239轮" in joined and "第000轮" not in joined and "第200轮" not in joined,
              f"首个可见轮次 {rows[0][:12] if rows else ''!r}")
        check("超长历史的重排耗时 < 200ms", dur < 200, f"{dur:.0f} ms / {len(rows)} 行")

        print("12. 思考面板折叠（Ctrl+T）与轮次徽标")
        # 折叠只改宽度、不动内容：折叠期间思考照常写入 PanelLog，展开时由 reflow
        # 原样铺回来。这条依赖"0 宽不折行、只记历史"（见第 11 节），所以在这里
        # 端到端验一遍。
        app._restore_panels()
        await pilot.pause(0.3)
        think.clear()
        await pilot.pause(0.1)
        app._think_round = None
        app._think_round_count = 0
        tool = app.query_one("#tool-panel", PanelLog)
        footer = app.query_one("#footer-bar", Static)

        await pilot.press("ctrl+t")
        await pilot.pause(0.3)
        check("Ctrl+T 折叠思考面板（宽度 0，其它等分）",
              str(think.styles.width) == "0" and str(tr.styles.width) == "1fr"
              and str(tool.styles.width) == "1fr",
              f"think={think.styles.width} chat={tr.styles.width} tool={tool.styles.width}")

        folded = "折叠期间思考照常写入但不上屏" * 40
        for ch in folded:
            app._handle_event({"type": "thinking_chunk", "content": ch, "round": 1})
        app._flush_think()
        await pilot.pause(0.2)
        check("折叠期间不上屏（内容只在重播历史里）", len(think.lines) == 0,
              f"{len(think.lines)} 行")
        check("轮次计数 +1", app._think_round_count == 1, f"count={app._think_round_count}")
        check("面板边框标题带轮次", "(1)" in (think.border_title or ""),
              f"{think.border_title!r}")
        check("折叠时底栏出现 🧠 徽标", "🧠 1" in str(footer.content),
              f"{str(footer.content)[:60]!r}")

        # 前缀缓存命中率（改动 1 的可见部分）：provider 报了才显示，不报就不显示——
        # 显示成 0% 会把"没有统计"误报成"缓存完全没命中"。
        app._handle_event({"type": "stream_usage", "usage": None,
                           "cache": {"hit_ratio": 0.75}})
        await pilot.pause(0.1)
        check("底栏显示前缀缓存命中率", "缓存 75%" in str(footer.content),
              f"{str(footer.content)[:70]!r}")
        app._handle_event({"type": "token_usage", "usage": None})  # 该次没有 cache 字段
        await pilot.pause(0.1)
        check("provider 不报缓存时保留上次值（不假装 0%）",
              "缓存 75%" in str(footer.content), f"{str(footer.content)[:70]!r}")

        # 同一轮内继续出字不该重复计数
        app._handle_event({"type": "thinking_chunk", "content": "同一轮续写", "round": 1})
        app._flush_think()
        await pilot.pause(0.1)
        check("同一轮内不重复计数", app._think_round_count == 1,
              f"count={app._think_round_count}")
        app._handle_event({"type": "thinking_chunk", "content": "第二轮", "round": 2})
        app._flush_think()
        await pilot.pause(0.1)
        check("换轮次才计数", app._think_round_count == 2, f"count={app._think_round_count}")

        await pilot.press("ctrl+t")
        await pilot.pause(0.6)
        check("再按一次 Ctrl+T 恢复等分", str(think.styles.width) == "1fr",
              f"think={think.styles.width}")

        # 新行为：一轮结束（下一轮开始）后该块自动折叠，只留标题 + 尾部预览；
        # 正在写入的块保持展开。全文要展开才看得到（z 键 / expand_all）。
        check("已完成块自动折叠、当前块保持展开",
              [b.collapsed for b in think._blocks] == [True, False],
              f"{[b.collapsed for b in think._blocks]}")
        folded_view = "".join("".join(s.text for s in ln) for ln in think.lines).replace(" ", "")
        check("折叠块只留标题与尾部预览（正文不再铺出）",
              folded_view.count(folded) == 0 and "Round2" in folded_view
              and len(folded_view) < len(folded),
              f"折叠视图 {len(folded_view)} 字 / 全文 {len(folded)} 字")

        think.expand_all()
        await pilot.pause(0.3)
        rows = "".join("".join(s.text for s in ln) for ln in think.lines).replace(" ", "")
        check("折叠期间的思考内容原样回来",
              rows.startswith(folded + "同一轮续写") and rows.endswith("第二轮"),
              f"{len(rows)} 字，开头 {rows[:12]!r}，结尾 {rows[-10:]!r}")
        check("展开后底栏不再显示徽标", "🧠" not in str(footer.content),
              f"{str(footer.content)[:60]!r}")

        # 聚焦折叠的面板要先把它放出来，否则焦点落在 0 宽控件上、按键如同消失
        await pilot.press("ctrl+t")
        await pilot.pause(0.3)
        app.action_focus_panel("think")
        await pilot.pause(0.3)
        check("聚焦折叠的面板会把它放出来", str(think.styles.width) == "1fr",
              f"think={think.styles.width}")

        # 展开态与折叠态互斥：折叠要连同双击展开一起收起
        app._expand_panel("think-panel")
        await pilot.pause(0.2)
        check("双击展开时独占 1fr", str(think.styles.width) == "1fr"
              and str(tr.styles.width) == "0",
              f"think={think.styles.width} chat={tr.styles.width}")
        await pilot.press("ctrl+t")
        await pilot.pause(0.3)
        check("折叠会连同展开态一起收起",
              app._expanded_panel == "" and str(think.styles.width) == "0",
              f"expanded={app._expanded_panel!r} think={think.styles.width}")
        await pilot.press("ctrl+t")
        await pilot.pause(0.3)
        check("再次恢复为三栏等分",
              str(think.styles.width) == "1fr" and str(tr.styles.width) == "1fr",
              f"think={think.styles.width} chat={tr.styles.width}")

        print("13. 折叠功能的边角：焦点让位、跨问题轮次、/clear 归零")
        # 聚焦某个面板时，它可能被 0 宽挡住：自己处于折叠态，或别的面板正占满宽度。
        # 不处理的话焦点落在 0 宽控件上，按键如同消失。
        app._restore_panels()
        await pilot.pause(0.2)
        app._expand_panel("chat-panel")
        await pilot.pause(0.2)
        # 直接调 action（焦点在输入框时按 "2" 会被 TextArea 吃掉，那是既有行为）
        app.action_focus_panel("think")
        await pilot.pause(0.3)
        check("聚焦被展开态挡住的面板会先收起展开态",
              app._expanded_panel == "" and str(think.styles.width) == "1fr"
              and app.focused is think,
              f"expanded={app._expanded_panel!r} think={think.styles.width} "
              f"focus={type(app.focused).__name__}")

        # 折叠/展开互斥是双向的：展开思考面板时也要把挡着它的展开态收起来，
        # 否则"展开了"但仍是 0 宽，按键看起来毫无反应。
        app._expand_panel("chat-panel")
        await pilot.pause(0.2)
        await pilot.press("ctrl+t")
        await pilot.pause(0.3)
        check("折叠不影响别的面板的展开态",
              app._expanded_panel == "chat-panel" and str(think.styles.width) == "0",
              f"expanded={app._expanded_panel!r} think={think.styles.width}")
        await pilot.press("ctrl+t")
        await pilot.pause(0.3)
        check("展开思考面板会收起挡着它的展开态",
              app._expanded_panel == "" and str(think.styles.width) == "1fr",
              f"expanded={app._expanded_panel!r} think={think.styles.width}")

        # runner 每个问题都从 round=1 重新数，所以"新问题"必须被认成新轮次：
        # 否则第二问的第 1 轮不出分隔线（两问思考连成一片）且计数少一次。
        app._restore_panels()
        await pilot.pause(0.2)
        think.clear()
        app._think_round = 1
        app._think_round_count = 1
        launched = []
        orig_run_agent = app._run_agent
        app._run_agent = lambda q: launched.append(q)   # 别真起 agent 线程
        textarea = app.query_one("#input-area", TextArea)
        try:
            textarea.text = "第二个问题"
            app.action_submit_input()
            await pilot.pause(0.2)
        finally:
            app._run_agent = orig_run_agent
            app._agent_busy = False
            textarea.disabled = False
        check("提交新问题会重置轮次",
              launched == ["第二个问题"] and app._think_round is app._NEW_QUESTION,
              f"launched={launched} think_round={app._think_round!r}")
        app._handle_event({"type": "thinking_chunk", "content": "第二问的第一轮", "round": 1})
        app._flush_think()
        await pilot.pause(0.2)
        sep = "".join("".join(s.text for s in ln) for ln in think.lines).replace(" ", "")
        check("第二问的第 1 轮被当成新轮次（打分隔线 + 计数 +1）",
              app._think_round_count == 2 and "Round1" in sep,
              f"count={app._think_round_count} 分隔线={'Round1' in sep}")

        # /clear 清空思考面板后，轮次徽标不能留着旧计数
        textarea.text = "/clear"
        app.action_submit_input()
        await pilot.pause(0.3)
        check("/clear 后轮次与徽标一起归零",
              app._think_round_count == 0 and app._think_round is None
              and len(think.lines) == 0 and "(1)" not in (think.border_title or ""),
              f"count={app._think_round_count} 行={len(think.lines)} "
              f"title={think.border_title!r}")

        print("14. 块级选择：j/k 选一轮、Enter/单击标题只展开那一轮")
        # 用户反馈原话："不知道怎么选中特定回合展开" —— 只有 z（全折/全开）时确实是
        # 全有或全无，所以这里钉住"能选中单块"的三条入口：键盘光标、Enter、鼠标单击。
        app._restore_panels()
        await pilot.pause(0.2)
        think.clear()
        app._think_round = None
        for rnd in (1, 2, 3):
            for ch in f"第{rnd}轮的思考内容":
                app._handle_event({"type": "thinking_chunk", "content": ch, "round": rnd})
            app._flush_think()
        await pilot.pause(0.3)
        check("三个块都建出来了", len(think._blocks) == 3, f"{len(think._blocks)} 块")
        check("光标默认落在最新块上", think._cursor == 2, f"cursor={think._cursor}")
        check("已完成块自动折叠", [b.collapsed for b in think._blocks] == [True, True, False],
              f"{[b.collapsed for b in think._blocks]}")

        app.action_focus_panel("think")
        await pilot.pause(0.3)
        check("聚焦思考面板时边框副标题给出键位提示",
              "j/k" in (think.border_subtitle or ""), f"{think.border_subtitle!r}")

        await pilot.press("k")
        await pilot.pause(0.3)
        check("k 把光标上移一块", think._cursor == 1, f"cursor={think._cursor}")
        picked = think._blocks[1]
        rendered = "".join("".join(s.text for s in ln) for ln in think.lines)
        check("选中的块标题反白标记出现在屏幕上",
              rendered.count("▶") == 1 and "Round 2" in rendered.split("▶")[1][:40],
              f"▶×{rendered.count('▶')}")

        before = [b.collapsed for b in think._blocks]
        await pilot.press("enter")
        await pilot.pause(0.3)
        after = [b.collapsed for b in think._blocks]
        check("Enter 只翻转选中那一块", before[1] and not after[1] and before[0] == after[0]
              and before[2] == after[2], f"{before} -> {after}")
        check("展开的那一块正文回到屏幕上", "第2轮的思考内容" in
              "".join("".join(s.text for s in ln) for ln in think.lines), "")

        # 鼠标入口：行号 → 块 的映射（点标题行只折它自己；点正文不折、留给双击放大）
        title_line = think._block_line[picked.key]
        check("标题行映射回它自己", think.block_at_line(title_line) is picked,
              f"line={title_line}")
        check("展开态只有标题行算点击区",
              think.is_title_click(picked, title_line) and not picked.collapsed
              and not think.is_title_click(picked, title_line + 3),
              f"title={title_line} collapsed={picked.collapsed}")
        picked.collapsed = True
        think.reflow()
        await pilot.pause(0.2)
        check("折叠态连尾部预览行一起算点击区（否则预览行点了没反应）",
              think.is_title_click(picked, title_line + 1)
              and not think.is_title_click(picked, title_line + 2),
              f"title={title_line}")
        picked.collapsed = False  # 还原成 [True, False, False]，后面几条断言要干净状态
        think.reflow()
        await pilot.pause(0.2)

        think._cursor = 0
        await pilot.press("enter")
        await pilot.pause(0.3)
        check("再选第一块并展开（逐块独立）",
              [b.collapsed for b in think._blocks] == [False, False, False],
              f"{[b.collapsed for b in think._blocks]}")

        await pilot.press("z")
        await pilot.pause(0.3)
        check("z 仍是整体折叠（当前块除外）",
              [b.collapsed for b in think._blocks] == [True, True, False],
              f"{[b.collapsed for b in think._blocks]}")
        await pilot.press("o")
        await pilot.pause(0.3)
        check("o 展开全部（幂等）", all(not b.collapsed for b in think._blocks),
              f"{[b.collapsed for b in think._blocks]}")

        # 真鼠标点击：坐标换算（边框 + 滚动）必须落到"那一行"，否则点谁都折第一块
        think.expand_all()
        await pilot.pause(0.3)
        target = think._blocks[1]
        click_line = think._block_line[target.key]
        click_y = 1 + (click_line - int(think.scroll_offset.y))  # +1 = 上边框那一行
        collapsed_before = [b.collapsed for b in think._blocks]
        await pilot.click("#think-panel", offset=(3, max(0, click_y)))
        await pilot.pause(0.3)
        collapsed_after = [b.collapsed for b in think._blocks]
        check("真鼠标点标题行：只有被点的那一块折叠",
              (not collapsed_before[1]) and collapsed_after[1]
              and collapsed_after[0] == collapsed_before[0]
              and collapsed_after[2] == collapsed_before[2],
              f"y={click_y} {collapsed_before} -> {collapsed_after}")

        # 点正文不该折块（留双击放大面板）。用**最后一块**的正文行：它下面没有别的块，
        # 不会因为上一块折叠导致行号移位而误点到隔壁块的标题。
        think.expand_all()
        await pilot.pause(0.3)
        last = think._blocks[-1]
        body_y = 1 + (think._block_line[last.key] + 1 - int(think.scroll_offset.y))
        collapsed_before = [b.collapsed for b in think._blocks]
        await pilot.click("#think-panel", offset=(3, max(0, body_y)))
        await pilot.pause(0.3)
        check("真鼠标点正文不会折块",
              [b.collapsed for b in think._blocks] == collapsed_before,
              f"y={body_y} {collapsed_before}")

        keys = {b.key for b in PanelLog.BINDINGS}
        check("块操作键位绑在面板上（只在聚焦时生效）",
              {"j", "k", "enter", "space"} <= keys, f"{sorted(keys)}")
        check("整体操作键位留在 App 上（输入框里也能按）",
              {"z", "o"} <= {b.key for b in AgentTUI.BINDINGS}, "")


def main() -> None:
    asyncio.run(run())
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项失败: {FAILURES}")
        sys.exit(1)
    print("test_tui_markdown: 全部通过")


if __name__ == "__main__":
    main()
