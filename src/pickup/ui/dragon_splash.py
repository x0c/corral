"""空白右栏开屏画：浅灰灰度中国龙打底 + 居中 pickup 厚块字符 Logo。

与横飞彩蛋（dragon_easter_egg）共用同一份龙点阵与 ▀/▄ 半块渲染思路：
- 整条龙按「铺满面板（cover）」缩放并居中裁切，所有非背景色压进
  浅灰灰度带（暗色 → 深灰、亮色 → 浅灰），形成浅灰底纹；
- pickup 紧凑厚块 Logo 叠在画面正中，用龙身原色红（#BA1F14）；
- 原空态提示文案（split.empty_hint）挪到底部居中，保持 dim。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget

from pickup.ui.dragon_easter_egg import HORIZONTAL_CORRECTION, DragonGrid, load_dragon_grid

# 灰度带：龙身最暗的原色（深红/墨绿）落在深端，金色鳞片落在浅端。
_GRAY_DARKEST = 0xA8
_GRAY_LIGHTEST = 0xE4

# Logo 用龙身原色红（点阵 palette 中的 #BA1F14 一系）。
LOGO_COLOR = "#BA1F14"

# toilet -f pagga 的输出；宽 23、高 3。
LOGO_LINES = (
    "░█▀█░▀█▀░█▀▀░█░█░█░█░█▀█",
    "░█▀▀░░█░░█░░░█▀▄░█░█░█▀▀",
    "░▀░░░▀▀▀░▀▀▀░▀░▀░▀▀▀░▀░░",
)
LOGO_WIDTH = max(len(line) for line in LOGO_LINES)

# 面板小于该尺寸时不铺龙、只居中提示文案（与旧 Static 空态等价）。
_MIN_COLS = 24
_MIN_ROWS = 10

_UPPER_HALF = "▀"
_LOWER_HALF = "▄"

_GRAY_STYLES: dict[str, Style] = {}
_LOGO_STYLE = Style(color=LOGO_COLOR, bold=True)
_HINT_STYLE = Style(dim=True)


def _luminance(color: str) -> float:
    """十六进制颜色 → 相对亮度（0~1，Rec. 601 加权）。"""
    value = int(color.lstrip("#"), 16)
    r, g, b = (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def grayscale_palette(grid: DragonGrid) -> tuple[str | None, ...]:
    """把点阵调色板压进浅灰灰度带；背景索引映射为 None（透出面板底色）。"""
    lums = [_luminance(c) for c in grid.palette]
    body = [lum for idx, lum in enumerate(lums) if idx != grid.background_index]
    lo, hi = (min(body), max(body)) if body else (0.0, 1.0)
    out: list[str | None] = []
    for idx, lum in enumerate(lums):
        if idx == grid.background_index:
            out.append(None)
            continue
        t = (lum - lo) / (hi - lo) if hi > lo else 0.5
        gray = round(_GRAY_DARKEST + t * (_GRAY_LIGHTEST - _GRAY_DARKEST))
        out.append(f"#{gray:02X}{gray:02X}{gray:02X}")
    return tuple(out)


def _gray_style(gray: str) -> Style:
    style = _GRAY_STYLES.get(gray)
    if style is None:
        style = Style(color=gray)
        _GRAY_STYLES[gray] = style
    return style


@dataclass(frozen=True)
class SplashLayout:
    """某一面板尺寸下的开屏画布局（cover 缩放 + Logo/提示文案摆位）。"""

    cols: int
    rows: int
    scale: float
    offset_x: float
    offset_y: float
    logo_x: int
    logo_y: int
    hint_y: int
    show_dragon: bool


def splash_layout(cols: int, rows: int) -> SplashLayout:
    """按面板尺寸算 cover 几何：龙铺满整格，超出部分居中裁切。"""
    show_dragon = cols >= _MIN_COLS and rows >= _MIN_ROWS
    grid = load_dragon_grid()
    if show_dragon:
        scale = max(
            cols / (grid.width * HORIZONTAL_CORRECTION),
            rows * 2 / grid.height,
        )
        dragon_w = grid.width * scale * HORIZONTAL_CORRECTION
        dragon_h = grid.height * scale
        offset_x = (dragon_w - cols) / 2
        offset_y = (dragon_h - rows * 2) / 2
    else:
        scale, offset_x, offset_y = 1.0, 0.0, 0.0
    logo_x = (cols - LOGO_WIDTH) // 2
    logo_y = (rows - len(LOGO_LINES)) // 2
    return SplashLayout(
        cols=cols,
        rows=rows,
        scale=scale,
        offset_x=offset_x,
        offset_y=offset_y,
        logo_x=logo_x,
        logo_y=logo_y,
        hint_y=rows - 2,
        show_dragon=show_dragon,
    )


def _sample(grid: DragonGrid, x: float, y: float) -> int:
    """渲染坐标 → 点阵索引（clamp 到边界内）。"""
    sx = min(grid.width - 1, max(0, int(math.floor(x))))
    sy = min(grid.height - 1, max(0, int(math.floor(y))))
    return grid.cells[sy][sx]


def compose_splash_line(
    y: int,
    *,
    layout: SplashLayout,
    grays: tuple[str | None, ...],
    hint: str,
    grid: DragonGrid | None = None,
) -> Strip:
    """渲染开屏画的第 y 行：灰度龙 + 居中红色 Logo + 底部提示。"""
    grid = grid or load_dragon_grid()
    cols = layout.cols
    columns: list[Segment] = [Segment(" ")] * cols

    if layout.show_dragon:
        # 渲染坐标系：每终端行 = 龙点阵两行（▀ 上半 / ▄ 下半）。
        dragon_w = grid.width * layout.scale * HORIZONTAL_CORRECTION
        dragon_h = grid.height * layout.scale
        for x in range(cols):
            top = _sample(
                grid,
                (layout.offset_x + x) * grid.width / dragon_w,
                (layout.offset_y + y * 2) * grid.height / dragon_h,
            )
            bottom = _sample(
                grid,
                (layout.offset_x + x) * grid.width / dragon_w,
                (layout.offset_y + y * 2 + 1) * grid.height / dragon_h,
            )
            top_gray, bottom_gray = grays[top], grays[bottom]
            if top_gray is None and bottom_gray is None:
                continue
            if top_gray is not None and bottom_gray is not None:
                columns[x] = Segment(
                    _UPPER_HALF,
                    Style(color=top_gray, bgcolor=bottom_gray),
                )
            elif top_gray is not None:
                columns[x] = Segment(_UPPER_HALF, _gray_style(top_gray))
            else:
                columns[x] = Segment(_LOWER_HALF, _gray_style(bottom_gray))

    # Logo 叠加：非空字符以红色覆盖龙纹；面板过窄时两侧等量裁掉。
    logo_row = y - layout.logo_y
    if 0 <= logo_row < len(LOGO_LINES):
        line = LOGO_LINES[logo_row]
        start = max(0, -layout.logo_x)
        end = min(len(line), cols - layout.logo_x)
        for j in range(start, end):
            ch = line[j]
            if ch != " ":
                columns[layout.logo_x + j] = Segment(ch, _LOGO_STYLE)

    # 底部提示：居中、dim；沿用旧空态文案，不再单独占据中央。
    if y == layout.hint_y and hint:
        visible = hint[: cols - 2]
        x0 = (cols - len(visible)) // 2
        for j, ch in enumerate(visible):
            columns[x0 + j] = Segment(ch, _HINT_STYLE)

    # 合并相邻同风格段，减少 Segment 数量。
    segments: list[Segment] = []
    for seg in columns:
        if (
            segments
            and segments[-1].style == seg.style
            and segments[-1].text != " "
            and seg.text != " "
            and len(segments[-1].text) < 32
        ):
            segments[-1] = Segment(segments[-1].text + seg.text, seg.style)
        else:
            segments.append(seg)
    strip = Strip(segments).adjust_cell_length(cols)
    if not layout.show_dragon:
        # 无龙的小面板：只画提示行，其余留空。
        if y == layout.rows // 2 and hint:
            visible = hint[: cols - 2]
            x0 = (cols - len(visible)) // 2
            return Strip(
                [Segment(" " * x0), Segment(visible, _HINT_STYLE)]
            ).adjust_cell_length(cols)
        return Strip.blank(cols)
    return strip


class DragonSplash(Widget):
    """灰度龙开屏画：右栏空态（嵌在格子内）或启动加载占位屏（全屏覆盖）。

    全屏形态由 `fullscreen=True` 开启：铺满 Screen 的 overlay 层、不透明底色，
    遮住尚未就绪的骨架 UI；扫描完成后由 MainScreen 摘除。
    """

    ALLOW_SELECT = False
    can_focus = False

    DEFAULT_CSS = """
    DragonSplash {
        width: 1fr;
        height: 1fr;
        color: $text-muted;
    }
    DragonSplash.-fullscreen {
        layer: overlay;
        dock: top;
        width: 100%;
        height: 100%;
        background: $background;
    }
    """

    def __init__(self, hint: str, *, fullscreen: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self._hint = hint
        self._layout: SplashLayout | None = None
        self._grays: tuple[str | None, ...] | None = None
        if fullscreen:
            self.add_class("-fullscreen")

    def _ensure_layout(self) -> tuple[SplashLayout, tuple[str | None, ...]]:
        size = self.size
        if self._layout is None or (self._layout.cols, self._layout.rows) != (
            max(1, size.width),
            max(1, size.height),
        ):
            self._layout = splash_layout(max(1, size.width), max(1, size.height))
            grid = load_dragon_grid()
            self._grays = grayscale_palette(grid)
        assert self._grays is not None
        return self._layout, self._grays

    def render_line(self, y: int) -> Strip:
        layout, grays = self._ensure_layout()
        return compose_splash_line(
            y,
            layout=layout,
            grays=grays,
            hint=self._hint,
        ).apply_offsets(0, y)
