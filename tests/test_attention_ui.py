"""会话关注状态在侧边栏与右侧详情中的界面回归测试。"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

import pickup
from pickup import i18n
from pickup.attention import AttentionState
from pickup.models import ConversationMessage
from pickup.ui.app import PickupApp
from pickup.ui.main_screen import MainScreen
from pickup.ui.session_list import SessionCard
from textual.geometry import Size


def _make_store(*, sessions: list[dict] | None = None):
    sessions = sessions or [
        {
            "source": "claude",
            "id": f"s{index}",
            "short_id": f"s{index}",
            "mtime": time.time() - index,
            "size_bytes": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": f"会话{index}",
            "cwd": "/tmp/pickup",
            "live": False,
        }
        for index in range(2)
    ]
    runtime = mock.Mock(id="claude", display_name="Claude")
    runtime.is_available.return_value = True
    runtime.scan_sessions.return_value = sessions
    runtime.load_conversation.return_value = [
        ConversationMessage("user", "测试问题"),
        ConversationMessage("assistant", "测试答复"),
    ]
    attention_store = mock.Mock()
    attention_store.reconcile.return_value = {}
    registry = pickup.RuntimeRegistry((runtime,))
    with mock.patch.object(pickup.titles, "load_cache", return_value={}):
        store = pickup.SessionStore(
            limit=20, registry=registry, attention_store=attention_store,
        )
        store.load()
    return store


def _set_attention(store, key: str, kind: str) -> None:
    session = store.find_session(key)
    session["attention_kind"] = kind
    session["attention_token"] = f"{kind}-token"
    session["attention_updated_at"] = time.time()
    store.attention_states[key] = AttentionState(
        kind=kind, activity_token=f"{kind}-token", updated_at=time.time(),
    )


def _mark_read_side_effect(store):
    def mark(key: str) -> AttentionState:
        session = store.find_session(key)
        session["attention_kind"] = "none"
        session["attention_updated_at"] = time.time()
        state = AttentionState()
        store.attention_states[key] = state
        return state

    return mark


class SessionAttentionCardTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n.set_lang("en")

    @staticmethod
    def _card(kind: str, *, live: bool = False) -> SessionCard:
        runtime = mock.Mock(id="claude", display_name="Claude")
        store = mock.Mock()
        store.registry.get.return_value = runtime
        session = {
            "source": "claude",
            "id": "visual",
            "fallback_title": "修复状态展示",
            "cwd": "/tmp/pickup",
            "mtime": time.time(),
            "live": live,
            "attention_kind": kind,
            "attention_token": "token",
            "attention_updated_at": 1.0,
        }
        return SessionCard(session, store, display_title="修复状态展示")

    def _render(self, kind: str, *, live: bool = False):
        card = self._card(kind, live=live)
        with mock.patch.object(
            SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 3),
        ):
            return card.render()

    def test_one_dot_uses_waiting_working_unread_colors(self) -> None:
        expected = {
            "waiting": "yellow",
            "working": "green",
            "unread": "red",
        }
        for kind, color in expected.items():
            with self.subTest(kind=kind):
                rendered = self._render(kind)
                lines = rendered.plain.splitlines()
                self.assertEqual(lines[1].count("●"), 1)
                dot = rendered.plain.index("●")
                dot_spans = [span for span in rendered.spans if span.start <= dot < span.end]
                self.assertTrue(
                    any(color in str(span.style).lower() for span in dot_spans),
                    dot_spans,
                )

        self.assertNotIn("●", self._render("none").plain)

    def test_card_keeps_three_lines_fixed_width_and_runtime_right_aligned(self) -> None:
        rendered = self._render("waiting")
        lines = rendered.plain.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual([pickup._text_width(line) for line in lines], [39, 39, 39])
        self.assertTrue(lines[1].startswith("● "))
        self.assertTrue(lines[1].endswith("Claude"))

    def test_title_style_is_uniform_even_when_session_is_live(self) -> None:
        rendered = self._render("working", live=True)
        title_end = rendered.plain.index("\n")
        title_spans = [span for span in rendered.spans if span.start < title_end]
        self.assertTrue(any("bold" in str(span.style).lower() for span in title_spans))
        self.assertFalse(any("green" in str(span.style).lower() for span in title_spans))
        self.assertFalse(any("#3f9a6a" in str(span.style).lower() for span in title_spans))


class AttentionDetailTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_detail_header_exposes_attention_and_lifecycle_in_both_languages(self) -> None:
        store = _make_store()
        session = store.find_session("claude:s0")
        screen = MainScreen(store, embed_ok=False)
        expected = {
            "waiting": ("Waiting for your answer", "等待你的回答"),
            "working": ("Working", "执行中"),
            "unread": ("New result", "有新结果"),
            "none": ("No attention status", "无关注状态"),
        }
        for kind, (english, chinese) in expected.items():
            session["attention_kind"] = kind
            i18n.set_lang("en")
            header = screen._detail_header(session).plain
            self.assertIn("Ended", header)
            self.assertIn(english, header)
            i18n.set_lang("zh")
            header = screen._detail_header(session).plain
            self.assertIn("已结束", header)
            self.assertIn(chinese, header)
        i18n.set_lang("en")


class AttentionReadFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        i18n.set_lang("en")

    async def test_loaded_preview_clears_unread_only_after_stable_delay(self) -> None:
        store = _make_store()
        store.mark_session_read = mock.Mock(side_effect=_mark_read_side_effect(store))
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.ui.main_screen._ATTENTION_READ_DELAY", 0.12),
            mock.patch("pickup.ui.main_screen._ATTENTION_READY_POLL", 0.01),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.05)
                await pilot.press("down")
                await pilot.pause(delay=0.12)
                _set_attention(store, "claude:s0", "unread")
                app.screen._begin_attention_read("claude:s0")
                await asyncio.sleep(0.07)
                self.assertFalse(store.mark_session_read.called)
                await asyncio.sleep(0.18)
                store.mark_session_read.assert_called_once_with("claude:s0")
                self.assertEqual(store.find_session("claude:s0")["attention_kind"], "none")

    async def test_quick_selection_change_does_not_clear_passed_session(self) -> None:
        store = _make_store()
        store.mark_session_read = mock.Mock(side_effect=_mark_read_side_effect(store))
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.ui.main_screen._ATTENTION_READ_DELAY", 0.15),
            mock.patch("pickup.ui.main_screen._ATTENTION_READY_POLL", 0.01),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.05)
                await pilot.press("down")
                await pilot.pause(delay=0.12)
                _set_attention(store, "claude:s0", "unread")
                app.screen._begin_attention_read("claude:s0")
                await asyncio.sleep(0.03)
                list_view = app.screen.query_one("#session-list")
                list_view.index = 2
                app.screen.on_list_view_highlighted(None)
                await asyncio.sleep(0.22)
                store.mark_session_read.assert_not_called()
                self.assertEqual(store.find_session("claude:s0")["attention_kind"], "unread")

    async def test_app_blur_cancels_read_and_refocus_restarts_full_delay(self) -> None:
        store = _make_store()
        store.mark_session_read = mock.Mock(side_effect=_mark_read_side_effect(store))
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.ui.main_screen._ATTENTION_READ_DELAY", 0.14),
            mock.patch("pickup.ui.main_screen._ATTENTION_READY_POLL", 0.01),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.05)
                await pilot.press("down")
                await pilot.pause(delay=0.12)
                _set_attention(store, "claude:s0", "unread")
                app.screen._begin_attention_read("claude:s0")
                await asyncio.sleep(0.06)
                app.screen._on_app_focus_changed(False)
                await asyncio.sleep(0.18)
                store.mark_session_read.assert_not_called()
                app.screen._on_app_focus_changed(True)
                await asyncio.sleep(0.08)
                store.mark_session_read.assert_not_called()
                await asyncio.sleep(0.14)
                store.mark_session_read.assert_called_once_with("claude:s0")

    async def test_preview_load_failure_never_clears_unread(self) -> None:
        store = _make_store()
        store.mark_session_read = mock.Mock(side_effect=_mark_read_side_effect(store))
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.ui.main_screen._ATTENTION_READ_DELAY", 0.08),
            mock.patch("pickup.ui.main_screen._ATTENTION_READY_POLL", 0.01),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.05)
                await pilot.press("down")
                await pilot.pause(delay=0.12)
                store.conversations.clear()
                store.get_conversation = mock.Mock(side_effect=OSError("模拟预览加载失败"))
                _set_attention(store, "claude:s0", "unread")
                app.screen._begin_attention_read("claude:s0")
                await asyncio.sleep(0.22)
                store.mark_session_read.assert_not_called()

    async def test_waiting_and_working_are_never_cleared_by_viewing(self) -> None:
        for kind in ("waiting", "working"):
            with self.subTest(kind=kind):
                store = _make_store()
                store.mark_session_read = mock.Mock(side_effect=_mark_read_side_effect(store))
                app = PickupApp(store, embed_ok=True)
                with mock.patch("pickup.ui.main_screen._ATTENTION_READY_POLL", 0.01):
                    async with app.run_test(size=(120, 30)) as pilot:
                        await pilot.pause(delay=0.08)
                        _set_attention(store, "claude:s0", kind)
                        app.screen._begin_attention_read("claude:s0")
                        await pilot.pause(delay=0.15)
                        store.mark_session_read.assert_not_called()


class CursorObserverBackgroundInstallTests(unittest.IsolatedAsyncioTestCase):
    async def test_install_runs_in_background_and_failure_is_silent(self) -> None:
        store = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            with mock.patch(
                "pickup.cursor_observer.install", side_effect=OSError("模拟配置不可写"),
            ) as install:
                worker = app.screen._install_cursor_observer()
                await worker.wait()
                await pilot.pause()
            install.assert_called_once_with()
            self.assertIs(app.screen, app.screen)


if __name__ == "__main__":
    unittest.main()
