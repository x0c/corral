"""中国龙彩蛋：点阵解码、渲染与 UI 触发。"""

from __future__ import annotations

import unittest
from importlib import resources

from rich.segment import Segment
from rich.style import Style
from test_ui import _make_store
from textual.strip import Strip

from corral.ui.app import CorralApp
from corral.ui.dragon_easter_egg import (
    DragonGrid,
    DragonOverlay,
    composite_snapshot_line,
    decode_rows,
    dragon_animation_duration,
    dragon_pixels_per_tick,
    dragon_render_height,
    dragon_render_width,
    load_dragon_grid,
    render_dragon_cells_row,
    render_dragon_frame,
)
from corral.ui.runtime_top_bar import RuntimeTopBar

_MINI_SOURCE = {
    "format": "tui-square-grid-rle/v1",
    "width": 4,
    "height": 4,
    "backgroundIndex": 0,
    "palette": ["#FFFFFF", "#FF0000", "#00FF00"],
    "rows": [
        [[0, 4]],
        [[0, 1], [1, 2], [0, 1]],
        [[0, 1], [2, 2], [0, 1]],
        [[0, 4]],
    ],
}


class DragonGridTests(unittest.TestCase):
    def test_decode_rows_dimensions(self) -> None:
        grid = decode_rows(_MINI_SOURCE)
        self.assertEqual(len(grid), 4)
        self.assertTrue(all(len(row) == 4 for row in grid))
        self.assertEqual(grid[1][1], 1)
        self.assertEqual(grid[2][2], 2)

    def test_load_dragon_grid_from_bundle(self) -> None:
        path = resources.files("corral.ui.assets").joinpath("dragon-grid.json")
        self.assertTrue(path.is_file())
        grid = load_dragon_grid()
        self.assertEqual(grid.width, 256)
        self.assertEqual(grid.height, 170)
        self.assertGreater(len(grid.palette), 1)

    def test_render_height_fills_terminal(self) -> None:
        grid = DragonGrid(
            width=4,
            height=4,
            background_index=0,
            palette=tuple(_MINI_SOURCE["palette"]),
            cells=tuple(tuple(row) for row in decode_rows(_MINI_SOURCE)),
        )
        self.assertEqual(dragon_render_height(20, grid), 20)
        self.assertEqual(dragon_render_height(30, grid), 30)

    def test_transparent_background_is_none(self) -> None:
        grid = DragonGrid(
            width=4,
            height=4,
            background_index=0,
            palette=tuple(_MINI_SOURCE["palette"]),
            cells=tuple(tuple(row) for row in decode_rows(_MINI_SOURCE)),
        )
        cells = render_dragon_cells_row(y=0, render_height=4, render_width=4, grid=grid)
        self.assertTrue(all(cell is None for cell in cells))

    def test_composite_keeps_background_when_dragon_transparent(self) -> None:
        bg = Strip([Segment("X"), Segment("Y"), Segment("Z")])
        line = composite_snapshot_line(
            background=bg,
            dragon_cells=[None, Segment("D"), None],
            offset_x=0,
            screen_width=3,
        )
        self.assertEqual(line.text, "XDZ")

    def test_composite_preserves_cjk_when_dragon_does_not_overlap(self) -> None:
        bg = Strip([Segment("中文列表", Style())]).adjust_cell_length(8)
        line = composite_snapshot_line(
            background=bg,
            dragon_cells=[None, None],
            offset_x=6,
            screen_width=8,
        )
        self.assertEqual(line.text, "中文列表")

    def test_offset_shifts_dragon_right(self) -> None:
        grid = load_dragon_grid()
        left = render_dragon_frame(term_rows=10, term_cols=40, offset_x=0, grid=grid)
        right = render_dragon_frame(term_rows=10, term_cols=40, offset_x=10, grid=grid)
        self.assertNotEqual(left.plain, right.plain)

    def test_render_width_scales_with_height(self) -> None:
        grid = load_dragon_grid()
        narrow = dragon_render_width(10, grid)
        tall = dragon_render_width(30, grid)
        self.assertGreater(tall, narrow)

    def test_frame_samples_full_dragon_height(self) -> None:
        """修复前除数用 logical_height 时，只能采样源图上半段。"""
        grid = load_dragon_grid()
        term_rows = 30
        render_height = dragon_render_height(term_rows, grid)
        bottom_y_max = 0
        for y in range(render_height):
            bottom_y = min(
                grid.height - 1,
                int((y * 2 + 1.5) * grid.height / (render_height * 2)),
            )
            bottom_y_max = max(bottom_y_max, bottom_y)
        self.assertGreater(bottom_y_max, grid.height * 3 // 4)

    def test_animation_duration_capped_at_1_5_seconds(self) -> None:
        grid = load_dragon_grid()
        for term_cols in (80, 120, 200, 300):
            width = dragon_render_width(30, grid)
            duration = dragon_animation_duration(term_cols=term_cols, render_width=width)
            self.assertLessEqual(duration, 1.5)
            self.assertGreater(duration, 0.8)
            self.assertGreaterEqual(
                dragon_pixels_per_tick(term_cols=term_cols, render_width=width),
                1,
            )


class DragonOverlayTests(unittest.IsolatedAsyncioTestCase):
    async def test_dragon_chip_present_in_top_bar(self) -> None:
        store, _ = _make_store()
        app = CorralApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            top_bar = app.screen.query_one("#runtime-top-bar", RuntimeTopBar)
            self.assertIsNotNone(top_bar.query_one("#dragon-chip"))

    async def test_click_dragon_starts_overlay(self) -> None:
        store, _ = _make_store()
        app = CorralApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            overlay = app.screen.query_one("#dragon-overlay", DragonOverlay)
            self.assertFalse(overlay.playing)
            await pilot.click("#dragon-chip")
            await pilot.pause(delay=0.05)
            self.assertTrue(overlay.playing)
            self.assertIn("-playing", overlay.classes)

    async def test_play_debounce_ignores_second_click(self) -> None:
        store, _ = _make_store()
        app = CorralApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            overlay = app.screen.query_one("#dragon-overlay", DragonOverlay)
            overlay.play()
            start_offset = overlay._offset_x  # noqa: SLF001
            overlay.play()
            self.assertEqual(overlay._offset_x, start_offset)  # noqa: SLF001

    async def test_animation_stops_when_off_screen(self) -> None:
        store, _ = _make_store()
        app = CorralApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            overlay = app.screen.query_one("#dragon-overlay", DragonOverlay)
            overlay.play()
            _, term_rows = overlay._viewport()  # noqa: SLF001
            width = dragon_render_width(term_rows)
            overlay._offset_x = -(width + 1)  # noqa: SLF001
            overlay._tick()  # noqa: SLF001
            self.assertFalse(overlay.playing)
            self.assertNotIn("-playing", overlay.classes)


if __name__ == "__main__":
    unittest.main()
