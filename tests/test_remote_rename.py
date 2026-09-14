"""corral remote rename：只改开发机显示名，不碰连接、配对与会话逻辑。"""

from __future__ import annotations

import argparse
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

from corral.remote import cli as remote_cli
from corral.remote import config as remote_config


def _args(name="", clear=False, use_json=False):
    return argparse.Namespace(name=name, clear=clear, json=use_json)


class RemoteRenameTests(unittest.TestCase):
    def test_rename_saves_sanitized_name(self) -> None:
        state = remote_config.RemoteState(host_id="h1", host_name="dev")
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state") as save,
        ):
            buf = StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_rename(_args(name="  MacBook-Max  "))
        self.assertEqual(code, 0)
        self.assertEqual(state.host_name, "MacBook-Max")
        save.assert_called_once_with(state)
        self.assertIn("MacBook-Max", buf.getvalue())

    def test_rename_rejects_blank_name(self) -> None:
        state = remote_config.RemoteState(host_id="h1", host_name="dev")
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state") as save,
        ):
            code = remote_cli._cmd_rename(_args(name="   "))
        self.assertEqual(code, remote_cli.EXIT_USAGE)
        self.assertEqual(state.host_name, "dev")
        save.assert_not_called()

    def test_rename_strips_control_characters(self) -> None:
        state = remote_config.RemoteState(host_id="h1", host_name="dev")
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state"),
        ):
            buf = StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_rename(_args(name="mac\x1b[2J"))
        self.assertEqual(code, 0)
        self.assertEqual(state.host_name, "mac[2J")

    def test_rename_clear_restores_default(self) -> None:
        state = remote_config.RemoteState(host_id="h1", host_name="旧名字")
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state") as save,
            mock.patch.object(remote_config, "default_host_name", return_value="macbook"),
        ):
            buf = StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_rename(_args(clear=True))
        self.assertEqual(code, 0)
        self.assertEqual(state.host_name, "macbook")
        save.assert_called_once_with(state)

    def test_rename_json_envelope(self) -> None:
        state = remote_config.RemoteState(host_id="h1", host_name="dev")
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state"),
        ):
            buf = StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_rename(_args(name="mac", use_json=True))
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["host_name"], "mac")
        self.assertFalse(payload["data"]["cleared"])

    def test_rename_parser_wires_clear_flag(self) -> None:
        parser = remote_cli.build_parser()
        parsed = parser.parse_args(["rename", "--clear"])
        self.assertTrue(parsed.clear)
        self.assertEqual(parsed.func, remote_cli._cmd_rename)
        parsed = parser.parse_args(["rename", "新名字"])
        self.assertEqual(parsed.name, "新名字")
        self.assertFalse(parsed.clear)


if __name__ == "__main__":
    unittest.main()
