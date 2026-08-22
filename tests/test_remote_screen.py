"""corral.remote.screen：行级差分与 status_line 宽字符口径。"""

from __future__ import annotations

import unittest

from corral.embed import Cell
from corral.remote.screen import ScreenEncoder, encode_row, status_line


def _grid(rows: list[str]) -> list[list[Cell]]:
    return [[Cell(ch=c) for c in row] for row in rows]


class EncodeRowTests(unittest.TestCase):
    def test_wide_character_counts_one_index(self) -> None:
        cells = [
            Cell(ch="你"),
            Cell(ch=" ", wide_cont=True),
            Cell(ch="好"),
        ]
        text, spans = encode_row(cells)
        self.assertEqual(text, "你好")
        self.assertEqual(len(text), 2)
        self.assertEqual(spans, [])


class StatusLineTests(unittest.TestCase):
    def test_skips_wide_cont_placeholder(self) -> None:
        grid = _grid(["   "])
        grid.append([Cell(ch="来"), Cell(ch=" ", wide_cont=True), Cell(ch="自")])
        self.assertEqual(status_line(grid), "来自")

    def test_returns_last_non_empty_row(self) -> None:
        grid = _grid(["旧状态", "   ", "Esc to interrupt · 12s"])
        self.assertEqual(status_line(grid), "Esc to interrupt · 12s")


class ScreenEncoderDiffTests(unittest.TestCase):
    def test_first_frame_is_full(self) -> None:
        encoder = ScreenEncoder()
        grid = _grid(["hello", "world"])
        frame = encoder.encode(grid, cursor=(0, 0, True), history_size=10, history_offset=0)
        assert frame is not None
        self.assertTrue(frame.full)
        self.assertEqual(len(frame.lines), 2)
        self.assertEqual(frame.status, "world")

    def test_unchanged_frame_returns_none(self) -> None:
        encoder = ScreenEncoder()
        grid = _grid(["same", "line"])
        encoder.encode(grid)
        self.assertIsNone(encoder.encode(grid))

    def test_only_changed_lines_are_sent(self) -> None:
        encoder = ScreenEncoder()
        grid = _grid(["row0", "row1", "row2"])
        encoder.encode(grid)
        grid[1][0] = Cell(ch="X")
        frame = encoder.encode(grid)
        assert frame is not None
        self.assertFalse(frame.full)
        self.assertEqual(len(frame.lines), 1)
        self.assertEqual(frame.lines[0][0], 1)
        self.assertEqual(frame.lines[0][1], "Xow1")

    def test_size_change_forces_full_resync(self) -> None:
        encoder = ScreenEncoder()
        encoder.encode(_grid(["a", "b"]))
        frame = encoder.encode(_grid(["a", "b", "c"]))
        assert frame is not None
        self.assertTrue(frame.full)
        self.assertEqual(frame.rows, 3)

    def test_scroll_offset_change_forces_full_resync(self) -> None:
        encoder = ScreenEncoder()
        grid = _grid(["a", "b"])
        encoder.encode(grid, history_offset=0)
        frame = encoder.encode(grid, history_offset=5)
        assert frame is not None
        self.assertTrue(frame.full)

    def test_status_line_with_cjk_in_last_row(self) -> None:
        encoder = ScreenEncoder()
        grid = _grid(["进度", "正在编译 · 3s"])
        frame = encoder.encode(grid)
        assert frame is not None
        self.assertEqual(frame.status, "正在编译 · 3s")


if __name__ == "__main__":
    unittest.main()
