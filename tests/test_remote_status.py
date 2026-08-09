"""remote status：中继真实在线状态要进快照与人读输出。"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from pickup.remote import cli as remote_cli
from pickup.remote import config as remote_config


class RemoteStatusRelayOnlineTests(unittest.TestCase):
    def test_status_text_shows_relay_online_from_snapshot(self) -> None:
        state = remote_config.RemoteState(
            host_id="h1",
            host_name="suzhou",
            host_token="tok",
            relay_url="wss://pickup-relay.caozc.top",
            relay_enabled=True,
            local_enabled=True,
            local_port=8737,
            devices=[],
        )
        snapshot = {
            "updated_at": 1_700_000_000.0,
            "online": [],
            "recent": [],
            "relay_online": True,
            "relay_connected_at": 1_700_000_100.0,
            "relay_error": "",
        }
        args = mock.Mock(json=False)
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "read_pid", return_value=12345),
            mock.patch.object(remote_config, "read_pairing", return_value=None),
            mock.patch.object(remote_config, "read_status_snapshot", return_value=snapshot),
            mock.patch.object(remote_cli, "_check_dependencies", return_value=None),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_status(args)
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertIn("中继：在线", text)
        self.assertIn("pickup-relay.caozc.top", text)

    def test_status_json_includes_relay_online_fields(self) -> None:
        state = remote_config.RemoteState(
            host_id="h1",
            host_name="suzhou",
            host_token="tok",
            relay_url="wss://pickup-relay.caozc.top",
            relay_enabled=True,
            local_enabled=True,
            local_port=8737,
            devices=[],
        )
        snapshot = {
            "relay_online": False,
            "relay_connected_at": None,
            "relay_error": "连接被重置",
            "online": [],
            "recent": [],
        }
        args = mock.Mock(json=True)
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "read_pid", return_value=99),
            mock.patch.object(remote_config, "read_pairing", return_value=None),
            mock.patch.object(remote_config, "read_status_snapshot", return_value=snapshot),
            mock.patch.object(remote_cli, "_check_dependencies", return_value=None),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_status(args)
        self.assertEqual(code, 0)
        self.assertIn('"relay_online": false', buf.getvalue())
        self.assertIn("连接被重置", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
