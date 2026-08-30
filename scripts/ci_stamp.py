"""完整检查戳：同一工作区产品代码未改时，发版推送和收尾不再整套重跑。"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

FINGERPRINT_DIRS = ("src", "tests", "scripts", ".githooks", "rust")
FINGERPRINT_FILES = ("pyproject.toml", "Cargo.toml", "Cargo.lock")
_SKIP_DIR_NAMES = {"__pycache__", "target", ".egg-info"}
_SKIP_SUFFIXES = {".pyc", ".so", ".dylib"}


def stamp_path(root: Path) -> Path:
    """戳写在 git 目录里，不进工作区、不进提交。"""
    override = os.environ.get("CORRAL_CI_STAMP")
    if override:
        return Path(override)
    git_dir = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "--git-dir"],
        text=True,
    ).strip()
    path = Path(git_dir)
    if not path.is_absolute():
        path = (root / path).resolve()
    return path / "corral-ci-stamp"


def worktree_fingerprint(root: Path) -> str:
    """按文件内容指纹，不看提交号——先测再提交后仍能对上。"""
    files: list[Path] = []
    for name in FINGERPRINT_FILES:
        path = root / name
        if path.is_file():
            files.append(path)
    for dirname in FINGERPRINT_DIRS:
        base = root / dirname
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part in _SKIP_DIR_NAMES for part in path.parts):
                continue
            if path.suffix in _SKIP_SUFFIXES:
                continue
            files.append(path)
    hasher = hashlib.sha256()
    for path in sorted(files, key=lambda p: path_key(p, root)):
        rel = path_key(path, root).encode()
        hasher.update(rel)
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\n")
    return hasher.hexdigest()


def path_key(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def write_stamp(root: Path, fingerprint: str | None = None) -> Path:
    path = stamp_path(root)
    digest = fingerprint if fingerprint is not None else worktree_fingerprint(root)
    path.write_text(f"fingerprint={digest}\n", encoding="utf-8")
    return path


def read_stamp(path: Path) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("fingerprint="):
            return line.split("=", 1)[1].strip()
    return None


def stamp_matches(root: Path) -> bool:
    recorded = read_stamp(stamp_path(root))
    if not recorded:
        return False
    return recorded == worktree_fingerprint(root)
