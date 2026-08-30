"""corral.remote.sessions：会话载荷字段与置顶/搜索/删除后布局一致性。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import split_layout
from corral.embed import Cell
from corral.remote import sessions as remote_sessions
from corral.remote.screen import ScreenEncoder
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

    def test_unchanged_capture_skips_screen_parsing_and_encoding(self) -> None:
        session = _session(sid="live")
        session["keepalive_name"] = "pane-live"
        watch = remote_sessions._ScreenWatch("claude:live", ScreenEncoder())
        state = (0, 0, True, False, False, 0, 80, 24)
        grid = [[Cell(ch="o"), Cell(ch="k")]]
        with (
            mock.patch.object(self.hub.store, "find_session", return_value=session),
            mock.patch.object(remote_sessions.embed, "pane_state", return_value=state),
            mock.patch.object(remote_sessions.embed, "capture", return_value="ok"),
            mock.patch.object(remote_sessions.embed, "parse_screen", return_value=grid) as parse_screen,
        ):
            self.assertIsNotNone(self.hub._capture_frame(watch))
            self.assertIsNone(self.hub._capture_frame(watch))
        parse_screen.assert_called_once_with("ok", 80, 24)

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

    def test_default_list_keeps_waiting_and_caps_idle_history(self) -> None:
        """手机首包不能把几百条闲置历史整表塞出去，但等待中/置顶必须留下。"""
        sessions = [
            _session(sid=f"idle{index}", title=f"闲置 {index}", mtime=1_700_000_000 + index)
            for index in range(120)
        ]
        sessions.append(_session(sid="wait", title="等你回答", attention="waiting", mtime=1))
        sessions.append(_session(sid="pin", title="置顶", mtime=2))
        self.hub.layout_db.toggle_session_pin("claude:pin")
        with (
            mock.patch.object(self.hub.store, "all_sessions", return_value=sessions),
            mock.patch.object(
                self.hub.store, "get_title", side_effect=lambda item: item["fallback_title"]
            ),
        ):
            listed = self.hub.list_sessions()
            searched = self.hub.list_sessions(query="等你回答")
        keys = {item["key"] for item in listed}
        self.assertIn("claude:wait", keys)
        self.assertIn("claude:pin", keys)
        self.assertEqual(len(listed), remote_sessions._PHONE_LIST_LIMIT)
        self.assertEqual([item["key"] for item in searched], ["claude:wait"])

    def test_default_list_builds_payloads_only_for_the_window(self) -> None:
        """截窗必须发生在组摘要之前，不能先为几百条闲置会话做完整打包。"""
        sessions = [
            _session(sid=f"idle{index}", title=f"闲置 {index}", mtime=1_700_000_000 + index)
            for index in range(120)
        ]
        calls = {"n": 0}
        real = SessionHub.session_payload

        def wrapped(hub, session, layout=None):
            calls["n"] += 1
            return real(hub, session, layout)

        with (
            mock.patch.object(self.hub.store, "all_sessions", return_value=sessions),
            mock.patch.object(
                self.hub.store, "get_title", side_effect=lambda item: item["fallback_title"]
            ),
            mock.patch.object(SessionHub, "session_payload", wrapped),
        ):
            listed = self.hub.list_sessions()
        self.assertEqual(len(listed), remote_sessions._PHONE_LIST_LIMIT)
        self.assertEqual(calls["n"], remote_sessions._PHONE_LIST_LIMIT)

    def test_list_snapshot_skips_sessions_when_version_matches(self) -> None:
        sessions = [_session(sid="a", title="alpha"), _session(sid="b", title="beta")]
        calls = {"n": 0}
        real = SessionHub.session_payload

        def wrapped(hub, session, layout=None):
            calls["n"] += 1
            return real(hub, session, layout)

        with (
            mock.patch.object(self.hub.store, "all_sessions", return_value=sessions),
            mock.patch.object(
                self.hub.store, "get_title", side_effect=lambda item: item["fallback_title"]
            ),
        ):
            first = self.hub.list_snapshot()
            payload_calls_after_first = 0
            with mock.patch.object(SessionHub, "session_payload", wrapped):
                again = self.hub.list_snapshot(since_version=str(first["version"]))
                payload_calls_after_first = calls["n"]
        self.assertFalse(first["unchanged"])
        self.assertEqual(len(first["sessions"]), 2)
        self.assertTrue(again["unchanged"])
        self.assertNotIn("sessions", again)
        self.assertEqual(again["version"], first["version"])
        self.assertEqual(payload_calls_after_first, 0)

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
        """第二路 session.watch 也必须拿到首屏窗口，不能因为共享订阅计数变成空列表。"""
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
        self.assertEqual(len(first["messages"]), 1)
        self.assertEqual(first["messages"][0]["text"], "你好")
        self.assertEqual(len(second["messages"]), 1)
        self.assertEqual(second["messages"][0]["text"], "你好")
        self.assertEqual(first["has_more"], False)
        self.hub.unwatch_conversation("claude:a")
        self.hub.unwatch_conversation("claude:a")

    def test_message_page_is_bounded_and_supports_before_cursor(self) -> None:
        items = [
            remote_sessions.richmsg.RichMessage(index, "assistant", f"消息 {index}")
            for index in range(1, 7)
        ]
        page = remote_sessions._message_page(items, limit=3)
        self.assertEqual([item["seq"] for item in page["messages"]], [4, 5, 6])
        self.assertTrue(page["has_more"])
        self.assertEqual(page["oldest_seq"], 4)
        self.assertEqual(page["from"], 4)
        self.assertEqual(page["to"], 6)
        self.assertEqual(page["total"], 6)
        self.assertEqual(page["generation"], 1)

        earlier = remote_sessions._message_page(items, limit=3, before_seq=4)
        self.assertEqual([item["seq"] for item in earlier["messages"]], [1, 2, 3])
        self.assertFalse(earlier["has_more"])

    def test_opening_session_parses_history_once(self) -> None:
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
        reads = {"all": 0}
        original = remote_sessions.richmsg.RichReader.read_all

        def counting(reader, *args, **kwargs):
            reads["all"] += 1
            return original(reader, *args, **kwargs)

        with mock.patch.object(self.hub, "require_session", return_value=session):
            with mock.patch.object(
                remote_sessions.richmsg.RichReader, "read_all", counting
            ):
                first = self.hub.watch_conversation("claude:a")
                page = self.hub.message_page("claude:a")
                prompts = self.hub.prompts("claude:a")
        self.assertEqual(reads["all"], 1)
        self.assertEqual(len(first["messages"]), 1)
        self.assertEqual(len(page["messages"]), 1)
        self.assertEqual(first["generation"], page["generation"])
        self.assertEqual(prompts, [])

    def test_disk_cache_avoids_full_reread_on_new_hub(self) -> None:
        path = Path(self._tmp.name) / "claude.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "缓存命中"}],
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
            self.hub.watch_conversation("claude:a")

        hub2 = SessionHub(scan_limit=10)
        self.addCleanup(hub2.stop)
        reads = {"all": 0}
        original = remote_sessions.richmsg.RichReader.read_all

        def counting(reader, *args, **kwargs):
            reads["all"] += 1
            return original(reader, *args, **kwargs)

        with mock.patch.object(hub2, "require_session", return_value=session):
            with mock.patch.object(
                remote_sessions.richmsg.RichReader, "read_all", counting
            ):
                page = hub2.message_page("claude:a")
        self.assertEqual(reads["all"], 0)
        self.assertEqual(page["messages"][0]["text"], "缓存命中")

    def test_watch_without_after_seq_returns_tail(self) -> None:
        """旧手机不传 after_seq，仍拿当前尾部窗口。"""
        path = Path(self._tmp.name) / "claude.jsonl"
        _write_assistant_jsonl(path, ["一", "二", "三", "四", "五"])
        session = _session(sid="a")
        session["path"] = str(path)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            page = self.hub.watch_conversation("claude:a", limit=3)
        self.assertEqual(page["resume"], "tail")
        self.assertEqual([item["seq"] for item in page["messages"]], [3, 4, 5])
        self.assertEqual(page["kind"], "snapshot")
        self.hub.unwatch_conversation("claude:a")

    def test_watch_same_generation_replays_only_the_gap(self) -> None:
        """generation 相同且缺口还在规范化缓存里时，只返回 after_seq 之后的更新。"""
        path = Path(self._tmp.name) / "claude.jsonl"
        _write_assistant_jsonl(path, ["一", "二", "三", "四", "五"])
        session = _session(sid="a")
        session["path"] = str(path)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            first = self.hub.watch_conversation("claude:a")
            self.hub.unwatch_conversation("claude:a")
            gap = self.hub.watch_conversation(
                "claude:a",
                after_seq=3,
                generation=first["generation"],
            )
        self.assertEqual(first["resume"], "tail")
        self.assertEqual(gap["resume"], "replay")
        self.assertEqual([item["seq"] for item in gap["messages"]], [4, 5])
        self.assertEqual(gap["generation"], first["generation"])
        self.hub.unwatch_conversation("claude:a")

    def test_watch_generation_mismatch_returns_tail(self) -> None:
        """手机记下的 generation 对不上时，退回当前尾部，不得假装回放。"""
        path = Path(self._tmp.name) / "claude.jsonl"
        _write_assistant_jsonl(path, ["一", "二", "三"])
        session = _session(sid="a")
        session["path"] = str(path)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            first = self.hub.watch_conversation("claude:a")
            self.hub.unwatch_conversation("claude:a")
            page = self.hub.watch_conversation(
                "claude:a",
                after_seq=1,
                generation=first["generation"] + 9,
            )
        self.assertEqual(page["resume"], "tail")
        self.assertEqual(len(page["messages"]), 3)
        self.hub.unwatch_conversation("claude:a")

    def test_empty_replay_is_not_a_clear(self) -> None:
        """已追上时 replay 的 messages 为空，表示没有新缺口，不是让手机清空。"""
        path = Path(self._tmp.name) / "claude.jsonl"
        _write_assistant_jsonl(path, ["一", "二"])
        session = _session(sid="a")
        session["path"] = str(path)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            first = self.hub.watch_conversation("claude:a")
            self.hub.unwatch_conversation("claude:a")
            caught_up = self.hub.watch_conversation(
                "claude:a",
                after_seq=first["newest_seq"],
                generation=first["generation"],
            )
        self.assertEqual(caught_up["resume"], "replay")
        self.assertEqual(caught_up["messages"], [])
        self.assertEqual(first["messages"][0]["text"], "一")
        self.hub.unwatch_conversation("claude:a")

    def test_delta_buffer_overflow_falls_back_to_tail(self) -> None:
        """有界缓冲溢出后，旧序号不可回放，改为当前尾部。"""
        path = Path(self._tmp.name) / "claude.jsonl"
        _write_assistant_jsonl(path, ["一", "二", "三"])
        session = _session(sid="a")
        session["path"] = str(path)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            first = self.hub.watch_conversation("claude:a")
            watch = self.hub._conversations["claude:a"]
            watch.deltas = remote_sessions._DeltaBuffer(maxlen=3)
            watch.deltas.append(
                [
                    remote_sessions.richmsg.RichMessage(index, "assistant", f"增量 {index}")
                    for index in range(1, 6)
                ]
            )
            page = self.hub.watch_conversation(
                "claude:a",
                after_seq=1,
                generation=first["generation"],
            )
        self.assertEqual(page["resume"], "tail")
        self.assertEqual(len(page["messages"]), 3)
        self.hub.unwatch_conversation("claude:a")
        self.hub.unwatch_conversation("claude:a")

    def test_large_history_opens_tail_and_pages_earlier_without_full_parse(self) -> None:
        path = Path(self._tmp.name) / "claude.jsonl"
        total = 4000
        _write_assistant_jsonl(path, [f"尾部消息-{index}" for index in range(total)])
        session = _session(sid="a")
        session["path"] = str(path)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            page = self.hub.watch_conversation("claude:a")
            reader = self.hub._transcripts["claude:a"].reader
            parsed = reader.parsed_line_count
            self.assertTrue(page["has_more"])
            self.assertEqual(page["messages"][-1]["text"], f"尾部消息-{total - 1}")
            self.assertEqual(len(page["messages"]), 80)
            self.assertLess(parsed, total // 2)

            earlier = self.hub.message_page("claude:a", before_seq=page["oldest_seq"])
            self.assertGreater(len(earlier["messages"]), 0)
            self.assertNotEqual(earlier["messages"][-1]["text"], page["messages"][-1]["text"])
            self.assertLess(earlier["messages"][-1]["seq"], page["oldest_seq"])
            self.assertLess(reader.parsed_line_count, total)

            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "uuid": "new",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "追加一条"}],
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            added = reader.poll()
        self.assertEqual([item.text for item in added], ["追加一条"])
        self.hub.unwatch_conversation("claude:a")

    def test_require_session_follows_placeholder_key_migration(self) -> None:
        """占位卡转正后，手机仍拿着旧键也必须能找到会话，不能报已经不在列表里。"""
        real = _session(sid="real-id", title="正式会话")
        self.hub.store.sessions = {"claude": [real]}
        self.hub.store._session_key_migrations["claude:placeholder"] = "claude:real-id"
        found = self.hub.require_session("claude:placeholder")
        self.assertEqual(found["id"], "real-id")
        with self.assertRaises(remote_sessions.ActionError) as raised:
            self.hub.require_session("claude:missing")
        self.assertEqual(raised.exception.code, "not_found")

    def test_conversation_watch_rebinding_keeps_phone_channel(self) -> None:
        """转正后实时订阅仍走手机原来的通道，但读取正式历史。"""
        old_path = Path(self._tmp.name) / "old.jsonl"
        old_path.write_text("", encoding="utf-8")
        new_path = Path(self._tmp.name) / "new.jsonl"
        _write_assistant_jsonl(new_path, ["转正后的回复"])
        placeholder = _session(sid="placeholder")
        placeholder["path"] = str(old_path)
        real = _session(sid="real-id")
        real["path"] = str(new_path)
        self.hub.store.sessions = {"claude": [placeholder]}
        page = self.hub.watch_conversation("claude:placeholder")
        self.assertEqual(page["messages"], [])
        self.hub.store.sessions = {"claude": [real]}
        self.hub.store._session_key_migrations["claude:placeholder"] = "claude:real-id"
        self.hub._follow_key_migrations()
        found = self.hub.require_session("claude:placeholder")
        self.assertEqual(found["id"], "real-id")
        watch = self.hub._conversations["claude:placeholder"]
        self.assertEqual(watch.key, "claude:placeholder")
        self.assertEqual(watch.canonical_key, "claude:real-id")
        cached = self.hub._transcripts["claude:real-id"]
        self.assertEqual([item.text for item in cached.messages], ["转正后的回复"])
        with new_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "later",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "后来追加"}],
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        added = watch.reader.poll()
        self.assertEqual([item.text for item in added], ["后来追加"])
        self.hub.unwatch_conversation("claude:placeholder")

    def test_stop_and_delete_mutate_canonical_key(self) -> None:
        """手机仍拿旧键时，停止/删除必须改正式会话，不能写到已经不存在的占位卡。"""
        real = _session(sid="real-id")
        real["keepalive_name"] = "corral-claude-real"
        self.hub.store.sessions = {"claude": [real]}
        self.hub.store.hosted["claude:real-id"] = "corral-claude-real"
        self.hub.store._session_key_migrations["claude:placeholder"] = "claude:real-id"
        with mock.patch("corral.remote.sessions.keepalive.kill", return_value=True) as mocked:
            self.hub.stop_session("claude:placeholder")
        mocked.assert_called_once_with("corral-claude-real")
        self.assertNotIn("claude:real-id", self.hub.store.hosted)
        stopped = self.hub.store.find_session("claude:real-id")
        self.assertIsNotNone(stopped)
        self.assertFalse(stopped["live"])

        real = _session(sid="real-id")
        self.hub.store.sessions = {"claude": [real]}
        self.hub.store._deleted.clear()
        self.hub.store._session_key_migrations["claude:placeholder"] = "claude:real-id"
        runtime = mock.Mock()
        with mock.patch.object(self.hub, "_runtime_of", return_value=runtime):
            self.hub.delete_session("claude:placeholder")
        runtime.delete_session.assert_called_once()
        self.assertIn("claude:real-id", self.hub.store._deleted)
        self.assertIn("claude:placeholder", self.hub.store._deleted)
        self.assertIsNone(self.hub.store.find_session("claude:real-id"))


def _write_assistant_jsonl(path: Path, texts: list[str]) -> None:
    lines = []
    for index, text in enumerate(texts, start=1):
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": f"u{index}",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    },
                },
                ensure_ascii=False,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
