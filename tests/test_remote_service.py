"""pickup.remote.service：方法路由、配对准入与订阅记账。

这里用一个假的会话中枢，把「谁能调什么」和「断线后后台还在不在白抓帧」这两件
真正容易出错的事单独拎出来验证，不去碰真实的 tmux 与助手历史文件。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from pickup.remote import config as remote_config
from pickup.remote import crypto, protocol
from pickup.remote.service import Connection, RemoteService
from pickup.remote.sessions import ActionError


class FakeHub:
    """只记录被调了什么，不做任何真事。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.watching_sessions = 0
        self.watching_screens: dict[str, int] = {}
        self._on_event = None

    def _record(self, name, *args):
        self.calls.append((name, args))

    def runtimes(self):
        return [{"id": "codex", "name": "Codex", "available": True}]

    def list_sessions(self, query: str = "", limit: int = 0):
        self._record("list_sessions", query, limit)
        return [{"key": "codex:abc", "title": "示例会话"}]

    def watch_sessions(self):
        self.watching_sessions += 1

    def unwatch_sessions(self):
        self.watching_sessions -= 1

    def watch_screen(self, key: str):
        self.watching_screens[key] = self.watching_screens.get(key, 0) + 1
        return {"cols": 80, "rows": 24, "full": True, "status": "first", "lines": []}

    def resync_screen(self, key: str):
        self._record("resync_screen", key)
        return {"cols": 80, "rows": 24, "full": True, "status": "resync", "lines": []}

    def unwatch_screen(self, key: str):
        self.watching_screens[key] = self.watching_screens.get(key, 0) - 1

    def watch_conversation(self, key: str):
        self.watching_conversations = getattr(self, "watching_conversations", {})
        self.watching_conversations[key] = self.watching_conversations.get(key, 0) + 1
        return [{"id": "m1", "role": "user", "text": "你好"}]

    def conversation_snapshot(self, key: str):
        self._record("conversation_snapshot", key)
        return [{"id": "m1", "role": "user", "text": "你好"}, {"id": "m2", "role": "assistant", "text": "在"}]

    def unwatch_conversation(self, key: str):
        self.watching_conversations = getattr(self, "watching_conversations", {})
        self.watching_conversations[key] = self.watching_conversations.get(key, 0) - 1

    def send_text(self, key: str, text: str, submit: bool):
        self._record("send_text", key, text, submit)

    def send_keys(self, key: str, keys):
        self._record("send_keys", key, tuple(keys))

    def send_image(self, key: str, image_bytes: bytes) -> str:
        self._record("send_image", key, len(image_bytes))
        return "/tmp/paste.jpg"

    def projects(self):
        return [
            {
                "cwd": "/tmp/demo",
                "path": "/tmp/demo",
                "label": "demo",
                "name": "demo",
                "count": 1,
                "mtime": 1.0,
            }
        ]

    def delete_session(self, key: str):
        raise ActionError(protocol.E_NOT_FOUND, "会话不在了")


class RemoteServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_cache = os.environ.get("PICKUP_CACHE_DIR")
        os.environ["PICKUP_CACHE_DIR"] = self._tmp.name
        self.addCleanup(self._restore_cache)

        self.hub = FakeHub()
        self.service = RemoteService(self.hub)  # type: ignore[arg-type]
        self.sent: list[dict] = []

    def _restore_cache(self) -> None:
        if self._old_cache is None:
            os.environ.pop("PICKUP_CACHE_DIR", None)
        else:
            os.environ["PICKUP_CACHE_DIR"] = self._old_cache

    def _connect(self, public_key: str = "aa" * 32) -> Connection:
        connection = Connection(public_key, self.sent.append)
        self.service.attach(connection)
        return connection

    def _call(self, connection: Connection, method: str, params: dict | None = None) -> dict:
        self.sent.clear()
        self.service.handle(connection, protocol.request(1, method, params or {}))
        self.assertEqual(len(self.sent), 1, f"{method} 应当恰好回一条消息")
        return self.sent[0]

    # -- 准入 ---------------------------------------------------------------

    def test_unpaired_device_cannot_read_sessions(self):
        connection = self._connect()
        reply = self._call(connection, protocol.M_SESSIONS_LIST)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["e"]["code"], protocol.E_UNAUTHORIZED)
        self.assertEqual(self.hub.calls, [], "没配对就不该碰到会话数据")

    def test_unpaired_device_cannot_send_input(self):
        connection = self._connect()
        reply = self._call(connection, protocol.M_INPUT_TEXT, {"key": "codex:abc", "text": "rm -rf"})
        self.assertFalse(reply["ok"])
        self.assertEqual(self.hub.calls, [])

    def test_hello_works_before_pairing_but_hides_capabilities(self):
        connection = self._connect()
        reply = self._call(connection, protocol.M_HELLO, {"name": "iPhone"})
        self.assertTrue(reply["ok"])
        self.assertFalse(reply["d"]["paired"])
        self.assertEqual(reply["d"]["runtimes"], [], "没配对不该看到装了哪些助手")

    def test_pairing_with_the_right_code_unlocks_everything(self):
        code = self.service.begin_pairing()
        connection = self._connect()
        reply = self._call(connection, protocol.M_PAIR, {"code": code, "name": "我的 iPhone"})
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(connection.paired)
        listing = self._call(connection, protocol.M_SESSIONS_LIST)
        self.assertTrue(listing["ok"])
        self.assertEqual(listing["d"]["sessions"][0]["key"], "codex:abc")

    def test_sessions_list_rejects_non_integer_limit(self):
        code = self.service.begin_pairing()
        connection = self._connect()
        self.assertTrue(self._call(connection, protocol.M_PAIR, {"code": code})["ok"])
        reply = self._call(connection, protocol.M_SESSIONS_LIST, {"limit": "很多"})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)

    def test_pairing_code_is_single_use(self):
        code = self.service.begin_pairing()
        first = self._connect("aa" * 32)
        self.assertTrue(self._call(first, protocol.M_PAIR, {"code": code})["ok"])
        second = self._connect("bb" * 32)
        reply = self._call(second, protocol.M_PAIR, {"code": code})
        self.assertFalse(reply["ok"], "配对码用过一次就该作废")

    def test_wrong_pairing_code_is_rejected(self):
        self.service.begin_pairing()
        connection = self._connect()
        reply = self._call(connection, protocol.M_PAIR, {"code": "WRONGWRONGWRONG1"})
        self.assertFalse(reply["ok"])
        self.assertFalse(connection.paired)

    def test_pairing_without_an_open_window_is_rejected(self):
        connection = self._connect()
        reply = self._call(connection, protocol.M_PAIR, {"code": "ANYTHINGATALL123"})
        self.assertFalse(reply["ok"])

    def test_known_device_is_admitted_without_a_pairing_window(self):
        code = self.service.begin_pairing()
        first = self._connect()
        self._call(first, protocol.M_PAIR, {"code": code})
        self.assertFalse(self.service.pairing_open())
        self.assertTrue(self.service.accepts("aa" * 32))
        self.assertFalse(self.service.accepts("cc" * 32), "陌生设备在窗口关闭后不该被放行")

    def test_reconnecting_paired_device_is_paired_from_the_start(self):
        code = self.service.begin_pairing()
        self._call(self._connect(), protocol.M_PAIR, {"code": code})
        again = self._connect()
        self.assertTrue(again.paired)

    # -- 订阅记账 -----------------------------------------------------------

    def _paired(self) -> Connection:
        code = self.service.begin_pairing()
        connection = self._connect()
        self._call(connection, protocol.M_PAIR, {"code": code})
        return connection

    def test_duplicate_watch_does_not_double_count(self):
        connection = self._paired()
        self._call(connection, protocol.M_SESSIONS_WATCH)
        self._call(connection, protocol.M_SESSIONS_WATCH)
        self.assertEqual(self.hub.watching_sessions, 1)

    def test_duplicate_screen_watch_resyncs_without_double_count(self):
        """聊天状态条与终端页各调一次 watch：订阅只记一次，第二次必须仍有整帧。"""
        connection = self._paired()
        first = self._call(connection, protocol.M_SCREEN_WATCH, {"key": "codex:abc"})
        second = self._call(connection, protocol.M_SCREEN_WATCH, {"key": "codex:abc"})
        self.assertEqual(self.hub.watching_screens["codex:abc"], 1)
        self.assertEqual(first["d"]["frame"]["status"], "first")
        self.assertEqual(second["d"]["frame"]["status"], "resync")
        self.assertIn(("resync_screen", ("codex:abc",)), self.hub.calls)

    def test_duplicate_session_watch_returns_snapshot_without_double_count(self):
        """同连接第二次 session.watch 不得空历史，且不得再加一层订阅计数。"""
        connection = self._paired()
        first = self._call(connection, protocol.M_SESSION_WATCH, {"key": "codex:abc"})
        second = self._call(connection, protocol.M_SESSION_WATCH, {"key": "codex:abc"})
        self.assertEqual(self.hub.watching_conversations["codex:abc"], 1)
        self.assertEqual(len(first["d"]["messages"]), 1)
        self.assertEqual(len(second["d"]["messages"]), 2)
        self.assertIn(("conversation_snapshot", ("codex:abc",)), self.hub.calls)

    def test_disconnect_releases_every_subscription(self):
        """断线不退订会让后台一直抓帧，是最典型的「电脑莫名发烫」来源。"""
        connection = self._paired()
        self._call(connection, protocol.M_SESSIONS_WATCH)
        self._call(connection, protocol.M_SCREEN_WATCH, {"key": "codex:abc"})
        self.service.detach(connection)
        self.assertEqual(self.hub.watching_sessions, 0)
        self.assertEqual(self.hub.watching_screens["codex:abc"], 0)

    def test_events_only_reach_subscribers(self):
        watcher = self._paired()
        bystander = self._connect("bb" * 32)
        self._call(watcher, protocol.M_SESSIONS_WATCH)
        self.sent.clear()
        self.service._dispatch_event(protocol.CH_SESSIONS, {"sessions": []})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["c"], protocol.CH_SESSIONS)
        self.service.detach(bystander)

    def test_closed_connection_stops_receiving_events(self):
        connection = self._paired()
        self._call(connection, protocol.M_SESSIONS_WATCH)
        connection.closed = True
        self.sent.clear()
        self.service._dispatch_event(protocol.CH_SESSIONS, {"sessions": []})
        self.assertEqual(self.sent, [])

    # -- 错误处理 -----------------------------------------------------------

    def test_missing_session_key_is_a_usage_error(self):
        connection = self._paired()
        reply = self._call(connection, protocol.M_SCREEN_WATCH, {})
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)
        self.assertEqual(self.hub.watching_screens, {})

    def test_empty_key_list_is_rejected_before_touching_tmux(self):
        connection = self._paired()
        reply = self._call(connection, protocol.M_INPUT_KEYS, {"key": "codex:abc", "keys": []})
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)
        self.assertEqual(self.hub.calls, [])

    def test_hub_errors_are_passed_through_verbatim(self):
        connection = self._paired()
        reply = self._call(connection, protocol.M_SESSION_DELETE, {"key": "codex:abc"})
        self.assertEqual(reply["e"]["code"], protocol.E_NOT_FOUND)
        self.assertEqual(reply["e"]["message"], "会话不在了")

    def test_unknown_method_is_reported_not_crashed(self):
        connection = self._paired()
        reply = self._call(connection, "session.selfDestruct")
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)

    def test_non_request_messages_are_ignored(self):
        connection = self._paired()
        self.sent.clear()
        self.service.handle(connection, protocol.event("sessions", {}))
        self.assertEqual(self.sent, [])

    def test_image_with_broken_base64_is_rejected(self):
        connection = self._paired()
        reply = self._call(connection, protocol.M_INPUT_IMAGE, {"key": "codex:abc", "data": "not-base64!!"})
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)

    def test_image_with_empty_payload_is_rejected(self):
        connection = self._paired()
        reply = self._call(connection, protocol.M_INPUT_IMAGE, {"key": "codex:abc", "data": ""})
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)
        self.assertEqual(self.hub.calls, [])

    def test_whitespace_only_keys_are_rejected(self):
        connection = self._paired()
        reply = self._call(
            connection, protocol.M_INPUT_KEYS, {"key": "codex:abc", "keys": ["  ", "\t"]}
        )
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)
        self.assertEqual(self.hub.calls, [])

    def test_screen_resize_is_rejected_without_touching_desktop(self):
        """手机误发 screen.resize 也绝不能改桌面窗口尺寸。"""
        connection = self._paired()
        reply = self._call(
            connection,
            protocol.M_SCREEN_RESIZE,
            {"key": "codex:abc", "cols": 40, "rows": 20},
        )
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)
        self.assertEqual(self.hub.calls, [])

    def test_projects_list_includes_ios_path_name_fields(self):
        connection = self._paired()
        reply = self._call(connection, protocol.M_PROJECTS_LIST)
        self.assertTrue(reply["ok"])
        project = reply["d"]["projects"][0]
        self.assertEqual(project["path"], "/tmp/demo")
        self.assertEqual(project["name"], "demo")
        self.assertEqual(project["cwd"], "/tmp/demo")
        self.assertEqual(project["label"], "demo")

    def test_session_new_requires_runtime(self):
        connection = self._paired()
        reply = self._call(connection, protocol.M_SESSION_NEW, {"cwd": "/tmp"})
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)


class PairingWindowTests(unittest.TestCase):
    """配对窗口存在文件里，所以要单独确认过期与清理的行为。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = os.environ.get("PICKUP_CACHE_DIR")
        os.environ["PICKUP_CACHE_DIR"] = self._tmp.name
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._old is None:
            os.environ.pop("PICKUP_CACHE_DIR", None)
        else:
            os.environ["PICKUP_CACHE_DIR"] = self._old

    def test_expired_window_is_treated_as_closed(self):
        remote_config.write_pairing(crypto.new_pairing_code(), ttl=-1)
        self.assertIsNone(remote_config.read_pairing())

    def test_reading_an_expired_window_cleans_up_the_file(self):
        remote_config.write_pairing(crypto.new_pairing_code(), ttl=-1)
        remote_config.read_pairing()
        self.assertFalse(remote_config.pairing_path().exists())

    def test_identity_key_is_stable_and_private(self):
        first = remote_config.load_or_create_identity()
        self.assertEqual(first, remote_config.load_or_create_identity())
        mode = remote_config.identity_key_path().stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, "私钥文件权限必须是 0600")

    def test_devices_survive_a_restart(self):
        state = remote_config.load_state()
        remote_config.add_device(
            state,
            remote_config.PairedDevice(
                id="d1", name="iPhone", public_key="aa" * 32, paired_at=1.0
            ),
        )
        reloaded = remote_config.load_state()
        self.assertIsNotNone(remote_config.find_device(reloaded, "aa" * 32))
        self.assertEqual(reloaded.host_id, state.host_id)


if __name__ == "__main__":
    unittest.main()
