"""Cursor 观察器在 pickup 顶层命令入口的分发测试。"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pickup import cli


class CursorObserverCliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "PICKUP_CACHE_DIR": str(Path(self.temp.name) / "cache"),
                "XDG_CACHE_HOME": str(Path(self.temp.name) / "xdg"),
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_public_command_is_forwarded_without_tui_or_other_dispatch(self):
        with mock.patch.object(cli.sys, "argv", ["pickup", "observer", "install", "cursor"]), \
             mock.patch.object(cli.cursor_observer, "cli_main", return_value=4) as observer_main, \
             mock.patch.object(cli.observe, "install_crash_hooks") as crash_hooks, \
             mock.patch.object(cli.agent_api, "dispatch") as agent_dispatch, \
             mock.patch.object(cli, "default_registry") as registry, \
             self.assertRaises(SystemExit) as raised:
            cli.main()

        self.assertEqual(raised.exception.code, 4)
        observer_main.assert_called_once_with(["install", "cursor"])
        crash_hooks.assert_not_called()
        agent_dispatch.assert_not_called()
        registry.assert_not_called()

    def test_public_non_tty_json_and_exit_code_reach_top_level(self):
        output = io.StringIO()
        with mock.patch.object(
            cli.sys,
            "argv",
            ["pickup", "observer", "install", "cursor", "--dry-run", "--json"],
        ), mock.patch.object(cli.sys, "stdout", output), self.assertRaises(SystemExit) as raised:
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["status"], "would_install")
        self.assertFalse((self.home / ".cursor").exists())

    def test_public_missing_target_exit_code_is_preserved(self):
        output = io.StringIO()
        with mock.patch.object(
            cli.sys,
            "argv",
            ["pickup", "observer", "status", "unknown", "--json"],
        ), mock.patch.object(cli.sys, "stdout", output), self.assertRaises(SystemExit) as raised:
            cli.main()

        self.assertEqual(raised.exception.code, 3)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "target_not_found")

    def test_hidden_hook_forwards_valid_object_and_returns_empty_json(self):
        payload = {
            "hook_event_name": "beforeSubmitPrompt",
            "conversation_id": "private-session",
            "prompt": "不应出现在输出或日志里的正文",
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.sys, "argv", ["pickup", "_cursor-hook"]), \
             mock.patch.object(cli.sys, "stdin", io.StringIO(json.dumps(payload))), \
             mock.patch.object(cli.sys, "stdout", stdout), \
             mock.patch.object(cli.sys, "stderr", stderr), \
             mock.patch.object(cli.cursor_observer, "ingest") as ingest, \
             mock.patch.object(cli.observe, "install_crash_hooks") as crash_hooks, \
             mock.patch.object(cli.agent_api, "dispatch") as agent_dispatch, \
             mock.patch.object(cli, "default_registry") as registry, \
             self.assertRaises(SystemExit) as raised:
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        ingest.assert_called_once_with("beforeSubmitPrompt", payload)
        self.assertEqual(stdout.getvalue(), "{}\n")
        self.assertEqual(stderr.getvalue(), "")
        crash_hooks.assert_not_called()
        agent_dispatch.assert_not_called()
        registry.assert_not_called()

    def test_hidden_hook_malformed_json_returns_empty_json_and_zero(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.sys, "argv", ["pickup", "_cursor-hook"]), \
             mock.patch.object(cli.sys, "stdin", io.StringIO("{broken")), \
             mock.patch.object(cli.sys, "stdout", stdout), \
             mock.patch.object(cli.sys, "stderr", stderr), \
             mock.patch.object(cli.cursor_observer, "ingest") as ingest, \
             mock.patch.object(cli, "default_registry") as registry, \
             self.assertRaises(SystemExit) as raised:
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        ingest.assert_not_called()
        registry.assert_not_called()
        self.assertEqual(stdout.getvalue(), "{}\n")
        self.assertEqual(stderr.getvalue(), "")

    def test_hidden_hook_write_failure_returns_empty_json_and_zero(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.sys, "argv", ["pickup", "_cursor-hook"]), \
             mock.patch.object(
                 cli.sys,
                 "stdin",
                 io.StringIO('{"hook_event_name":"stop","conversation_id":"secret"}'),
             ), \
             mock.patch.object(cli.sys, "stdout", stdout), \
             mock.patch.object(cli.sys, "stderr", stderr), \
             mock.patch.object(cli.cursor_observer, "ingest", side_effect=RuntimeError("私密错误")), \
             self.assertRaises(SystemExit) as raised:
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue(), "{}\n")
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
