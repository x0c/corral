"""会话小窗：内嵌实时画面右上角的悬浮摘要，一眼看清「这个会话在干啥」。

形态与取舍（改之前先读）：

- **默认收起成「条数 + 最初 + 最近」三行**，展开才补上中间那段。小窗是盖在托管画面上
  的浮层，盖住多少行就有多少行助手输出看不见；Textual 没有「点击穿透」，被盖住的区域
  滚轮不会再转发给托管会话、也划不了词。收起态只留这两头，把这两笔代价压到最小。
- **顺序恒为从上到下、由旧到新**，与右栏完整对话一致。条数超上限时砍中间、留两头。
- **只画在当前激活的那一格、且只对实时托管画面画**。已结束会话的右栏本来就是完整
  对话，再叠一层是重复信息；多格同时画会刷屏。
- 用 `dock: right` + `width/height: auto` 把浮层贴到右上角：这样浮层的命中区域**只有
  胶囊本身**。不要改成「整行宽的容器里右对齐」（`UpdateToast` 那种写法）——那会让
  整条横带都吃掉鼠标事件，托管画面顶部一整行都滚不动。
- 浮层挂在 `PaneCell` 的 `hud` 层（见 `PaneCell` 的 `layers`）。同层内单独排版，
  `margin-top: 1` 让它落在标题栏下面第一行，而不是压住分栏标题。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from rich.text import Text
from textual import events
from textual.geometry import Size
from textual.widget import Widget

from pickup.display import _fit_cell, _text_width
from pickup.i18n import t
from pickup.models import ConversationMessage

# 展开态最多列几条提问；再多就靠"更早 N 条"一行如实说明，不做静默截断。
MAX_ENTRIES = 6
_MIN_WIDTH = 16
# 内容宽度上限。46 太保守，提问经常在这里被截成半句话；按实际使用反馈放宽到 170%。
# 这只是**上限**：真实宽度仍取 `min(上限, 本格可用宽度 - 4)`，所以三分屏那种窄格
# 不受影响，只有单格 / 宽终端才吃得到这个上限。收起态还会再按内容实宽收缩。
_MAX_WIDTH = 78
# 窄到这个宽度以下（三分屏 + 窄终端）直接不画：浮层会把整格盖掉。
_MIN_PANE_WIDTH = 24


@dataclass(frozen=True)
class HudData:
    """小窗要展示的会话摘要。

    `entries` 是 (时间, 单行正文)，**从旧到新**排——与右栏完整对话、与人读聊天记录
    的方向一致。`entries[0]` 恒为本会话**最早**那条提问（判断"这个会话本来是要干嘛"），
    `entries[-1]` 是最新一条（判断"现在做到哪"）；条数超上限时省掉的是中间那段，
    数量记在 `omitted` 里，由界面如实说明，不做静默截断。
    """

    count: int
    entries: tuple[tuple[str, str], ...] = ()
    omitted: int = 0

    @property
    def oldest(self) -> tuple[str, str] | None:
        return self.entries[0] if self.entries else None

    @property
    def latest(self) -> tuple[str, str] | None:
        return self.entries[-1] if self.entries else None

    def __bool__(self) -> bool:
        return bool(self.entries)


def _short_time(timestamp: float, now: float | None = None) -> str:
    """小窗自己的时间列：当天只给 `HH:MM`，更早只给 `MM-DD`。

    不复用 `format_message_time`（`MM-DD HH:MM`）：那是右栏完整对话和侧边栏共用的
    格式，它们有整行宽度可用；小窗横向寸土寸金，11 格的时间列会把正文挤掉一截，
    而同一个会话里绝大多数提问都在当天，日期是纯冗余。两种写法都恰好 5 格宽，
    混排时列也不会错位；`:` 与 `-` 足以区分是几点还是哪天。
    """
    stamp = datetime.fromtimestamp(timestamp)
    today = datetime.fromtimestamp(now) if now is not None else datetime.now()
    if stamp.date() == today.date():
        return stamp.strftime("%H:%M")
    return stamp.strftime("%m-%d")


def _one_line(text: str) -> str:
    """把多行提问压成一行：换行/制表一律折成单空格，连续空白合并。"""
    return " ".join(str(text or "").split())


def summarize_user_messages(
    messages: list[ConversationMessage], limit: int = MAX_ENTRIES,
) -> HudData:
    """从会话对话里挑出真人提问，按从旧到新整理成小窗摘要。

    只认 `role == "user"`：扫描层已经把 monitor / task-notification 这类系统注入
    事件挡在外面了，这里不再二次猜测。

    超过 `limit` 条时**保留最早那条、砍中间**：最早一条决定"这个会话本来要干嘛"，
    没有它就只剩一串近期动作，看不出来龙去脉；被砍掉的条数原样返回给界面说明。
    """
    users = [m for m in messages if m.role == "user" and _one_line(m.text)]
    if not users:
        return HudData(0, ())

    def _entry(message: ConversationMessage) -> tuple[str, str]:
        stamp = _short_time(message.timestamp) if message.timestamp else ""
        return stamp, _one_line(message.text)

    limit = max(2, limit)
    if len(users) <= limit:
        return HudData(len(users), tuple(_entry(m) for m in users), 0)
    tail = users[-(limit - 1):]
    entries = (_entry(users[0]), *(_entry(m) for m in tail))
    return HudData(len(users), entries, len(users) - 1 - len(tail))


def _plural(key: str, count: int, **kwargs: object) -> str:
    """英文单复数：中文不需要但同名 key 也必须给，否则中文界面会冒出英文。"""
    if count == 1:
        return t(f"{key}_one", count=count, **kwargs)
    return t(key, count=count, **kwargs)


class SessionHud(Widget):
    """右上角悬浮小窗本体。数据与展开状态都由 `MainScreen` 统一喂，自己不查 store。"""

    ALLOW_SELECT = False
    can_focus = False

    # 底色用分栏激活条那个变量：小窗本来就是「当前这一格」的附属信息，视觉上跟着
    # 该格的高亮条走，一眼看出是 pickup 的浮层而不是助手自己画的。
    # **不要加边框**：托管画面底色跟着用户终端走，边框线在某些终端字体下会连成
    # 实心方块（分栏分隔线就是为此不画的）；整块实底本身已经足够"浮起来"，还省下
    # 边框那两行两列的遮挡。
    DEFAULT_CSS = """
    SessionHud {
        layer: hud;
        dock: right;
        width: auto;
        height: auto;
        margin: 1 1 0 0;
        padding: 0 1;
        background: $pane-active-background;
        color: auto 90%;
        display: none;
    }
    SessionHud.-visible {
        display: block;
    }
    SessionHud:hover {
        background: $primary-muted;
    }
    """

    def __init__(self, on_toggle: Callable[[], None] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._data = HudData(0, ())
        self._expanded = False
        self._on_toggle = on_toggle

    # ---- 外部驱动 ----

    def update_data(self, data: HudData | None, *, expanded: bool) -> None:
        """喂新数据；data 为空（没有提问）时整个小窗不出现。"""
        changed = (data or HudData(0, ())) != self._data or expanded != self._expanded
        self._data = data or HudData(0, ())
        self._expanded = expanded
        self.set_class(bool(self._data), "-visible")
        self.set_class(bool(self._data) and expanded, "-expanded")
        if changed:
            # 行数/宽度都可能变，必须连布局一起刷新。
            self.refresh(layout=True)

    def hide(self) -> None:
        if not self._data and not self.has_class("-visible"):
            return
        self._data = HudData(0, ())
        self.set_class(False, "-visible")
        self.set_class(False, "-expanded")
        self.refresh(layout=True)

    @property
    def expanded(self) -> bool:
        return self._expanded

    @property
    def data(self) -> HudData:
        return self._data

    # ---- 尺寸与内容 ----

    def _inner_width(self, available: int) -> int:
        """按所在格的可用宽度算内容宽度；留出右边距（1）与左右内边距（2）。"""
        return max(_MIN_WIDTH, min(_MAX_WIDTH, available - 4))

    def _prefixed(self, prefix: str, body: str, width: int) -> Text:
        """前缀（时间或「最初/最近」标签）用淡色，正文用正常色，整行补齐到同宽。"""
        line = Text(no_wrap=True)
        line.append(prefix, style="dim")
        line.append(_fit_cell(body, max(1, width - _text_width(prefix)), ellipsis=True))
        return line

    def lines(self, width: int) -> list[Text]:
        """按给定内容宽度生成每一行；所有行补齐到同宽，浮层底色才是规整的矩形。

        两种形态都是**从上到下、从旧到新**：收起态只留两头（最初一条 + 最近一条），
        展开态把中间那段补上。方向和右栏完整对话一致，不要改成最新在最前。
        """
        data = self._data
        if not data:
            return []

        header = _plural("hud.count", data.count) if not self._expanded else t(
            "hud.title", count=data.count,
        )
        out: list[Text] = [
            Text(_fit_cell(header, width, ellipsis=True), style="bold", no_wrap=True),
        ]

        if not self._expanded:
            label_width = max(
                _text_width(t("hud.label_first")), _text_width(t("hud.label_latest")),
            ) + 2
            oldest, latest = data.oldest, data.latest
            if data.count > 1 and oldest is not None:
                out.append(self._prefixed(
                    _fit_cell(t("hud.label_first"), label_width), oldest[1], width,
                ))
            if latest is not None:
                out.append(self._prefixed(
                    _fit_cell(t("hud.label_latest"), label_width), latest[1], width,
                ))
            return out

        for index, (stamp, body) in enumerate(data.entries):
            if index == 1 and data.omitted:
                # 省略的是中间那段，说明行就画在被省掉的位置上。
                out.append(Text(
                    _fit_cell(_plural("hud.omitted", data.omitted), width, ellipsis=True),
                    style="dim", no_wrap=True,
                ))
            out.append(
                self._prefixed(f"{stamp}  ", body, width) if stamp
                else Text(_fit_cell(body, width, ellipsis=True), no_wrap=True)
            )
        out.append(
            Text(_fit_cell(t("hud.collapse_hint"), width, ellipsis=True),
                 style="dim", no_wrap=True),
        )
        return out

    def get_content_width(self, container: Size, viewport: Size) -> int:
        if not self._data or container.width < _MIN_PANE_WIDTH:
            return 0
        width = self._inner_width(container.width)
        if self._expanded:
            return width
        # 收起态按内容实宽收缩，短提问不必画成一整条长条。
        rendered = self.lines(width)
        if not rendered:
            return 0
        # 各行等宽是浮层底色画成规整矩形的前提，所以取最宽那行，再由 lines() 统一补齐。
        return min(width, max(_text_width(line.plain.rstrip()) for line in rendered))

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        if not self._data or container.width < _MIN_PANE_WIDTH:
            return 0
        return len(self.lines(width or self._inner_width(container.width)))

    def render(self) -> Text:
        width = self.content_size.width or self._inner_width(self.container_size.width)
        rendered = self.lines(width)
        out = Text(no_wrap=True)
        for index, line in enumerate(rendered):
            if index:
                out.append("\n")
            out.append_text(line)
        return out

    # ---- 交互：点哪儿都是展开/收起；不抢焦点（自身与祖先都不可聚焦） ----

    def on_click(self, event: events.Click) -> None:
        event.stop()
        if self._on_toggle is not None:
            self._on_toggle()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        # 浮层盖住的那几行滚轮到不了托管画面；就地吃掉，别让它去滚别的控件。
        event.stop()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
