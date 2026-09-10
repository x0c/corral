"""Unified share transcript — implemented by SessKit.

``SCHEMA_ID`` is ``sesskit.transcript/v1``. Readers should also accept the
legacy ``corral.share/v1`` alias (``LEGACY_SCHEMA_IDS``).
"""

from __future__ import annotations

from sesskit.transcript import *  # noqa: F403
from sesskit.transcript import EVENT_TYPES, LEGACY_SCHEMA_IDS, SCHEMA_ID, count_events, load_events

__all__ = [
    "EVENT_TYPES",
    "LEGACY_SCHEMA_IDS",
    "SCHEMA_ID",
    "count_events",
    "load_events",
]
