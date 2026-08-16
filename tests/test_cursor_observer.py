"""Cursor 会话状态观察器测试；全部路径均隔离，不触碰真实用户配置。"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pickup import cursor_observer


class _FakeAttentionStore:
    def __init__(self):
        self.events = []

    def record_event(self, runtime_id, session_id, evidence):
        self.events.append((runtime_id, session_id, evidence))


class CursorObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        self.cache = Path(self.temp.name) / "cache"
        self.home.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "PICKUP_CACHE_DIR": str(self.cache),
                "XDG_CACHE_HOME": str(Path(self.temp.name) / "xdg"),
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    @property
    def config_path(self):
        return self.home / ".cursor" / "hooks.json"

    def _write(self, payload):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _read(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_install_preserves_existing_hooks_and_is_idempotent(self):
        existing = {
            "version": 1,
            "custom": {"keep": True},
            "hooks": {"beforeSubmitPrompt": [{"command": "existing-tool"}]},
        }
        self._write(existing)
        first = cursor_observer.install(self.home)
        after_first = self.config_path.read_bytes()
        second = cursor_observer.install(self.home)

        self.assertEqual(first["status"], "updated")
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(after_first, self.config_path.read_bytes())
        config = self._read()
        self.assertEqual(config["custom"], {"keep": True})
        self.assertEqual(config["hooks"]["beforeSubmitPrompt"][0], {"command": "existing-tool"})
        for event in cursor_observer.HOOK_EVENTS:
            managed = [entry for entry in config["hooks"][event] if cursor_observer._managed_entry(entry)]
            self.assertEqual(len(managed), 1)

    def test_install_upgrades_only_pickup_entries(self):
        old = "/old/python -m pickup _cursor-hook"
        self._write({
            "version": 1,
            "hooks": {
                event: [{"command": "user-tool"}, {"command": old}]
                for event in cursor_observer.HOOK_EVENTS
            },
        })
        result = cursor_observer.install(self.home)
        self.assertEqual(result["status"], "updated")
        for entries in self._read()["hooks"].values():
            self.assertEqual(entries[0], {"command": "user-tool"})
            self.assertEqual(entries[1], {"command": cursor_observer._hook_command()})

    def test_dry_run_is_strictly_read_only(self):
        self._write({"version": 1, "hooks": {}})
        before = self.config_path.read_bytes()
        result = cursor_observer.install(self.home, dry_run=True)
        self.assertEqual(result["status"], "would_update")
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.cache.exists())

    def test_new_install_dry_run_does_not_create_directories(self):
        result = cursor_observer.install(self.home, dry_run=True)
        self.assertEqual(result["status"], "would_install")
        self.assertFalse((self.home / ".cursor").exists())
        self.assertFalse(self.cache.exists())

    def test_install_backups_original_before_atomic_replace(self):
        original = b'{"version": 1, "hooks": {"stop": [{"command": "mine"}]}}'
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_bytes(original)
        result = cursor_observer.install(self.home)
        backup_path = Path(result["backup_path"])
        self.assertTrue(backup_path.is_file())
        self.assertEqual(backup_path.read_bytes(), original)
        self.assertNotEqual(self.config_path.read_bytes(), original)
        self.assertEqual(list(self.config_path.parent.glob("*.tmp.*")), [])

    def test_corrupt_or_unsupported_config_is_never_overwritten(self):
        cases = [
            b"not-json",
            b'{"version": 2, "hooks": {}}',
            b'{"version": 1, "hooks": {"stop": {}}}',
        ]
        for index, original in enumerate(cases):
            with self.subTest(index=index):
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self.config_path.write_bytes(original)
                with self.assertRaises(cursor_observer.ObserverError):
                    cursor_observer.install(self.home)
                self.assertEqual(self.config_path.read_bytes(), original)
                self.assertFalse(self.cache.exists())

    def test_uninstall_removes_only_pickup_entries_and_is_idempotent(self):
        cursor_observer.install(self.home)
        config = self._read()
        config["hooks"]["stop"].insert(0, {"command": "user-tool"})
        self._write(config)
        first = cursor_observer.uninstall(self.home)
        second = cursor_observer.uninstall(self.home)
        self.assertEqual(first["status"], "uninstalled")
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(self._read()["hooks"]["stop"], [{"command": "user-tool"}])
        for event in cursor_observer.HOOK_EVENTS:
            self.assertFalse(any(cursor_observer._managed_entry(x) for x in self._read()["hooks"][event]))

    def test_uninstall_dry_run_leaves_config_and_backups_untouched(self):
        cursor_observer.install(self.home)
        before = self.config_path.read_bytes()
        backup_count = len(list(self.cache.rglob("*.json")))
        result = cursor_observer.uninstall(self.home, dry_run=True)
        self.assertEqual(result["status"], "would_uninstall")
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(len(list(self.cache.rglob("*.json"))), backup_count)

    def test_status_reports_missing_installed_and_outdated(self):
        self.assertEqual(cursor_observer.status(self.home)["status"], "not_installed")
        cursor_observer.install(self.home)
        self.assertEqual(cursor_observer.status(self.home)["status"], "installed")
        config = self._read()
        config["hooks"]["stop"][0]["command"] = "/old/python -m pickup _cursor-hook"
        self._write(config)
        self.assertEqual(cursor_observer.status(self.home)["status"], "outdated")

    def test_hook_command_quotes_posix_interpreter_and_never_embeds_payload(self):
        with mock.patch.object(sys, "executable", "/dir with space/python"):
            command = cursor_observer._hook_command()
        self.assertEqual(command, "'/dir with space/python' -m pickup _cursor-hook")
        self.assertNotIn("conversation_id", command)

    def test_hook_command_uses_windows_native_quoting(self):
        with mock.patch.object(sys, "executable", r"C:\Program Files\Python\python.exe"), \
             mock.patch.object(cursor_observer.os, "name", "nt"):
            command = cursor_observer._hook_command()
        self.assertEqual(
            command,
            '"C:\\Program Files\\Python\\python.exe" -m pickup _cursor-hook',
        )
        self.assertNotIn("'", command)

    def test_ingest_records_working_and_idle_transitions(self):
        store = _FakeAttentionStore()
        for event, expected in (
            ("beforeSubmitPrompt", "working"),
            ("afterAgentResponse", "idle"),
            ("stop", "idle"),
            ("sessionEnd", "idle"),
        ):
            result = cursor_observer.ingest(
                event,
                {"conversation_id": "chat-1", "generation_id": "generation-1"},
                store,
            )
            self.assertEqual(result["status"], "recorded")
            self.assertEqual(store.events[-1][2].phase, expected)
            self.assertIn(event, store.events[-1][2].activity_token)
            self.assertEqual(store.events[-1][2].source, "observer")

    def test_ingest_uses_payload_event_and_session_id_fallback(self):
        store = _FakeAttentionStore()
        result = cursor_observer.ingest(
            None,
            {"hook_event_name": "sessionEnd", "session_id": "session-2"},
            store,
        )
        self.assertEqual(result["phase"], "idle")
        self.assertEqual(store.events[0][:2], ("cursor", "session-2"))

    def test_ingest_missing_id_and_store_failure_are_fail_open(self):
        self.assertEqual(
            cursor_observer.ingest("stop", {}, _FakeAttentionStore()),
            {"status": "ignored", "reason": "missing_session_id"},
        )
        broken = mock.Mock()
        broken.record_event.side_effect = RuntimeError("boom")
        self.assertEqual(
            cursor_observer.ingest("stop", {"conversation_id": "chat"}, broken),
            {"status": "ignored", "reason": "observer_failure"},
        )


class CursorObserverCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "PICKUP_CACHE_DIR": str(Path(self.temp.name) / "cache")},
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _run(self, argv):
        output = io.StringIO()
        with mock.patch.object(sys, "stdout", output):
            code = cursor_observer.cli_main(argv)
        return code, json.loads(output.getvalue())

    def test_json_success_envelope_and_idempotency(self):
        code, first = self._run(["install", "cursor", "--json"])
        code2, second = self._run(["install", "cursor", "--json"])
        self.assertEqual((code, code2), (0, 0))
        self.assertEqual(set(first), {"ok", "data", "error", "meta"})
        self.assertTrue(first["ok"])
        self.assertEqual(second["data"]["status"], "unchanged")

    def test_non_tty_automatically_uses_json(self):
        code, payload = self._run(["status", "cursor"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])

    def test_usage_and_missing_target_exit_codes_are_structured(self):
        code, payload = self._run(["status", "cursor", "--dry-run", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "usage_error")
        code, payload = self._run(["status", "unknown", "--json"])
        self.assertEqual(code, 3)
        self.assertEqual(payload["error"]["code"], "target_not_found")

    def test_invalid_config_returns_general_failure_envelope(self):
        path = self.home / ".cursor" / "hooks.json"
        path.parent.mkdir(parents=True)
        path.write_text("broken", encoding="utf-8")
        code, payload = self._run(["install", "cursor", "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "invalid_config")

    def test_permission_error_maps_to_four(self):
        error = cursor_observer.ObserverError(
            "permission_denied",
            "没有权限",
            exit_code=cursor_observer.EXIT_PERMISSION,
        )
        with mock.patch.object(cursor_observer, "install", side_effect=error):
            code, payload = self._run(["install", "cursor", "--json"])
        self.assertEqual(code, 4)
        self.assertEqual(payload["error"]["code"], "permission_denied")


if __name__ == "__main__":
    unittest.main()
