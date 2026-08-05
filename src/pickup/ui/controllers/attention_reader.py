"""关注已读跟踪器：红点会话在右侧真实可见并连续停留 0.5 秒后自动标已读。

从 `main_screen.MainScreen` 拆出的方法组（架构整改阶段四）：只依赖屏幕提供的
`embed_ok` / `_app_focused` / `_split_area()` / `store` / `set_timer` / `query_one`。
状态（`_attention_read_*` / `_attention_visible_since` / `_app_focused`）仍挂在
MainScreen 实例上，这里只是方法容器。
"""

from __future__ import annotations

from pickup.ui.session_list import SessionListView

# 红点只有在右侧内容真实可见并连续稳定一段时间后才算已读；等待首帧或对话缓存
# 时做轻量轮询，不能把「选中过」误当成「看过了」。
_ATTENTION_READ_DELAY = 0.5
_ATTENTION_READY_POLL = 0.1


class AttentionReaderMixin:
    """依赖宿主提供：`embed_ok`、`_app_focused`、`_split_area()`、`store`。"""

    def _on_app_focus_changed(self, focused: bool) -> None:
        """终端应用失焦即作废连续查看；重新聚焦后从零开始计算。"""
        self._app_focused = bool(focused)
        self._cancel_attention_read()
        if focused:
            self.call_next(self._begin_selected_attention_read)

    def _cancel_attention_read(self) -> None:
        timer = self._attention_read_timer
        self._attention_read_timer = None
        if timer is not None:
            timer.stop()
        self._attention_read_key = None
        self._attention_read_token = None
        self._attention_visible_since = None

    def _begin_selected_attention_read(self) -> None:
        if not self.embed_ok:
            return
        key = self.query_one(SessionListView)._displayed_selected_key()
        if key is None:
            return
        self._begin_attention_read(key)

    def _begin_attention_read(self, key: str) -> None:
        """开始观察一条红点会话；此时不等于已读，先等右侧内容真实就绪。"""
        self._cancel_attention_read()
        if not self.embed_ok or not self._app_focused:
            return
        session = self.store.find_session(key)
        if session is None or session.get("attention_kind") != "unread":
            return
        self._attention_read_key = key
        self._attention_read_token = session.get("attention_token")
        self._attention_read_timer = self.set_timer(
            _ATTENTION_READY_POLL, self._check_attention_read,
        )

    def _attention_view_ready(self, key: str) -> bool:
        """目标会话是否仍被选中，且右侧已画出可读的预览或真实终端首帧。"""
        if not self.embed_ok or not self._app_focused:
            return False
        if self.query_one(SessionListView)._displayed_selected_key() != key:
            return False
        session = self.store.find_session(key)
        if session is None or session.get("attention_kind") != "unread":
            return False
        area = self._split_area()
        for cell in area.cells():
            if cell.spec.session_key != key:
                continue
            pane = cell.embed_pane()
            if pane is None or pane.size.width <= 0 or pane.size.height <= 0:
                return False
            keepalive_name = cell.spec.keepalive_name
            if keepalive_name:
                # 仅挂上控件或正在显示静态回退都不算；首帧成功写入网格后，用户
                # 才真正看到了这个托管终端。
                return (
                    pane.session_name == keepalive_name
                    and not pane.dead
                    and pane._grid is not None  # noqa: SLF001
                )
            # 静态详情只有对话缓存已成功填充后才算就绪；加载异常会一直保持 None，
            # 因而不会因为右栏只出现标题或空白回退而误清红点。
            return pane.session_name is None and self.store.peek_conversation(session) is not None
        return False

    def _check_attention_read(self) -> None:
        """轮询首帧/预览就绪，并在连续可见 0.5 秒后清除红点。"""
        import time as _time

        self._attention_read_timer = None
        key = self._attention_read_key
        if key is None:
            return
        session = self.store.find_session(key)
        if session is None or session.get("attention_kind") != "unread":
            self._cancel_attention_read()
            return
        token = session.get("attention_token")
        if token != self._attention_read_token:
            # 正在看的 0.5 秒内又到了一条新结果：旧计时不能顺手把新结果也标成
            # 已读，必须从新内容真实可见的时刻重新完整计算。
            self._attention_read_token = token
            self._attention_visible_since = None
        if not self._attention_view_ready(key):
            self._attention_visible_since = None
            if self._app_focused:
                self._attention_read_timer = self.set_timer(
                    _ATTENTION_READY_POLL, self._check_attention_read,
                )
            return
        now = _time.monotonic()
        if self._attention_visible_since is None:
            self._attention_visible_since = now
            self._attention_read_timer = self.set_timer(
                _ATTENTION_READ_DELAY, self._check_attention_read,
            )
            return
        remaining = _ATTENTION_READ_DELAY - (now - self._attention_visible_since)
        if remaining > 0:
            self._attention_read_timer = self.set_timer(
                remaining, self._check_attention_read,
            )
            return

        self._attention_read_key = None
        self._attention_read_token = None
        self._attention_visible_since = None
        state = self.store.mark_session_read(key)
        if state.kind == "none":
            # mark_session_read 已原地更新会话快照；重建只会刷新发生变化的卡片，
            # 同时让详情头的可访问文字与红点一起消失。
            self.call_next(self._rebuild_list, key)
