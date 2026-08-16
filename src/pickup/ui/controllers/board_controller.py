"""活跃会话看板：侧栏冻项选中后，右栏自动铺当前需要盯的托管会话。

不写入持久分屏组合。离开看板（点某条具体会话）后恢复原来的单会话 / 手动分屏。
"""

from __future__ import annotations

from pickup.activity_board import ActivityBoard, collect_candidates
from pickup.ui.session_list import (
    STICKY_IDS,
    SessionListView,
    _focused_board_session_key,
)


class BoardControllerMixin:
    """依赖宿主提供：`embed_ok`、`store`、`_split_area()`、`_build_hosted_entries`、
    `_begin_attention_read`、`_sync_split_marks`、`_live_embed_focused`、`_can_autofocus`。"""

    _activity_board: ActivityBoard
    _activity_board_active: bool

    def _leave_activity_board(self) -> None:
        if not getattr(self, "_activity_board_active", False):
            return
        self._activity_board_active = False
        self._activity_board.reset()
        self._cancel_board_linger_timer()
        try:
            area = self._split_area()
        except Exception:
            return
        area.title_with_project = False
        area.allow_cross_project = False

    def _board_skips_split_cap(self) -> bool:
        """看板超额进队列翻页，不走水果组「分屏已满」。"""
        return bool(getattr(self, "_activity_board_active", False))

    def _leave_activity_board_to_first_session(self) -> None:
        """顶栏开终端等明确离开：侧栏离开看板，避免跟随又把看板铺回来。"""
        try:
            session_list = self.query_one(SessionListView)
        except Exception:
            self._leave_activity_board()
            return
        sticky_n = len(STICKY_IDS)
        items = session_list.list_children
        if session_list.is_activity_board_selected() and len(items) > sticky_n:
            session_list.index = sticky_n
        elif session_list.is_activity_board_selected():
            session_list.index = 0
        self._leave_activity_board()

    def _sync_activity_board_entry(self) -> None:
        """刷新侧栏看板入口的页码/等待提示；已进入看板时同步右栏成员。"""
        try:
            session_list = self.query_one(SessionListView)
        except Exception:
            return
        if getattr(self, "_activity_board_active", False):
            self._show_activity_board(focus_pane=False)
            return
        # 未进入看板时角标永远按第一页计算：临时实例只算 total / 后页急件，不携带状态。
        snapshot = ActivityBoard().sync(collect_candidates(self.store))
        session_list.set_board_snapshot(snapshot)

    def _show_activity_board(self, *, focus_pane: bool = False) -> None:
        if not self.embed_ok:
            return
        if not self._activity_board_active:
            self._activity_board.reset()
            self._activity_board_active = True
        session_list = self.query_one(SessionListView)
        area = self._split_area()
        typing = _focused_board_session_key(getattr(self.app, "focused", None))
        self._activity_board.set_typing_key(typing)
        snapshot = self._activity_board.sync(collect_candidates(self.store))
        session_list.set_board_snapshot(snapshot)
        self._arm_board_linger_timer()
        if not snapshot.keys:
            if area.ordered_session_keys() == ["__board_empty__"]:
                return
            area.show_activity_board_empty()
            self._sync_split_marks()
            return
        import pickup

        entries = self._build_hosted_entries(list(snapshot.keys))
        if not entries:
            area.show_activity_board_empty()
            self._sync_split_marks()
            return
        first = entries[0][0]
        project = pickup._normalize_cwd(first.get("cwd"))
        focus_key = typing if typing in snapshot.keys else snapshot.keys[0]
        area.show_hosted_group(
            project,
            entries,
            focus_key=focus_key,
            focus_pane=focus_pane and self._can_autofocus(),
            title_with_project=True,
            allow_cross_project=True,
        )
        self._sync_split_marks()
        if focus_key:
            self._begin_attention_read(focus_key)

    def _page_activity_board(self, delta: int) -> None:
        if not self._activity_board_active:
            return
        if self._live_embed_focused():
            return
        self._activity_board.turn_page(delta)
        self._show_activity_board(focus_pane=False)

    def action_board_prev(self) -> None:
        self._page_activity_board(-1)

    def action_board_next(self) -> None:
        self._page_activity_board(1)

    def _dismiss_board_pane(self, session_key: str) -> None:
        """看板里关格：本轮不再展示这一格，不改持久会话组。"""
        self._activity_board.dismiss(session_key)
        self._show_activity_board(focus_pane=False)

    def _cancel_board_linger_timer(self) -> None:
        timer = getattr(self, "_board_linger_timer", None)
        if timer is None:
            return
        timer.stop()
        self._board_linger_timer = None

    def _arm_board_linger_timer(self) -> None:
        """暂留到期后必须再铺一次，否则后台重扫没变化时格子会一直占着。"""
        import time as time_mod

        self._cancel_board_linger_timer()
        deadline = self._activity_board.next_linger_deadline()
        if deadline is None:
            return
        delay = max(0.05, deadline - time_mod.monotonic())
        self._board_linger_timer = self.set_timer(delay, self._on_board_linger_expired)

    def _on_board_linger_expired(self) -> None:
        self._board_linger_timer = None
        if not getattr(self, "_activity_board_active", False):
            return
        self._show_activity_board(focus_pane=False)
