"""split_layout 分屏组合记忆单测。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from pickup import split_layout


class SplitLayoutStoreTests(unittest.TestCase):
    def test_set_group_and_lookup(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.set_group("/tmp/proj", ["claude:a", "codex:b"], focus_key="codex:b")
        group = store.get_group("claude:a")
        self.assertIsNotNone(group)
        assert group is not None
        self.assertEqual(group.session_keys, ["claude:a", "codex:b"])
        self.assertEqual(store.get_group("codex:b"), group)

    def test_max_three_panes(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.set_group(
            "/p",
            ["claude:1", "codex:2", "kimi:3", "cursor:4"],
        )
        group = store.get_group("claude:1")
        assert group is not None
        self.assertEqual(len(group.session_keys), split_layout.MAX_PANES)

    def test_remove_session_dissolves_group_with_one_member_left(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.set_group("/p", ["claude:a", "codex:b"])
        store.remove_session("codex:b")
        self.assertIsNone(store.get_group("codex:b"))
        self.assertIsNone(store.get_group("claude:a"))

    def test_prune_inactive(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.set_group("/p", ["claude:a", "codex:b"])
        store.prune_inactive(lambda k: k == "claude:a")
        self.assertIsNone(store.get_group("claude:a"))

    def test_migrate_session_key(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.set_group("/p", ["cursor:short", "claude:a"], focus_key="cursor:short")
        store.migrate_session_key("cursor:short", "cursor:full-uuid")
        group = store.get_group("cursor:full-uuid")
        assert group is not None
        self.assertEqual(group.session_keys, ["cursor:full-uuid", "claude:a"])
        self.assertEqual(group.focus_key, "cursor:full-uuid")
        self.assertIsNone(store.get_group("cursor:short"))

    def test_resolve_active_group_degrades_dead_mates(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.set_group("/p", ["claude:a", "codex:b"])
        sessions = {
            "claude:a": {"cwd": "/p", "keepalive_name": "n1"},
            "codex:b": {"cwd": "/p"},
        }

        def is_active(k: str) -> bool:
            return k == "claude:a"

        def find_session(k: str) -> dict | None:
            return sessions.get(k)

        project, keys = split_layout.resolve_active_group(
            store, "claude:a", is_active=is_active, find_session=find_session,
        )
        self.assertEqual(project, "/p")
        self.assertEqual(keys, ["claude:a"])

    def test_save_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "split-layout.json")
            with mock.patch.object(split_layout, "LAYOUT_FILE", path):
                with mock.patch.object(split_layout, "CACHE_DIR", td):
                    store = split_layout.SplitLayoutStore()
                    store.set_group("/proj", ["claude:x", "codex:y"], focus_key="claude:x")
                    split_layout.save_layout(store)
                    loaded = split_layout.load_layout()
                    group = loaded.get_group("codex:y")
                    assert group is not None
                    self.assertEqual(group.session_keys, ["claude:x", "codex:y"])
                    self.assertEqual(loaded.last_project, "/proj")
                    self.assertTrue(group.name.startswith("Group "))

    def test_load_rebuilds_index_from_groups_only(self) -> None:
        """磁盘里陈旧/矛盾的 session_to_group 不得覆盖 groups 真相。"""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "split-layout.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "version": 1,
                        "last_project": "/p",
                        "last_focus_key": "claude:a",
                        "groups": {
                            "g1": {
                                "project_cwd": "/p",
                                "session_keys": ["claude:a", "codex:b"],
                                "focus_key": "claude:a",
                            }
                        },
                        "session_to_group": {"claude:ghost": "g1", "claude:a": "wrong"},
                    },
                    f,
                )
            with mock.patch.object(split_layout, "LAYOUT_FILE", path):
                loaded = split_layout.load_layout()
            self.assertEqual(
                loaded.session_to_group,
                {"claude:a": "g1", "codex:b": "g1"},
            )
            self.assertIsNone(loaded.get_group("claude:ghost"))

    def test_group_names_are_fruit_based_and_unique(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.set_group("/a", ["claude:a", "codex:b"])
        store.set_group("/b", ["claude:c", "codex:d"])
        names = {group.name for group in store.groups.values()}
        self.assertEqual(len(names), 2)
        self.assertTrue(all(name.startswith("Group ") for name in names))

    def test_pin_state_roundtrip_and_group_members_cannot_stay_individually_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "split-layout.json")
            with mock.patch.object(split_layout, "LAYOUT_FILE", path):
                store = split_layout.SplitLayoutStore()
                self.assertTrue(store.toggle_session_pin("claude:a"))
                store.set_group("/p", ["claude:a", "codex:b"])
                group = store.get_group("claude:a")
                assert group is not None
                self.assertNotIn("claude:a", store.pinned_session_keys)
                self.assertTrue(store.toggle_group_pin(group.group_id))
                store.set_collapsed(group.group_id, True)
                split_layout.save_layout(store)
                loaded = split_layout.load_layout()
            loaded_group = loaded.get_group("claude:a")
            assert loaded_group is not None
            self.assertTrue(loaded_group.collapsed)
            self.assertIn(loaded_group.group_id, loaded.pinned_group_ids)

    def test_independent_session_pin_migrates_with_provisional_key(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.toggle_session_pin("claude:short")
        store.migrate_session_key("claude:short", "claude:full")
        self.assertNotIn("claude:short", store.pinned_session_keys)
        self.assertIn("claude:full", store.pinned_session_keys)


if __name__ == "__main__":
    unittest.main()
