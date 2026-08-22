"""remote status：中继真实在线状态要进快照与人读输出。"""

from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from corral.i18n import t
from corral.remote import cli as remote_cli
from corral.remote import config as remote_config


class RemoteConfigEmptyDirTests(unittest.TestCase):
    def test_load_state_on_empty_dir_does_not_deadlock(self) -> None:
        # load_state 持锁时会再写 identity.key / host.key；普通 Lock 会在空目录死锁。
        previous = os.environ.get("CORRAL_CACHE_DIR")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CORRAL_CACHE_DIR"] = tmp
            try:
                state = remote_config.load_state()
            finally:
                if previous is None:
                    os.environ.pop("CORRAL_CACHE_DIR", None)
                else:
                    os.environ["CORRAL_CACHE_DIR"] = previous
        self.assertTrue(state.host_id)
        self.assertEqual(len(state.host_id), 26)


class RemoteStatusRelayOnlineTests(unittest.TestCase):
    def test_status_text_shows_relay_online_from_snapshot(self) -> None:
        state = remote_config.RemoteState(
            host_id="h1",
            host_name="suzhou",
            relay_url="wss://corral-relay.caozc.top",
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
        since = t(
            "remote.status.relay_since",
            time=time.strftime("%H:%M:%S", time.localtime(1_700_000_100.0)),
        )
        self.assertIn(
            t(
                "remote.status.relay_online",
                label="wss://corral-relay.caozc.top",
                since=since,
            ),
            text,
        )
        self.assertIn("corral-relay.caozc.top", text)

    def test_status_json_includes_relay_online_fields(self) -> None:
        state = remote_config.RemoteState(
            host_id="h1",
            host_name="suzhou",
            relay_url="wss://corral-relay.caozc.top",
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
