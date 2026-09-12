"""HistoryWatcher: FS wake + debounce; no dependency on live assistant dirs."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from corral.history_watch import HistoryWatcher, default_history_roots


class HistoryWatcherTests(unittest.TestCase):
    def test_default_roots_skip_missing(self) -> None:
        with mock.patch("corral.history_watch._history_watch_enabled", return_value=True):
            roots = default_history_roots()
        self.assertTrue(all(p.is_dir() for p in roots))

    def test_isolation_disables_default_roots(self) -> None:
        with mock.patch("corral.history_watch._history_watch_enabled", return_value=False):
            self.assertEqual(default_history_roots(), [])

    def test_empty_roots_never_starts_thread(self) -> None:
        watcher = HistoryWatcher(roots=[])
        watcher.start()
        self.assertIsNone(watcher._thread)
        self.assertEqual(watcher.backend, "none")

    def test_wait_wakes_on_file_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            watcher = HistoryWatcher(roots=[root], debounce=0.05)
            watcher.start()
            # Native backend needs a brief moment to attach.
            time.sleep(0.25)
            if watcher.backend == "none":
                watcher.stop()
                self.skipTest("no fsevents/inotify on this platform")
            (root / "probe.jsonl").write_text("hello\n", encoding="utf-8")
            self.assertTrue(watcher.wait(timeout=2.0))
            watcher.clear()
            self.assertFalse(watcher.is_set())
            watcher.stop()

    def test_debounce_coalesces_bursts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fires: list[float] = []
            watcher = HistoryWatcher(
                roots=[root],
                debounce=0.2,
                on_change=lambda: fires.append(time.monotonic()),
            )
            watcher.start()
            time.sleep(0.25)
            if watcher.backend == "none":
                watcher.stop()
                self.skipTest("no fsevents/inotify on this platform")
            for i in range(5):
                (root / f"burst-{i}.txt").write_text(str(i), encoding="utf-8")
                time.sleep(0.02)
            self.assertTrue(watcher.wait(timeout=2.0))
            time.sleep(0.3)
            self.assertLessEqual(len(fires), 3)
            watcher.stop()

    def test_stop_unblocks_wait(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            watcher = HistoryWatcher(roots=[Path(td)], debounce=0.05)
            watcher.start()
            time.sleep(0.1)

            def _stop_soon() -> None:
                time.sleep(0.05)
                watcher.stop()

            import threading

            threading.Thread(target=_stop_soon, daemon=True).start()
            # stop() sets the event; wait returns True even without a file change.
            self.assertTrue(watcher.wait(timeout=2.0))


class SessionStoreFreshnessTests(unittest.TestCase):
    def test_refresh_records_wall_clock_age(self) -> None:
        from corral.store import SessionStore

        runtime = mock.Mock(id="claude", display_name="Claude")
        registry = mock.MagicMock()
        registry.ids = ("claude",)
        registry.get.return_value = runtime
        registry.__iter__.side_effect = lambda: iter((runtime,))
        registry.scan_all.return_value = {"claude": []}
        registry.last_scan_shared = False
        registry.last_scan_cache_hit_all = False

        with mock.patch("corral.titles.load_cache", return_value={}):
            store = SessionStore(limit=5, registry=registry)
        self.assertIsNone(store.refresh_age_seconds())
        store.load()
        age = store.refresh_age_seconds()
        self.assertIsNotNone(age)
        assert age is not None
        self.assertLess(age, 2.0)
        store.refresh()
        self.assertIsNotNone(store.last_refresh_at)


if __name__ == "__main__":
    unittest.main()
