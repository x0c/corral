"""MainScreen ↔ UpdateToast 的接线：后台检查 worker、点击回调、忽略状态持久化。

真实 detect_channel() 在开发树里恒为 "dev"（见 test_updater.py），所以这里全程
mock updater 的对外函数，既不发真实网络请求，也能覆盖"有新版本"这条路径。
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

import corral
from corral import i18n, updater
from corral.ui.app import CorralApp
from corral.ui.update_toast import UpdateToast

i18n.set_lang("en")


def _make_store():
    sessions = [
        {
            "source": "claude", "id": "s0", "short_id": "s0",
            "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
            "native_title": None, "fallback_title": "会话0",
            "cwd": "/tmp", "live": False,
        }
    ]
    claude = mock.Mock()
    claude.id = "claude"
    claude.display_name = "Claude"
    claude.is_available.return_value = True
    claude.scan_sessions.return_value = sessions
    registry = corral.RuntimeRegistry((claude,))
    with mock.patch.object(corral.titles, "load_cache", return_value={}):
        store = corral.SessionStore(limit=20, registry=registry)
        store.load()
    return store


async def _wait_until(predicate, *, tries: int = 100, interval: float = 0.02) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met in time")


class UpdateCheckWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_updatable_channel_with_newer_version_reveals_toast(self) -> None:
        store = _make_store()
        app = CorralApp(store, embed_ok=False)
        with mock.patch.object(updater, "detect_channel", return_value="pip"), \
             mock.patch.object(updater, "is_updatable", return_value=True), \
             mock.patch.object(updater, "fetch_latest", return_value="9.9.9"), \
             mock.patch.object(updater, "should_prompt", return_value=True):
            async with app.run_test(size=(100, 30)):
                toast = app.screen.query_one(UpdateToast)
                await _wait_until(lambda: toast.has_class("-visible"))
                body_text = toast.query_one("#toast-body").render().plain
                self.assertIn("9.9.9", body_text)

    async def test_dev_channel_never_shows_toast(self) -> None:
        store = _make_store()
        app = CorralApp(store, embed_ok=False)
        with mock.patch.object(updater, "detect_channel", return_value="dev"), \
             mock.patch.object(updater, "fetch_latest") as fetch:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await asyncio.sleep(0.1)
                toast = app.screen.query_one(UpdateToast)
                self.assertFalse(toast.has_class("-visible"))
                fetch.assert_not_called()

    async def test_not_newer_version_never_shows_toast(self) -> None:
        store = _make_store()
        app = CorralApp(store, embed_ok=False)
        with mock.patch.object(updater, "detect_channel", return_value="pip"), \
             mock.patch.object(updater, "is_updatable", return_value=True), \
             mock.patch.object(updater, "fetch_latest", return_value="0.1.0"), \
             mock.patch.object(updater, "should_prompt", return_value=False):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await asyncio.sleep(0.1)
                toast = app.screen.query_one(UpdateToast)
                self.assertFalse(toast.has_class("-visible"))

    async def test_click_update_exits_before_installation_starts(self) -> None:
        store = _make_store()
        app = CorralApp(store, embed_ok=False)
        with mock.patch.object(updater, "detect_channel", return_value="pip"), \
             mock.patch.object(updater, "is_updatable", return_value=True), \
             mock.patch.object(updater, "fetch_latest", return_value="9.9.9"), \
             mock.patch.object(updater, "should_prompt", return_value=True), \
             mock.patch.object(updater, "run_update") as run_update:
            async with app.run_test(size=(100, 30)) as pilot:
                toast = app.screen.query_one(UpdateToast)
                await _wait_until(lambda: toast.has_class("-visible"))
                await pilot.click("#toast-body")
                await _wait_until(lambda: app._exit)
        run_update.assert_not_called()
        self.assertIsInstance(app.return_value, updater.RestartRequest)
        self.assertEqual(app.return_value.latest, "9.9.9")
        self.assertEqual(app.return_value.channel, "pip")

    async def test_dismiss_calls_mark_dismissed_and_hides_toast(self) -> None:
        store = _make_store()
        app = CorralApp(store, embed_ok=False)
        with mock.patch.object(updater, "detect_channel", return_value="pip"), \
             mock.patch.object(updater, "is_updatable", return_value=True), \
             mock.patch.object(updater, "fetch_latest", return_value="9.9.9"), \
             mock.patch.object(updater, "should_prompt", return_value=True), \
             mock.patch.object(updater, "mark_dismissed") as mark_dismissed:
            async with app.run_test(size=(100, 30)) as pilot:
                toast = app.screen.query_one(UpdateToast)
                await _wait_until(lambda: toast.has_class("-visible"))
                await pilot.click("#toast-close")
                await _wait_until(lambda: not toast.has_class("-visible"))
        mark_dismissed.assert_called_once_with("9.9.9")


if __name__ == "__main__":
    unittest.main()
