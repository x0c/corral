"""Shared title state and remote title-only propagation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import titles
from corral.models import session_key
from corral.remote import richmsg
from corral.remote.sessions import SessionHub, _ConversationWatch
from corral.runtime.registry import RuntimeRegistry
from corral.store import SessionStore


def _session(sid: str = "abc", title: str = "临时兜底标题足够长") -> dict:
    return {
        "source": "claude",
        "id": sid,
        "short_id": sid,
        "mtime": 1,
        "size_bytes": 32,
        "size_kb": 1,
        "fallback_title": title,
        "first_user_msg": title,
        "last_user_msg": title,
        "last_agent_msg": "",
        "native_title": None,
        "cwd": "/tmp/proj",
        "cwd_display": "/tmp/proj",
        "live": False,
        "attention_kind": "none",
        "path": f"/tmp/{sid}.jsonl",
    }


class TitleStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_file = Path(self._tmp.name) / "titles.json"
        self._patches = [
            mock.patch.object(titles, "CACHE_DIR", self._tmp.name),
            mock.patch.object(titles, "CACHE_FILE", str(self.cache_file)),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_poll_detects_title_file_change_without_history_change(self) -> None:
        session = _session()
        key = session_key(session)
        def mtime() -> int:
            return self.cache_file.stat().st_mtime_ns if self.cache_file.exists() else 0

        state = titles.TitleState(cache={}, mtime_fn=mtime)
        self.assertEqual(state.poll([session]), set())
        self.assertEqual(state.revision, 0)

        titles.save_cache({key: {"fp": titles._fingerprint(session), "title": "共享生成标题"}})
        changed = state.poll([session])
        self.assertEqual(changed, {key})
        self.assertEqual(state.revision, 1)
        title, needs = titles.resolve_initial_title(session, state.cache)
        self.assertEqual(title, "共享生成标题")
        self.assertFalse(needs)

        # Corrupt / unreadable replacement must not wipe a good cache.
        self.cache_file.write_text("{not-json", encoding="utf-8")
        # Force mtime change observation.
        os.utime(self.cache_file, None)
        self.assertEqual(state.poll([session]), set())
        self.assertEqual(state.cache[key]["title"], "共享生成标题")
        self.assertEqual(state.revision, 1)

    def test_two_stores_converge_on_title_file_write(self) -> None:
        session = _session("shared")
        key = session_key(session)
        runtime = mock.Mock()
        runtime.id = "claude"
        runtime.display_name = "Claude"
        runtime.scan_sessions.return_value = [session]
        registry = RuntimeRegistry((runtime,))
        with mock.patch.object(titles, "load_cache", return_value={}):
            tui = SessionStore(limit=5, registry=registry)
            remote = SessionStore(limit=5, registry=registry)
            tui.load()
            remote.load()
        self.assertNotEqual(tui.get_title(session), "两端一致的标题")

        titles.save_cache({key: {"fp": titles._fingerprint(session), "title": "两端一致的标题"}})
        self.assertEqual(tui.poll_title_updates(), {key})
        self.assertEqual(remote.poll_title_updates(), {key})
        self.assertEqual(tui.get_title(session), "两端一致的标题")
        self.assertEqual(remote.get_title(session), "两端一致的标题")
        self.assertEqual(tui.title_revision, remote.title_revision)
        self.assertGreater(tui.title_revision, 0)


class RemoteTitlePropagationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_file = Path(self._tmp.name) / "titles.json"
        self._env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self._tmp.name}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        self._cache_patches = [
            mock.patch.object(titles, "CACHE_DIR", self._tmp.name),
            mock.patch.object(titles, "CACHE_FILE", str(self.cache_file)),
        ]
        for patcher in self._cache_patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.events: list[tuple[str, dict]] = []
        self.hub = SessionHub(on_event=lambda channel, data: self.events.append((channel, data)))
        self.addCleanup(self.hub.stop)

    def test_list_snapshot_includes_additive_revision(self) -> None:
        session = _session("rev")
        with (
            mock.patch.object(self.hub.store, "all_sessions", return_value=[session]),
            mock.patch.object(self.hub.store, "get_title", return_value=session["fallback_title"]),
        ):
            snap = self.hub.list_snapshot()
            again = self.hub.list_snapshot(since_version=str(snap["version"]))
        self.assertIn("revision", snap)
        self.assertEqual(snap["revision"], self.hub.store.title_revision)
        self.assertTrue(again["unchanged"])
        self.assertEqual(again["revision"], snap["revision"])
        self.assertNotIn("sessions", again)

    def test_title_only_refresh_emits_sessions_and_metadata(self) -> None:
        session = _session("phone")
        key = session_key(session)
        self.hub.store.sessions = {"claude": [session]}
        self.hub.store.display_titles[key] = session["fallback_title"]
        self.hub.store.generating.add(key)
        self.hub._sessions_watchers = 1
        self.hub._conversations[key] = _ConversationWatch(
            key=key,
            reader=richmsg.RichReader(session),
            watchers=1,
        )

        titles.save_cache({key: {"fp": titles._fingerprint(session), "title": "手机可见新标题"}})
        # Simulate one refresh-loop iteration with an unchanged history scan.
        with mock.patch.object(self.hub.store, "refresh", return_value=False):
            title_keys = self.hub.store.poll_title_updates()
            changed = self.hub.store.refresh()
            title_keys.update(self.hub.store.poll_title_updates())
            if (changed or title_keys) and self.hub._sessions_watchers:
                self.hub._on_event("sessions", self.hub.list_snapshot())
            if title_keys:
                for watch in list(self.hub._conversations.values()):
                    if watch.watchers <= 0:
                        continue
                    found = self.hub.store.find_session(watch.canonical_key or watch.key)
                    if found is None:
                        continue
                    self.hub._on_event(
                        f"session:{watch.key}",
                        {
                            "version": 1,
                            "kind": "metadata",
                            "session": watch.key,
                            "revision": self.hub.store.title_revision,
                            "summary": self.hub.session_payload(found, self.hub._layout()),
                        },
                    )

        channels = [channel for channel, _ in self.events]
        self.assertIn("sessions", channels)
        self.assertIn(f"session:{key}", channels)
        list_event = next(data for channel, data in self.events if channel == "sessions")
        self.assertIn("revision", list_event)
        self.assertGreater(list_event["revision"], 0)
        self.assertEqual(list_event["sessions"][0]["title"], "手机可见新标题")
        meta = next(data for channel, data in self.events if channel == f"session:{key}")
        self.assertEqual(meta["kind"], "metadata")
        self.assertEqual(meta["revision"], list_event["revision"])
        self.assertEqual(meta["summary"]["title"], "手机可见新标题")
        # User cache file remains a valid JSON object with the new title.
        on_disk = json.loads(self.cache_file.read_text(encoding="utf-8"))
        self.assertEqual(on_disk[key]["title"], "手机可见新标题")

    def test_hub_default_construction_does_not_inject_spawn(self) -> None:
        hub = SessionHub()
        self.addCleanup(hub.stop)
        self.assertIsNone(hub.store._title_spawn_fn)


if __name__ == "__main__":
    unittest.main()
