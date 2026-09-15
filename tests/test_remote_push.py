"""Session-end and waiting pushes from PushNotifier + SessionHub status hooks."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sesskit import titles as sesskit_titles

from corral.remote import crypto
from corral.remote.config import PairedDevice, RemoteState
from corral.remote.push import PushNotifier
from corral.remote.sessions import SessionHub

_HAS_CRYPTO = crypto.available()
_SKIP = "未安装 remote 附加组件（pip install '.[remote]'）"


def _session(
    *,
    sid: str = "s1",
    status_tag: str = sesskit_titles.STATUS_PENDING,
    last_agent: str = "PONG",
    attention: str = "none",
) -> dict:
    return {
        "source": "pi",
        "id": sid,
        "short_id": sid,
        "cwd": "/tmp/proj",
        "cwd_display": "/tmp/proj",
        "mtime": 1_700_000_000.0,
        "display_time": "01-01 12:00",
        "size_kb": 1.0,
        "status_tag": status_tag,
        "live": True,
        "keepalive_name": None,
        "fallback_title": "probe",
        "attention_kind": attention,
        "last_user_msg": "ping",
        "last_agent_msg": last_agent,
        "path": "/tmp/hist.jsonl",
    }


@unittest.skipUnless(_HAS_CRYPTO, _SKIP)
class PushNotifierSessionEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.device_private = crypto.generate_private_key_bytes()
        self.host_private = crypto.generate_private_key_bytes()
        self.sent: list[tuple[str, str, bytes]] = []
        state = RemoteState(
            host_id="host",
            host_name="Mac",
            devices=[
                PairedDevice(
                    id="d1",
                    name="iPhone",
                    public_key=crypto.public_key_bytes(self.device_private).hex(),
                    paired_at=1.0,
                    push_token="a" * 64,
                    push_env="sandbox",
                )
            ],
        )
        self.notifier = PushNotifier(state, self.host_private, sender=self._capture)

    def _capture(self, token: str, env: str, payload: bytes) -> None:
        self.sent.append((token, env, payload))

    def _open(self, sealed_b64: bytes) -> dict:
        sealed = __import__("base64").b64decode(sealed_b64)
        plain = crypto.open_from_host(
            self.device_private,
            crypto.public_key_bytes(self.host_private),
            sealed,
        )
        return json.loads(plain.decode("utf-8"))

    def test_pending_to_done_sends_completed(self) -> None:
        session = {
            "key": "pi:s1",
            "title": "probe",
            "runtime": "pi",
            "cwd_display": "/tmp",
            "last_agent": "all good",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 1)
        payload = self._open(self.sent[0][2])
        self.assertEqual(payload["kind"], "completed")
        self.assertEqual(payload["body"], "all good")
        self.assertEqual(payload["key"], "pi:s1")

    def test_pending_to_aborted_keeps_error_body(self) -> None:
        session = {
            "key": "pi:s1",
            "title": "probe",
            "runtime": "pi",
            "last_agent": "429 weekly limit",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_ABORTED
        )
        payload = self._open(self.sent[0][2])
        self.assertEqual(payload["kind"], "aborted")
        self.assertIn("429", payload["body"])

    def test_done_to_done_is_silent(self) -> None:
        session = {"key": "pi:s1", "title": "x", "last_agent": "same"}
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_DONE, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(self.sent, [])

    def test_waiting_still_fires(self) -> None:
        session = {
            "key": "pi:s1",
            "title": "probe",
            "runtime": "pi",
            "last_agent": "pick one",
        }
        self.notifier.on_attention_change(session, "working", "waiting")
        payload = self._open(self.sent[0][2])
        self.assertEqual(payload["kind"], "waiting")
        self.assertEqual(payload["body"], "pick one")

    def test_throttle_same_kind(self) -> None:
        session = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "done",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 1)

    def test_skips_device_without_token(self) -> None:
        self.notifier.state.devices[0].push_token = ""
        self.notifier.on_status_change(
            {"key": "pi:s1", "title": "x", "last_agent": "y"},
            sesskit_titles.STATUS_PENDING,
            sesskit_titles.STATUS_DONE,
        )
        self.assertEqual(self.sent, [])


class SessionHubStatusHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(
            os.environ, {"CORRAL_CACHE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.hub = SessionHub()
        self.calls: list[tuple[str, str]] = []
        self.hub.set_status_hook(self._hook)

    def _hook(self, session: dict, previous: str, current: str) -> None:
        self.calls.append((previous, current))

    def test_first_snapshot_does_not_push(self) -> None:
        path = Path(self._tmp.name) / "pi.jsonl"
        path.write_text("", encoding="utf-8")
        session = _session(status_tag=sesskit_titles.STATUS_DONE)
        session["path"] = str(path)
        self.hub.store.sessions = {"pi": [session]}
        self.hub._snapshot_status()
        self.hub._detect_status_changes()
        self.assertEqual(self.calls, [])

    def test_fresh_new_terminal_session_notifies(self) -> None:
        """Session that first appears already DONE still notifies when mtime is fresh."""
        path = Path(self._tmp.name) / "pi.jsonl"
        path.write_text("", encoding="utf-8")
        session = _session(status_tag=sesskit_titles.STATUS_DONE, last_agent="PONG")
        session["path"] = str(path)
        session["mtime"] = time.time()
        self.hub.store.sessions = {"pi": [session]}
        # No snapshot — key is brand new to the hub
        self.hub._detect_status_changes()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], sesskit_titles.STATUS_DONE)

    def test_stale_new_terminal_session_is_silent(self) -> None:
        path = Path(self._tmp.name) / "pi.jsonl"
        path.write_text("", encoding="utf-8")
        session = _session(status_tag=sesskit_titles.STATUS_ABORTED, last_agent="old")
        session["path"] = str(path)
        session["mtime"] = time.time() - 10_000
        self.hub.store.sessions = {"pi": [session]}
        self.hub._detect_status_changes()
        self.assertEqual(self.calls, [])

    def test_pending_to_done_invokes_hook(self) -> None:
        path = Path(self._tmp.name) / "pi.jsonl"
        path.write_text("", encoding="utf-8")
        session = _session(status_tag=sesskit_titles.STATUS_PENDING)
        session["path"] = str(path)
        self.hub.store.sessions = {"pi": [session]}
        self.hub._snapshot_status()
        session["status_tag"] = sesskit_titles.STATUS_DONE
        session["last_agent_msg"] = "finished"
        self.hub._detect_status_changes()
        self.assertEqual(
            self.calls,
            [(sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE)],
        )

    def test_aborted_invokes_hook(self) -> None:
        path = Path(self._tmp.name) / "pi.jsonl"
        path.write_text("", encoding="utf-8")
        session = _session(status_tag=sesskit_titles.STATUS_PENDING)
        session["path"] = str(path)
        self.hub.store.sessions = {"pi": [session]}
        self.hub._snapshot_status()
        session["status_tag"] = sesskit_titles.STATUS_ABORTED
        session["last_agent_msg"] = "quota"
        self.hub._detect_status_changes()
        self.assertEqual(self.calls[-1][1], sesskit_titles.STATUS_ABORTED)


if __name__ == "__main__":
    unittest.main()
