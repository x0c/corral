#!/usr/bin/env python3
"""用 Textual Pilot 渲染真实 TUI 并导出截图（SVG → PNG），供 README / 改动验收。

不读真实用户历史：夹具会话内容是虚构的。依赖：textual；SVG→PNG 优先 cairosvg
（`pip install cairosvg`），否则回退 ImageMagick `convert`（对 Rich 的 clipPath
文字支持很差，通常会出空白图，不推荐）。

用法（在 cli/ 目录）：

    python3 docs/screenshots/capture.py

产物写入本目录：list.png（左栏列表 + 右栏完整对话预览；无 Rich 假窗口边框）。

**NO_COLOR：** 许多 CI / Agent 环境默认 `NO_COLOR=1`。Textual 会启用 Monochrome
滤镜，整屏真彩变灰阶。本脚本在创建 App 前清除该变量；不要在带着 NO_COLOR 的
壳里另写绕过路径。

真机运行中的 TUI 请用 **F12**（`MainScreen.action_save_screenshot`）导出到
`~/.cache/pickup/screenshots/`；勿把含真实对话的截图提交进仓库。
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Textual 在 App.__init__ 里若见到 NO_COLOR 会启用 Monochrome；必须在创建
# PickupApp 之前清掉。setdefault 不覆盖调用方已显式设置的真彩 / 语言。
os.environ.pop("NO_COLOR", None)
os.environ.setdefault("COLORTERM", "truecolor")
os.environ.setdefault("PICKUP_LANG", "en")
# 侧边栏记忆（会话组/置顶/显隐）只读临时库，绝不读写机主真实 ~/.cache/pickup。
# 库路径认 PICKUP_CACHE_DIR > XDG > ~/.cache，且设了该变量时旧 JSON 迁移也只在
# 这个目录里找，正好顺带隔离。
_CAPTURE_CACHE_DIR = tempfile.mkdtemp(prefix="pickup-capture-cache-")
os.environ["PICKUP_CACHE_DIR"] = _CAPTURE_CACHE_DIR

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import pickup
from pickup.models import ConversationMessage
from pickup import session_key
from pickup.split_layout import _FRUIT_EMOJI
from pickup.ui.app import PickupApp


OUT_DIR = Path(__file__).resolve().parent

# Rich/Textual SVG 默认 Fira Code，本机常无 CJK；换成 mono+CJK 本地字体，避免豆腐块。
# CJK 字体必须排在最前：cairosvg 不做逐字形回退，整个文本段只用第一个可用
# 字体族，把非 CJK 的 "Noto Sans Mono" 前置会让全部中文变成豆腐块（实测）。
_FONT_CSS = '"Noto Sans Mono CJK SC", "Noto Sans CJK SC", "Droid Sans Fallback", monospace'
# 代价：`●`（U+25CF）的 East Asian Width 是 Ambiguous，CJK 字体按两格宽画，会
# 盖掉后面的分隔空格，出图看着像「●项目」。真实终端按一格推进（已实测），所以
# 这是纯出图字体现象；用 _NARROW_GLYPH_FONT 只给这个字形单独换族来修，不要去改
# 产品侧的圆点字符或间距。
# 单引号故意的：这串会写进 SVG 的 style="…" 属性里，用双引号会把属性提前闭合。
_NARROW_GLYPH_FONT = "'Noto Sans Mono', 'DejaVu Sans Mono', monospace"
_NARROW_GLYPHS = "●"

# 会话组名前的水果 emoji：session_list.py 把它单独成一个 style span（见该文件
# render() 注释），这里才能像圆点一样按字形单独换成彩色 emoji 字体——
# cairosvg 不做逐字形回退，跟 CJK 正文共用字体族只会画出方框。本机没有
# fonts-noto-color-emoji 时同样会变豆腐块，跟 CJK 缺字体是同类出图环境问题，
# 不代表产品有问题（真实终端由终端自身的 emoji 字体回退渲染，不受此限）。
_EMOJI_FONT = "'Noto Color Emoji', 'Apple Color Emoji', 'Segoe UI Emoji', sans-serif"
_FRUIT_EMOJI_GLYPHS = "".join(sorted(set(_FRUIT_EMOJI.values())))

# 演示用外层终端底色：对齐左栏列表空区实测色 (#1e242b)，避免右栏垫成
# pickup-dark $background (#0d1117) 后出现「半边深半边浅」的割裂感。
# osc_report=None 时 EmbedPane 不设 styles.background，空白格会透成纯黑。
_DEMO_BG_HEX = "#1e242b"
_DEMO_OSC_REPORT = b"\x1b]11;rgb:1e1e/2424/2b2b\x07"


# 演示会话的时间：按「刚刚 / 快一小时前 / 大半天前」铺开，让侧边栏时间行的
# 亮度梯度（越新越亮）在截图里能看出来。取相对时间而不是写死时间戳，否则三条
# 会话永远落在最旧那一档，改动验收时看不出差别。
_DEMO_AGES = (90.0, 55 * 60.0, 7 * 3600.0)


def _demo_store():
    import time as _time

    now = _time.time()
    sessions = [
        {
            "source": "claude",
            "id": "demo-claude-1",
            "short_id": "demo1",
            "mtime": now - _DEMO_AGES[0],
            "size_bytes": 4096,
            "size_kb": 4.0,
            "native_title": "Fix login flake",
            "fallback_title": "Fix login flake",
            "cwd": "/Users/demo/Codes/webapp",
            "cwd_display": "~/Codes/webapp",
            "live": True,
            "path": "/tmp/demo-claude-1.jsonl",
            "first_user_msg": "登录偶发失败，帮我定位",
            "last_user_msg": "再补一组回归测试",
            "last_agent_msg": "已加上 flaky 重试与断言",
        },
        {
            "source": "cursor",
            "id": "demo-cursor-1",
            "short_id": "democ1",
            "mtime": now - _DEMO_AGES[1],
            "size_bytes": 2048,
            "size_kb": 2.0,
            "native_title": "Add Cursor runtime",
            "fallback_title": "Add Cursor runtime",
            "cwd": "/Users/demo/Codes/pickup",
            "cwd_display": "~/Codes/pickup",
            "live": True,
            "path": "/tmp/demo-cursor-1",
            "first_user_msg": "帮我加上 cursor-cli 支持",
            "last_user_msg": "右栏统一完整预览",
            "last_agent_msg": "已改选中即预览",
        },
        {
            "source": "codex",
            "id": "demo-codex-1",
            "short_id": "demox1",
            "mtime": now - _DEMO_AGES[2],
            "size_bytes": 1024,
            "size_kb": 1.0,
            "native_title": "Tighten handoff prompt",
            "fallback_title": "Tighten handoff prompt",
            "cwd": "/Users/demo/Codes/pickup",
            "cwd_display": "~/Codes/pickup",
            "live": False,
            "path": "/tmp/demo-codex-1.jsonl",
            "first_user_msg": "接力提示词太散",
            "last_user_msg": "再压一版摘录",
            "last_agent_msg": "已收敛 digest 字段",
        },
    ]
    conversations = {
        "claude:demo-claude-1": [
            ConversationMessage("user", "登录偶发失败，帮我定位"),
            ConversationMessage(
                "assistant",
                "根因是并发下 session cookie 被覆盖。已加锁并补 flaky 回归。",
            ),
            ConversationMessage("user", "再补一组回归测试"),
            ConversationMessage("assistant", "已加上重试与断言，本地全绿。"),
        ],
        "cursor:demo-cursor-1": [
            ConversationMessage("user", "帮我加上 cursor-cli 支持"),
            ConversationMessage("assistant", "已按完整适配器接入扫描/恢复/接力/直启。"),
            ConversationMessage("user", "右栏统一完整预览"),
            ConversationMessage("assistant", "选中即完整对话；进行中仍走内嵌实时窗口。"),
        ],
        "codex:demo-codex-1": [
            ConversationMessage("user", "接力提示词太散"),
            ConversationMessage("assistant", "已把摘录收敛到原始需求 + 最近对话。"),
        ],
    }
    # 截图用稳定「已生成」标题，避免转圈兜底文案进 README
    demo_titles = {
        "claude:demo-claude-1": "Fix login flake",
        "cursor:demo-cursor-1": "Add Cursor runtime",
        "codex:demo-codex-1": "Tighten handoff prompt",
    }

    from unittest import mock
    from pickup.attention import AttentionState
    from pickup.runtime import RuntimeRegistry

    runtimes = []
    for rid, name in (("claude", "Claude"), ("cursor", "Cursor"), ("codex", "Codex")):
        rt = mock.Mock()
        rt.id = rid
        rt.display_name = name
        rt.is_available.return_value = True
        rt.scan_signature.return_value = None
        own = [s for s in sessions if s["source"] == rid]
        rt.scan_sessions.return_value = own
        rt.load_conversation.side_effect = (
            lambda session, _rid=rid: list(conversations.get(f"{session['source']}:{session['id']}", []))
        )
        runtimes.append(rt)

    registry = RuntimeRegistry(tuple(runtimes))
    # 截图夹具不得读写真实用户的关注状态库；用内存 mock 固定三种演示状态。
    attention_store = mock.Mock()
    attention_store.reconcile.return_value = {}
    with mock.patch.object(pickup.titles, "load_cache", return_value={}):
        store = pickup.SessionStore(
            limit=20,
            registry=registry,
            attention_store=attention_store,
        )
        store.load()
    # 注意：all_sessions() 自己会拿 store.lock；不可在持锁时再调，否则死锁。
    sessions_now = store.all_sessions()
    with store.lock:
        store.generating.clear()
        for session in sessions_now:
            key = session_key(session)
            store.display_titles[key] = demo_titles[key]
        # 三张卡分别覆盖：黄点等待回答、绿点执行中、红点新结果。
        attention_kinds = {
            "claude:demo-claude-1": "waiting",
            "cursor:demo-cursor-1": "working",
            "codex:demo-codex-1": "unread",
        }
        for session in sessions_now:
            key = session_key(session)
            kind = attention_kinds[key]
            token = f"demo-{kind}-token"
            session["attention_kind"] = kind
            session["attention_token"] = token
            session["attention_updated_at"] = session["mtime"]
            store.attention_states[key] = AttentionState(
                kind=kind,
                activity_token=token,
                updated_at=session["mtime"],
            )
    for session in sessions_now:
        store.get_conversation(session)
    return store


def _terminal_background_hex(svg_text: str) -> str:
    """终端底色：优先演示 OSC / pickup-dark，否则用 SVG 里已出现的同系深色。

    Rich 只给「有内容的格子」画背景 rect；空白格透明。去掉假窗口铬后若不再
    垫一层底，cairosvg 会把透明渲成纯黑，右栏大片空洞 vs 左栏石板色——半边黑。
    """
    lowered = svg_text.lower()
    for candidate in (_DEMO_BG_HEX, "#161b22", "#1c2430"):
        if candidate in lowered:
            return candidate
    chrome = re.search(
        r'<rect\b[^>]*\bfill="(#[0-9A-Fa-f]{6})"[^>]*\brx="8"',
        svg_text,
    )
    if chrome:
        return chrome.group(1).lower()
    return _DEMO_BG_HEX


def _strip_window_chrome(svg_text: str) -> str:
    """去掉 Rich SVG 假 macOS 标题栏，只留终端内容区，并垫上不透明终端底色。"""
    bg = _terminal_background_hex(svg_text)
    # 三色点
    svg_text = re.sub(
        r'<g transform="translate\(26,\s*22\)">\s*'
        r"(?:<circle\b[^/]*/>\s*){3}"
        r"</g>\s*",
        "",
        svg_text,
        flags=re.S,
    )
    # 标题「pickup」
    svg_text = re.sub(
        r'<text class="[^"]*-title"[^>]*>.*?</text>\s*',
        "",
        svg_text,
        flags=re.S,
    )
    # 圆角外框（其 fill 曾充当整窗底；删掉后必须另垫，见下方）
    svg_text = re.sub(
        r'<rect\b[^>]*\brx="8"\s*/>\s*',
        "",
        svg_text,
        count=1,
    )
    # 内容组移到原点（Rich 默认 translate(terminal_x, terminal_y)≈(9,41)）
    svg_text = re.sub(
        r'(<g transform=")translate\([^"]+\)(" clip-path="url\(#[^"]*-clip-terminal\)">)',
        r"\1translate(0, 0)\2",
        svg_text,
        count=1,
    )
    clip = re.search(
        r'id="[^"]*-clip-terminal"\s*>\s*<rect\b[^>]*?\bwidth="([^"]+)"[^>]*?\bheight="([^"]+)"',
        svg_text,
        flags=re.S,
    )
    if clip is None:
        clip = re.search(
            r'id="[^"]*-clip-terminal"\s*>\s*<rect\b[^>]*?\bheight="([^"]+)"[^>]*?\bwidth="([^"]+)"',
            svg_text,
            flags=re.S,
        )
        if clip is not None:
            height, width = clip.group(1), clip.group(2)
        else:
            width = height = None
    else:
        width, height = clip.group(1), clip.group(2)
    if width is not None and height is not None:
        svg_text = re.sub(
            r'viewBox="0 0 [^"]+"',
            f'viewBox="0 0 {width} {height}"',
            svg_text,
            count=1,
        )
        # 垫在内容组之前：空白格不再透出纯黑
        backdrop = (
            f'<rect fill="{bg}" x="0" y="0" width="{width}" height="{height}" '
            f'shape-rendering="crispEdges"/>\n'
        )
        svg_text = re.sub(
            r'(<g transform="translate\(0, 0\)" clip-path="url\(#[^"]*-clip-terminal\)">)',
            backdrop + r"\1",
            svg_text,
            count=1,
        )
    return svg_text


def _prepare_svg(svg_text: str) -> str:
    """去假窗口铬、远程 @font-face，换成带 CJK 的本地字体，并去掉 textLength/逐行 clip。

    Rich 按 Fira Code 字宽写了 textLength；换成 Droid 后字宽对不上，cairosvg
    会把字形压成豆腐块。去掉强制字宽与行裁剪后，截图可读（间距略松一点可接受）。
    """
    svg_text = _strip_window_chrome(svg_text)
    svg_text = re.sub(r"@font-face\s*\{.*?\}", "", svg_text, flags=re.S)
    svg_text = re.sub(
        r"font-family:\s*Fira Code,\s*monospace;",
        f"font-family: {_FONT_CSS};",
        svg_text,
    )
    svg_text = re.sub(
        r'font-family:\s*"Fira Code"',
        f"font-family: {_FONT_CSS}",
        svg_text,
    )
    # 侧边栏关注圆点在 SVG 里是独立 `<text>`（内容就一个字形、不含中文），只给
    # 这类元素换成非 CJK 等宽族，圆点就按一格宽画、不再吃掉后面的分隔空格。必须
    # 限定「内容恰为该字形」：右栏对话里的 `●` 与中文同段，换族会整段变豆腐块。
    svg_text = re.sub(
        rf"(<text\b[^>]*)(>[{_NARROW_GLYPHS}]</text>)",
        rf'\1 style="font-family: {_NARROW_GLYPH_FONT}"\2',
        svg_text,
    )
    # 会话组名前的水果 emoji 同理：内容恰为该字形时才单独换成彩色 emoji 字体。
    svg_text = re.sub(
        rf"(<text\b[^>]*)(>[{_FRUIT_EMOJI_GLYPHS}]</text>)",
        rf'\1 style="font-family: {_EMOJI_FONT}"\2',
        svg_text,
    )
    svg_text = re.sub(r'\s+textLength="[^"]*"', "", svg_text)
    svg_text = re.sub(r'\s+clip-path="url\([^"]+\)"', "", svg_text)
    # Droid Sans Fallback 无真正的 bold；合成粗体时 cairosvg 常把字形渲成空框。
    svg_text = re.sub(r"font-weight:\s*bold;?", "", svg_text)
    return svg_text


def _svg_to_png(svg_path: Path, png_path: Path) -> None:
    prepared = _prepare_svg(svg_path.read_text(encoding="utf-8"))
    prepared_path = svg_path.with_suffix(".prepared.svg")
    prepared_path.write_text(prepared, encoding="utf-8")

    # cairosvg 可能装在另一份 Python（如 python3.11）；本解释器没有就 subprocess 调。
    converters: list[list[str]] = []
    try:
        from cairosvg import svg2png  # noqa: F401
        converters.append([sys.executable, "-c", _CAIRO_SNIPPET, str(prepared_path), str(png_path)])
    except ImportError:
        pass
    for candidate in ("python3.11", "python3.12", "python3"):
        converters.append([candidate, "-c", _CAIRO_SNIPPET, str(prepared_path), str(png_path)])

    last_err: Exception | None = None
    for cmd in converters:
        try:
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            return
        except (OSError, subprocess.CalledProcessError) as exc:
            last_err = exc
            continue

    # 最后才回退 ImageMagick：对 Rich 的 per-glyph clipPath 支持很差，常出空白图。
    try:
        subprocess.check_call(
            ["convert", "-background", "#121212", str(prepared_path), str(png_path)],
        )
        print(
            "warning: cairosvg 不可用，已回退 ImageMagick convert；"
            "Rich SVG 常被渲成空白，请 pip install cairosvg",
            file=sys.stderr,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "无法将 SVG 转为 PNG：请安装 cairosvg（pip install cairosvg）"
        ) from (last_err or exc)


_CAIRO_SNIPPET = (
    "import sys; from cairosvg import svg2png; "
    "svg2png(url=sys.argv[1], write_to=sys.argv[2])"
)

async def _capture() -> None:
    from pickup import split_layout

    store = _demo_store()
    with tempfile.TemporaryDirectory() as layout_td:
        with mock.patch.dict(
            os.environ, {"PICKUP_CACHE_DIR": str(layout_td)}, clear=False,
        ):
            split_layout.reset_default_layout_db()
            keys = ["cursor:demo-cursor-1", "codex:demo-codex-1"]
            seed = split_layout.default_layout_db().set_group(
                "/Users/demo/Codes/pickup", keys, focus_key=keys[0]
            )
            group_id = seed.get_group(keys[0]).group_id
            split_layout.default_layout_db().apply(
                lambda store: (
                    setattr(store.groups[group_id], "name", "Group Pineapple"),
                    setattr(store.groups[group_id], "collapsed", True),
                )
            )
            split_layout.default_layout_db().toggle_group_pin(group_id)
            split_layout.reset_default_layout_db()

            app = PickupApp(store, embed_ok=True, osc_report=_DEMO_OSC_REPORT)
            if app.no_color:
                raise RuntimeError(
                    "PickupApp.no_color 仍为 True：NO_COLOR 未在创建 App 前清除，截图会灰阶"
                )
            async with app.run_test(size=(140, 36)) as pilot:
                await pilot.pause(delay=0.4)
                # 跳过「新建会话」，选中置顶会话组 → 右栏显示整组预览。
                await pilot.press("down")
                await pilot.pause(delay=0.5)
                with tempfile.TemporaryDirectory() as td:
                    svg = app.save_screenshot("list.svg", path=td)
                    png_path = OUT_DIR / "list.png"
                    _svg_to_png(Path(td) / Path(svg).name, png_path)
                    _assert_png_sane(png_path)
                print(f"wrote {OUT_DIR / 'list.png'}")


def _assert_png_sane(png_path: Path) -> None:
    """出图后自检：拒绝整栏纯黑空洞 / 灰阶（上次 README 翻车点）。"""
    try:
        from PIL import Image
    except ImportError:
        print("warning: Pillow 不可用，跳过 PNG 自检", file=sys.stderr)
        return
    im = Image.open(png_path).convert("RGB")
    w, h = im.size
    right_black = 0
    right_total = 0
    chromatic = 0
    for y in range(0, h, 4):
        for x in range(w // 3, w, 4):
            r, g, b = im.getpixel((x, y))
            right_total += 1
            if r == 0 and g == 0 and b == 0:
                right_black += 1
            if abs(r - g) > 20 or abs(g - b) > 20:
                chromatic += 1
    if right_total and right_black / right_total > 0.05:
        raise RuntimeError(
            f"截图右栏纯黑占比 {right_black}/{right_total}="
            f"{100 * right_black / right_total:.0f}%（空白格未垫底），拒绝写入"
        )
    if chromatic < 10:
        raise RuntimeError(
            f"截图几乎无彩色像素（chromatic={chromatic}），可能仍被 NO_COLOR 灰阶化"
        )


async def _capture_search() -> None:
    """全文搜索弹窗（Ctrl+F）：命中行 + 关键词高亮。"""
    from pickup.ui.search_modal import FullTextSearchModal

    from pickup import split_layout

    store = _demo_store()
    with tempfile.TemporaryDirectory() as layout_td:
        with mock.patch.dict(
            os.environ, {"PICKUP_CACHE_DIR": str(layout_td)}, clear=False,
        ):
            split_layout.reset_default_layout_db()
            try:
                app = PickupApp(store, embed_ok=True, osc_report=_DEMO_OSC_REPORT)
                async with app.run_test(size=(140, 36)) as pilot:
                    await pilot.pause(delay=0.4)
                    await pilot.press("ctrl+f")
                    for _ in range(100):
                        if (
                            isinstance(app.screen, FullTextSearchModal)
                            and not app.screen._indexing
                        ):
                            break
                        await asyncio.sleep(0.05)
                    else:
                        raise RuntimeError("全文搜索弹窗没有就绪")
                    modal = app.screen
                    modal.query_one("#search-query").load_text("回归")
                    await pilot.pause(delay=0.5)
                    if not modal._matches:
                        raise RuntimeError("演示查询没有命中，截图会是空列表")
                    with tempfile.TemporaryDirectory() as td:
                        svg = app.save_screenshot("search.svg", path=td)
                        png_path = OUT_DIR / "search.png"
                        _svg_to_png(Path(td) / Path(svg).name, png_path)
                        _assert_png_sane(png_path)
                    print(f"wrote {png_path}")
            finally:
                split_layout.reset_default_layout_db()


def main() -> None:
    asyncio.run(_capture())
    asyncio.run(_capture_search())


if __name__ == "__main__":
    main()
