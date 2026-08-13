"""内嵌托管控制器：启动计划 → 后台 host worker → 成功收尾挂进右栏。

从 `main_screen.MainScreen` 拆出的方法组（架构整改阶段四）。`embed.host_session`
是真正阻塞的 tmux 子进程调用（`_CREATE_TIMEOUT` 上限 5s），必须甩给后台 worker，
不在 Textual 事件循环所在线程上跑，否则系统负载高/磁盘慢时整个 UI 被冻住那么久。
状态（`_host_pending`）仍挂在 MainScreen 实例上。
"""

from __future__ import annotations

import os

from textual import work

from pickup.i18n import t
from pickup.models import SHELL_RUNTIME_ID, LaunchPlan


def _shell_launch_plan(cwd: str) -> LaunchPlan:
    shell = (os.environ.get("SHELL") or "").strip() or "/bin/bash"
    if not os.path.isfile(shell) or not os.access(shell, os.X_OK):
        shell = "/bin/sh"
    return LaunchPlan(argv=(shell,), cwd=cwd)


class HostControllerMixin:
    """依赖宿主提供：`direct`、`embed_ok`、`osc_report`、`store`、`_split_area()`、
    `_can_autofocus`、`_render_detail`、`_show_session_group`、`_persist_split_composition`、
    `_begin_attention_read`、`_rebuild_list`、`_restore_direct_search_focus`。"""

    def _embed_open(self, request, *, add_pane: bool = False) -> None:
        """准备启动计划（不涉及阻塞 I/O）后，把 `embed.host_session` 这个真正阻塞的
        tmux 子进程调用甩给后台 worker（见 `_host_and_focus`），不在 Textual 事件
        循环所在线程上跑——tmux 卡顿（系统负载高/磁盘慢）时 `_CREATE_TIMEOUT` 上限
        有 5s，同步跑会把整个 UI 冻住那么久。"""
        import pickup
        from pickup import keepalive
        from pickup.split_layout import MAX_PANES

        # 原生恢复 = 同助手且未 force_new / copy_session；
        # 高级操作同助手另起 / 复制会话的官方分叉走新建分支（旁挂 + 占位卡）。
        native_resume = isinstance(request, pickup.LaunchRequest) and (
            request.session.get("source") == request.target_runtime_id
            and not request.force_new
            and not request.copy_session
        )
        area = self._split_area()
        if isinstance(request, pickup.LaunchRequest):
            key = pickup.session_key(request.session)
            current = self.store.find_session(key) or request.session
            request = pickup.LaunchRequest(
                current,
                request.target_runtime_id,
                request.title,
                force_new=request.force_new,
                copy_session=request.copy_session,
            )
            existing = request.session.get("keepalive_name") if native_resume else None
            if existing:
                # 回车打开已托管会话 = 明确意图，直接把输入交给右栏那一格。
                if add_pane:
                    area.add_hosted_pane(
                        current, str(existing),
                        lambda s=current: self._render_detail(s),
                        focus=True,
                        focus_pane=self._can_autofocus(),
                    )
                else:
                    self._show_session_group(key, focus_pane=True)
                return
            if self._host_pending > 0 and not add_pane:
                self.app.bell()
                return
            if add_pane and (area.pane_count() + self._host_pending) >= MAX_PANES:
                self.notify(t("split.full", n=MAX_PANES))
                self.app.bell()
                return
            plan = self.store.registry.build_launch_plan(request)
            ident = request.session["id"] if native_resume else keepalive.new_session_ident()
        else:
            if not add_pane and area.pane_count() > 0 and not area.can_add_pane():
                self.notify(t("split.full", n=MAX_PANES))
                self.app.bell()
                return
            if self._host_pending > 0 and not add_pane:
                self.app.bell()
                return
            if add_pane and (area.pane_count() + self._host_pending) >= MAX_PANES:
                self.notify(t("split.full", n=MAX_PANES))
                self.app.bell()
                return
            plan = self.store.registry.build_new_session_plan(request)
            ident = keepalive.new_session_ident()

        width, height = area.host_pane_size()
        self._host_pending += 1
        self._host_and_focus(
            request, plan, ident, native_resume, width, height, add_pane=add_pane,
        )

    @work(thread=True, group="host")
    def _host_and_focus(
        self, request, plan, ident, native_resume, width, height, *, add_pane: bool = False,
    ) -> None:
        import time

        import pickup
        from pickup import embed, observe

        t0 = time.perf_counter()
        runtime = request.target_runtime_id
        try:
            name = embed.host_session(
                plan, request.target_runtime_id, ident, width, height, osc_report=self.osc_report,
            )
        except Exception as exc:
            observe.event(
                "host_session",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                runtime=runtime,
                ok=False,
            )
            pickup._log_embed_error("内嵌会话启动线程", exc)
            self.app.call_from_thread(self._on_host_failed)
            return
        observe.event(
            "host_session",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            runtime=runtime,
            ok=True,
        )
        self.app.call_from_thread(
            self._on_embed_hosted, request, name, native_resume, add_pane,
        )

    def _on_host_failed(self) -> None:
        """host worker 失败收尾：释放托管计数并给用户终端响铃。"""
        self._host_pending = max(0, self._host_pending - 1)
        self._restore_direct_search_focus()
        self.app.bell()

    def _on_embed_hosted(
        self, request, name: str, native_resume: bool, add_pane: bool = False,
    ) -> None:
        """`_host_and_focus` worker 成功后的收尾：只在主线程操作 Textual/store 状态。

        `request` 可能是 `LaunchRequest`（恢复/接力）或 `NewSessionRequest`（空白新建）。
        后者没有关联会话，不能读 `.session`——空白新建路径曾经因此闪退。

        接力新建（含同助手 force_new）/ 空白新建时目标助手可能尚未落盘历史（例如
        Cursor 卡在 Workspace Trust），扫描器看不到条目；必须立刻插入托管占位卡并
        选中它，否则左栏空白、随后的 `_rebuild_list` 还会按仍选中的源会话把右栏盖回去。
        """
        import pickup

        self._host_pending = max(0, self._host_pending - 1)
        area = self._split_area()
        fallback = None
        select_key = None
        if isinstance(request, pickup.LaunchRequest):
            current = request.session
            if native_resume:
                key = pickup.session_key(request.session)
                marked = self.store.mark_hosted(key, name)
                if marked is None:
                    request.session["keepalive_name"] = name
                current = marked or request.session
            else:
                source_name = self.store.registry.get(
                    str(request.session.get("source") or "")
                ).display_name
                if request.copy_session:
                    title = request.title or f"复制自 {source_name}"
                else:
                    title = request.title or f"接力自 {source_name}"
                current = self.store.register_hosted_session(
                    runtime_id=request.target_runtime_id,
                    keepalive_name=name,
                    title=title,
                    cwd=str(request.session.get("cwd") or "") or None,
                )
                select_key = pickup.session_key(current)

            def fallback(s=current):
                return self._render_detail(s)
        else:
            runtime = self.store.registry.get(request.target_runtime_id)
            current = self.store.register_hosted_session(
                runtime_id=request.target_runtime_id,
                keepalive_name=name,
                title=f"新{runtime.display_name}会话",
                cwd=request.cwd,
            )
            select_key = pickup.session_key(current)

            def fallback(s=current):
                return self._render_detail(s)
        # 新建 / 接力托管成功同样是明确意图：用户就是来跟这个新会话说话的。
        autofocus = self._can_autofocus()
        if add_pane:
            area.add_hosted_pane(
                current, name, fallback, focus=True, focus_pane=autofocus,
            )
        elif self._split_store.get_group(pickup.session_key(current)) is not None:
            # 重启的是会话组里的成员：整组一起摆回去，只是它那一格从静态预览换成
            # 实时画面。退回单格会把用户的分屏组合当场拆掉。
            self._show_session_group(
                pickup.session_key(current), focus_pane=True, include_inactive=True
            )
        else:
            import pickup as pickup_mod

            key = pickup.session_key(current)
            project = pickup_mod._normalize_cwd(current.get("cwd"))
            area.show_hosted_group(
                project,
                [(current, name, fallback)],
                focus_key=key,
                focus_pane=autofocus,
            )
        self._persist_split_composition()
        self._begin_attention_read(pickup.session_key(current))
        self.call_next(self._rebuild_list, select_key)

    def _embed_open_shell(self, cwd: str) -> None:
        """顶栏「终端」：在当前项目目录下内嵌一个可自由输入的 shell 分屏。"""
        from pickup.split_layout import MAX_PANES

        area = self._split_area()
        if not area.can_add_pane():
            self.notify(t("split.full", n=MAX_PANES))
            self.app.bell()
            return
        if self._host_pending > 0:
            self.app.bell()
            return
        if area.pane_count() + self._host_pending >= MAX_PANES:
            self.notify(t("split.full", n=MAX_PANES))
            self.app.bell()
            return
        plan = _shell_launch_plan(cwd)
        from pickup import keepalive

        ident = keepalive.new_session_ident()
        width, height = area.host_pane_size()
        self._host_pending += 1
        self._host_shell_and_focus(cwd, plan, ident, width, height)

    @work(thread=True, group="host")
    def _host_shell_and_focus(
        self, cwd: str, plan: LaunchPlan, ident: str, width: int, height: int,
    ) -> None:
        import time

        import pickup
        from pickup import embed, observe

        t0 = time.perf_counter()
        try:
            name = embed.host_session(
                plan, SHELL_RUNTIME_ID, ident, width, height, osc_report=self.osc_report,
            )
        except Exception as exc:
            observe.event(
                "host_session",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                runtime=SHELL_RUNTIME_ID,
                ok=False,
            )
            pickup._log_embed_error("内嵌 shell 启动线程", exc)
            self.app.call_from_thread(self._on_host_failed)
            return
        observe.event(
            "host_session",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            runtime=SHELL_RUNTIME_ID,
            ok=True,
        )
        self.app.call_from_thread(self._on_shell_hosted, name, cwd)

    def _on_shell_hosted(self, name: str, cwd: str) -> None:
        self._host_pending = max(0, self._host_pending - 1)
        area = self._split_area()
        current = self.store.register_hosted_session(
            runtime_id=SHELL_RUNTIME_ID,
            keepalive_name=name,
            title=t("shell.pane_title"),
            cwd=cwd,
        )
        area.add_hosted_pane(
            current,
            name,
            None,
            focus=True,
            focus_pane=self._can_autofocus(),
            is_shell=True,
        )
        self._persist_split_composition()

    def _host_direct_launch(self) -> None:
        if self._host_pending >= 3:
            self.app.bell()
            return
        direct = self.direct
        area = self._split_area()
        width, height = area.host_pane_size()
        self._host_pending += 1
        self._host_direct_worker(direct, width, height)

    @work(thread=True, group="host")
    def _host_direct_worker(self, direct, width: int, height: int) -> None:
        import time

        import pickup
        from pickup import embed, observe

        t0 = time.perf_counter()
        runtime = direct.runtime_id
        try:
            name = embed.host_session(
                direct.plan, direct.runtime_id, direct.ident, width, height, osc_report=self.osc_report,
            )
        except Exception as exc:
            observe.event(
                "host_session",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                runtime=runtime,
                ok=False,
            )
            pickup._log_embed_error("直启会话启动线程", exc)
            self.app.call_from_thread(self._on_host_failed)
            return
        observe.event(
            "host_session",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            runtime=runtime,
            ok=True,
        )
        self.app.call_from_thread(self._on_direct_hosted, name)

    def _on_direct_hosted(self, name: str) -> None:
        self._host_pending = max(0, self._host_pending - 1)
        self._restore_direct_search_focus()
        area = self._split_area()
        direct = self.direct
        runtime = self.store.registry.get(direct.runtime_id)
        cwd = direct.plan.cwd or os.getcwd()
        session = self.store.register_hosted_session(
            runtime_id=direct.runtime_id,
            keepalive_name=name,
            title=f"新{runtime.display_name}会话",
            cwd=cwd,
            ident=direct.ident,
        )
        from pickup import _normalize_cwd, session_key

        key = session_key(session)
        area.show_hosted_group(
            _normalize_cwd(cwd),
            [(session, name, None)],
            focus_key=key,
        )
        self._persist_split_composition()
        self.call_next(self._rebuild_list, key)
        cells = area.cells()
        if cells:
            pane = cells[0].embed_pane()
            if pane is None:
                # 竞态窗口：worker 回调到达时 pane 尚未创建（或已被卸载）。
                # 下一轮 rebuild 会按托管身份把画面挂回来，这里直接放弃聚焦。
                return
            pane.focus_session(name)
            self.set_focus(pane)
