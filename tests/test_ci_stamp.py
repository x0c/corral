import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "ci_stamp.py"
SPEC = importlib.util.spec_from_file_location("ci_stamp", SCRIPT_PATH)
assert SPEC and SPEC.loader
ci_stamp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_stamp)


def _write(root: pathlib.Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class CiStampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.stamp = self.root / "stamp"
        self._old = os.environ.get("CORRAL_CI_STAMP")
        os.environ["CORRAL_CI_STAMP"] = str(self.stamp)
        _write(self.root, "src/pkg.py", "x = 1\n")
        _write(self.root, "tests/test_pkg.py", "assert True\n")
        _write(self.root, "pyproject.toml", "version = \"0\"\n")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("CORRAL_CI_STAMP", None)
        else:
            os.environ["CORRAL_CI_STAMP"] = self._old
        self.tmp.cleanup()

    def test_fingerprint_changes_when_product_code_changes(self):
        first = ci_stamp.worktree_fingerprint(self.root)
        _write(self.root, "src/pkg.py", "x = 2\n")
        self.assertNotEqual(first, ci_stamp.worktree_fingerprint(self.root))

    def test_stamp_matches_until_code_changes(self):
        ci_stamp.write_stamp(self.root)
        self.assertTrue(ci_stamp.stamp_matches(self.root))
        _write(self.root, "tests/test_pkg.py", "assert False\n")
        self.assertFalse(ci_stamp.stamp_matches(self.root))

    def test_docs_outside_fingerprint_do_not_invalidate(self):
        ci_stamp.write_stamp(self.root)
        _write(self.root, "docs/note.md", "hello\n")
        self.assertTrue(ci_stamp.stamp_matches(self.root))

    def test_pycache_is_ignored(self):
        first = ci_stamp.worktree_fingerprint(self.root)
        _write(self.root, "src/__pycache__/pkg.cpython-314.pyc", "junk")
        self.assertEqual(first, ci_stamp.worktree_fingerprint(self.root))

    def test_missing_stamp_does_not_match(self):
        self.assertFalse(ci_stamp.stamp_matches(self.root))

    def test_check_stamp_cli_uses_fingerprint_not_commit(self):
        repo = SCRIPT_PATH.resolve().parents[1]
        digest = ci_stamp.worktree_fingerprint(repo)
        self.stamp.write_text(f"fingerprint={digest}\n", encoding="utf-8")
        script = repo / "scripts" / "ci-test.py"
        matched = subprocess.run(
            [sys.executable, str(script), "--check-stamp"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        self.assertEqual(matched.returncode, 0)
        self.stamp.write_text("fingerprint=deadbeef\n", encoding="utf-8")
        stale = subprocess.run(
            [sys.executable, str(script), "--check-stamp"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        self.assertEqual(stale.returncode, 1)
