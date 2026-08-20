"""侧边栏记忆（会话组、置顶、折叠、显隐）单测。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from pickup import split_layout


class _TempLayoutDB(unittest.TestCase):
    """把库和旧文件一起隔离到临时目录。

    `PICKUP_CACHE_DIR` 必须设：`split_layout` 只有看到这个覆盖变量才不去真实家目录
    找旧版 JSON。少了它，测试会读到（历史上还改名过）机主真实的侧边栏记忆。
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache_dir = Path(self.temp.name)
        patcher = mock.patch.dict(
            os.environ, {"PICKUP_CACHE_DIR": str(self.cache_dir)}, clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)

    def db(self) -> split_layout.SidebarLayoutDB:
        return split_layout.SidebarLayoutDB()


class SplitLayoutStoreTests(unittest.TestCase):
    """纯内存变更逻辑（库会在事务里对最新快照重放这些方法）。"""

    def test_set_group_and_lookup(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.set_group("/tmp/proj", ["claude:a", "codex:b"], focus_key="codex:b")
        group = store.get_group("claude:a")
        self.assertIsNotNone(group)
        assert group is not None
        self.assertEqual(group.session_keys, ["claude:a", "codex:b"])
        self.assertEqual(store.get_group("codex:b"), group)

    def test_group_truncates_to_max_panes(self) -> None:
        store = split_layout.SplitLayoutStore()
        # 多给一个成员，确认超出上限的部分被截掉（而不是恰好等于上限，测不出截断）。
        keys = [f"rt{i}:s{i}" for i in range(split_layout.MAX_PANES + 1)]
        store.set_group("/p", keys)
        group = store.get_group(keys[0])
        assert group is not None
        self.assertEqual(group.session_keys, keys[: split_layout.MAX_PANES])

    def test_remove_session_dissolves_group_with_one_member_left(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.set_group("/p", ["claude:a", "codex:b"])
        store.remove_session("codex:b")
        self.assertIsNone(store.get_group("codex:b"))
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

    def test_set_focus_does_not_create_or_revive_group(self) -> None:
        """只切焦点不得新建/复活会话组——否则多窗口下会和别人的解散动作来回打架。"""
        store = split_layout.SplitLayoutStore()
        store.set_focus("/p", "claude:lonely")
        self.assertEqual(store.groups, {})
        self.assertEqual(store.last_focus_key, "claude:lonely")

        store.set_group("/p", ["claude:a", "codex:b"], focus_key="claude:a")
        name = store.get_group("claude:a").name
        store.set_focus("/p", "codex:b")
        group = store.get_group("codex:b")
        assert group is not None
        self.assertEqual(group.focus_key, "codex:b")
        self.assertEqual(group.name, name)
        self.assertEqual(len(store.groups), 1)

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

    def test_group_names_are_fruit_based_and_unique(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.set_group("/a", ["claude:a", "codex:b"])
        store.set_group("/b", ["claude:c", "codex:d"])
        names = {group.name for group in store.groups.values()}
        self.assertEqual(len(names), 2)
        self.assertTrue(all(name.startswith("Group ") for name in names))

    def test_group_names_keep_fruit_variety_when_pool_exhausted(self) -> None:
        """水果名用尽后，新组名应随机挑水果加序号，而不是全部落到 Apple N。"""
        store = split_layout.SplitLayoutStore()
        # 先把全部水果名各占一组（每种水果两个成员）。
        for i, _fruit in enumerate(split_layout._FRUIT_NAMES):
            store.set_group(f"/p{i}", [f"claude:{i}a", f"codex:{i}b"])
        self.assertEqual(len(store.groups), len(split_layout._FRUIT_NAMES))
        store.set_group("/overflow", ["claude:oa", "codex:ob"])
        overflow = store.get_group("claude:oa")
        self.assertIsNotNone(overflow)
        # 组名形如 "Group <水果> 2"，能解析出水果 emoji，且不与现有组重名。
        fruit = overflow.name[len("Group "):].split(" ", 1)[0]
        self.assertIn(fruit, split_layout._FRUIT_NAMES)
        self.assertTrue(overflow.name.endswith(" 2"))
        self.assertNotIn(split_layout.group_emoji(overflow.name), ("",))
        used = {group.name for group in store.groups.values()}
        self.assertEqual(len(used), len(store.groups))

    def test_legacy_apple_fallback_names_are_regenerated_on_load(self) -> None:
        """旧版 "Group Apple N" 组名读入时重新生成，不再全挂 Apple。"""
        store = split_layout.SplitLayoutStore()
        store.set_group("/p1", ["claude:a", "codex:b"])
        group = store.get_group("claude:a")
        group.name = "Group Apple 7"
        store.set_group("/p2", ["claude:c", "codex:d"])
        fixed = store.get_group("claude:c")
        fixed.name = "Group Cherry"
        split_layout._normalize_store(store)
        self.assertNotEqual(store.get_group("claude:a").name, "Group Apple 7")
        self.assertEqual(store.get_group("claude:c").name, "Group Cherry")

    def test_legacy_name_migration_is_deterministic_across_reads(self) -> None:
        """读路径不落盘，迁移名必须每次读取都一样，否则多窗口对不上、列表反复重建。"""
        def build() -> list[str]:
            store = split_layout.SplitLayoutStore()
            for i in range(3):
                store.set_group(f"/p{i}", [f"claude:{i}a", f"codex:{i}b"])
                store.get_group(f"claude:{i}a").name = f"Group Apple {i + 2}"
            split_layout._normalize_store(store)
            return [group.name for group in store.ordered_groups()]

        self.assertEqual(build(), build())
        self.assertTrue(all(not split_layout._is_legacy_fallback_name(n) for n in build()))

    def test_independent_session_pin_migrates_with_provisional_key(self) -> None:
        store = split_layout.SplitLayoutStore()
        store.toggle_session_pin("claude:short")
        store.migrate_session_key("claude:short", "claude:full")
        self.assertNotIn("claude:short", store.pinned_session_keys)
        self.assertIn("claude:full", store.pinned_session_keys)

    def test_sidebar_fingerprint_ignores_focus_only_changes(self) -> None:
        """焦点变化不该触发别的窗口重建列表（全量重建是秒级重活）。"""
        store = split_layout.SplitLayoutStore()
        store.set_group("/p", ["claude:a", "codex:b"], focus_key="claude:a")
        before = split_layout.sidebar_fingerprint(store)
        store.set_focus("/p", "codex:b")
        self.assertEqual(split_layout.sidebar_fingerprint(store), before)
        store.set_collapsed(store.get_group("claude:a").group_id, True)
        self.assertNotEqual(split_layout.sidebar_fingerprint(store), before)


class SidebarLayoutDBTests(_TempLayoutDB):
    def test_roundtrip(self) -> None:
        db = self.db()
        db.set_group("/proj", ["claude:x", "codex:y"], focus_key="claude:x")
        loaded = self.db().read()
        group = loaded.get_group("codex:y")
        assert group is not None
        self.assertEqual(group.session_keys, ["claude:x", "codex:y"])
        self.assertEqual(loaded.last_project, "/proj")
        self.assertTrue(group.name.startswith("Group "))

    def test_pin_and_collapse_roundtrip(self) -> None:
        db = self.db()
        snapshot = db.toggle_session_pin("claude:a")
        self.assertIn("claude:a", snapshot.pinned_session_keys)
        snapshot = db.set_group("/p", ["claude:a", "codex:b"])
        # 进了会话组就只能整组置顶，旧的单会话置顶不再生效。
        self.assertNotIn("claude:a", snapshot.pinned_session_keys)
        gid = snapshot.get_group("claude:a").group_id
        db.toggle_group_pin(gid)
        db.set_collapsed(gid, True)
        loaded = self.db().read()
        self.assertIn(gid, loaded.pinned_group_ids)
        self.assertTrue(loaded.groups[gid].collapsed)

    def test_interleaved_windows_do_not_clobber_each_other(self) -> None:
        """当初的真实缺陷：A 置顶 → B 建组 → A 再置顶，三样改动必须都在。

        旧实现是「启动读一次、之后整份覆盖」，这个序列会让 B 抹掉 A 的置顶、
        A 再抹掉 B 的分组。
        """
        window_a = self.db()
        window_b = self.db()
        # 两个窗口都在最开始各读了一次（此后各自手上的快照都会过时）
        window_a.read()
        window_b.read()

        window_a.toggle_session_pin("claude:s1")
        window_b.set_group("/proj", ["codex:s2", "codex:s3"])
        window_a.toggle_session_pin("claude:s9")

        final = self.db().read()
        self.assertEqual(
            sorted(final.pinned_session_keys), ["claude:s1", "claude:s9"],
        )
        self.assertEqual(len(final.groups), 1)

    def test_revision_advances_only_on_real_changes(self) -> None:
        db = self.db()
        snapshot = db.set_group("/p", ["claude:a", "codex:b"])
        gid = snapshot.get_group("claude:a").group_id
        after_group = snapshot.revision
        # 折叠成同一个值不算改动，不该惊动别的窗口
        unchanged = db.set_collapsed(gid, False)
        self.assertEqual(unchanged.revision, after_group)
        changed = db.set_collapsed(gid, True)
        self.assertGreater(changed.revision, after_group)
        self.assertEqual(self.db().read_revision(), changed.revision)

    def test_multiple_processes_can_write_concurrently(self) -> None:
        """真并发写：每个进程各置顶一条，一条都不能少。"""
        script = (
            "import sys;"
            f"sys.path.insert(0, {str(Path(split_layout.__file__).resolve().parents[1])!r});"
            "from pickup.split_layout import SidebarLayoutDB;"
            "SidebarLayoutDB().toggle_session_pin(sys.argv[1])"
        )
        keys = [f"claude:p{i}" for i in range(8)]
        env = dict(os.environ, PICKUP_CACHE_DIR=str(self.cache_dir))

        def run(key: str) -> int:
            return subprocess.run(
                [sys.executable, "-c", script, key], env=env, timeout=60,
            ).returncode

        with ThreadPoolExecutor(max_workers=len(keys)) as pool:
            codes = list(pool.map(run, keys))
        self.assertEqual(codes, [0] * len(keys))
        self.assertEqual(sorted(self.db().read().pinned_session_keys), sorted(keys))

    def test_imports_legacy_json_once_without_touching_the_files(self) -> None:
        """旧版两个 JSON 只导入一次，且**不得改名或删除**。

        升级期间机器上很可能还开着跑旧代码的窗口，它仍在往那两个文件里写；动它们
        既会互相打架，也会让回退到旧版本时凭空丢记忆。
        """
        layout_file = self.cache_dir / "split-layout.json"
        layout_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "last_project": "/p",
                    "last_focus_key": "claude:a",
                    "pinned_session_keys": {"claude:solo": 100.0},
                    "groups": {
                        "g1": {
                            "project_cwd": "/p",
                            "session_keys": ["claude:a", "codex:b"],
                            "focus_key": "claude:a",
                            "name": "Group Kiwi",
                            "collapsed": True,
                        }
                    },
                    # 陈旧/矛盾的反向索引不得覆盖 groups 真相
                    "session_to_group": {"claude:ghost": "g1", "claude:a": "wrong"},
                },
            ),
            encoding="utf-8",
        )
        (self.cache_dir / "ui-prefs.json").write_text(
            json.dumps({"version": 1, "sidebar_visible": False}), encoding="utf-8",
        )

        loaded = self.db().read()
        self.assertEqual(loaded.groups["g1"].session_keys, ["claude:a", "codex:b"])
        self.assertEqual(loaded.groups["g1"].name, "Group Kiwi")
        self.assertTrue(loaded.groups["g1"].collapsed)
        self.assertEqual(loaded.session_to_group, {"claude:a": "g1", "codex:b": "g1"})
        self.assertIsNone(loaded.get_group("claude:ghost"))
        self.assertIn("claude:solo", loaded.pinned_session_keys)
        self.assertFalse(self.db().sidebar_visible())

        self.assertTrue(layout_file.exists())
        self.assertFalse((self.cache_dir / "split-layout.json.migrated").exists())

        # 再开一次库不得重复导入：这次删掉库里的组，旧文件仍在也不该被灌回来
        db = self.db()
        db.remove_session("codex:b")
        self.assertEqual(self.db().read().groups, {})

    def test_ignores_legacy_files_outside_the_overridden_cache_dir(self) -> None:
        """设了缓存目录覆盖就只认那个目录，绝不回落到真实家目录。"""
        with tempfile.TemporaryDirectory() as other:
            with mock.patch.object(split_layout, "CACHE_DIR", other):
                Path(other, "split-layout.json").write_text(
                    json.dumps(
                        {
                            "groups": {
                                "g9": {
                                    "project_cwd": "/x",
                                    "session_keys": ["claude:x", "codex:y"],
                                }
                            }
                        },
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(self.db().read().groups, {})

    def test_sidebar_visible_roundtrip(self) -> None:
        from pickup import ui_prefs

        self.assertTrue(ui_prefs.load_sidebar_visible())
        ui_prefs.save_sidebar_visible(False)
        self.assertFalse(ui_prefs.load_sidebar_visible())
        ui_prefs.save_sidebar_visible(True)
        self.assertTrue(ui_prefs.load_sidebar_visible())

    def test_degrades_to_memory_when_db_unavailable(self) -> None:
        """库打不开时界面照常可用，只是本次不落盘，且不得抛异常。"""
        db = split_layout.SidebarLayoutDB(self.cache_dir / "nope" / "x.sqlite3")
        with mock.patch.object(
            split_layout.SidebarLayoutDB, "_open", return_value=None,
        ):
            snapshot = db.toggle_session_pin("claude:a")
            self.assertIn("claude:a", snapshot.pinned_session_keys)
            snapshot = db.set_group("/p", ["claude:m", "codex:n"])
            self.assertEqual(len(snapshot.groups), 1)
            self.assertTrue(db.sidebar_visible())
            db.set_sidebar_visible(False)
            self.assertFalse(db.sidebar_visible())
            self.assertGreater(db.read_revision(), 0)


class ReconcileSplitKeysTests(unittest.TestCase):
    """分屏格按 keepalive 名对齐 session_key：同名歧义时谁都不迁（真实事故：
    扫描串台后两条会话挂了同一个托管名，后写的那条把分屏格改绑到错的会话，
    会话组被拆掉）。
    """

    def test_ambiguous_keepalive_name_is_not_used_for_migration(self) -> None:
        from pickup.ui.controllers.layout_controller import LayoutControllerMixin

        class _Spec:
            def __init__(self, key: str, name: str) -> None:
                self.session_key = key
                self.keepalive_name = name

        class _Area:
            def __init__(self) -> None:
                self._specs = [_Spec("pi:aaa", "pickup-pi-bbb"), _Spec("pi:bbb", "pickup-pi-bbb")]
                self.reconciled: dict[str, str] | None = None

            def pane_specs(self):
                return list(self._specs)

            def reconcile_session_keys(self, mapping):
                self.reconciled = dict(mapping)

        class _Store:
            sessions = {
                "pi": [
                    {"source": "pi", "id": "aaa", "keepalive_name": "pickup-pi-bbb"},
                    {"source": "pi", "id": "bbb", "keepalive_name": "pickup-pi-bbb"},
                ],
            }
            hosted = {"pi:bbb": "pickup-pi-bbb"}

        class _Host(LayoutControllerMixin):
            def __init__(self) -> None:
                self.store = _Store()
                self._area = _Area()

            def _split_area(self):
                return self._area

            def _apply_layout_change(self, mutate):
                raise AssertionError("歧义名不得触发任何迁移写入")

        host = _Host()
        migrated = host._reconcile_split_session_keys()
        self.assertEqual(migrated, {})
        self.assertNotIn("pickup-pi-bbb", host._area.reconciled)

    def test_unique_keepalive_name_still_migrates(self) -> None:
        from pickup.ui.controllers.layout_controller import LayoutControllerMixin

        class _Spec:
            def __init__(self, key: str, name: str) -> None:
                self.session_key = key
                self.keepalive_name = name

        class _Area:
            def __init__(self) -> None:
                self._specs = [_Spec("pi:placeholder", "pickup-pi-ccc")]
                self.reconciled: dict[str, str] | None = None

            def pane_specs(self):
                return list(self._specs)

            def reconcile_session_keys(self, mapping):
                self.reconciled = dict(mapping)

        class _Store:
            sessions = {
                "pi": [{"source": "pi", "id": "ccc", "keepalive_name": "pickup-pi-ccc"}],
            }
            hosted = {}

        class _Host(LayoutControllerMixin):
            def __init__(self) -> None:
                self.store = _Store()
                self._area = _Area()

            def _split_area(self):
                return self._area

            def _apply_layout_change(self, mutate):
                return None

        host = _Host()
        migrated = host._reconcile_split_session_keys()
        self.assertEqual(migrated, {"pi:placeholder": "pi:ccc"})
        self.assertEqual(host._area.reconciled.get("pickup-pi-ccc"), "pi:ccc")


class DedupeKeepaliveNameTests(unittest.TestCase):
    def test_store_keeps_hosted_owner_when_two_sessions_share_a_name(self) -> None:
        from pickup.store import SessionStore

        store = SessionStore(limit=5)
        store.hosted = {"pi:aaa": "pickup-pi-aaa"}
        owner = {"source": "pi", "id": "aaa", "keepalive_name": "pickup-pi-aaa"}
        other = {"source": "pi", "id": "bbb", "keepalive_name": "pickup-pi-aaa"}
        by_key = {"pi:aaa": owner, "pi:bbb": other}
        store._dedupe_keepalive_names(by_key)
        self.assertEqual(owner.get("keepalive_name"), "pickup-pi-aaa")
        self.assertNotIn("keepalive_name", other)
        self.assertNotIn("pi:bbb", store.hosted)

    def test_duplicate_keepalive_only_embeds_once(self) -> None:
        from pickup.ui.controllers.layout_controller import LayoutControllerMixin

        owner = {
            "source": "pi", "id": "aaa", "keepalive_name": "pickup-pi-aaa", "live": True,
        }
        other = {
            "source": "pi", "id": "bbb", "keepalive_name": "pickup-pi-aaa", "live": True,
        }

        class _Store:
            def find_session(self, key):
                return {"pi:aaa": owner, "pi:bbb": other}.get(key)

        class _Host(LayoutControllerMixin):
            def __init__(self) -> None:
                self.store = _Store()
                self._preview_gen = 0
                self.warmed: list[str] = []

            def _detail_renderer_for(self, session):
                return lambda: session["id"]

            def _warm_conversation(self, session, _gen):
                self.warmed.append(session["id"])

        host = _Host()
        entries = host._build_hosted_entries(["pi:aaa", "pi:bbb"])
        self.assertEqual(entries[0][0]["id"], "aaa")
        self.assertEqual(entries[0][1], "pickup-pi-aaa")
        self.assertIsNone(entries[0][2])
        self.assertEqual(entries[1][0]["id"], "bbb")
        self.assertIsNone(entries[1][1])
        self.assertIsNotNone(entries[1][2])
        self.assertEqual(host.warmed, ["bbb"])


if __name__ == "__main__":
    unittest.main()
