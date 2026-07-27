"""ui_prefs：侧边栏显隐偏好读写。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from pickup import ui_prefs


class UiPrefsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="pickup-ui-prefs-ut-")
        self._old = ui_prefs.PREFS_FILE
        ui_prefs.PREFS_FILE = os.path.join(self._tmpdir, "ui-prefs.json")

    def tearDown(self) -> None:
        ui_prefs.PREFS_FILE = self._old

    def test_default_when_missing(self) -> None:
        self.assertTrue(ui_prefs.load_sidebar_visible(default=True))
        self.assertFalse(ui_prefs.load_sidebar_visible(default=False))

    def test_roundtrip(self) -> None:
        ui_prefs.save_sidebar_visible(False)
        self.assertFalse(ui_prefs.load_sidebar_visible(default=True))
        ui_prefs.save_sidebar_visible(True)
        self.assertTrue(ui_prefs.load_sidebar_visible(default=False))
        with open(ui_prefs.PREFS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["sidebar_visible"], True)
        self.assertEqual(data["version"], ui_prefs.PREFS_VERSION)

    def test_corrupt_file_falls_back(self) -> None:
        with open(ui_prefs.PREFS_FILE, "w", encoding="utf-8") as fh:
            fh.write("{not-json")
        self.assertTrue(ui_prefs.load_sidebar_visible(default=True))


if __name__ == "__main__":
    unittest.main()
