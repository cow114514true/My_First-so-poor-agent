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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 上一次运行留下的 autosave 会在启动时弹出恢复对话框，盖住屏幕把后面的鼠标
# 命中测试全部带偏。测试自己造干净环境。
_SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversations")
for _f in glob.glob(os.path.join(_SAVE_DIR, ".autosave*.json")):
    try:
        os.remove(_f)
    except OSError:
        pass

from tui import AgentTUI, ChatTranscript, ChatMessage, PanelLog  # noqa: E402

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


def main() -> None:
    asyncio.run(run())
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项失败: {FAILURES}")
        sys.exit(1)
    print("test_tui_markdown: 全部通过")


if __name__ == "__main__":
    main()
