"""改名兼容层：CORRAL_ 优先，PICKUP_ / SC_ 兜底；缓存与 tmux 过渡名只在本模块出现。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral.legacy_names import (
    ALL_SOCKET_NAMES,
    cache_dir,
    getenv,
    hosted_env_pairs,
    hosted_isolation_dirname,
    hosted_session_id,
    is_hosted_isolation_dir,
    is_managed_session,
    socket_for_session,
    state_dir,
)


class EnvFallbackTests(unittest.TestCase):
    def test_corral_wins_over_pickup_and_sc(self) -> None:
        env = {
            "CORRAL_CACHE_DIR": "/new",
            "PICKUP_CACHE_DIR": "/old-pickup",
            "SC_CACHE_DIR": "/older-sc",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(getenv("CACHE_DIR"), "/new")

    def test_pickup_used_when_corral_unset(self) -> None:
        with mock.patch.dict("os.environ", {"PICKUP_CACHE_DIR": "/old-pickup"}, clear=True):
            self.assertEqual(getenv("CACHE_DIR"), "/old-pickup")

    def test_sc_used_when_newer_names_unset(self) -> None:
        with mock.patch.dict("os.environ", {"SC_CACHE_DIR": "/older-sc"}, clear=True):
            self.assertEqual(getenv("CACHE_DIR"), "/older-sc")

    def test_empty_corral_does_not_fall_through(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"CORRAL_PROJECT_ROOTS": "", "PICKUP_PROJECT_ROOTS": "/should-not-use"},
            clear=True,
        ):
            self.assertEqual(getenv("PROJECT_ROOTS"), "")

    def test_hosted_session_id_reads_pickup(self) -> None:
        self.assertEqual(hosted_session_id({"PICKUP_SESSION_ID": "abc"}), "abc")
        self.assertEqual(
            hosted_session_id({"CORRAL_SESSION_ID": "new", "PICKUP_SESSION_ID": "old"}),
            "new",
        )

    def test_hosted_env_pairs_inject_all_three_prefixes(self) -> None:
        pairs = hosted_env_pairs("claude", "abcd")
        joined = " ".join(pairs)
        for key in (
            "CORRAL_RUNTIME=claude",
            "CORRAL_SESSION_ID=abcd",
            "PICKUP_RUNTIME=claude",
            "PICKUP_SESSION_ID=abcd",
            "SC_RUNTIME=claude",
            "SC_SESSION_ID=abcd",
        ):
            self.assertIn(key, joined)


class CacheMigrateTests(unittest.TestCase):
    def test_renames_pickup_cache_when_new_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "pickup"
            old.mkdir()
            (old / "marker").write_text("keep", encoding="utf-8")
            with mock.patch.dict("os.environ", {"XDG_CACHE_HOME": str(root)}, clear=True):
                dest = cache_dir()
            self.assertEqual(dest, root / "corral")
            self.assertTrue((dest / "marker").is_file())
            self.assertFalse(old.exists())

    def test_explicit_override_skips_migrate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pickup").mkdir()
            override = root / "override"
            with mock.patch.dict(
                "os.environ",
                {"CORRAL_CACHE_DIR": str(override), "XDG_CACHE_HOME": str(root)},
                clear=True,
            ):
                self.assertEqual(cache_dir(), override)
            self.assertTrue((root / "pickup").is_dir())

    def test_renames_pickup_state_when_new_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "pickup"
            old.mkdir()
            (old / "remote").mkdir()
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": str(root)}, clear=True):
                dest = state_dir()
            self.assertEqual(dest, root / "corral")
            self.assertTrue((dest / "remote").is_dir())
            self.assertFalse(old.exists())


class TmuxCompatTests(unittest.TestCase):
    def test_new_sessions_use_new_socket(self) -> None:
        self.assertEqual(socket_for_session("corral-claude-abcd"), "corral-keepalive")
        self.assertIn("corral-keepalive", ALL_SOCKET_NAMES)
        self.assertIn("pickup-keepalive", ALL_SOCKET_NAMES)

    def test_legacy_sessions_use_pickup_socket(self) -> None:
        self.assertEqual(socket_for_session("pickup-claude-abcd"), "pickup-keepalive")
        self.assertEqual(socket_for_session("sc-claude-abcd"), "pickup-keepalive")

    def test_managed_prefixes(self) -> None:
        self.assertTrue(is_managed_session("corral-claude-x"))
        self.assertTrue(is_managed_session("pickup-claude-x"))
        self.assertTrue(is_managed_session("sc-claude-x"))
        self.assertFalse(is_managed_session("ctl-it"))
        self.assertFalse(is_managed_session(""))


class IsolationDirTests(unittest.TestCase):
    def test_new_dir_uses_corral_prefix(self) -> None:
        self.assertEqual(hosted_isolation_dirname("abcd"), "corral-abcd")

    def test_legacy_pickup_dir_still_recognized(self) -> None:
        self.assertTrue(is_hosted_isolation_dir("/tmp/--proj--/corral-abcd"))
        self.assertTrue(is_hosted_isolation_dir("/tmp/--proj--/pickup-abcd"))
        self.assertFalse(is_hosted_isolation_dir("/tmp/--proj--"))


if __name__ == "__main__":
    unittest.main()
