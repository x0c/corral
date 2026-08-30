"""旧 Pi 隔离历史迁移测试：无覆盖、幂等、只认精确主会话。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from corral import pi_migration


def _write_session(path: Path, session_id: str, cwd: str, text: str = "问题") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "type": "session",
            "version": 3,
            "id": session_id,
            "timestamp": "2026-08-26T00:00:00Z",
            "cwd": cwd,
        },
        {
            "type": "message",
            "id": f"u-{session_id}",
            "parentId": None,
            "timestamp": "2026-08-26T00:00:01Z",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        },
    ]
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in rows) + "\n",
        encoding="utf-8",
    )


class MigrationTests(unittest.TestCase):
    def test_copies_exact_main_and_leaves_subagent_and_source_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            root = Path(td)
            cwd = "/Users/example/project"
            legacy = root / "sessions" / "--Users-example-project--" / "corral-main1234"
            main = legacy / "2026-08-26T00-00-00-000Z_main1234.jsonl"
            subagent = legacy / "2026-08-26T00-01-00-000Z_subagent-uuid.jsonl"
            _write_session(main, "main1234", cwd)
            _write_session(subagent, "subagent-uuid", cwd, "内部任务")

            report = pi_migration.migrate_legacy_sessions(
                root, cache, active_dirs=set()
            )

            self.assertEqual(len(report["copied"]), 1)
            destination = Path(report["copied"][0]["destination"])
            self.assertTrue(destination.exists())
            self.assertEqual(destination.read_bytes(), main.read_bytes())
            self.assertTrue(main.exists())
            self.assertTrue(subagent.exists())
            self.assertNotIn("corral-main1234", str(destination.parent))
            self.assertTrue((Path(cache) / "pi-migration-v1.json").exists())

    def test_is_idempotent_when_destination_hash_matches(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            root = Path(td)
            cwd = "/tmp/project"
            legacy = root / "sessions" / "--tmp-project--" / "pickup-abc12345"
            main = legacy / "2026-08-26T00-00-00-000Z_abc12345.jsonl"
            _write_session(main, "abc12345", cwd)
            first = pi_migration.migrate_legacy_sessions(root, cache, active_dirs=set())
            second = pi_migration.migrate_legacy_sessions(root, cache, active_dirs=set())
            self.assertEqual(len(first["copied"]), 1)
            self.assertEqual(len(second["already"]), 1)
            self.assertEqual(second["conflicts"], [])

    def test_different_existing_destination_is_conflict_and_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            root = Path(td)
            cwd = "/Users/example/project"
            source = (
                root / "sessions" / "--Users-example-project--" / "corral-abc12345"
                / "2026-08-26T00-00-00-000Z_abc12345.jsonl"
            )
            _write_session(source, "abc12345", cwd, "旧内容")
            destination = root / "sessions" / "--Users-example-project--" / source.name
            _write_session(destination, "abc12345", cwd, "不同内容")
            before = hashlib.sha256(destination.read_bytes()).hexdigest()

            report = pi_migration.migrate_legacy_sessions(root, cache, active_dirs=set())

            self.assertEqual(len(report["conflicts"]), 1)
            self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(), before)
            self.assertTrue(source.exists())

    def test_active_directory_is_deferred_without_touching_files(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            root = Path(td)
            cwd = "/tmp/project"
            legacy = root / "sessions" / "--tmp-project--" / "corral-live1234"
            source = legacy / "2026-08-26T00-00-00-000Z_live1234.jsonl"
            _write_session(source, "live1234", cwd)

            report = pi_migration.migrate_legacy_sessions(
                root, cache, active_dirs={str(legacy.resolve())}
            )

            self.assertEqual(len(report["deferred"]), 1)
            self.assertEqual(report["copied"], [])
            destination = root / "sessions" / "--tmp-project--" / source.name
            self.assertFalse(destination.exists())

    def test_unmatched_directory_never_guesses_latest_file(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            root = Path(td)
            cwd = "/tmp/project"
            legacy = root / "sessions" / "--tmp-project--" / "corral-main1234"
            _write_session(legacy / "newest.jsonl", "some-subagent", cwd)

            report = pi_migration.migrate_legacy_sessions(root, cache, active_dirs=set())

            self.assertEqual(report["copied"], [])
            self.assertEqual(report["skipped"][0]["reason"], "no-exact-main")


if __name__ == "__main__":
    unittest.main()
