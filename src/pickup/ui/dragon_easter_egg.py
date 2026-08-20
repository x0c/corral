"""中国龙横飞彩蛋：内嵌 chinese-dragon-tui 点阵数据，全屏 overlay 播放。

点阵来源：https://github.com/x0c/chinese-dragon-tui （MIT）

透明实现：Textual overlay 无法可靠「透看到」同屏下层 widget，因此在触发时
用 compositor 抓取一帧 pickup 画面；动画期间每行把龙像素叠在快照上（背景格
不绘制），形成抠图效果。动画约 1.5 秒内底层 TUI 不再刷新，结束后恢复正常。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual._cells import cell_len
from textual.strip import Strip
from textual.timer import Timer
from textual.widget import Widget

if TYPE_CHECKING:
    from textual.screen import Screen

HORIZONTAL_CORRECTION = 1.25
_TICK_INTERVAL = 0.1  # 秒，≈10fps。全屏快照合成每帧较重，20fps 会让 timer 落后、龙看起来飘得慢
_MAX_DURATION = 1.5  # 秒，龙从进入到离开屏幕的上限（相对旧 3s 横移约两倍速）
_UPPER_HALF = "▀"
_LOWER_HALF = "▄"

_GRID: DragonGrid | None = None


@dataclass(frozen=True)
class DragonGrid:
    """解码后的龙点阵。"""

    width: int
    height: int
    background_index: int
    palette: tuple[str, ...]
    cells: tuple[tuple[int, ...], ...]


def _load_source() -> dict:
    path = resources.files("pickup.ui.assets").joinpath("dragon-grid.json")
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def decode_rows(source: dict) -> list[list[int]]:
    """把 RLE 行展开为固定宽度的调色板索引网格。"""
    width = int(source["width"])
    palette_len = len(source["palette"])
    grid: list[list[int]] = []
    for runs in source["rows"]:
        row: list[int] = []
        for color_index, length in runs:
            if (
                not isinstance(color_index, int)
                or not isinstance(length, int)
                or length < 1
                or color_index < 0
                or color_index >= palette_len
            ):
                raise ValueError("invalid_grid")
            row.extend([color_index] * length)
        if len(row) != width:
            raise ValueError("invalid_width")
        grid.append(row)
    if len(grid) != int(source["height"]):
        raise ValueError("invalid_height")
    return grid


def load_dragon_grid() -> DragonGrid:
    """懒加载并缓存龙点阵。"""
    global _GRID
    if _GRID is not None:
        return _GRID
    source = _load_source()
    if source.get("format") != "tui-square-grid-rle/v1":
        raise ValueError("unsupported_format")
    cells = decode_rows(source)
    _GRID = DragonGrid(
        width=int(source["width"]),
        height=int(source["height"]),
        background_index=int(source.get("backgroundIndex", 0)),
        palette=tuple(str(c) for c in source["palette"]),
        cells=tuple(tuple(row) for row in cells),
    )
    return _GRID


def dragon_render_height(term_rows: int, grid: DragonGrid | None = None) -> int:
    """给定终端行数，返回龙 art 实际占用的终端行数（▀ 两行合一）。"""
    grid = grid or load_dragon_grid()
    scale = (term_rows * 2) / grid.height
    logical_height = max(2, int(math.floor(grid.height * scale / 2) * 2))
    return logical_height // 2


def dragon_render_width(term_rows: int, grid: DragonGrid | None = None) -> int:
    """给定终端行数，返回龙 art 渲染宽度（列）。"""
    grid = grid or load_dragon_grid()
    scale = (term_rows * 2) / grid.height
    return max(1, int(math.floor(grid.width * scale * HORIZONTAL_CORRECTION)))


def dragon_pixels_per_tick(*, term_cols: int, render_width: int) -> int:
    """按屏宽与龙宽估算每帧横移列数，使整段动画不超过 _MAX_DURATION。"""
    total_distance = term_cols + render_width
    tick_count = _MAX_DURATION / _TICK_INTERVAL
    return max(1, math.ceil(total_distance / tick_count))


def dragon_animation_duration(*, term_cols: int, render_width: int) -> float:
    """估算动画时长（秒）。"""
    total_distance = term_cols + render_width
    step = dragon_pixels_per_tick(term_cols=term_cols, render_width=render_width)
    tick_count = math.ceil(total_distance / step)
    return tick_count * _TICK_INTERVAL


def _cell_segment(grid: DragonGrid, top: int, bottom: int) -> Segment | None:
    """渲染单格；背景索引返回 None（透明，合成时用快照像素）。"""
    if top == grid.background_index and bottom == grid.background_index:
        return None
    if top != grid.background_index and bottom != grid.background_index:
        return Segment(
            _UPPER_HALF,
            Style(color=grid.palette[top], bgcolor=grid.palette[bottom]),
        )
    if top != grid.background_index:
        return Segment(_UPPER_HALF, Style(color=grid.palette[top]))
    return Segment(_LOWER_HALF, Style(color=grid.palette[bottom]))


def render_dragon_cells_row(
    *,
    y: int,
    render_height: int,
    render_width: int,
    grid: DragonGrid | None = None,
) -> list[Segment | None]:
    """渲染龙 art 的单行像素（None = 透明）。"""
    grid = grid or load_dragon_grid()
    if y >= render_height:
        return [None] * render_width
    top_y = min(
        grid.height - 1,
        int(math.floor((y * 2 + 0.5) * grid.height / (render_height * 2))),
    )
    bottom_y = min(
        grid.height - 1,
        int(math.floor((y * 2 + 1.5) * grid.height / (render_height * 2))),
    )
    cells: list[Segment | None] = []
    for dragon_x in range(render_width):
        source_x = min(
            grid.width - 1,
            int(math.floor((dragon_x + 0.5) * grid.width / render_width)),
        )
        top = grid.cells[top_y][source_x]
        bottom = grid.cells[bottom_y][source_x]
        cells.append(_cell_segment(grid, top, bottom))
    return cells


def _strip_to_cell_columns(strip: Strip, width: int) -> list[Segment]:
    """把 Strip 展开为按终端列索引的 segment（宽字符占多列）。"""
    columns: list[Segment] = [Segment(" ")] * width
    cell_x = 0
    for text, style, control in strip:
        if control:
            continue
        for ch in text:
            if cell_x >= width:
                break
            span = cell_len(ch)
            columns[cell_x] = Segment(ch, style)
            for j in range(1, min(span, width - cell_x)):
                columns[cell_x + j] = Segment("", style)
            cell_x += span
    return columns


def _cell_columns_to_strip(columns: list[Segment]) -> Strip:
    """把按列展开的 segment 重新合并为 Strip。"""
    segments: list[Segment] = []
    i = 0
    while i < len(columns):
        seg = columns[i]
        if seg.text == "":
            i += 1
            continue
        segments.append(Segment(seg.text, seg.style))
        i += cell_len(seg.text)
    return Strip(segments).adjust_cell_length(len(columns))


def composite_snapshot_line(
    *,
    background: Strip,
    dragon_cells: list[Segment | None],
    offset_x: int,
    screen_width: int,
) -> Strip:
    """把龙像素叠在快照行上：仅非透明格覆盖快照；宽字符按列展开再合并，避免 crop 拆字。"""
    base = background.adjust_cell_length(screen_width)
    if not any(
        cell is not None and 0 <= offset_x + dx < screen_width
        for dx, cell in enumerate(dragon_cells)
    ):
        return base

    columns = _strip_to_cell_columns(base, screen_width)
    for dx, ink in enumerate(dragon_cells):
        x = offset_x + dx
        if ink is not None and 0 <= x < screen_width:
            columns[x] = ink
    return _cell_columns_to_strip(columns)


def capture_screen_snapshot(screen: Screen) -> list[Strip]:
    """抓取当前 Screen 合成结果（overlay 未显示时调用）。"""
    size = screen.size
    strips = screen._compositor.render_strips(size)  # noqa: SLF001
    width = max(1, size.width)
    out: list[Strip] = []
    for y in range(max(1, size.height)):
        if y < len(strips):
            strip = strips[y]
            if strip.cell_length != width:
                strip = strip.adjust_cell_length(width)
            out.append(strip)
        else:
            out.append(Strip.blank(width))
    return out


def render_dragon_frame(
    *,
    term_rows: int,
    term_cols: int,
    offset_x: int,
    grid: DragonGrid | None = None,
) -> Text:
    """把龙 art 贴到虚拟屏幕坐标（单测用，无快照时用空格底）。"""
    grid = grid or load_dragon_grid()
    render_height = dragon_render_height(term_rows, grid)
    render_width = dragon_render_width(term_rows, grid)
    frame = Text()
    for y in range(term_rows):
        if y >= render_height:
            if y + 1 < term_rows:
                frame.append("\n")
            continue
        bg = Strip.blank(term_cols)
        dragon_cells = render_dragon_cells_row(
            y=y,
            render_height=render_height,
            render_width=render_width,
            grid=grid,
        )
        line = composite_snapshot_line(
            background=bg,
            dragon_cells=dragon_cells,
            offset_x=offset_x,
            screen_width=term_cols,
        )
        frame.append_text(Text(line.text))
        if y + 1 < term_rows:
            frame.append("\n")
    return frame


class DragonOverlay(Widget):
    """全屏 overlay：快照底 + 龙像素横飞。"""

    ALLOW_SELECT = False
    can_focus = False

    DEFAULT_CSS = """
    DragonOverlay {
        layer: overlay;
        dock: top;
        width: 100%;
        height: 100%;
        display: none;
        background: transparent 0%;
        overflow: hidden;
    }
    DragonOverlay.-playing {
        display: block;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._offset_x = 0
        self._pixels_per_tick = 1
        self._render_width = 1
        self._render_height = 1
        self._screen_width = 1
        self._snapshot: list[Strip] | None = None
        self._timer: Timer | None = None
        self._playing = False

    @property
    def playing(self) -> bool:
        return self._playing

    def _viewport(self) -> tuple[int, int]:
        size = self.screen.size
        return max(1, size.width), max(1, size.height)

    def _prepare_playback(self) -> None:
        term_cols, term_rows = self._viewport()
        self._screen_width = term_cols
        self._render_height = dragon_render_height(term_rows)
        self._render_width = dragon_render_width(term_rows)
        self._pixels_per_tick = dragon_pixels_per_tick(
            term_cols=term_cols,
            render_width=self._render_width,
        )

    def play(self) -> None:
        if self._playing:
            return
        self._prepare_playback()
        # 在 overlay 显示前抓帧，避免把白底/空 overlay 合成进快照。
        self._snapshot = capture_screen_snapshot(self.screen)
        self._offset_x = self._screen_width
        self._playing = True
        self.add_class("-playing")
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_interval(_TICK_INTERVAL, self._tick)
        self.refresh()

    def _stop(self) -> None:
        self._playing = False
        self._snapshot = None
        self.remove_class("-playing")
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.refresh()

    def _tick(self) -> None:
        if not self._playing:
            return
        self._offset_x -= self._pixels_per_tick
        if self._offset_x + self._render_width < 0:
            self._stop()
            return
        self.refresh()

    def _background_line(self, y: int) -> Strip:
        if self._snapshot is None:
            return Strip.blank(self._screen_width)
        if 0 <= y < len(self._snapshot):
            return self._snapshot[y]
        return Strip.blank(self._screen_width)

    def render_line(self, y: int) -> Strip:
        if not self._playing or self._snapshot is None:
            return Strip.blank(max(1, self.size.width))
        dragon_cells = render_dragon_cells_row(
            y=y,
            render_height=self._render_height,
            render_width=self._render_width,
        )
        return composite_snapshot_line(
            background=self._background_line(y),
            dragon_cells=dragon_cells,
            offset_x=self._offset_x,
            screen_width=self._screen_width,
        ).apply_offsets(0, y)

    def render(self) -> Text:
        if not self._playing:
            return Text("")
        height = max(1, self.size.height)
        return Text("\n".join(self.render_line(y).text for y in range(height)))
