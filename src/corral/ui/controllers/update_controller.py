"""客户端自动更新浮层控制器：右下角浮层的检查 / 升级 / 重启 / 重试 / 忽略。

从 `main_screen.MainScreen` 拆出的方法组（架构整改阶段四）。每次打开 corral
都后台查一次最新版本；源码/开发安装（无法一键升级）时直接跳过，不弹窗打扰。
检查跑在 worker 线程，任何异常都不能拖垮 UI 或阻塞首屏。真正升级前必须先退出
界面：Homebrew / pipx / pip 都可能替换当前进程正在使用的安装目录，旧界面继续渲染
会在下一次惰性导入时随机缺模块。状态（`_update_channel` / `_update_latest`）仍挂在
MainScreen 实例上。
"""

from __future__ import annotations

from textual import work
from textual.worker import get_current_worker

from corral import updater
from corral.ui.update_toast import UpdateToast


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
        self.app.exit(
            result=updater.RestartRequest(self._update_latest, self._update_channel)
        )

    def _on_update_toast_restart(self) -> None:
        # 兼容已进入完成态的浮层；正常路径点击「更新」时已经直接退出界面。
        self._on_update_toast_update()

    def _on_update_toast_retry(self) -> None:
        self._on_update_toast_update()

    def _on_update_toast_dismiss(self, version: str) -> None:
        updater.mark_dismissed(version)
        self.query_one(UpdateToast).hide()
