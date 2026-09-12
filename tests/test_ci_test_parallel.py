"""ci-test parallel sharding helpers (no full suite)."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import unittest

SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "ci-test.py"
SPEC = importlib.util.spec_from_file_location("ci_test", SCRIPT_PATH)
assert SPEC and SPEC.loader
ci_test = importlib.util.module_from_spec(SPEC)
# dataclasses need the module registered before exec_module.
sys.modules[SPEC.name] = ci_test
SPEC.loader.exec_module(ci_test)


class CiTestParallelHelpers(unittest.TestCase):
    def test_serial_lane_covers_ui_and_embed(self) -> None:
        self.assertIn("test_ui", ci_test._SERIAL_MODULES)
        self.assertIn("test_embed", ci_test._SERIAL_MODULES)
        self.assertNotIn("test_remote_crypto", ci_test._SERIAL_MODULES)

    def test_discover_modules_lists_test_files(self) -> None:
        modules = ci_test._discover_modules()
        self.assertIn("test_ui", modules)
        self.assertIn("test_ci_stamp", modules)
        self.assertEqual(modules, sorted(modules))

    def test_parse_worker_output_reads_result_line(self) -> None:
        payload = {
            "ok": False,
            "failed_ids": ["test_x.T.test_a"],
            "tests_run": 3,
            "seconds": 1.25,
            "modules": ["test_x"],
        }
        blob = (
            "ok tests...\n"
            f"{ci_test._RESULT_PREFIX}{json.dumps(payload)}\n"
        )
        shard = ci_test._parse_worker_output(blob, ["test_x"], returncode=1)
        self.assertFalse(shard.ok)
        self.assertEqual(shard.failed_ids, ("test_x.T.test_a",))
        self.assertEqual(shard.tests_run, 3)
        self.assertIn("ok tests...", shard.output)
        self.assertNotIn(ci_test._RESULT_PREFIX, shard.output)

    def test_parse_worker_output_missing_marker_is_hard_fail(self) -> None:
        shard = ci_test._parse_worker_output("boom\n", ["test_x"], returncode=1)
        self.assertFalse(shard.ok)
        self.assertEqual(shard.failed_ids, ())
        self.assertIn("boom", shard.output)

    def test_run_modules_loads_top_level_test_module(self) -> None:
        shard = ci_test._run_modules_in_process(["test_i18n"])
        self.assertTrue(shard.ok, shard.output[-500:])
        self.assertGreaterEqual(shard.tests_run, 1)
        self.assertEqual(shard.modules, ("test_i18n",))


if __name__ == "__main__":
    unittest.main()
