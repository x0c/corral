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
from typing import TYPE_CHECKING

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget
from textual.widgets import ListItem, ListView

if TYPE_CHECKING:
    import pickup

from pickup.i18n import t


NEW_SESSION_ID = "__new_session__"


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


class SessionCard(Widget):
    """会话卡片：三行正文（总高 3）——关注圆点+项目+标题 / 运行时 / 时间。"""

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
    """

    def __init__(
        self,
        session: dict,
        store: "pickup.SessionStore",
        *,
        display_title: str | None = None,
    ) -> None:
        super().__init__()
        self.session = session
        self._store = store
        # 展示标题由外部（rebuild()/_update_cards_in_place）注入并按需更新，不在
        # render() 里自己调用 store.snapshot()——那个方法要拿锁、拷贝整个
        # display_titles dict，卡片一多就是重复的拷贝开销。
        self.display_title = display_title if display_title is not None else session["fallback_title"]
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
        )

    def apply_update(self, session: dict, display_title: str) -> bool:
        """原地更新路径专用：替换会话引用与展示态，仅当渲染相关字段确实变化
        时才 refresh()。返回是否触发了 refresh，供调用方按需断言/统计。"""
        self.session = session
        self.display_title = display_title
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

        project_path = pickup._normalize_cwd(session.get("cwd"))
        project = (
            os.path.basename(project_path)
            if project_path
            else str(session.get("cwd_display") or t("project.unknown"))
        )
        multi_prefix = "▸ " if self._multi_selected else ""
        title_prefix = f"{multi_prefix}{project} "
        width = max(10, self.size.width or 40)

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
        title_cell = pickup._fit_cell(title_prefix + title, max(1, width - dot_width))
        runtime_cell = pickup._fit_cell_right(runtime_name, width)

        relative_time = pickup._format_relative_time(session.get("mtime") or 0)
        time_cell = pickup._fit_cell_right(relative_time, width)

        # 首行整体 bold（与下面两行拉开层级），但项目名比标题淡一档：项目名是
        # 定位用的前缀，同亮度时会和标题抢视线。用 dim 而不是具体颜色，深浅色
        # 主题下都成立，也和运行时未知色、时间行用的是同一套弱化语汇。
        # 进行状态只由首行最左的圆点表达，标题本身不随运行状态变色。
        out = Text()
        if dot_style is not None:
            out.append("●", style=dot_style)
            out.append(" ")
        content_len = len(title_cell.rstrip(" "))
        out.append(title_cell)
        if content_len > 0:
            out.stylize("bold", dot_width, dot_width + content_len)
            # 窄栏时截断可能吃掉部分项目名，取两者较小值，别把 dim 涂到标题上。
            project_end = min(len(title_prefix), content_len)
            project_start = min(len(multi_prefix), project_end)
            if project_end > project_start:
                out.stylize("dim", dot_width + project_start, dot_width + project_end)
        out.append("\n")
        out.append(runtime_cell, style=pickup.runtime_label_style(runtime_id))
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
    """

    BINDINGS = [
        Binding("j", "cursor_down", "Select", show=False),
        Binding("k", "cursor_up", "Select", show=False),
        # 覆盖 ScrollableContainer 的 up/down=scroll_*：会话列表应移光标，不是滚视口
        Binding("down", "cursor_down", "Select", show=False),
        Binding("up", "cursor_up", "Select", show=False),
        Binding("space", "toggle_multi", t("action.toggle_multi"), show=False),
    ]

    def __init__(self, store: "pickup.SessionStore", nav, **kwargs) -> None:
        super().__init__(**kwargs)
        self.store = store
        # 项目搜索查询只认 nav.project_query 这一份，供 visible_sessions /
        # 页头占位文案 / 新建会话目录解析共用，禁止在本类另开一份状态。
        self.nav = nav
        self._multi_keys: list[str] = []
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

    def _session_cards(self) -> list[SessionCard]:
        """按当前显示顺序返回全部 SessionCard（跳过顶部固定的新建会话项）。"""
        cards = []
        for item in self.children:
            if item.id == NEW_SESSION_ID:
                continue
            card = item.children[0] if item.children else None
            if isinstance(card, SessionCard):
                cards.append(card)
        return cards

    def _current_session_keys(self) -> list[str]:
        import pickup

        return [pickup.session_key(card.session) for card in self._session_cards()]

    def _update_cards_in_place(self, sessions: list[dict]) -> None:
        """会话集合（顺序+成员）没变，只需换 SessionCard 手上的 session 引用、
        按需 refresh，不碰 ListView 子项结构（不 mount/unmount 任何 Widget）。"""
        import pickup

        display_titles = self.store.snapshot()
        for card, session in zip(self._session_cards(), sessions):
            key = pickup.session_key(session)
            card.apply_update(
                session,
                display_titles.get(key, session["fallback_title"]),
            )

    def visible_sessions(self) -> list[dict]:
        import pickup

        display_titles = self.store.snapshot()
        return pickup._filter_sessions_by_query(
            self.store.all_sessions(),
            self.nav.project_query,
            titles=display_titles,
        )

    def selected_session(self) -> dict | None:
        sessions = self.visible_sessions()
        idx = self.index
        if idx is None or idx == 0:
            return None
        pos = idx - 1
        return sessions[pos] if 0 <= pos < len(sessions) else None

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

    def _apply_multi_markers(self) -> None:
        import pickup

        selected = set(self._multi_keys)
        for card in self._session_cards():
            key = pickup.session_key(card.session)
            card.set_multi_selected(key in selected)

    def _index_for_session_key(self, session_key: str) -> int | None:
        import pickup

        for i, card in enumerate(self._session_cards()):
            if pickup.session_key(card.session) == session_key:
                return i + 1
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
        session = self.selected_session()
        if session is None:
            return
        import pickup

        self._toggle_multi_key(pickup.session_key(session))

    def action_cursor_down(self) -> None:
        self.clear_multi()
        super().action_cursor_down()

    def action_cursor_up(self) -> None:
        self.clear_multi()
        super().action_cursor_up()

    def on_session_multi_toggle_requested(self, event: SessionMultiToggleRequested) -> None:
        event.stop()
        self._toggle_multi_key(event.session_key)

    def select_session_key(self, session_key: str) -> bool:
        """按会话键设置列表高亮；找不到对应项时返回 False。

        用于右栏分屏焦点 → 侧边栏同步。`__hint__` 对应顶部「＋ 新建」项。
        """
        import pickup

        if session_key == "__hint__":
            if self.index != 0:
                self.index = 0
            return True
        for i, card in enumerate(self._session_cards()):
            if pickup.session_key(card.session) == session_key:
                target = i + 1
                if self.index != target:
                    self.index = target
                return True
        return False

    def _displayed_selected_key(self) -> str | None:
        """按当前已渲染的 DOM 卡片（而非刚重算过的 `visible_sessions()`）取回
        「用户此刻实际选中的会话」键。

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
        if idx is None or idx == 0:
            return None
        cards = self._session_cards()
        pos = idx - 1
        if 0 <= pos < len(cards):
            return pickup.session_key(cards[pos].session)
        return None

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

        previous_key = select_key
        if previous_key is None and keep_selection:
            previous_key = self._displayed_selected_key()

        sessions = self.visible_sessions()
        new_keys = [pickup.session_key(session) for session in sessions]
        self._prune_multi_keys(set(new_keys))
        t0 = time.perf_counter()

        if new_keys == self._current_session_keys() and select_key is None:
            self._update_cards_in_place(sessions)
            if previous_key is None and self.index is None:
                self.index = 1 if sessions else 0
            from pickup import observe
            observe.event(
                "list_rebuild",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                mode="in_place",
                card_count=len(sessions),
            )
            return

        display_titles = self.store.snapshot()
        items = [ListItem(NewSessionCard(), id=NEW_SESSION_ID)]
        for session in sessions:
            key = pickup.session_key(session)
            items.append(
                ListItem(
                    SessionCard(
                        session,
                        self.store,
                        display_title=display_titles.get(key, session["fallback_title"]),
                    )
                )
            )

        # clear 前记下是否已有会话卡：用来区分「初次填充」和「用户正停在新建项」
        had_session_cards = bool(self._session_cards())

        # batch_update() 抑制 clear()+extend() 中间那次多余重绘；两步都要 await
        # 完成（DOM 真正更新），批量 API 本身已经把"多次 mount"合成一轮。
        with self.app.batch_update():
            await self.clear()
            await self.extend(items)

        new_index = 0
        for i, session in enumerate(sessions):
            if previous_key is not None and pickup.session_key(session) == previous_key:
                new_index = i + 1
                break
        if previous_key is not None:
            self.index = new_index
        elif not had_session_cards:
            # 初次填充：默认选最近一条会话（进 pickup 回车即恢复）
            self.index = 1 if sessions else 0
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
            card_count=len(sessions),
        )
