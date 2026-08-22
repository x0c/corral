"""会话关联完整性回归测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from corral import split_layout
from corral.ui.controllers.layout_controller import (
    LayoutControllerMixin,
    _preserve_missing_group_members,
)
from corral.ui.main_screen import MainScreen


class SplitGroupIntegrityTests(unittest.TestCase):
    def test_browsing_group_does_not_persist_member_missing_from_scan(self) -> None:
        """一次扫描暂缺成员时，只临时少显示，不能缩写正式分组。"""
        group = SimpleNamespace(session_keys=["cursor:a", "cursor:b"])

        self.assertTrue(
            _preserve_missing_group_members(
                group, ["cursor:a"], include_inactive=True,
            )
        )

    def test_explicit_composition_paths_may_still_replace_group(self) -> None:
        """只有浏览路径受保护；明确调整组合仍允许保存新成员列表。"""
        group = SimpleNamespace(session_keys=["cursor:a", "cursor:b"])

        self.assertFalse(
            _preserve_missing_group_members(
                group, ["cursor:a"], include_inactive=False,
            )
        )

    def test_group_view_uses_focus_only_when_scan_temporarily_omits_member(self) -> None:
        """浏览暂缺成员的既有分组时不得覆盖保存为单成员组合。"""
        session = {"source": "cursor", "id": "a"}

        class Area:
            def show_hosted_group(self, *_args, **_kwargs) -> None:
                pass

        class Controller(LayoutControllerMixin):
            embed_ok = True

            def __init__(self) -> None:
                self._split_store = split_layout.SplitLayoutStore()
                self._split_store.set_group("/tmp", ["cursor:a", "cursor:b"])
                self.store = SimpleNamespace(
                    find_session=lambda key: session if key == "cursor:a" else None,
                )
                self.focus_updates = 0
                self.composition_updates = 0

            def _build_hosted_entries(self, _keys):
                return [(session, None, None)]

            def _split_area(self):
                return Area()

            def _can_autofocus(self) -> bool:
                return False

            def _persist_split_focus(self) -> None:
                self.focus_updates += 1

            def _persist_split_composition(self) -> None:
                self.composition_updates += 1

            def _begin_attention_read(self, _key: str | None = None) -> None:
                pass

            def _prefetch_group_screens(self, _entries) -> None:
                pass

        controller = Controller()
        with mock.patch("corral.observe.event") as event:
            controller._show_session_group("cursor:a", include_inactive=True)

        self.assertEqual(controller.focus_updates, 1)
        self.assertEqual(controller.composition_updates, 0)
        self.assertEqual(
            controller._split_store.get_group("cursor:b").session_keys,
            ["cursor:a", "cursor:b"],
        )
        event.assert_called_once_with("split_group_member_missing", missing_count=1)


class PreviewIntegrityTests(unittest.TestCase):
    def test_stale_preview_refresh_cannot_invalidate_current_right_pane(self) -> None:
        """后台完成旧会话读取后，当前右栏已换会话则直接丢弃结果。"""
        area = SimpleNamespace(
            ordered_session_keys=lambda: ["cursor:current"],
            any_embed_focused=lambda: False,
            invalidate_visible_previews=mock.Mock(),
        )
        screen = SimpleNamespace(
            _preview_gen=3,
            embed_ok=True,
            _split_area=lambda: area,
        )

        MainScreen._refresh_preview_detail(screen, "cursor:old", 3)
        MainScreen._refresh_preview_detail(screen, "cursor:current", 2)

        area.invalidate_visible_previews.assert_not_called()


if __name__ == "__main__":
    unittest.main()
