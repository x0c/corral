"""Codex 托管会话的创建回执：只接受包装器报告的完整 thread id。"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

CLAIM_DIR = Path.home() / ".cache" / "corral" / "codex-claims"
CLAIM_PATH_ENV = "CORRAL_CODEX_CLAIM_PATH"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

def claim_env_pairs(ident: str) -> list[str]:
    CLAIM_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = CLAIM_DIR / f"{ident}.json"
    return ["-e", f"{CLAIM_PATH_ENV}={path}"]


def claim_signature() -> tuple[tuple[str, int, int], ...]:
    """Invalidate cached Codex scans when a pane changes its exact thread."""
    try:
        return tuple(sorted(
            (path.name, stat.st_mtime_ns, stat.st_size)
            for path in CLAIM_DIR.glob("*.json")
            if (stat := path.stat())
        ))
    except OSError:
        return ()


def write_claim(thread_id: str, rollout_path: str, pid: int) -> bool:
    """Record only a verified root thread and its exact local rollout file."""
    claim_path = os.environ.get(CLAIM_PATH_ENV)
    if not claim_path or not _UUID.fullmatch(thread_id):
        return False
    path = Path(rollout_path).resolve()
    root = (Path.home() / ".codex" / "sessions").resolve()
    if not path.is_relative_to(root) or not path.name.endswith(f"{thread_id}.jsonl"):
        return False
    try:
        # Codex returns an exact id/path before the first prompt creates the
        # rollout. Keep the pane provisional until the file appears.
        if path.exists():
            with path.open(encoding="utf-8") as stream:
                header = stream.readline()
            if header:
                first = json.loads(header)
                if first.get("type") != "session_meta" or first.get("payload", {}).get("id") != thread_id:
                    return False
        destination = Path(claim_path)
        if destination.parent.resolve() != CLAIM_DIR.resolve() or destination.is_symlink():
            return False
        CLAIM_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {"thread_id": thread_id, "rollout_path": str(path), "pid": pid}
        fd, temp_path = tempfile.mkstemp(prefix=f".{destination.stem}-", dir=CLAIM_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, destination)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        return True
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False

def live_claims(sessions_dir: str) -> dict[str, int]:
    root = Path(sessions_dir).resolve()
    out: dict[str, int] = {}
    try:
        files = list(CLAIM_DIR.glob("*.json"))
    except OSError:
        return out
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            thread_id = str(data["thread_id"])
            rollout = Path(str(data["rollout_path"])).resolve()
            pid = int(data["pid"])
            suffix = thread_id + ".jsonl"
            if (
                not _UUID.fullmatch(thread_id)
                or not rollout.is_relative_to(root)
                or not rollout.name.endswith(suffix)
            ):
                continue
            os.kill(pid, 0)
            out[thread_id] = pid
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    return out
