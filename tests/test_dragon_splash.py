"""空白右栏开屏画：灰度龙点阵、cover 布局与 Logo/提示叠层。"""

from __future__ import annotations

import unittest

from corral.ui.dragon_easter_egg import load_dragon_grid
from corral.ui.dragon_splash import (
    LOGO_LINES,
    LOGO_SCALE,
    LOGO_WIDTH,
    DragonSplash,
    compose_splash_line,
    grayscale_palette,
    splash_layout,
)


class GrayscalePaletteTests(unittest.TestCase):
    def test_background_maps_to_none(self) -> None:
        grid = load_dragon_grid()
        grays = grayscale_palette(grid)
        self.assertIsNone(grays[grid.background_index])

    def test_grays_stay_in_light_band(self) -> None:
        grid = load_dragon_grid()
        grays = grayscale_palette(grid)
        for gray in grays:
            if gray is None:
                continue
            value = int(gray.lstrip("#"), 16)
            channel = value & 0xFF
            r, g, b = (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
            self.assertEqual(r, channel)
            self.assertEqual(g, channel)
            self.assertEqual(b, channel)
            self.assertGreaterEqual(channel, 0xA8)
            self.assertLessEqual(channel, 0xE4)

    def test_darker_original_maps_to_darker_gray(self) -> None:
        grid = load_dragon_grid()
        grays = grayscale_palette(grid)
        # 深墨绿（暗）应比金色鳞片（亮）更靠近深端。
        dark = int(grays[grid.palette.index("#0B262E")].lstrip("#"), 16) & 0xFF  # type: ignore[union-attr]
        light = int(grays[grid.palette.index("#F8E6A4")].lstrip("#"), 16) & 0xFF  # type: ignore[union-attr]
        self.assertLess(dark, light)


class SplashLayoutTests(unittest.TestCase):
    def test_cover_fills_pane(self) -> None:
        grid = load_dragon_grid()
        for cols, rows in ((100, 40), (80, 24), (120, 50)):
            layout = splash_layout(cols, rows)
            self.assertTrue(layout.show_dragon)
            dragon_w = grid.width * layout.scale * 1.25
            dragon_h = grid.height * layout.scale
            self.assertGreaterEqual(dragon_w, cols)
            self.assertGreaterEqual(dragon_h, rows * 2)

    def test_logo_centered_and_scaled_up(self) -> None:
        layout = splash_layout(100, 40)
        # 宽高都放得下目标倍数 Logo：24*4=96 宽、3*4=12 高，居中摆放。
        self.assertEqual(layout.logo_scale, LOGO_SCALE)
        self.assertEqual(layout.logo_w, LOGO_WIDTH * 4)
        self.assertEqual(layout.logo_h, len(LOGO_LINES) * 4)
        self.assertEqual(layout.logo_x, (100 - layout.logo_w) // 2)
        self.assertEqual(layout.logo_y, (40 - layout.logo_h) // 2)

    def test_narrow_pane_reduces_scale_instead_of_clipping(self) -> None:
        # 60 列只能容纳 23*2：降到 2 倍而不是裁掉两侧字符。
        layout = splash_layout(60, 24)
        self.assertEqual(layout.logo_scale, 2)
        self.assertLessEqual(layout.logo_w, 60)
        self.assertGreaterEqual(layout.logo_x, 0)

    def test_small_pane_degrades_to_hint_only(self) -> None:
        layout = splash_layout(20, 6)
        self.assertFalse(layout.show_dragon)


class ComposeLineTests(unittest.TestCase):
    def _render(self, cols: int, rows: int, y: int, hint: str = "Pick a session"):
        layout = splash_layout(cols, rows)
        grays = grayscale_palette(load_dragon_grid())
        return compose_splash_line(y, layout=layout, grays=grays, hint=hint)

    def test_strip_width_matches_pane(self) -> None:
        for y in (0, 10, 39):
            self.assertEqual(self._render(100, 40, y).cell_length, 100)

    def test_logo_row_carries_dragon_red(self) -> None:
        layout = splash_layout(100, 40)
        red_count = 0
        for y in range(layout.logo_y, layout.logo_y + layout.logo_h):
            strip = self._render(100, 40, y)
            red_segments = [
                seg
                for seg in strip
                if seg.style is not None
                and seg.style.color is not None
                and "ba1f14" in str(seg.style.color)
            ]
            red_count += sum(len(seg.text) for seg in red_segments)
        # 4 倍 Logo 的落墨量应远大于点景级别。
        self.assertGreater(red_count, 100)

    def test_hint_at_bottom_center(self) -> None:
        layout = splash_layout(100, 40)
        strip = self._render(100, 40, layout.hint_y, hint="Pick a session")
        text = "".join(seg.text for seg in strip)
        x0 = (100 - len("Pick a session")) // 2
        self.assertEqual(text[x0 : x0 + len("Pick a session")], "Pick a session")
        hint_segs = [seg for seg in strip if "Pick" in seg.text]
        self.assertTrue(hint_segs)
        style = hint_segs[0].style
        self.assertIsNotNone(style)
        assert style is not None
        self.assertTrue(style.dim)

    def test_dragon_ink_present(self) -> None:
        # 龙身行：应有大量半块字符（灰度色块）。
        strip = self._render(100, 40, 5)
        text = "".join(seg.text for seg in strip)
        self.assertIn("▀", text)

    def test_logo_scale_renders_full_block_chars(self) -> None:
        # 放大后字符画由 ▀/▄ 组合出 █（上下半块都落墨）。
        layout = splash_layout(100, 40)
        text = "".join(
            seg.text
            for y in range(layout.logo_y, layout.logo_y + layout.logo_h)
            for seg in self._render(100, 40, y)
        )
        self.assertIn("█", text)
        self.assertIn("▀", text)

    def test_small_pane_hint_centered_no_dragon(self) -> None:
        layout = splash_layout(20, 6)
        grays = grayscale_palette(load_dragon_grid())
        strip = compose_splash_line(3, layout=layout, grays=grays, hint="Pick a session")
        text = "".join(seg.text for seg in strip)
        self.assertEqual(text.strip(), "Pick a session")
        self.assertNotIn("▀", text)


class DragonSplashWidgetTests(unittest.TestCase):
    def test_mount_and_render(self) -> None:
        import asyncio

        from textual.app import App

        class _App(App[None]):
            def compose(self):
                yield DragonSplash("Pick a session", id="pane-row-empty")

        async def run() -> tuple[int, set[str]]:
            app = _App()
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause()
                splash = app.query_one("#pane-row-empty", DragonSplash)
                colors: set[str] = set()
                for y in range(splash.size.height):
                    strip = splash.render_line(y)
                    for seg in strip:
                        if seg.style is not None and seg.style.color is not None:
                            colors.add(str(seg.style.color))
                return splash.size.width, colors

        width, colors = asyncio.run(run())
        self.assertEqual(width, 100)
        # 灰度带与 Logo 红都应出现在渲染结果里。
        self.assertTrue(any("ba1f14" in c for c in colors))
        self.assertTrue(
            any(any(f"{v:02x}" in c for c in colors) for v in range(0xA8, 0xE5))
        )


class BootSplashFlowTests(unittest.TestCase):
    """启动加载占位屏：未扫完时整屏铺龙，扫描完成自动退场。"""

    def _make_unloaded(self):
        import time
        from unittest import mock

        import corral

        sessions = [
            {
                "source": "claude", "id": "s0", "short_id": "s0",
                "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "会话0",
                "cwd": "/tmp", "live": False,
            }
        ]
        claude = mock.Mock()
        claude.id = "claude"
        claude.display_name = "Claude"
        claude.is_available.return_value = True
        claude.scan_sessions.return_value = sessions
        claude.load_conversation.return_value = []
        registry = corral.RuntimeRegistry((claude,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()
        # 重置为「未加载」态，模拟 main() 后台扫描还没跑完的真实启动窗口。
        store.loaded = False
        store.hydrated = False
        store._load_event.clear()
        return store

    def test_splash_shown_then_removed_on_load(self) -> None:
        import asyncio

        from corral.ui.app import CorralApp

        store = self._make_unloaded()

        async def run() -> tuple[bool, bool, bool]:
            app = CorralApp(store, embed_ok=True)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(delay=0.2)
                shown_at_boot = bool(app.screen.query("#boot-splash"))
                # 模拟后台扫描完成：唤醒 wait_loaded 事件。
                store.loaded = True
                store._load_event.set()
                for _ in range(20):
                    await pilot.pause(delay=0.1)
                    if not app.screen.query("#boot-splash"):
                        break
                still_shown = bool(app.screen.query("#boot-splash"))
                # 正常界面已接管：侧边栏列表存在。
                has_list = bool(app.screen.query("#session-list"))
                return shown_at_boot, still_shown, has_list

        shown_at_boot, still_shown, has_list = asyncio.run(run())
        self.assertTrue(shown_at_boot)
        self.assertFalse(still_shown)
        self.assertTrue(has_list)

    def test_no_splash_when_already_loaded_or_direct(self) -> None:
        import asyncio

        from corral.ui.app import CorralApp

        store = self._make_unloaded()
        store.loaded = True  # 已同步加载完（测试 / 预扫路径），不该出占位屏。
        store._load_event.set()

        async def run() -> bool:
            app = CorralApp(store, embed_ok=True)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(delay=0.2)
                return bool(app.screen.query("#boot-splash"))

        self.assertFalse(asyncio.run(run()))

    def test_unloaded_splash_renders_fullscreen_logo(self) -> None:
        import asyncio

        from corral.ui.app import CorralApp

        store = self._make_unloaded()

        async def run() -> set[str]:
            app = CorralApp(store, embed_ok=True)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(delay=0.2)
                splash = app.screen.query_one("#boot-splash")
                colors: set[str] = set()
                for y in range(splash.size.height):
                    for seg in splash.render_line(y):
                        if seg.style is not None and seg.style.color is not None:
                            colors.add(str(seg.style.color))
                return colors

        colors = asyncio.run(run())
        self.assertTrue(any("ba1f14" in c for c in colors))
        # 灰度带：存在 R=G=B 且落在 A8~E4 的灰色。
        import re

        grays = [
            int(hex6, 16)
            for c in colors
            for hex6 in re.findall(r"#([0-9A-Fa-f]{6})", c)
            if hex6[0:2] == hex6[2:4] == hex6[4:6]
        ]
        self.assertTrue(any(0xA8A8A8 <= g <= 0xE4E4E4 for g in grays))


if __name__ == "__main__":
    unittest.main()
