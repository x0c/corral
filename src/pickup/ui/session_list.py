"""会话列表：左栏会话卡片 + 顶部新建项，取代旧版 curses 手绘表格。

侧边栏布局硬约定（凡往左栏加控件都必须遵守，见 AGENTS.md / MAINTAINER_GUIDE）：
搜索框/新建项最后一行是间隔空行，画在控件自身高度内并算进命中区；禁止用 margin
或兄弟空隙做分隔。当前：搜索框高 2、新建项高 2、会话卡高 3（标题 / 运行时 /
时间；首行最左是关注状态圆点、随后是「项目 标题」，运行时与时间各自靠右，
无末行空行）。

业务格式化逻辑（相对时间、宽字符对齐、标题兜底）直接复用 pickup.py 里已测试的
纯函数，这里只负责「怎么在 Textual 里画卡片、怎么响应选择」。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget
from textual.widgets import ListItem, ListView

if TYPE_CHECKING:
    import pickup
    from pickup.split_layout import SplitGroup, SplitLayoutStore

from pickup.i18n import t


NEW_SESSION_ID = "__new_session__"
GROUP_ID_PREFIX = "__group__-"

# 时间行档位在「控件还没挂载」时的兜底样式：单测会直接构造 SessionCard 调
# render()，此时主题变量尚未解析，退回旧的二值 dim 表现，不让渲染整体失败。
_TIME_FALLBACK_STYLES = {
    "fresh": Style(),
    "recent": Style(dim=True),
    "today": Style(dim=True),
    "old": Style(dim=True),
}


def _focused_live_session_key(focused) -> str | None:
    """焦点控件若是右栏某个「活着的实时终端」，返回它此刻绑定的会话键。

    必须在鼠标按下的当帧解析成会话键，不能只记下控件对象事后再反查：紧随点击的
    选择跟随会把同一个面板控件**就地改绑**到刚点的那个会话（`PaneCell.rebind`
    复用控件不重建），事后按控件身份比对，会把「点了另一张卡」误判成「点了正
    持有输入的那张卡」，焦点被撤回侧边栏——真机表现就是连续点不同会话时焦点
    在侧边栏和右栏之间来回跳。
    """
    if focused is None or getattr(focused, "dead", True):
        # 只有 EmbedPane 有 dead；其它控件（列表、搜索框）一律不算持有右栏输入
        return None
    node = getattr(focused, "parent", None)
    while node is not None:
        spec = getattr(node, "spec", None)
        session_key = getattr(spec, "session_key", None)
        if session_key:
            return session_key if getattr(spec, "keepalive_name", None) else None
        node = getattr(node, "parent", None)
    return None


class SessionMultiToggleRequested(Message):
    """Ctrl/Cmd+点击会话卡：切换侧边栏多选集（不触发 ListView Selected）。"""

    bubble = True

    def __init__(self, session_key: str) -> None:
        super().__init__()
        self.session_key = session_key


class SessionGroupToggleRequested(Message):
    """点击组卡三角：切换展开状态，不触发打开会话组。"""

    bubble = True

    def __init__(self, group_id: str) -> None:
        super().__init__()
        self.group_id = group_id


@dataclass(frozen=True)
class _SidebarRow:
    """侧边栏的一行逻辑条目；组卡与会话卡共用同一套重建顺序。"""

    kind: str
    identity: str
    session: dict | None = None
    group: "SplitGroup | None" = None
    member_sessions: tuple[dict, ...] = ()
    tree_position: str | None = None
    pinned: bool = False


class SessionCard(Widget):
    """会话卡片：三行正文（总高 3）——关注圆点+项目+标题 / 运行时 / 时间。"""

    COMPONENT_CLASSES = {
        "session-card--time-fresh",
        "session-card--time-recent",
        "session-card--time-today",
        "session-card--time-old",
    }

    # Textual 默认所有 Widget 都允许鼠标拖拽文本选择（ALLOW_SELECT=True）；这类
    # 卡片是"点击=选中该会话"的列表项，不是可选文本内容，必须关掉——否则鼠标
    # 点击会触发 Textual 内置的 SelectStart 逻辑，在 ListView 卡片这种没有常规
    # 可滚动祖先的场景下，container 解析为 None 后访问 .region 直接崩溃退出
    # （真机实测复现：点击会话卡直接闪退，AttributeError: 'NoneType' object
    # has no attribute 'region'）。
    ALLOW_SELECT = False

    DEFAULT_CSS = """
    SessionCard {
        height: 3;
        width: 1fr;
        /* 标题行统一吃这里的基础色：满亮前景整栏铺开太扎眼，压到 8 成
           （alpha 与当前背景混合，深浅色主题各自成立）。关注状态只由首行最左
           的圆点表达，避免整行标题变色压过真正需要用户处理的状态。 */
        color: $foreground 80%;
    }
    /* 第三行时间按新鲜度分档着色：半小时内与标题同亮（=卡片基础色，着重显示），
       之后逐级压暗到几乎只剩轮廓。全部用 $foreground + alpha 表达，深浅色主题
       各自与背景混合成立，不写死具体颜色。 */
    SessionCard > .session-card--time-fresh {
        color: $foreground 80%;
    }
    SessionCard > .session-card--time-recent {
        color: $foreground 58%;
    }
    SessionCard > .session-card--time-today {
        color: $foreground 42%;
    }
    SessionCard > .session-card--time-old {
        color: $foreground 30%;
    }
    """

    def __init__(
        self,
        session: dict,
        store: "pickup.SessionStore",
        *,
        display_title: str | None = None,
        tree_position: str | None = None,
        pinned: bool = False,
    ) -> None:
        super().__init__()
        self.session = session
        self._store = store
        # 展示标题由外部（rebuild()/_update_cards_in_place）注入并按需更新，不在
        # render() 里自己调用 store.snapshot()——那个方法要拿锁、拷贝整个
        # display_titles dict，卡片一多就是重复的拷贝开销。
        self.display_title = (
            display_title
            if display_title is not None
            else session["fallback_title"]
        )
        self.tree_position = tree_position
        self.pinned = pinned
        self._multi_selected = False
        self._render_signature = self._compute_signature()

    def set_multi_selected(self, selected: bool) -> None:
        if selected == self._multi_selected:
            return
        self._multi_selected = selected
        self.refresh()

    def on_click(self, event: events.Click) -> None:
        if not (event.ctrl or event.meta):
            return
        import pickup

        event.stop()
        self.post_message(SessionMultiToggleRequested(pickup.session_key(self.session)))

    def _time_tier(self) -> str:
        import pickup

        return pickup._time_brightness_tier(self.session.get("mtime") or 0)

    def _time_style(self, tier: str) -> Style:
        """时间行的档位配色；未挂载（单测直接调 render）时退回 dim 兜底。

        只取组件样式里混色后的**前景**，丢掉它带出来的背景色——否则时间那一行
        会用卡片自己的底色盖住列表选中高亮/分屏底色，整行看着缺一块。
        """
        fallback = _TIME_FALLBACK_STYLES[tier]
        style = self.get_component_rich_style(
            f"session-card--time-{tier}", default=fallback
        )
        if style is fallback or style.color is None:
            return fallback
        return Style.from_color(style.color)

    def _compute_signature(self) -> tuple:
        """渲染相关字段的轻量快照，用来判定"内容是否真的变了"、要不要 refresh()。"""
        session = self.session
        return (
            self.display_title,
            self._multi_selected,
            session.get("attention_kind"),
            session.get("attention_token"),
            session.get("attention_updated_at"),
            session.get("mtime"),
            # mtime 不变但会话「变旧」跨过档位线时，时间行要跟着压暗，所以档位
            # 本身也得进签名，否则原地更新路径不会重绘。
            self._time_tier(),
            self.tree_position,
            self.pinned,
        )

    def apply_update(
        self,
        session: dict,
        display_title: str,
        *,
        tree_position: str | None = None,
        pinned: bool = False,
    ) -> bool:
        """原地更新路径专用：替换会话引用与展示态，仅当渲染相关字段确实变化
        时才 refresh()。返回是否触发了 refresh，供调用方按需断言/统计。"""
        self.session = session
        self.display_title = display_title
        self.tree_position = tree_position
        self.pinned = pinned
        signature = self._compute_signature()
        changed = signature != self._render_signature
        self._render_signature = signature
        if changed:
            self.refresh()
        return changed

    def render(self) -> Text:
        import pickup  # 延迟导入：ui 包只在 pickup.main() 运行期才加载，届时模块已就绪

        session = self.session
        store = self._store
        title = self.display_title
        from pickup.i18n import t

        # 组内子项挂在项目已知的会话组下，标题前再写项目名是重复噪音；
        # 独立会话卡仍用「项目 标题」前缀做定位。
        show_project = self.tree_position is None
        project = ""
        if show_project:
            project_path = pickup._normalize_cwd(session.get("cwd"))
            project = (
                os.path.basename(project_path)
                if project_path
                else str(session.get("cwd_display") or t("project.unknown"))
            )
        multi_prefix = "▸ " if self._multi_selected else ""
        # 终端字体对彩色图钉 emoji 的覆盖很差，真实截图会变成方框；用单格上箭头。
        pin_prefix = "↑ " if self.pinned else ""
        title_prefix = f"{multi_prefix}{pin_prefix}"
        if show_project:
            title_prefix = f"{title_prefix}{project} "
        width = max(10, self.size.width or 40)
        first_prefix = ""
        continuation_prefix = ""
        if self.tree_position == "middle":
            first_prefix = "  ├─ "
            continuation_prefix = "  │  "
        elif self.tree_position == "last":
            first_prefix = "  └─ "
            continuation_prefix = "     "
        content_width = max(5, width - pickup._text_width(first_prefix))

        runtime = store.registry.get(str(session.get("source") or ""))
        runtime_name = runtime.display_name
        runtime_id = getattr(runtime, "id", None) or str(session.get("source") or "")

        attention_kind = str(session.get("attention_kind") or "none")
        dot_style = {
            "waiting": "bold yellow",
            "working": "bold green",
            "unread": "bold red",
        }.get(attention_kind)
        # 有圆点才让出「圆点 + 空格」这两列；没有圆点的卡片不留占位空格，标题
        # 直接顶到最左并吃满整行宽度。
        dot_width = 0 if dot_style is None else 2
        # 放不下就直接截断，不留 `...`：省略号本身要占 3 格，等于把最后几个
        # 有效字符换成没有信息量的符号，宁可多显示几个字。
        title_cell = pickup._fit_cell(
            title_prefix + title, max(1, content_width - dot_width)
        )
        runtime_cell = pickup._fit_cell_right(runtime_name, content_width)

        relative_time = pickup._format_relative_time(session.get("mtime") or 0)
        time_cell = pickup._fit_cell_right(relative_time, content_width)
        # 时间按新鲜度取一档亮度：半小时内与标题同亮，越旧越暗，让「刚刚还在动」
        # 的会话在一列时间里一眼可见。
        time_style = self._time_style(self._time_tier())

        # 首行整体 bold（与下面两行拉开层级）；独立卡的项目名再 dim 一档，
        # 避免和标题抢视线。组内子项不写项目名，也就没有这段 dim。
        # 进行状态只由首行最左的圆点表达，标题本身不随运行状态变色。
        out = Text()
        out.append(first_prefix, style="dim")
        content_start = len(first_prefix)
        if dot_style is not None:
            out.append("●", style=dot_style)
            out.append(" ")
        content_len = len(title_cell.rstrip(" "))
        out.append(title_cell)
        if content_len > 0:
            out.stylize(
                "bold", content_start + dot_width, content_start + dot_width + content_len
            )
            if show_project:
                # 窄栏时截断可能吃掉部分项目名，取两者较小值，别把 dim 涂到标题上。
                project_end = min(len(title_prefix), content_len)
                project_start = min(len(multi_prefix), project_end)
                if project_end > project_start:
                    out.stylize(
                        "dim",
                        content_start + dot_width + project_start,
                        content_start + dot_width + project_end,
                    )
        out.append("\n")
        out.append(continuation_prefix, style="dim")
        out.append(runtime_cell, style=pickup.runtime_label_style(runtime_id))
        out.append("\n")
        out.append(continuation_prefix, style="dim")
        out.append(time_cell, style=time_style)
        return out


class SessionGroupCard(Widget):
    """会话组三行卡：展开三角+水果名 / 项目与数量 / 最近活动时间。"""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    SessionGroupCard {
        height: 3;
        width: 1fr;
        color: $foreground 88%;
    }
    """

    def __init__(
        self,
        group: "SplitGroup",
        member_sessions: tuple[dict, ...],
        *,
        pinned: bool = False,
    ) -> None:
        super().__init__()
        self.group = group
        self.member_sessions = member_sessions
        self.pinned = pinned
        self._render_signature = self._compute_signature()

    def _compute_signature(self) -> tuple:
        return (
            self.group.name,
            self.group.project_cwd,
            self.group.collapsed,
            self.pinned,
            tuple(
                (session.get("mtime"), session.get("cwd"))
                for session in self.member_sessions
            ),
        )

    def apply_update(
        self,
        group: "SplitGroup",
        member_sessions: tuple[dict, ...],
        *,
        pinned: bool = False,
    ) -> bool:
        self.group = group
        self.member_sessions = member_sessions
        self.pinned = pinned
        signature = self._compute_signature()
        changed = signature != self._render_signature
        self._render_signature = signature
        if changed:
            self.refresh()
        return changed

    def on_click(self, event: events.Click) -> None:
        # 只有三角本身负责折叠；点击卡片其它位置仍是「打开这个会话组」。
        if event.x > 1:
            return
        event.stop()
        self.post_message(SessionGroupToggleRequested(self.group.group_id))

    def render(self) -> Text:
        import pickup

        width = max(10, self.size.width or 40)
        arrow = "▶" if self.group.collapsed else "▼"
        pin = " ↑" if self.pinned else ""
        title = pickup._fit_cell(f"{arrow}{pin} {self.group.name}", width)
        project = os.path.basename(self.group.project_cwd.rstrip(os.sep))
        if not project:
            project = t("project.unknown")
        count = len(self.member_sessions)
        count_key = "group.session_count_one" if count == 1 else "group.session_count"
        summary = f"{project} · {t(count_key, count=count)}"
        summary_cell = pickup._fit_cell_right(summary, width)
        latest = max(
            (float(session.get("mtime") or 0) for session in self.member_sessions),
            default=0,
        )
        time_cell = pickup._fit_cell_right(pickup._format_relative_time(latest), width)
        out = Text(title.rstrip(), style="bold")
        out.append(" " * max(0, width - pickup._text_width(title.rstrip())))
        out.append("\n")
        out.append(summary_cell, style="dim")
        out.append("\n")
        out.append(time_cell, style="dim")
        return out


class NewSessionCard(Widget):
    """列表顶部「新建会话」：一行正文 + 末行间隔（总高 2）。"""

    ALLOW_SELECT = False  # 原因同 SessionCard：点击这项是选中动作，不是选文本

    DEFAULT_CSS = """
    NewSessionCard {
        height: 2;
        width: 1fr;
    }
    """

    def render(self) -> Text:
        from pickup.i18n import t

        # 第二行空行：与会话卡同样把分隔算进本项命中区
        return Text(t("list.new_session"), style="bold") + Text("\n")


class SessionListView(ListView):
    """会话列表：虚拟索引 0 固定为新建会话项，之后是稳定顺序的会话卡片。"""

    # 隐藏滚动条占位，保留键盘/滚轮滚动（scrollbar-size: 0 不关掉 overflow）。
    DEFAULT_CSS = """
    SessionListView {
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 0;
    }
    /* 右栏分屏时只给会话组标题铺一档底色，当前持有输入的子会话再重一档；同组
       其它子会话不高亮，避免树形结构已经表达过一次关系后又整块重复强调。 */
    SessionListView > ListItem.-in-split {
        background: $sidebar-split-background;
    }
    SessionListView > ListItem.-split-active {
        background: $sidebar-split-active-background;
    }
    SessionListView:focus > ListItem.-in-split.-highlight {
        background: $sidebar-split-cursor-background;
    }
    SessionListView:focus > ListItem.-split-active.-highlight {
        background: $sidebar-split-active-cursor-background;
    }
    """

    BINDINGS = [
        Binding("j", "cursor_down", "Select", show=False),
        Binding("k", "cursor_up", "Select", show=False),
        # 覆盖 ScrollableContainer 的 up/down=scroll_*：会话列表应移光标，不是滚视口
        Binding("down", "cursor_down", "Select", show=False),
        Binding("up", "cursor_up", "Select", show=False),
        Binding("space", "toggle_multi", t("action.toggle_multi"), show=False),
        Binding("p", "toggle_pin", t("action.toggle_pin"), show=False),
    ]

    def __init__(
        self,
        store: "pickup.SessionStore",
        nav,
        *,
        group_store: "SplitLayoutStore | None" = None,
        on_group_changed: Callable[[], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.store = store
        # 侧边栏搜索查询只认 nav.project_query 这一份，供 visible_sessions /
        # 页头占位文案 / 新建会话目录解析共用，禁止在本类另开一份状态。
        self.nav = nav
        self.group_store = group_store
        self.on_group_changed = on_group_changed
        self._multi_keys: list[str] = []
        # 右栏分屏在侧边栏的投影：用于定位当前组标题与当前激活子会话。
        self._split_keys: list[str] = []
        self._split_active_key: str | None = None
        # 鼠标按下前，右栏哪一格正持有输入（会话键），见 focus_on_click()。
        self.focus_before_click = None
        # rebuild() 的并发闸门：见该方法注释，多条 pump 上的调用方必须串行进 DOM。
        self._rebuild_lock = asyncio.Lock()
        self._rebuild_seq = 0

    def focus_on_click(self) -> bool:
        """记下「这次鼠标按下之前，右栏哪一格正持有输入」（会话键，没有则 None）。

        Textual 在 MouseDown 阶段先 `set_focus(列表)` 再把事件发下来，等我们收到
        点击 / `ListView.Selected` 时焦点已经是列表了，没法再区分「从右栏点回来」
        和「本来就在列表里点」。这个钩子是唯一还能看到旧焦点的时机——点击当前
        正持有输入的那张会话卡要能把焦点撤回侧边栏，就靠它。解析成会话键而不是
        留着控件对象的原因见 `_focused_live_session_key()`。
        """
        self.focus_before_click = _focused_live_session_key(
            getattr(self.app, "focused", None)
        )
        return True

    def take_focus_before_click(self):
        """读取并清空按下前的持有输入会话键，保证一次点击只被判定一次。"""
        before = self.focus_before_click
        self.focus_before_click = None
        return before

    async def on_mount(self) -> None:
        await self.rebuild()

    def _session_items(self) -> list[tuple[ListItem, SessionCard]]:
        """按当前显示顺序返回 (列表项, 会话卡)（跳过顶部固定的新建会话项）。

        底色类标在 ListItem 上而不是卡片上：整行铺满、且不会盖掉卡片自身的文字
        样式，也才能和 Textual 内置的选中高亮按 CSS 优先级正常分胜负。
        """
        items = []
        for item in self.children:
            if item.id == NEW_SESSION_ID:
                continue
            card = item.children[0] if item.children else None
            if isinstance(card, SessionCard):
                items.append((item, card))
        return items

    def _session_cards(self) -> list[SessionCard]:
        """按当前显示顺序返回全部 SessionCard（跳过顶部固定的新建会话项）。"""
        return [card for _, card in self._session_items()]

    def _group_items(self) -> list[tuple[ListItem, SessionGroupCard]]:
        """按当前显示顺序返回全部会话组卡。"""
        items = []
        for item in self.children:
            card = item.children[0] if item.children else None
            if isinstance(card, SessionGroupCard):
                items.append((item, card))
        return items

    def _current_row_identities(self) -> list[str]:
        """返回当前 DOM 的组/会话身份，用于判断能否原地刷新。"""
        import pickup

        identities: list[str] = []
        for item in self.children:
            if item.id == NEW_SESSION_ID or not item.children:
                continue
            card = item.children[0]
            if isinstance(card, SessionGroupCard):
                identities.append(f"{GROUP_ID_PREFIX}{card.group.group_id}")
            elif isinstance(card, SessionCard):
                identities.append(pickup.session_key(card.session))
        return identities

    def _update_rows_in_place(self, rows: list[_SidebarRow]) -> None:
        """条目身份与顺序不变时只换展示数据，不改 DOM。"""
        import pickup

        display_titles = self.store.snapshot()
        widgets = [
            item.children[0]
            for item in self.children
            if item.id != NEW_SESSION_ID and item.children
        ]
        for widget, row in zip(widgets, rows):
            if isinstance(widget, SessionGroupCard) and row.group is not None:
                widget.apply_update(
                    row.group, row.member_sessions, pinned=row.pinned
                )
            elif isinstance(widget, SessionCard) and row.session is not None:
                key = pickup.session_key(row.session)
                widget.apply_update(
                    row.session,
                    display_titles.get(key, row.session["fallback_title"]),
                    tree_position=row.tree_position,
                    pinned=row.pinned,
                )

    def visible_sessions(self) -> list[dict]:
        import pickup

        display_titles = self.store.snapshot()
        sessions = self.store.all_sessions()
        visible = pickup._filter_sessions_by_query(
            self.store.all_sessions(),
            self.nav.project_query,
            titles=display_titles,
        )
        query = self.nav.project_query.strip().casefold()
        if not query or self.group_store is None:
            return visible
        visible_keys = {pickup.session_key(session) for session in visible}
        by_key = {pickup.session_key(session): session for session in sessions}
        for group in self.group_store.groups.values():
            if query not in group.name.casefold():
                continue
            for key in group.session_keys:
                session = by_key.get(key)
                if session is not None and key not in visible_keys:
                    visible.append(session)
                    visible_keys.add(key)
        return visible

    def _sidebar_rows(self) -> list[_SidebarRow]:
        """把持久会话组投影成「组卡 + 缩进子会话」，其余会话保持扁平。"""
        import pickup

        sessions = self.store.all_sessions()
        by_key = {pickup.session_key(session): session for session in sessions}
        filtered = self.visible_sessions()
        filtered_keys = {pickup.session_key(session) for session in filtered}
        query = self.nav.project_query.strip().casefold()
        pinned_blocks: list[tuple[float, list[_SidebarRow]]] = []
        group_blocks: list[list[_SidebarRow]] = []
        session_rows: list[_SidebarRow] = []
        grouped_keys: set[str] = set()

        if self.group_store is not None:
            for group in self.group_store.ordered_groups():
                all_members = tuple(
                    by_key[key] for key in group.session_keys if key in by_key
                )
                # 历史记录缺失或会话已被明确删除后，侧边栏不显示空壳组。
                if len(all_members) < 2:
                    continue
                grouped_keys.update(
                    pickup.session_key(session) for session in all_members
                )
                group_matches = bool(query and query in group.name.casefold())
                members = (
                    all_members
                    if not query or group_matches
                    else tuple(
                        session
                        for session in all_members
                        if pickup.session_key(session) in filtered_keys
                    )
                )
                if query and not members:
                    continue
                group_row = _SidebarRow(
                    kind="group",
                    identity=f"{GROUP_ID_PREFIX}{group.group_id}",
                    group=group,
                    member_sessions=all_members,
                    pinned=group.group_id in self.group_store.pinned_group_ids,
                )
                child_rows: list[_SidebarRow] = []
                if not group.collapsed or query:
                    for index, session in enumerate(members):
                        child_rows.append(
                            _SidebarRow(
                                kind="session",
                                identity=pickup.session_key(session),
                                session=session,
                                tree_position=(
                                    "last"
                                    if index == len(members) - 1
                                    else "middle"
                                ),
                            )
                        )
                block = [group_row, *child_rows]
                pinned_at = self.group_store.pinned_group_ids.get(group.group_id)
                if pinned_at is not None:
                    # 组卡与子会话是不可拆散的一个排序块。
                    pinned_blocks.append((pinned_at, block))
                else:
                    group_blocks.append(block)

        for session in filtered:
            key = pickup.session_key(session)
            if key not in grouped_keys:
                row = _SidebarRow(
                    kind="session",
                    identity=key,
                    session=session,
                    pinned=(
                        self.group_store is not None
                        and key in self.group_store.pinned_session_keys
                    ),
                )
                pinned_at = (
                    self.group_store.pinned_session_keys.get(key)
                    if self.group_store is not None
                    else None
                )
                if pinned_at is not None:
                    pinned_blocks.append((pinned_at, [row]))
                else:
                    session_rows.append(row)

        # 先排置顶块，再排普通组，最后才是普通会话。置顶组的子会话紧随组卡，
        # 不能被其它置顶项插进组的树形结构中间。
        rows: list[_SidebarRow] = []
        pinned_blocks.sort(key=lambda item: item[0], reverse=True)
        for _, block in pinned_blocks:
            rows.extend(block)
        for block in group_blocks:
            rows.extend(block)
        rows.extend(session_rows)
        return rows

    def selected_session(self) -> dict | None:
        idx = self.index
        if idx is None or idx < 0 or idx >= len(self.children):
            return None
        item = self.children[idx]
        card = item.children[0] if item.children else None
        return card.session if isinstance(card, SessionCard) else None

    def selected_group(self) -> "SplitGroup | None":
        """返回当前选中的会话组；普通会话或新建项返回 None。"""
        idx = self.index
        if idx is None or idx < 0 or idx >= len(self.children):
            return None
        item = self.children[idx]
        card = item.children[0] if item.children else None
        return card.group if isinstance(card, SessionGroupCard) else None

    def is_new_session_selected(self) -> bool:
        return self.index == 0

    def multi_count(self) -> int:
        return len(self._multi_keys)

    def multi_keys(self) -> list[str]:
        return list(self._multi_keys)

    def clear_multi(self) -> None:
        if not self._multi_keys:
            return
        self._multi_keys.clear()
        self._apply_multi_markers()

    def _prune_multi_keys(self, valid_keys: set[str]) -> None:
        if not self._multi_keys:
            return
        self._multi_keys = [key for key in self._multi_keys if key in valid_keys]
        self._apply_multi_markers()

    def set_split_marks(self, pane_keys: list[str], active_key: str | None) -> None:
        """把右栏分屏投影到侧边栏：只高亮组标题和当前激活的子会话。

        只有真正分屏（≥2 格）才标。单格时列表光标本身就指着那一格，再叠一层
        底色只会和光标高亮互相打架，反而看不出焦点在哪。
        """
        keys = [key for key in pane_keys if not key.startswith("__")]
        if len(keys) < 2:
            keys = []
        active = active_key if active_key in keys else None
        if keys == self._split_keys and active == self._split_active_key:
            return
        self._split_keys = keys
        self._split_active_key = active
        self._apply_split_marks()

    def split_marks(self) -> tuple[list[str], str | None]:
        """当前生效的分屏标记（组合内会话键，激活会话键），供测试与同步比对。"""
        return list(self._split_keys), self._split_active_key

    def _apply_split_marks(self) -> None:
        import pickup

        keys = set(self._split_keys)
        active = self._split_active_key
        for item, card in self._group_items():
            group_keys = set(card.group.session_keys)
            is_current_group = bool(
                keys and active in group_keys and keys.issubset(group_keys)
            )
            item.set_class(is_current_group, "-in-split")
            item.set_class(False, "-split-active")
        for item, card in self._session_items():
            key = pickup.session_key(card.session)
            is_active = active is not None and key == active
            item.set_class(False, "-in-split")
            item.set_class(is_active, "-split-active")

    def _apply_multi_markers(self) -> None:
        import pickup

        selected = set(self._multi_keys)
        for card in self._session_cards():
            key = pickup.session_key(card.session)
            card.set_multi_selected(key in selected)

    def _index_for_session_key(self, session_key: str) -> int | None:
        import pickup

        for i, item in enumerate(self.children):
            card = item.children[0] if item.children else None
            if (
                isinstance(card, SessionCard)
                and pickup.session_key(card.session) == session_key
            ):
                return i
        return None

    def _toggle_multi_key(self, session_key: str) -> None:
        from pickup.split_layout import MAX_PANES

        if session_key in self._multi_keys:
            self._multi_keys.remove(session_key)
        else:
            if len(self._multi_keys) >= MAX_PANES:
                self.notify(t("split.multi_full"))
                self.app.bell()
                return
            self._multi_keys.append(session_key)
        target = self._index_for_session_key(session_key)
        if target is not None:
            self.index = target
        self._apply_multi_markers()

    def action_toggle_multi(self) -> None:
        group = self.selected_group()
        if group is not None:
            self._toggle_group(group.group_id)
            return
        session = self.selected_session()
        if session is None:
            return
        import pickup

        self._toggle_multi_key(pickup.session_key(session))

    def action_toggle_pin(self) -> None:
        """用 p 切换独立会话或整个会话组的置顶状态。"""
        if self.group_store is None:
            return
        group = self.selected_group()
        if group is not None:
            pinned = self.group_store.toggle_group_pin(group.group_id)
        else:
            session = self.selected_session()
            if session is None:
                return
            import pickup

            key = pickup.session_key(session)
            if self.group_store.get_group(key) is not None:
                self.notify(t("pin.group_member_hint"))
                self.app.bell()
                return
            pinned = self.group_store.toggle_session_pin(key)
        if self.on_group_changed is not None:
            self.on_group_changed()
        self.notify(t("pin.enabled" if pinned else "pin.disabled"))
        self.call_next(self.rebuild)

    def action_cursor_down(self) -> None:
        self.clear_multi()
        super().action_cursor_down()

    def action_cursor_up(self) -> None:
        self.clear_multi()
        super().action_cursor_up()

    def on_session_multi_toggle_requested(self, event: SessionMultiToggleRequested) -> None:
        event.stop()
        self._toggle_multi_key(event.session_key)

    def on_session_group_toggle_requested(
        self, event: SessionGroupToggleRequested,
    ) -> None:
        event.stop()
        self._toggle_group(event.group_id)

    def _toggle_group(self, group_id: str) -> None:
        if self.group_store is None:
            return
        group = self.group_store.groups.get(group_id)
        if group is None:
            return
        if not self.group_store.set_collapsed(group_id, not group.collapsed):
            return
        if self.on_group_changed is not None:
            self.on_group_changed()
        self.call_next(self.rebuild)

    def select_session_key(self, session_key: str) -> bool:
        """按会话键设置列表高亮；找不到对应项时返回 False。

        用于右栏分屏焦点 → 侧边栏同步。`__hint__` 对应顶部「＋ 新建」项。
        """
        import pickup

        if session_key == "__hint__":
            if self.index != 0:
                self.index = 0
            return True
        for i, item in enumerate(self.children):
            card = item.children[0] if item.children else None
            if isinstance(card, SessionCard) and pickup.session_key(card.session) == session_key:
                target = i
                if self.index != target:
                    self.index = target
                return True
        if self.group_store is not None:
            group = self.group_store.get_group(session_key)
            if group is not None:
                target_identity = f"{GROUP_ID_PREFIX}{group.group_id}"
                for i, item in enumerate(self.children):
                    card = item.children[0] if item.children else None
                    if (
                        isinstance(card, SessionGroupCard)
                        and target_identity
                        == f"{GROUP_ID_PREFIX}{card.group.group_id}"
                    ):
                        if self.index != i:
                            self.index = i
                        return True
        return False

    def _displayed_selected_identity(self) -> str | None:
        """按当前已渲染的 DOM 卡片（而非刚重算过的 `visible_sessions()`）取回
        「用户此刻实际选中的会话或会话组」身份。

        `self.index` 是 DOM 子项下标；只有在 DOM 与 store 同步时它才等价于
        `visible_sessions()` 的下标。后台重扫时 store 先于 DOM 更新（新会话
        按 mtime 置顶插入），此时若仍用 `selected_session()`（内部按新算出的
        `visible_sessions()` 索引 `self.index`）推导原选中会话，会因列表顺序
        已变而错指到相邻会话——真实复现过：聚焦第三条时后台刷出新会话，
        高亮和右栏跟着串到第二条。`rebuild()` 必须用这个方法取原选中键，
        `selected_session()` 仍保留给用户交互期（回车/删除/结束会话等），
        那些时刻 DOM 与 store 本就同步，不受影响。
        """
        import pickup

        idx = self.index
        if idx is None or idx == 0 or idx >= len(self.children):
            return None
        item = self.children[idx]
        card = item.children[0] if item.children else None
        if isinstance(card, SessionGroupCard):
            return f"{GROUP_ID_PREFIX}{card.group.group_id}"
        if isinstance(card, SessionCard):
            return pickup.session_key(card.session)
        return None

    def _displayed_selected_key(self) -> str | None:
        """兼容只关心会话的调用方；组卡选中时返回 None。"""
        identity = self._displayed_selected_identity()
        if identity is None or identity.startswith(GROUP_ID_PREFIX):
            return None
        return identity

    async def rebuild(
        self,
        *,
        keep_selection: bool = True,
        select_key: str | None = None,
    ) -> None:
        """按当前筛选重建条目；尽量保持原有选中的会话不变（后台重扫后调用）。

        会话集合（顺序+成员）没变时走原地更新——只换 SessionCard 手上的
        session 引用、按需 refresh()，不碰 ListView 子项结构；集合真的变了
        （新增/删除/顺序变化）才走批量清空重建，见 docs/MAINTAINER_GUIDE.md
        「界面」节的性能优化记录。

        `select_key`：跨运行时接力 / 空白新建后强制选中刚插入的托管占位卡。

        **必须串行**：调用方分布在两条互不相让的 Textual 消息泵上——后台重扫
        worker 走 `app.call_from_thread(_rebuild_list)`（App 泵，且 MainScreen
        自己那把锁只挡得住同泵的重入），搜索框输入走 `on_input_changed`
        （Screen 泵）。全量重建里的 `clear()` / `extend()` 都会 await 让出，
        两条泵一旦交错，前一次的 extend 会把新建项插到后一次已经填好的列表上，
        Textual 直接抛 DuplicateIds 打崩整个 TUI（2026-07-26 真机复现：连续退格
        清空搜索词，命中数 50→57→71 连做全量重建，单次耗时已到 2s 量级，撞上
        后台重扫必崩）。这把锁是唯一的进 DOM 闸门，禁止绕过它直接改子项结构。
        """
        self._rebuild_seq += 1
        seq = self._rebuild_seq
        async with self._rebuild_lock:
            # 排队期间又来了更新的请求：本次没有强制选中语义就直接让位，避免
            # 连续输入把每个中间态都全量重建一遍（只认最后一次的筛选结果）。
            if select_key is None and seq != self._rebuild_seq:
                return
            await self._rebuild_locked(
                keep_selection=keep_selection, select_key=select_key
            )

    async def _rebuild_locked(
        self,
        *,
        keep_selection: bool,
        select_key: str | None,
    ) -> None:
        """rebuild() 的实现体；只允许持 `_rebuild_lock` 时调用。"""
        import pickup

        previous_identity = select_key
        if previous_identity is None and keep_selection:
            previous_identity = self._displayed_selected_identity()

        rows = self._sidebar_rows()
        new_identities = [row.identity for row in rows]
        self._prune_multi_keys(
            {row.identity for row in rows if row.kind == "session"}
        )
        t0 = time.perf_counter()

        if new_identities == self._current_row_identities() and select_key is None:
            self._update_rows_in_place(rows)
            if previous_identity is None and self.index is None:
                self.index = 1 if rows else 0
            from pickup import observe
            observe.event(
                "list_rebuild",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                mode="in_place",
                card_count=len(rows),
            )
            return

        display_titles = self.store.snapshot()
        items = [ListItem(NewSessionCard(), id=NEW_SESSION_ID)]
        for row in rows:
            if row.kind == "group" and row.group is not None:
                card: Widget = SessionGroupCard(
                    row.group, row.member_sessions, pinned=row.pinned
                )
            elif row.session is not None:
                key = pickup.session_key(row.session)
                card = SessionCard(
                    row.session,
                    self.store,
                    display_title=display_titles.get(
                        key, row.session["fallback_title"]
                    ),
                    tree_position=row.tree_position,
                    pinned=row.pinned,
                )
            else:
                continue
            items.append(ListItem(card))

        # clear 前记下是否已有会话卡：用来区分「初次填充」和「用户正停在新建项」
        had_rows = bool(self._current_row_identities())

        # batch_update() 抑制 clear()+extend() 中间那次多余重绘；两步都要 await
        # 完成（DOM 真正更新），批量 API 本身已经把"多次 mount"合成一轮。
        with self.app.batch_update():
            await self.clear()
            await self.extend(items)

        new_index = 0
        for i, identity in enumerate(new_identities):
            if previous_identity is not None and identity == previous_identity:
                new_index = i + 1
                break
        if previous_identity is not None:
            self.index = new_index
        elif not had_rows:
            # 初次填充：默认选最近一条会话（进 pickup 回车即恢复）
            self.index = 1 if rows else 0
        # 全量重建换掉了全部 ListItem，分屏底色标记要重新贴一遍（原地更新那条
        # 路径不动列表项结构，标记还在，不必重贴）。
        self._apply_split_marks()
        # Textual 已知问题（issue #6300）：clear()+extend() 后紧接着设置 index，
        # 高亮理论上可能只在内部状态里正确、要等用户交互才真正刷新到屏幕。在当前
        # 锁定版本（8.2.8）下用 Pilot 直接探查过 compositor 的增量重绘路径，没有
        # 复现出"选中但不刷新"的现象——但探查手段本身有局限（无法完全模拟真实
        # 终端的部分重绘时序），显式 refresh() 成本几乎为零，保留作为兜底不会有
        # 副作用，直接加上。
        self.refresh()
        from pickup import observe
        observe.event(
            "list_rebuild",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            mode="full",
            card_count=len(rows),
        )
