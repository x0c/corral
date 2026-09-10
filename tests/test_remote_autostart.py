"""Remote switch autostart (LaunchAgent / systemd) unit tests."""

from __future__ import annotations

import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral.remote import autostart


class RemoteAutostartTests(unittest.TestCase):
    def test_autostart_skipped_under_cache_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": tmp}, clear=False):
                self.assertFalse(autostart.autostart_allowed())
                self.assertEqual(autostart.enable(), "")
                self.assertFalse(autostart.is_installed())

    def test_darwin_plist_contains_serve_argv_and_keepalive(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("macOS only")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            plist_path = home / "Library" / "LaunchAgents" / "com.x0c.corral.remote.plist"
            with (
                mock.patch.object(autostart, "autostart_allowed", return_value=True),
                mock.patch.object(autostart, "_home", return_value=home),
                mock.patch.object(autostart, "_darwin_run", return_value=mock.Mock(returncode=0, stdout="", stderr="")),
            ):
                self.assertEqual(autostart.enable(), "")
            self.assertTrue(plist_path.is_file())
            with plist_path.open("rb") as fh:
                payload = plistlib.load(fh)
            self.assertEqual(payload["Label"], "com.x0c.corral.remote")
            self.assertTrue(payload["RunAtLoad"])
            self.assertTrue(payload["KeepAlive"])
            self.assertEqual(payload["ProgramArguments"][:3], [sys.executable, "-m", "corral"])
            self.assertEqual(payload["ProgramArguments"][-2:], ["remote", "_serve"])

    def test_darwin_disable_removes_plist(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("macOS only")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            plist_path = home / "Library" / "LaunchAgents" / "com.x0c.corral.remote.plist"
            plist_path.parent.mkdir(parents=True)
            plist_path.write_bytes(b"placeholder")
            with (
                mock.patch.object(autostart, "autostart_allowed", return_value=True),
                mock.patch.object(autostart, "_home", return_value=home),
                mock.patch.object(autostart, "_darwin_run", return_value=mock.Mock(returncode=0, stdout="", stderr="")),
            ):
                self.assertEqual(autostart.disable(), "")
            self.assertFalse(plist_path.exists())


if __name__ == "__main__":
    unittest.main()
