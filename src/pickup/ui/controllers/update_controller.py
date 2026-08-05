"""客户端自动更新浮层控制器：右下角浮层的检查 / 升级 / 重启 / 重试 / 忽略。

从 `main_screen.MainScreen` 拆出的方法组（架构整改阶段四）。每次打开 pickup
都后台查一次最新版本；源码/开发安装（无法一键升级）时直接跳过，不弹窗打扰。
检查/升级全程跑在 worker 线程，任何异常都不能拖垮 UI 或阻塞首屏——updater
模块本身已把网络/子进程异常全部吞掉。状态（`_update_channel` /
`_update_latest`）仍挂在 MainScreen 实例上。
"""

from __future__ import annotations

from textual import work
from textual.worker import get_current_worker

from pickup import updater
from pickup.ui.update_toast import UpdateToast


class UpdateControllerMixin:
    """依赖宿主提供：`app`、`query_one`。"""

    @work(thread=True, group="update-check")
    def _check_for_update(self) -> None:
        channel = updater.detect_channel()
        if not updater.is_updatable(channel):
            return
        latest = updater.fetch_latest()
        if latest is None or not updater.should_prompt(latest):
            return
        self._update_channel = channel
        self._update_latest = latest
        worker = get_current_worker()
        if not worker.is_cancelled:
            self.app.call_from_thread(lambda: self.query_one(UpdateToast).show_available(latest))

    def _on_update_toast_update(self) -> None:
        toast = self.query_one(UpdateToast)
        toast.show_updating()
        self._run_update_worker()

    @work(thread=True, group="update-apply")
    def _run_update_worker(self) -> None:
        from pickup import observe

        latest = self._update_latest
        ok, output = updater.run_update(latest, self._update_channel)
        observe.event("self_update", ok=ok, latest=latest, channel=self._update_channel)
        if not ok:
            observe.debug("self_update_output", output=output)
        worker = get_current_worker()
        if worker.is_cancelled:
            return
        toast = self.query_one(UpdateToast)
        if ok:
            self.app.call_from_thread(lambda: toast.show_done(latest))
        else:
            self.app.call_from_thread(lambda: toast.show_failed(output))

    def _on_update_toast_restart(self) -> None:
        # 交给 cli.main()：用新装好的磁盘代码 re-exec 一个全新 pickup 进程。
        self.app.exit(result=updater.RestartRequest())

    def _on_update_toast_retry(self) -> None:
        self._on_update_toast_update()

    def _on_update_toast_dismiss(self, version: str) -> None:
        updater.mark_dismissed(version)
        self.query_one(UpdateToast).hide()
