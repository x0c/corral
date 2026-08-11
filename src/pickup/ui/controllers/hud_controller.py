"""会话小窗控制器：每个右栏格右上角的提问概览浮层。

从 `main_screen.MainScreen` 拆出的方法组（架构整改阶段四）。主线程只做
stat + 内存缓存命中判断（`peek_conversation`），真正解析历史有 HUD_WARM_INTERVAL
节流并放后台 worker。状态（`_hud_expanded` / `_hud_cache` / `_hud_warm_at`）仍
挂在 MainScreen 实例上；`HUD_POLL_INTERVAL` 被 MainScreen.on_mount 引用，
由它从本模块导入。
"""

from __future__ import annotations

import time

from textual import work

from pickup.ui.session_hud import summarize_user_messages

# 秒，右上角会话小窗的同步间隔。每次只做一次 stat + 内存缓存命中判断（`peek_conversation`），
# 真正解析历史另有节流（HUD_WARM_INTERVAL），不会因为助手在狂写历史就每秒重解析一遍。
HUD_POLL_INTERVAL = 1.0
HUD_WARM_INTERVAL = 3.0  # 秒，同一会话两次重新解析对话之间的最小间隔


class HudControllerMixin:
    """依赖宿主提供：`embed_ok`、`store`、`_split_area()`。"""

    def _hud_live_targets(self) -> list[tuple[str, dict]]:
        """返回该画小窗的 (会话键, 会话) 列表。

        实时托管格与静态对话预览格都画：长对话里小窗仍能一眼扫到提问脉络，
        不必整篇翻预览。占位卡（直启/空白新建后尚未写出真实历史）在快照里
        找不到会话，先不画。
        """
        if not self.embed_ok:
            return []
        try:
            area = self._split_area()
        except Exception:
            # 内嵌不可用时右栏根本不在 DOM 里（纯列表模式），不能裸 query_one。
            return []
        targets: list[tuple[str, dict]] = []
        for spec in area.pane_specs():
            session = self.store.find_session(spec.session_key)
            if session is None:
                continue
            targets.append((spec.session_key, session))
        return targets

    def _sync_hud(self) -> None:
        """把每个右栏格的小窗刷成各自最新摘要。主线程调用，只做 stat + 内存缓存判定。"""
        if not self.embed_ok:
            return
        try:
            area = self._split_area()
        except Exception:
            return
        payloads: dict[str, object | None] = {}
        to_warm: list[tuple[dict, str]] = []
        for key, session in self._hud_live_targets():
            messages = self.store.peek_conversation(session)
            if messages is None:
                # 助手正在写历史，内存缓存已按 mtime 失效：继续显示上一次的摘要，
                # 同时按节流去后台重解析，避免小窗每秒空一下再闪回来。
                payloads[key] = self._hud_cache.get(key)
                to_warm.append((session, key))
            else:
                data = summarize_user_messages(messages)
                self._hud_cache[key] = data
                payloads[key] = data or None
        area.sync_hud(payloads, expanded=self._hud_expanded)
        if to_warm:
            self._schedule_hud_warm(to_warm)

    def _schedule_hud_warm(self, items: list[tuple[dict, str]]) -> None:
        now = time.monotonic()
        due: list[tuple[dict, str]] = []
        for session, key in items:
            if now - self._hud_warm_at.get(key, 0.0) < HUD_WARM_INTERVAL:
                continue
            self._hud_warm_at[key] = now
            due.append((session, key))
        if due:
            self._warm_hud(due)

    @work(thread=True, exclusive=True, group="hud-warm")
    def _warm_hud(self, items: list[tuple[dict, str]]) -> None:
        """后台解析对话（超大会话可到 200ms 量级），完成后回主线程刷小窗。"""
        for session, _key in items:
            try:
                self.store.get_conversation(session)
            except Exception:
                continue
        self.app.call_from_thread(self._sync_hud)

    def action_toggle_hud(self) -> None:
        """展开/收起会话小窗；展开状态所有格共用一份。"""
        if not self.embed_ok:
            return
        self._hud_expanded = not self._hud_expanded
        self._sync_hud()
