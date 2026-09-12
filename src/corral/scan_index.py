"""Cross-process shared scan index.

One process's completed ``scan_all`` result can be reused by others (TUI windows
and ``corral remote``) so the same history is not re-read from disk every few
seconds. The index is acceleration only: any failure or coverage miss falls
through to a local scan.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from corral.cache import cache_dir
from corral.cache import enabled as cache_enabled
from corral.legacy_names import getenv

INDEX_VERSION = 1
# Covering indexes (publisher scanned at least as deep as the consumer) stay
# usable this long. Consumers still force a local scan on their own cadence
# (see SessionStore._FULL_MERGE_INTERVAL) so new sessions cannot stall forever.
DEFAULT_TTL_SECONDS = 12.0


def _shared_index_enabled() -> bool:
    """Shared index is off when the derived cache is off, or under test isolation.

    ``CORRAL_ISOLATE_MANAGED_HOSTS=1`` (set by ``ci-test.py``) must also disable
    this file: otherwise a real-disk publish from one test is consumed by the
    next mock-based ``scan_all`` and fixtures fill with the developer's sessions.
    """
    if not cache_enabled():
        return False
    isolate = (getenv("ISOLATE_MANAGED_HOSTS", "") or "").strip().lower()
    return isolate not in {"1", "true", "yes", "on"}


def index_path():
    return cache_dir() / "scan-index.json"


def try_consume(
    limit: int,
    keep_ids_by_runtime: dict[str, set[str]] | None = None,
    *,
    max_age: float = DEFAULT_TTL_SECONDS,
) -> dict[str, list[dict[str, Any]]] | None:
    """Return a deep-copy of a fresh covering index, or ``None`` to scan locally."""
    if not _shared_index_enabled():
        return None
    try:
        path = index_path()
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
        return None
    try:
        published_at = float(payload.get("published_at") or 0)
        published_limit = int(payload.get("limit") or 0)
    except (TypeError, ValueError):
        return None
    if published_limit < limit:
        return None
    age = time.time() - published_at
    if age < 0 or age > max_age:
        return None
    sessions = payload.get("sessions")
    if not isinstance(sessions, dict):
        return None
    keep_ids_by_runtime = keep_ids_by_runtime or {}
    for runtime_id, keep_ids in keep_ids_by_runtime.items():
        if not keep_ids:
            continue
        bucket = sessions.get(runtime_id)
        if not isinstance(bucket, list):
            return None
        present = {str(item.get("id") or "") for item in bucket if isinstance(item, dict)}
        if not set(keep_ids) <= present:
            return None
    narrowed: dict[str, list[dict[str, Any]]] = {}
    for runtime_id, bucket in sessions.items():
        if not isinstance(bucket, list):
            continue
        keep_ids = keep_ids_by_runtime.get(runtime_id) or set()
        narrowed[runtime_id] = _narrow_bucket(bucket, limit, keep_ids)
    return narrowed


def publish(
    scanned: dict[str, list[dict[str, Any]]],
    *,
    limit: int,
    keep_ids_by_runtime: dict[str, set[str]] | None = None,
) -> None:
    """Atomically replace the shared index after a successful local scan."""
    if not _shared_index_enabled():
        return
    try:
        keep_payload = {
            runtime_id: sorted(ids)
            for runtime_id, ids in (keep_ids_by_runtime or {}).items()
            if ids
        }
        payload = {
            "version": INDEX_VERSION,
            "published_at": time.time(),
            "limit": int(limit),
            "keep_ids": keep_payload,
            "sessions": {
                runtime_id: [dict(session) for session in bucket]
                for runtime_id, bucket in scanned.items()
            },
        }
        path = index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — index is optional acceleration
        return


def _narrow_bucket(
    sessions: list,
    limit: int,
    keep_ids: set[str],
) -> list[dict[str, Any]]:
    """Re-apply a smaller limit onto a covering publisher result."""
    keep_ids = {str(item) for item in keep_ids}
    ranked = sorted(
        (item for item in sessions if isinstance(item, dict)),
        key=lambda session: float(session.get("file_mtime") or session.get("mtime") or 0),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for session in ranked:
        session_id = str(session.get("id") or "")
        if session_id in seen:
            continue
        if len(out) < limit or session_id in keep_ids:
            out.append(dict(session))
            seen.add(session_id)
    for session in ranked:
        session_id = str(session.get("id") or "")
        if session_id in keep_ids and session_id not in seen:
            out.append(dict(session))
            seen.add(session_id)
    return out
