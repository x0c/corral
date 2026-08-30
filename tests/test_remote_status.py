"""remote status：中继真实在线状态要进快照与人读输出。"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from corral.i18n import t
from corral.remote import account as remote_account
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
    def test_login_on_single_tenant_relay_does_not_request_device_code(self) -> None:
        with mock.patch.object(remote_account, "_request") as request:
            ok, message = remote_account.login("wss://pickup-relay.caozc.top")

        self.assertTrue(ok)
        self.assertEqual(message, t("remote.login.not_needed"))
        request.assert_not_called()

    def test_start_automatically_installs_missing_components(self) -> None:
        installed = []

        def missing() -> list[str]:
            return [] if installed else ["cryptography", "websockets", "segno"]

        def install(*_args, **_kwargs):
            installed.append(True)
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(remote_cli, "_missing_dependencies", side_effect=missing),
            mock.patch.object(remote_cli.updater, "detect_channel", return_value="pip"),
            mock.patch.object(remote_cli.subprocess, "run", side_effect=install) as run,
        ):
            self.assertEqual(remote_cli._ensure_dependencies(), "")

        command = run.call_args.args[0]
        self.assertEqual(
            command[:6],
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input"],
        )
        self.assertEqual(command[6:], ["cryptography", "websockets", "segno"])

    def test_start_uses_pipx_injection_for_pipx_install(self) -> None:
        installed = []

        def missing() -> list[str]:
            return [] if installed else ["cryptography", "websockets", "segno"]

        def install(*_args, **_kwargs):
            installed.append(True)
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(remote_cli, "_missing_dependencies", side_effect=missing),
            mock.patch.object(remote_cli.updater, "detect_channel", return_value="pipx"),
            mock.patch.object(remote_cli.subprocess, "run", side_effect=install) as run,
        ):
            self.assertEqual(remote_cli._ensure_dependencies(), "")

        self.assertEqual(
            run.call_args.args[0],
            ["pipx", "inject", "corral", "cryptography", "websockets", "segno"],
        )

    def test_start_reports_when_automatic_install_does_not_fix_environment(self) -> None:
        with (
            mock.patch.object(remote_cli, "_missing_dependencies", return_value=["cryptography"]),
            mock.patch.object(remote_cli.updater, "detect_channel", return_value="pip"),
            mock.patch.object(remote_cli.subprocess, "run", return_value=mock.Mock(returncode=1)),
        ):
            self.assertEqual(remote_cli._ensure_dependencies(), t("remote.deps.auto_install_failed"))

    def test_start_prints_a_fresh_pairing_code_even_when_a_phone_is_paired(self) -> None:
        state = remote_config.RemoteState(
            host_id="h1",
            host_name="suzhou",
            relay_url="",
            relay_enabled=False,
            local_enabled=False,
            local_port=8737,
            devices=[{"id": "existing-phone"}],
        )
        service = mock.Mock()
        service.begin_pairing.return_value = "新配对码"
        daemon = mock.Mock(static_private=object(), service=service)
        args = mock.Mock(
            relay_url=None,
            insecure_relay=False,
            no_relay=False,
            no_local=False,
            port=None,
            force=False,
            quiet=False,
            json=False,
        )
        with (
            mock.patch.object(remote_cli, "_check_dependencies", return_value=""),
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state"),
            mock.patch.object(remote_config, "read_pid", return_value=None),
            mock.patch("corral.remote.daemon.RemoteDaemon", return_value=daemon),
            mock.patch.object(remote_cli.crypto, "public_key_bytes", return_value=b"public-key"),
            mock.patch.object(remote_cli, "_print_pairing") as print_pairing,
            mock.patch.object(remote_cli.asyncio, "run"),
        ):
            self.assertEqual(remote_cli._cmd_start(args), 0)

        service.begin_pairing.assert_called_once_with(remote_cli._PAIRING_TTL)
        print_pairing.assert_called_once_with(state, "新配对码", b"public-key", 8737)

    def test_start_refreshes_pairing_code_when_service_is_already_running(self) -> None:
        state = remote_config.RemoteState(
            host_id="h1", host_name="suzhou", relay_url="", relay_enabled=False,
            local_enabled=False, local_port=8737, devices=[]
        )
        args = mock.Mock(
            relay_url=None, insecure_relay=False, no_relay=False, no_local=False,
            port=None, force=False, quiet=False, json=False,
        )
        with (
            mock.patch.object(remote_cli, "_ensure_dependencies", return_value=""),
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state"),
            mock.patch.object(remote_config, "read_pid", return_value=12345),
            mock.patch.object(remote_config, "load_or_create_identity", return_value=object()),
            mock.patch.object(remote_config, "write_pairing") as write_pairing,
            mock.patch.object(remote_cli.crypto, "public_key_bytes", return_value=b"public-key"),
            mock.patch.object(remote_cli.crypto, "new_pairing_code", return_value="新配对码"),
            mock.patch.object(remote_cli, "_print_pairing") as print_pairing,
        ):
            self.assertEqual(remote_cli._cmd_start(args), 0)

        write_pairing.assert_called_once_with("新配对码", remote_cli._PAIRING_TTL)
        print_pairing.assert_called_once_with(state, "新配对码", b"public-key", 8737)

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
