"""ui_prefs：侧边栏显隐偏好读写（存在共享的侧边栏记忆库里）。"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from pickup import split_layout, ui_prefs


class UiPrefsTests(unittest.TestCase):
    def setUp(self) -> None:
        # `PICKUP_CACHE_DIR` 是唯一的隔离开关，少了它会读到、甚至迁走机主真实的偏好。
        self.temp = tempfile.TemporaryDirectory(prefix="pickup-ui-prefs-ut-")
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(
            os.environ, {"PICKUP_CACHE_DIR": self.temp.name}, clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)

    def test_default_when_missing(self) -> None:
        self.assertTrue(ui_prefs.load_sidebar_visible(default=True))
        self.assertFalse(ui_prefs.load_sidebar_visible(default=False))

    def test_roundtrip(self) -> None:
        ui_prefs.save_sidebar_visible(False)
        self.assertFalse(ui_prefs.load_sidebar_visible(default=True))
        ui_prefs.save_sidebar_visible(True)
        self.assertTrue(ui_prefs.load_sidebar_visible(default=False))

    def test_shared_with_other_windows(self) -> None:
        """另一个窗口（另一个库句柄）读到的是同一份偏好。"""
        ui_prefs.save_sidebar_visible(False)
        self.assertFalse(split_layout.SidebarLayoutDB().sidebar_visible(default=True))

    def test_unreadable_store_falls_back_to_default(self) -> None:
        with mock.patch.object(
            split_layout.SidebarLayoutDB, "_open", return_value=None,
        ):
            self.assertTrue(ui_prefs.load_sidebar_visible(default=True))
            self.assertFalse(ui_prefs.load_sidebar_visible(default=False))


if __name__ == "__main__":
    unittest.main()
