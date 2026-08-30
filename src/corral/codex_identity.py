"""Codex 托管会话的创建回执：只接受包装器报告的完整 thread id。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

CLAIM_DIR = Path.home() / ".cache" / "corral" / "codex-claims"
CLAIM_PATH_ENV = "CORRAL_CODEX_CLAIM_PATH"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

def claim_env_pairs(ident: str) -> list[str]:
    CLAIM_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = CLAIM_DIR / f"{ident}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return ["-e", f"{CLAIM_PATH_ENV}={path}"]

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
