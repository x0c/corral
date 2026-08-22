"""corral.remote.sessions：会话载荷字段与置顶/搜索/删除后布局一致性。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import split_layout
from corral.remote.sessions import SessionHub


def _session(
    *,
    source: str = "claude",
    sid: object = "abc123",
    short_id: object = "abc123",
    title: str = "示例会话",
    cwd: str = "/tmp/proj",
    mtime: object = 1_700_000_000.0,
    attention: str = "none",
    last_user: str = "你好",
    last_agent: str = "好的",
) -> dict:
    return {
        "source": source,
        "id": sid,
        "short_id": short_id,
        "cwd": cwd,
        "cwd_display": cwd,
        "mtime": mtime,
        "display_time": "01-01 12:00",
        "size_kb": 1.5,
        "status_tag": "ended",
        "live": False,
        "keepalive_name": None,
        "fallback_title": title,
        "attention_kind": attention,
        "last_user_msg": last_user,
        "last_agent_msg": last_agent,
        "path": "/tmp/hist.jsonl",
    }


class SessionHubPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(
            os.environ, {"CORRAL_CACHE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self.hub = SessionHub(scan_limit=10)
        self.hub.layout_db = split_layout.SidebarLayoutDB()

    def tearDown(self) -> None:
        self.hub.stop()

    def test_session_payload_coerces_id_and_mtime_for_ios_decoder(self) -> None:
        """手机端 id/short_id 按 String、mtime 按 Double 解码；类型错会整表空白。"""
        session = _session(sid=42, short_id=42, mtime="1700000000")
        with mock.patch.object(self.hub.store, "get_title", return_value="标题"):
            payload = self.hub.session_payload(session, None)
        self.assertEqual(payload["id"], "42")
        self.assertEqual(payload["short_id"], "42")
        self.assertEqual(payload["mtime"], 1_700_000_000.0)
        self.assertIsInstance(payload["pinned"], bool)
        self.assertFalse(payload["pinned"])
        self.assertEqual(payload["attention"], "none")

    def test_session_payload_group_uses_group_id_attribute(self) -> None:
        layout = split_layout.SplitLayoutStore()
        layout.set_group("/tmp/proj", ["claude:a", "codex:b"])
        layout.toggle_group_pin(layout.get_group("claude:a").group_id)
        session = _session(sid="a")
        with mock.patch.object(self.hub.store, "get_title", return_value="A"):
            payload = self.hub.session_payload(session, layout)
        group = payload["group"]
        self.assertEqual(group["id"], layout.get_group("claude:a").group_id)
        self.assertTrue(group["pinned"])
        self.assertIn("emoji", group)
        self.assertFalse(payload["pinned"])

    def test_toggle_pin_independent_session_returns_true_then_false(self) -> None:
        self.assertTrue(self.hub.toggle_pin("claude:solo"))
        layout = self.hub.layout_db.read()
        self.assertIn("claude:solo", layout.pinned_session_keys)
        self.assertFalse(self.hub.toggle_pin("claude:solo"))
        layout = self.hub.layout_db.read()
        self.assertNotIn("claude:solo", layout.pinned_session_keys)

    def test_toggle_pin_group_member_pins_whole_group(self) -> None:
        """组成员单独置顶会被布局层抹掉；远程应改为切换整组置顶。"""
        self.hub.layout_db.set_group("/tmp/proj", ["claude:a", "codex:b"])
        self.assertTrue(self.hub.toggle_pin("claude:a"))
        layout = self.hub.layout_db.read()
        gid = layout.get_group("claude:a").group_id
        self.assertIn(gid, layout.pinned_group_ids)
        self.assertNotIn("claude:a", layout.pinned_session_keys)
        # 手机列表靠 group.pinned 归入置顶区
        session = _session(sid="a")
        with mock.patch.object(self.hub.store, "get_title", return_value="A"):
            payload = self.hub.session_payload(session, layout)
        self.assertTrue(payload["group"]["pinned"])

    def test_list_sessions_respects_limit_and_searches_group_name(self) -> None:
        sessions = [
            _session(sid="a", title="alpha"),
            _session(source="codex", sid="b", title="beta"),
        ]
        layout = self.hub.layout_db.set_group("/tmp/proj", ["claude:a", "codex:b"])
        group_name = layout.get_group("claude:a").name
        with (
            mock.patch.object(self.hub.store, "all_sessions", return_value=sessions),
            mock.patch.object(self.hub.store, "get_title", side_effect=lambda s: s["fallback_title"]),
        ):
            limited = self.hub.list_sessions(limit=1)
            self.assertEqual(len(limited), 1)
            found = self.hub.list_sessions(query=group_name.split()[-1].lower())
            self.assertEqual(len(found), 2)
            miss = self.hub.list_sessions(query="zzz-no-match")
            self.assertEqual(miss, [])

    def test_delete_session_removes_layout_membership(self) -> None:
        self.hub.layout_db.set_group("/tmp/proj", ["claude:a", "codex:b"])
        self.hub.layout_db.toggle_session_pin("claude:solo")
        session = _session(sid="a")
        runtime = mock.Mock()
        with (
            mock.patch.object(self.hub, "require_session", return_value=session),
            mock.patch.object(self.hub, "_runtime_of", return_value=runtime),
            mock.patch.object(self.hub.store, "mark_deleted"),
            mock.patch.object(self.hub.store, "abort_delete"),
        ):
            self.hub.delete_session("claude:a")
        runtime.delete_session.assert_called_once_with(session)
        layout = self.hub.layout_db.read()
        # 只剩一个成员时应解散组
        self.assertIsNone(layout.get_group("codex:b"))
        self.assertEqual(layout.groups, {})

    def test_second_conversation_watcher_still_gets_history(self) -> None:
        """第二路 session.watch 也必须拿到全文，不能因为共享订阅计数变成空列表。"""
        path = Path(self._tmp.name) / "claude.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "你好"}],
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        session = _session(sid="a")
        session["path"] = str(path)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            first = self.hub.watch_conversation("claude:a")
            second = self.hub.watch_conversation("claude:a")
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["text"], "你好")
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["text"], "你好")
        self.hub.unwatch_conversation("claude:a")
        self.hub.unwatch_conversation("claude:a")


if __name__ == "__main__":
    unittest.main()
