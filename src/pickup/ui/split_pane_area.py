"""右侧分屏区：助手顶栏 + 最多四格均分内嵌终端。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rich.text import Text
from textual import events
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from pickup.i18n import t
from pickup.models import session_key as make_session_key
from pickup.split_layout import MAX_PANES
from pickup.ui.embed_pane import EmbedPane, ModeChanged
from pickup.ui.runtime_top_bar import RuntimeTopBar
from pickup.ui.session_hud import SessionHud


@dataclass
class PaneSpec:
    """单格绑定的会话。"""

    session_key: str
    keepalive_name: str | None = None
    cell_id: str = ""


class _PaneClose(Static):
    ALLOW_SELECT = False

    DEFAULT_CSS = """
    _PaneClose {
        width: 3;
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }
    _PaneClose:hover {
        color: $error;
        background: $error-darken-3;
    }
    """

    def __init__(self, on_close: Callable[[], None], **kwargs) -> None:
        super().__init__("✕", **kwargs)
        self._on_close = on_close

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._on_close()


# 活跃格顶/底高亮色：主题变量 = $primary-muted 再提亮约 10%（见 app.py），
# 比纯 muted 更好辨认，仍避免高饱和蓝条抢过内嵌内容。
_ACTIVE_PANE_BG = "$pane-active-background"


class _PaneHeader(Horizontal):
    ALLOW_SELECT = False

    DEFAULT_CSS = f"""
    _PaneHeader {{
        height: 1;
        width: 1fr;
        margin: 0;
        padding: 0;
        color: auto 90%;
        background: $surface;
    }}
    _PaneHeader.-active {{
        color: auto 90%;
        background: {_ACTIVE_PANE_BG};
    }}
    _PaneHeader.-active _PaneClose {{
        color: auto 90%;
    }}
    _PaneHeader Static.title {{
        width: 1fr;
        height: 1;
        content-align: left middle;
        margin: 0;
        padding: 0;
        text-overflow: ellipsis;
    }}
    _PaneHeader Static.restart-hint {{
        width: auto;
        height: 1;
        content-align: right middle;
        margin: 0;
        padding: 0 1;
        color: auto 70%;
        text-overflow: ellipsis;
    }}
    """

    def __init__(
        self,
        title: str,
        on_close: Callable[[], None],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._title_widget = Static(title, classes="title")
        # 默认藏起：空 hint 若仍占 padding，会在标题和 ✕ 之间留出空隙。
        self._hint_widget = Static("", classes="restart-hint")
        self._hint_widget.display = False
        self._on_close = on_close

    def compose(self):
        yield self._title_widget
        yield self._hint_widget
        yield _PaneClose(self._on_close)

    def set_title(self, title: str) -> None:
        self._title = title
        self._title_widget.update(title)

    def set_restart_hint(self, text: str) -> None:
        """预览/已结束格在标题旁常驻短提示；非预览态传空串清空。"""
        self._hint_widget.update(text)
        self._hint_widget.display = bool(text)

    def set_active(self, active: bool) -> None:
        self.set_class(active, "-active")


class _PaneFooter(Static):
    """分栏底条：与标题栏同步高亮当前激活格；持有输入时提示怎么回列表。

    自动聚焦上线后，用户可能在没点过右栏的情况下就发现按键都进了内嵌会话；出口
    （`Ctrl+\\`）必须常驻可见，否则只能靠猜。预览/已结束格另写 Enter 重启——
    详情头里的同款提示会随钉底滚动滚出视野，底条不滚。非激活且非预览时保持无文字。
    """

    ALLOW_SELECT = False

    DEFAULT_CSS = f"""
    _PaneFooter {{
        height: 1;
        width: 1fr;
        margin: 0;
        padding: 0 1;
        color: auto 70%;
        background: $surface;
        text-overflow: ellipsis;
    }}
    _PaneFooter.-active {{
        background: {_ACTIVE_PANE_BG};
    }}
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)

    def set_state(
        self, active: bool, masked: bool, *, restart_target: bool = False
    ) -> None:
        """active=本格持有输入；masked=本格是实时会话但输入在别处。"""
        self.set_class(active, "-active")
        if restart_target:
            if active:
                self.update(t("pane.restart_focus_hint"))
            else:
                self.update(t("pane.restart_hint"))
        elif active:
            self.update(t("pane.focus_hint"))
        elif masked:
            self.update(t("pane.masked_hint"))
        else:
            self.update("")


class PaneCell(Vertical):
    """单格：标题栏 + EmbedPane + 底条高亮，右上角可浮一个会话小窗。"""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    PaneCell {
        layers: default hud;
        width: 1fr;
        height: 1fr;
        border: none;
        margin: 0 0 0 1;
        padding: 0;
    }
    PaneCell.-leading {
        margin-left: 0;
    }
    PaneCell.-spare {
        display: none;
        width: 0;
        margin: 0;
    }
    PaneCell EmbedPane {
        height: 1fr;
        margin: 0;
        padding: 0;
    }
    """

    def __init__(
        self,
        spec: PaneSpec,
        *,
        title: str,
        # 收 PaneSpec 而不是无参回调：格子可以就地改绑到另一个会话，关闭时必须
        # 用「此刻绑着的」spec，不能是构造时闭包捕获的那一个。
        on_close: Callable[[PaneSpec], None],
        on_focus_list: Callable[[], None],
        osc_report: bytes | None,
        detail_renderer: Callable[[], Text | str] | None = None,
        on_pane_focused: Callable[[str], None] | None = None,
        on_restart: Callable[[str, bool], None] | None = None,
        on_sync_mask: Callable[[], None] | None = None,
        on_hud_toggle: Callable[[], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.spec = spec
        self._on_close = on_close
        self._on_focus_list = on_focus_list
        self._on_pane_focused = on_pane_focused
        self._on_restart = on_restart
        self._on_sync_mask = on_sync_mask
        self._on_hud_toggle = on_hud_toggle
        self._osc_report = osc_report
        self._title = title
        # 活跃格不保存预览。即使调用方误把它传进来，抓帧切换/重排时也绝不能
        # 回退到消息预览。
        self._detail_renderer = None if spec.keepalive_name else detail_renderer
        self._input_masked = False
        self._pooled = False

    def set_pooled(self, pooled: bool) -> None:
        """闲置格：隐藏且不占宽，保留控件供下次改绑（跨组切屏免 remount）。"""
        self._pooled = pooled
        self.set_class(pooled, "-spare")
        if pooled:
            self.set_class(False, "-leading")
            self.display = False
        else:
            self.display = True

    def set_leading(self, leading: bool) -> None:
        if not self._pooled:
            self.set_class(leading, "-leading")

    def park(self) -> None:
        """收回进格池：清画面、关通道绑定，不销毁控件。"""
        pane = self.embed_pane()
        if pane is not None:
            pane.clear()
        self.spec = PaneSpec(session_key="__spare__", cell_id=self.spec.cell_id)
        self._detail_renderer = None
        self.set_title("")
        self.set_pooled(True)

    def _close_self(self) -> None:
        self._on_close(self.spec)

    def _restart_self(self, dead: bool) -> None:
        # 必须读此刻绑着的 spec：格子会就地改绑到别的会话（见 rebind）。
        if self._on_restart is not None:
            self._on_restart(self.spec.session_key, dead)

    def compose(self):
        yield _PaneHeader(self._title, self._close_self, classes="header")
        yield EmbedPane(
            on_focus_list=self._on_focus_list,
            on_restart=self._restart_self,
            osc_report=self._osc_report,
            id=f"embed-{self.spec.cell_id}",
        )
        yield _PaneFooter(classes="footer")
        # 浮层放在最后：同层内单独排版，dock 到右上角，不参与上面三个的纵向堆叠。
        yield SessionHud(self._on_hud_toggle, classes="hud")

    def on_mount(self) -> None:
        # 格池闲置格挂上时不启动会话；真正绑定走 rebind → _start_session。
        if self._pooled:
            return
        self.call_after_refresh(self._start_session)

    def rebind(
        self,
        spec: PaneSpec,
        *,
        title: str,
        detail_renderer: Callable[[], Text | str] | None,
        target_size: tuple[int, int] | None = None,
        discard_stale_screen: bool = False,
    ) -> None:
        """把这一格改绑到另一个会话，不销毁重建。

        销毁重建的代价（实测）是控件重建约 30ms + 重铺回退内容约 55ms + 重新建
        控制通道约 18ms，还会连带丢掉上一格的实时画面；就地改绑把这些全省掉，
        `EmbedPane.focus_session` 本身已经支持切换会话（提升抓帧代次、拦旧回调）。
        `cell_id` 必须沿用旧的：格子里的 EmbedPane 的 DOM id 是 compose 时按它
        生成的，换了会和 `_cell_for_spec` 的匹配对不上。
        """
        spec.cell_id = self.spec.cell_id
        self.spec = spec
        self._detail_renderer = detail_renderer
        self.set_pooled(False)
        self.set_title(title)
        # 单格与多格之间切换时，格子此刻还保留旧宽。用分栏区计算出的最终尺寸立即
        # 调整托管终端，同时丢掉旧宽画面；不能等布局后的防抖 resize，否则新单格会
        # 在约 200ms 内把旧半宽缓存贴在左侧。
        self._start_session(
            target_size=target_size,
            discard_stale_screen=discard_stale_screen,
        )

    def _start_session(
        self,
        *,
        target_size: tuple[int, int] | None = None,
        discard_stale_screen: bool = False,
    ) -> None:
        pane = self.embed_pane()
        if pane is None:
            return
        if self.spec.keepalive_name:
            pane.focus_session(
                self.spec.keepalive_name,
                target_size=target_size,
                discard_stale_screen=discard_stale_screen,
            )
            # 画面刚接上就要按当前焦点决定压不压暗，别等下一次焦点变化。
            if self._on_sync_mask is not None:
                self._on_sync_mask()
        else:
            # 改绑过来的格子可能还留着上一个会话的实时画面，renderer 为空也必须
            # 显式切回静态视图；新挂载的格子本来就是这个状态，重复调用无副作用。
            pane.show_detail(self._detail_renderer)
        # show_detail/focus_session 会发 ModeChanged；此处再钉一次，覆盖 compose
        # 后尚未挂齐顶底条、消息早到的竞态。
        self._sync_active_marker()

    def embed_pane(self) -> EmbedPane | None:
        for child in self.children:
            if isinstance(child, EmbedPane):
                return child
        return None

    def session_hud(self) -> SessionHud | None:
        """与标题栏同：分栏重建的中间态里浮层可能尚未挂上或已卸下。"""
        for child in self.children:
            if isinstance(child, SessionHud):
                return child
        return None

    def update_hud(self, data, *, expanded: bool) -> None:
        hud = self.session_hud()
        if hud is None:
            return
        if data is None:
            hud.hide()
        else:
            hud.update_data(data, expanded=expanded)

    def update_terminal_background(self, osc_report: bytes) -> None:
        self._osc_report = osc_report
        pane = self.embed_pane()
        if pane is not None:
            pane.update_terminal_background(osc_report)

    def _pane_header(self) -> _PaneHeader | None:
        """分栏重建/卸载过程中标题栏可能尚未挂上或已卸下。"""
        for child in self.children:
            if isinstance(child, _PaneHeader):
                return child
        return None

    def _pane_footer(self) -> _PaneFooter | None:
        """与标题栏同：重建中间态可能尚未挂上或已卸下。"""
        for child in self.children:
            if isinstance(child, _PaneFooter):
                return child
        return None

    def set_title(self, title: str) -> None:
        self._title = title
        header = self._pane_header()
        if header is not None:
            header.set_title(title)

    def focus_embed(self) -> None:
        pane = self.embed_pane()
        if pane is not None:
            pane.focus()

    def set_input_masked(self, masked: bool) -> None:
        pane = self.embed_pane()
        if pane is not None:
            pane.input_masked = masked
        self._input_masked = masked
        self._sync_active_marker()

    def _on_descendant_focus(self, event: events.DescendantFocus) -> None:
        self.call_after_refresh(self._sync_active_marker)
        self.call_after_refresh(self._notify_pane_focused)

    def _on_descendant_blur(self, event: events.DescendantBlur) -> None:
        self.call_after_refresh(self._sync_active_marker)

    def on_mode_changed(self, event: ModeChanged) -> None:
        """静态预览 ↔ 托管 ↔ 已结束：顶底 Enter 重启提示要跟着变。"""
        event.stop()
        self._sync_active_marker()

    def _notify_pane_focused(self) -> None:
        if not self.has_focus_within or self._on_pane_focused is None:
            return
        self._on_pane_focused(self.spec.session_key)

    def _is_restart_chrome_target(self) -> bool:
        """预览/已结束格才在顶底 chrome 写 Enter 重启；占位格与托管中不算。"""
        if self.spec.session_key.startswith("__"):
            return False
        pane = self.embed_pane()
        return pane is not None and pane._is_restart_target()  # noqa: SLF001

    def _sync_active_marker(self) -> None:
        # 双击顶栏助手、快速增删分栏时，焦点回调可能落在「标题栏/底条尚未 compose
        # / 旧格已卸下」的中间态；真机复现：NoMatches: '_PaneHeader'。缺件时
        # 静默跳过即可，下一轮焦点事件会再同步。
        active = self.has_focus_within
        restart_target = self._is_restart_chrome_target()
        header = self._pane_header()
        if header is not None:
            header.set_active(active)
            header.set_restart_hint(
                t("pane.restart_hint") if restart_target else ""
            )
        footer = self._pane_footer()
        if footer is not None:
            footer.set_state(
                active,
                self._input_masked and not active,
                restart_target=restart_target,
            )


class SplitPaneArea(Vertical):
    """右侧：顶栏 + 动态 1~4 格。"""

    DEFAULT_CSS = """
    SplitPaneArea {
        width: 1fr;
        height: 1fr;
        margin: 0 0 0 1;
    }
    SplitPaneArea #pane-row {
        width: 1fr;
        height: 1fr;
    }
    SplitPaneArea #pane-row-empty {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        store,
        *,
        on_runtime_pick: Callable[[str], None],
        on_pane_close: Callable[[str], None],
        on_focus_list: Callable[[], None],
        on_pane_focused: Callable[[str], None] | None = None,
        on_pane_restart: Callable[[str, bool], None] | None = None,
        on_hud_toggle: Callable[[], None] | None = None,
        on_dragon_click: Callable[[], None] | None = None,
        osc_report: bytes | None = None,
        render_detail: Callable[[dict], Text] | None = None,
        sidebar_visible: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.store = store
        self._on_runtime_pick = on_runtime_pick
        self._on_pane_close = on_pane_close
        self._on_focus_list = on_focus_list
        self._on_pane_focused = on_pane_focused
        self._on_pane_restart = on_pane_restart
        self._on_hud_toggle = on_hud_toggle
        self._on_dragon_click = on_dragon_click
        self._osc_report = osc_report
        self._render_detail = render_detail
        self._sidebar_visible = sidebar_visible
        self.current_project: str = ""
        self._panes: list[PaneSpec] = []
        self._focus_key: str | None = None
        # 「把输入交给某一格」的待兑现意图，以及尚未执行完的整排挂载数量。
        # 两者配合让意图能跨过一次异步 remount，见 _request_pane_focus。
        self._focus_intent_key: str | None = None
        # 焦点请求已发出、但 Textual 尚未真正把焦点送进内嵌终端时，右栏已经
        # 拥有输入。这个声明必须保留到 DescendantFocus 落下，不能在调用
        # Widget.focus() 后马上撤掉，否则此前排队的蒙版同步仍会灰掉一帧。
        self._input_claim_key: str | None = None
        self._mount_pending = 0
        self._focus_intent_serial = 0

    def compose(self):
        yield RuntimeTopBar(
            self.store.registry,
            self._on_runtime_pick,
            sidebar_visible=self._sidebar_visible,
            on_dragon_click=self._on_dragon_click,
            id="runtime-top-bar",
        )
        with Horizontal(id="pane-row"):
            yield Static(t("split.empty_hint"), id="pane-row-empty")

    def pane_count(self) -> int:
        return len(self._panes)

    def can_add_pane(self) -> bool:
        return len(self._panes) < MAX_PANES

    @property
    def focus_key(self) -> str | None:
        return self._focus_key

    def pane_specs(self) -> list[PaneSpec]:
        return list(self._panes)

    def cells(self) -> list[PaneCell]:
        return self._cells()

    def pane_width_for(self, session_key: str) -> int | None:
        """某条会话此刻挂在哪一格、那一格多宽（列）。找不到或尚未布局则 None。

        静态预览的 Markdown 是**按宽度预排好**再交出去的，不会再被上层重新折行，
        所以必须拿"真正要渲染它的那一格"的宽度——早先图省事取第一格，多分屏且
        各格不等宽时，分隔线和正文就会按别人的宽度排（真机上表现为横线长短对不上
        格子）。
        """
        for cell in self._cells():
            if cell.spec.session_key != session_key:
                continue
            pane = cell.embed_pane()
            width = pane.size.width if pane is not None else 0
            return width or None
        return None

    def any_embed_focused(self) -> bool:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None and pane.has_focus:
                return True
        return False

    def live_embed_focused(self) -> bool:
        """当前持有键盘焦点的格是不是「活着的实时终端」。

        为真时按键都被转发给托管会话，列表侧的单字母/翻页快捷键必须让路（见
        `MainScreen.check_action`）；静态对话预览格聚焦时不算，那些键仍归列表。
        """
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is None or not pane.has_focus:
                continue
            return bool(cell.spec.keepalive_name) and bool(pane.session_name) and not pane.dead
        return False

    def sync_input_mask(self) -> None:
        """焦点不在右栏时，把活着的实时终端整格压暗（输入无效的视觉提示）。

        只看「右栏是否持有输入」这一件事：焦点在另一格时不压暗其余格——那时输入
        是有效的，只是去了别的格，激活格自己的高亮条已经说明了这点。已经登记的
        自动聚焦已声明输入归属也视为右栏持有输入：用户点开会话的瞬间就已决定把
        后续输入交给它，直到真实焦点落下前都不能撤掉这个声明，否则此前排队的
        蒙版同步会在中间插入一帧灰色。
        """
        try:
            cells = self._cells()
        except Exception:  # noqa: BLE001 分栏重建中间态查不到 #pane-row
            return
        area_has_input = self.any_embed_focused() or self._input_claim_key is not None
        for cell in cells:
            pane = cell.embed_pane()
            live = (
                pane is not None
                and bool(cell.spec.keepalive_name)
                and bool(pane.session_name)
                and not pane.dead
            )
            cell.set_input_masked(live and not area_has_input)

    def update_terminal_background(self, osc_report: bytes) -> None:
        """保存新背景供后续分栏使用，并刷新所有已挂载面板。"""
        self._osc_report = osc_report
        for cell in self._cells():
            cell.update_terminal_background(osc_report)

    def host_pane_size(self) -> tuple[int, int]:
        """新建托管会话用的单格尺寸（主线程调用）。"""
        from pickup import embed as embed_mod

        row = self.query_one("#pane-row", Horizontal)
        count = max(1, len(self._panes) + 1)
        w = max(1, (row.size.width or 120) // count)
        h = max(1, row.size.height or 24)
        return embed_mod.normalize_host_size(w, h - 1)

    def _projected_embed_sizes(self, count: int) -> list[tuple[int, int]] | None:
        """计算本次分栏布局最终会给每个实时画面的内容尺寸。

        `PaneCell` 只有非首格占一列左间距，剩余列按 Textual 的 `1fr` 从左到右
        均分（不能在旧格上读取 `pane.size`，那正是闪跳来源）。顶、底栏各占一行。
        """
        if count <= 0:
            return []
        row = self.query_one("#pane-row", Horizontal)
        if row.size.width <= 0 or row.size.height <= 0:
            return None
        usable_width = max(1, row.size.width - (count - 1))
        base, remainder = divmod(usable_width, count)
        height = max(1, row.size.height - 2)
        return [
            (base + int(index >= count - remainder), height)
            for index in range(count)
        ]

    def sync_hud(self, payloads: dict[str, object] | None, *, expanded: bool) -> None:
        """每个右栏格画自己的会话小窗；payloads 里没有的格一律收掉。

        数据来源与展开状态都由 `MainScreen` 决定，本类不查 store。展开/收起状态
        所有格共用一份。
        """
        payloads = payloads or {}
        for cell in self._cells():
            key = cell.spec.session_key
            if key in payloads:
                cell.update_hud(payloads[key], expanded=expanded)
            else:
                cell.update_hud(None, expanded=False)

    def invalidate_all_details(self) -> None:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None:
                pane.invalidate_detail()

    def invalidate_visible_previews(self) -> None:
        """只失效非托管预览格，内嵌终端不被重扫连带刷新。"""
        for cell in self._cells():
            if cell.spec.keepalive_name:
                continue
            pane = cell.embed_pane()
            if pane is not None:
                pane.invalidate_detail()

    def scroll_preview_home(self) -> None:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None:
                pane.scroll_detail_home()

    def scroll_preview_end(self) -> None:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None:
                pane.scroll_detail_end()

    def scroll_preview_page(self, delta: int) -> None:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None:
                pane.scroll_detail_page(delta)

    def close_focused_pane(self) -> None:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None and pane.has_focus:
                self._close_spec(cell.spec)
                return
        if self._panes:
            self._close_spec(self._panes[-1])

    def remove_by_keepalive(self, keepalive_name: str) -> None:
        for spec in list(self._panes):
            if spec.keepalive_name == keepalive_name:
                self._close_spec(spec, notify=False)

    def show_new_session_hint(self) -> None:
        spec = PaneSpec(session_key="__hint__", cell_id=self._new_cell_id())
        self._panes = []
        self._schedule_mount(
            [
                (
                    spec,
                    {"source": "", "id": "__hint__", "fallback_title": ""},
                    lambda: Text(t("detail.new_session_hint")),
                ),
            ],
        )

    def show_single_preview(
        self,
        session: dict,
        renderer: Callable[[], Text | str],
    ) -> None:
        key = make_session_key(session)
        import pickup

        project = pickup._normalize_cwd(session.get("cwd"))
        self.current_project = project
        spec = PaneSpec(session_key=key, cell_id=self._new_cell_id())
        self._schedule_mount(
            [(spec, session, renderer)],
            focus_key=key,
        )

    def ordered_session_keys(self) -> list[str]:
        return [p.session_key for p in self._panes]

    def hosted_identity(self) -> list[tuple[str, str | None]]:
        """当前挂载的有序 (session_key, keepalive_name)，用于判断是否可跳过 remount。"""
        return [(p.session_key, p.keepalive_name) for p in self._panes]

    def show_hosted_group(
        self,
        project: str,
        entries: list[tuple[dict, str | None, Callable[[], Text | str] | None]],
        *,
        focus_key: str | None = None,
        focus_pane: bool = False,
    ) -> None:
        """entries: (session, keepalive_name, detail_renderer)。

        托管会话一律丢弃 detail_renderer；其首帧只能是运行时画面或空白底色。
        若 (session_key, keepalive_name) 有序身份与当前一致，只就地更新标题，
        禁止整排 remount（否则会清掉 live `_grid`）。

        `focus_pane`=True 表示调用方带着明确意图（回车打开 / 新建托管成功），
        此时把键盘焦点交给 `focus_key` 那一格；单纯的选择跟随不得传 True。
        """
        self.current_project = project
        # 这是最后一道边界：调用方未来即使错传预览，也不能污染活跃格。
        entries = [
            (session, kname, None if kname else renderer)
            for session, kname, renderer in entries
        ]
        target_identity = [
            (make_session_key(session), kname) for session, kname, _ in entries
        ]
        if (
            entries
            and self._cells()
            and self.hosted_identity() == target_identity
        ):
            self._update_hosted_group_inplace(
                entries, focus_key=focus_key, focus_pane=focus_pane,
            )
            return
        specs: list[tuple[PaneSpec, dict, Callable[[], Text | str] | None]] = []
        for session, kname, renderer in entries:
            key = make_session_key(session)
            spec = PaneSpec(session_key=key, keepalive_name=kname, cell_id=self._new_cell_id())
            specs.append((spec, session, renderer))
        self._panes = [s for s, _, _ in specs]
        self._focus_key = focus_key or (self._panes[0].session_key if self._panes else None)
        self._schedule_mount(
            [(s, sess, r) for s, sess, r in specs],
            focus_key=self._focus_key,
            focus_pane=focus_pane,
        )

    def _update_hosted_group_inplace(
        self,
        entries: list[tuple[dict, str | None, Callable[[], Text | str] | None]],
        *,
        focus_key: str | None = None,
        focus_pane: bool = False,
    ) -> None:
        """同身份：更新标题，并确保活跃格不残留静态预览。"""
        cells = self._cells()
        for cell, (session, kname, renderer) in zip(cells, entries, strict=False):
            cell.set_title(self._pane_title(session))
            pane = cell.embed_pane()
            if pane is None:
                continue
            # 就地更新是之前漏掉的路径：活跃格已有画面时看似不会用预览，但抓帧
            # 被清空或重排的一个绘制周期仍会读到残留 renderer。
            pane._detail_renderer = None if kname else renderer  # noqa: SLF001
            pane.invalidate_detail()
        if focus_key:
            self._focus_key = focus_key
        elif self._panes:
            self._focus_key = self._panes[0].session_key
        if focus_pane and self._focus_key:
            # 身份未变不会 remount，焦点不会自己跑过来，必须显式交过去。
            self._request_pane_focus(self._focus_key)

    def add_hosted_pane(
        self,
        session: dict,
        keepalive_name: str,
        renderer: Callable[[], Text | str] | None,
        *,
        focus: bool = False,
        focus_pane: bool = False,
    ) -> None:
        import pickup

        key = make_session_key(session)
        project = pickup._normalize_cwd(session.get("cwd"))
        if self.current_project and project and project != self.current_project:
            self.current_project = project
            self._panes = []
        elif not self.current_project:
            self.current_project = project
        spec = PaneSpec(session_key=key, keepalive_name=keepalive_name, cell_id=self._new_cell_id())
        existing = [(p, self._find_session(p.session_key)) for p in self._panes]
        rebuild: list[tuple[PaneSpec, dict, Callable[[], Text | str] | None]] = []
        for p, sess in existing:
            if sess is None:
                continue
            cell = self._cell_for_spec(p)
            renderer_fn = None
            if cell is not None:
                pane = cell.embed_pane()
                if pane is not None and not p.keepalive_name:
                    renderer_fn = pane._detail_renderer  # noqa: SLF001
            rebuild.append((p, sess, renderer_fn))
        # add_hosted_pane 是另一条创建活跃格的入口，同样不准携带消息预览。
        rebuild.append((spec, session, None))
        self._panes = [s for s, _, _ in rebuild]
        focus_key = key if focus else self._focus_key
        self._schedule_mount(rebuild, focus_key=focus_key, focus_pane=focus_pane)

    def focus_session_key(self, session_key: str, *, only_live: bool = False) -> bool:
        """把键盘焦点交给指定会话所在格；`only_live` 时只认活着的实时终端。

        自动聚焦一律带 `only_live=True`：静态预览格、已结束的格拿到焦点后用户
        敲的字会直接丢掉，比让他多点一下鼠标糟得多。
        """
        for cell in self._cells():
            if cell.spec.session_key != session_key:
                continue
            pane = cell.embed_pane()
            if only_live and (
                pane is None or not cell.spec.keepalive_name or pane.dead
            ):
                return False
            cell.focus_embed()
            self._focus_key = session_key
            return True
        return False

    def _handle_pane_focused(self, session_key: str) -> None:
        self._focus_key = session_key
        # 收到真实焦点事件才结束输入归属声明。Widget.focus() 本身是延迟落地的，
        # 在这里之前的任何蒙版同步都必须继续认为右栏可输入。
        self._input_claim_key = None
        self.sync_input_mask()
        if self._on_pane_focused is not None:
            self._on_pane_focused(session_key)

    def reconcile_session_keys(self, key_by_keepalive: dict[str, str]) -> None:
        """按 keepalive 名把格子的 session_key 对齐到最新扫描快照（占位→真实）。"""
        for spec in self._panes:
            kname = spec.keepalive_name
            if not kname:
                continue
            mapped = key_by_keepalive.get(kname)
            if mapped:
                spec.session_key = mapped

    def _cells(self) -> list[PaneCell]:
        """当前绑定中的可见格（不含格池闲置格）。"""
        return [c for c in self._pool_cells() if not c._pooled]  # noqa: SLF001

    def _pool_cells(self) -> list[PaneCell]:
        """右栏格子池：含闲置隐藏格，跨组切换时复用。"""
        row = self.query_one("#pane-row", Horizontal)
        return [c for c in row.children if isinstance(c, PaneCell)]

    def _cell_for_spec(self, spec: PaneSpec) -> PaneCell | None:
        for cell in self._pool_cells():
            if cell.spec.cell_id == spec.cell_id:
                return cell
        return None

    def _find_session(self, key: str) -> dict | None:
        return self.store.find_session(key)

    def _new_cell_id(self) -> str:
        import uuid

        return uuid.uuid4().hex[:8]

    def _pane_title(self, session: dict) -> str:
        source = str(session.get("source") or "")
        if not source:
            return ""
        runtime = self.store.registry.get(source)
        title = self.store.get_title(session)
        return f"{runtime.display_name} · {title}"

    def _sync_leading_cells(self) -> None:
        """可见格里最左一格去左边距；闲置格不参与 :first-child，故用手写标记。"""
        active = self._cells()
        for index, cell in enumerate(active):
            cell.set_leading(index == 0)

    def _make_cell(
        self,
        spec: PaneSpec,
        *,
        title: str,
        renderer: Callable[[], Text | str] | None,
    ) -> PaneCell:
        return PaneCell(
            spec,
            title=title,
            on_close=self._close_spec,
            on_focus_list=self._on_focus_list,
            on_pane_focused=self._handle_pane_focused,
            on_restart=self._on_pane_restart,
            on_sync_mask=self.sync_input_mask,
            on_hud_toggle=self._on_hud_toggle,
            osc_report=self._osc_report,
            detail_renderer=renderer,
        )

    def _close_spec(self, spec: PaneSpec, *, notify: bool = True) -> None:
        self._panes = [p for p in self._panes if p.cell_id != spec.cell_id]
        if self._focus_key == spec.session_key:
            self._focus_key = self._panes[-1].session_key if self._panes else None
        if notify:
            self._on_pane_close(spec.session_key)
        # 只把该格收回池里，勿销毁——同伴格与格池本身都要留下来供下次改绑。
        self.call_next(self._park_cell_async, spec)

    async def _park_cell_async(self, spec: PaneSpec) -> None:
        row = self.query_one("#pane-row", Horizontal)
        closing_had_focus = False
        target: PaneCell | None = None
        for cell in list(self._pool_cells()):
            if cell.spec.cell_id == spec.cell_id:
                closing_had_focus = cell.has_focus_within
                target = cell
                break
        if target is not None:
            target.park()
        if not self._panes:
            for cell in self._pool_cells():
                if not cell._pooled:  # noqa: SLF001
                    cell.park()
            if row.query("#pane-row-empty"):
                pass
            else:
                await row.mount(Static(t("split.empty_hint"), id="pane-row-empty"))
            self.call_after_refresh(self._on_focus_list)
            return
        empty = row.query("#pane-row-empty")
        if empty:
            await empty[0].remove()
        self._sync_leading_cells()
        if closing_had_focus:
            self.call_after_refresh(self._focus_after_close)
        self.call_after_refresh(self.sync_input_mask)

    def _focus_after_close(self) -> None:
        """关掉持有输入的那格后：交给剩余的实时终端，都不行就退回列表。"""
        for key in [self._focus_key, *[p.session_key for p in self._panes]]:
            if key and self.focus_session_key(key, only_live=True):
                return
        self._on_focus_list()

    def _request_pane_focus(self, key: str | None) -> None:
        """登记一次「把输入交给某一格」的明确意图（回车 / 点击 / 托管成功）。

        意图必须能跨过一次还没执行完的整排 remount：鼠标点会话卡时，选择跟随
        先排了一次异步挂载（收尾会把焦点还给列表），紧接着才轮到「打开」事件；
        若这里直接 call_after_refresh 去聚焦，会被后落地的挂载收尾抢回列表——
        真机表现就是「点已托管的会话卡不进右栏，还得再点一下右栏」。
        """
        if not key:
            return
        self._claim_pane_input(key)
        self.call_after_refresh(self._apply_focus_intent)

    def _claim_pane_input(self, key: str) -> None:
        """登记自动聚焦意图，并立刻移除“输入无效”的视觉状态。"""
        self._focus_intent_key = key
        self._input_claim_key = key
        # 焦点组件实际落地要等下一轮刷新；先同步撤去「输入无效」蒙版，避免用户
        # 已点击打开却在这段等待里看到整格灰一下再恢复。
        self.sync_input_mask()

    def clear_focus_intent(self) -> None:
        """用户已经主动决定焦点去哪（回列表 / 点了别处），丢弃待兑现意图。"""
        self._focus_intent_key = None
        self._input_claim_key = None

    def _apply_focus_intent(self) -> bool:
        """兑现待办意图；还有挂载在路上时保留意图，等挂载收尾再试。"""
        key = self._focus_intent_key
        if not key:
            return False
        if self.focus_session_key(key, only_live=True):
            self._focus_intent_key = None
            self._focus_intent_serial += 1
            return True
        if not self._mount_pending:
            # 目标不是实时格（预览 / 已结束 / 托管失败），放弃，不留到下次挂载诈尸
            self._focus_intent_key = None
            self._input_claim_key = None
            self.sync_input_mask()
        return False

    def _settle_focus_intent(self, restore_list: bool, serial: int) -> None:
        """挂载收尾：兑现意图，兑现不了就按挂载前的样子把焦点还给列表。

        两道闸门缺一不可，各管一种时序，都不能单独扛：

        - `serial` 是排这次挂载时的兑现计数。计数变了说明挂载排队期间已经有一次
          明确意图被兑现（点击/回车打开），此时绝不能再把焦点还回列表。这道闸门
          管的是**焦点还没落地**的情况——Textual 的 `Widget.focus()` 走
          `call_later` 延迟生效，那一刻 `any_embed_focused()` 现查还是 False。
        - `any_embed_focused()` 管的是**焦点已经落地**的情况：用户直接点进某个
          内嵌会话、或代码直接调 `EmbedPane.focus()`，都绕过了意图机制、推不动
          serial，于是这里迟到执行时会把焦点从人家刚点进去的格子抢回侧边栏。
          用户表现是「点进内嵌会话，键盘却还在侧边栏」；连带
          `PaneCell._notify_pane_focused` 读到焦点已不在本格而静默丢弃通知，
          侧边栏高亮和右上角会话小窗都停在旧格不动。

        为什么不能只靠在 `DescendantFocus` 上推 serial：那个事件冒泡到本区域是
        **异步**的，实测常常排在本方法之后才送达（`PaneCell` 自己的处理器倒是先
        跑，但它够不着这里的计数）。只有「此刻焦点是不是真在格子里」这个现查是
        可靠的。2026-07-31 由 CI 上确定性失败的
        `test_only_the_active_pane_draws_the_hud` 暴露，同一竞态也是
        `test_focusing_split_pane_highlights_matching_sidebar_session` 长期偶发的根因。
        """
        if self._apply_focus_intent() or self._focus_intent_key:
            return
        if (
            restore_list
            and self._focus_intent_serial == serial
            and not self.any_embed_focused()
        ):
            self._on_focus_list()

    def _schedule_mount(
        self,
        entries: list[tuple[PaneSpec, dict, Callable[[], Text | str] | None]],
        *,
        focus_key: str | None = None,
        focus_pane: bool = False,
    ) -> None:
        if focus_pane:
            if focus_key:
                self._claim_pane_input(focus_key)
        # 格池已够用且无需新建控件时同步改绑，少一帧「旧画面停住 / 空一帧」。
        # 首次建池或空态清场仍走 async（要 await mount/remove）。
        pool = self._pool_cells()
        need_async = (
            not entries
            or len(pool) < MAX_PANES
            or bool(self.query("#pane-row-empty"))
        )
        if not need_async and entries:
            self._mount_pending += 1
            self._apply_pane_bindings(
                entries, focus_key=focus_key, focus_pane=focus_pane, sync=True,
            )
            return
        self._mount_pending += 1
        self.call_next(
            self._mount_panes_async, entries, focus_key=focus_key, focus_pane=focus_pane,
        )

    def _apply_pane_bindings(
        self,
        entries: list[tuple[PaneSpec, dict, Callable[[], Text | str] | None]],
        *,
        focus_key: str | None = None,
        focus_pane: bool = False,
        sync: bool = False,
    ) -> None:
        """在已有格池上改绑 / 显隐。`sync=True` 表示同步路径（已计入 _mount_pending）。"""
        focused = getattr(self.app, "focused", None)
        list_had_focus = not focus_pane and focused is not None and (
            getattr(focused, "id", None) == "session-list"
            or type(focused).__name__ == "SessionListView"
        )
        serial = self._focus_intent_serial
        self._mount_pending = max(0, self._mount_pending - 1)
        pool = self._pool_cells()
        visible_before = sum(not cell._pooled for cell in pool)  # noqa: SLF001
        composition_changed = visible_before != len(entries)
        target_sizes = self._projected_embed_sizes(len(entries)) if composition_changed else None
        # 先把要用的前缀解冻并改绑，其余收回池里。
        self._panes = [s for s, _, _ in entries]
        for index, (spec, session, renderer) in enumerate(entries):
            title = self._pane_title(session)
            pool[index].rebind(
                spec,
                title=title,
                detail_renderer=renderer,
                target_size=target_sizes[index] if target_sizes is not None else None,
                discard_stale_screen=composition_changed,
            )
        for spare in pool[len(entries):]:
            if not spare._pooled:  # noqa: SLF001
                spare.park()
            else:
                spare.set_pooled(True)
        self._sync_leading_cells()
        if self._focus_intent_key or list_had_focus:
            self.call_after_refresh(
                lambda: self._settle_focus_intent(list_had_focus, serial)
            )
        elif focus_key:
            self.call_after_refresh(
                lambda: self.focus_session_key(focus_key, only_live=False)
            )
        self.call_after_refresh(self.sync_input_mask)

    async def _mount_panes_async(
        self,
        entries: list[tuple[PaneSpec, dict, Callable[[], Text | str] | None]],
        *,
        focus_key: str | None = None,
        focus_pane: bool = False,
    ) -> None:
        # remount 会弄丢焦点；列表原先有焦点时挂载后交回，避免 Enter 选不中。
        # 但 focus_pane 是调用方的明确意图（回车打开 / 新建托管成功），优先级
        # 高于「把焦点还回列表」，否则自动聚焦会被这段逻辑立刻撤销。
        focused = getattr(self.app, "focused", None)
        list_had_focus = not focus_pane and focused is not None and (
            getattr(focused, "id", None) == "session-list"
            or type(focused).__name__ == "SessionListView"
        )
        serial = self._focus_intent_serial
        self._mount_pending = max(0, self._mount_pending - 1)
        row = self.query_one("#pane-row", Horizontal)
        if not entries:
            for cell in self._pool_cells():
                cell.park()
            if not row.query("#pane-row-empty"):
                # 清掉非格子子节点（旧空态），保留格池控件。
                for child in list(row.children):
                    if not isinstance(child, PaneCell):
                        await child.remove()
                await row.mount(Static(t("split.empty_hint"), id="pane-row-empty"))
            self._panes = []
            self._focus_intent_key = None
            self._input_claim_key = None
            if list_had_focus:
                self.call_after_refresh(self._on_focus_list)
            return
        # 格池：一次挂满 MAX_PANES，之后跨组只改绑/显隐，2↔4 也不再 remove/mount。
        for child in list(row.children):
            if not isinstance(child, PaneCell):
                await child.remove()
        pool = self._pool_cells()
        while len(pool) < MAX_PANES:
            spare_spec = PaneSpec(
                session_key="__spare__", cell_id=self._new_cell_id(),
            )
            cell = self._make_cell(spare_spec, title="", renderer=None)
            cell.set_pooled(True)
            await row.mount(cell)
            pool = self._pool_cells()
        self._panes = [s for s, _, _ in entries]
        for index, (spec, session, renderer) in enumerate(entries):
            title = self._pane_title(session)
            pool[index].rebind(spec, title=title, detail_renderer=renderer)
        for spare in pool[len(entries):]:
            spare.park()
        self._sync_leading_cells()
        if self._focus_intent_key or list_had_focus:
            # 明确意图（可能是这次挂载排队期间才发生的点击/回车）压过「把焦点还回
            # 列表」；意图已在挂载期间兑现时也不能再抢回来，两种情况都由 settle 判。
            self.call_after_refresh(
                lambda: self._settle_focus_intent(list_had_focus, serial)
            )
        elif focus_key:
            self.call_after_refresh(
                lambda: self.focus_session_key(focus_key, only_live=False)
            )
        # 首帧要么已经压暗、要么已经交出焦点，不能等下一次焦点事件才同步。
        self.call_after_refresh(self.sync_input_mask)
