"""Helpers that keep Corral runtime adapters aligned with SessKit APIs."""

from __future__ import annotations

import inspect
from typing import Any, Callable

from sesskit.models import ConversationMessage, SessionInfo
from sesskit.registry import ConversationLoadError, load_session_conversation


def load_runtime_conversation(session: SessionInfo) -> list[ConversationMessage]:
    """Same session-dict adapter SessKit CLI uses.

    Corral UI/export keep the historical soft-fail behavior (missing history →
    empty list) so a deleted file never crashes the sidebar. SessKit CLI still
    raises ``ConversationLoadError`` for explicit callers.
    """
    try:
        return load_session_conversation(dict(session))
    except ConversationLoadError:
        return []


def call_scan(
    scan_fn: Callable[..., list[SessionInfo]],
    *,
    limit: int,
    keep_ids: set[str] | None = None,
    include_missing_cwd: bool = False,
) -> list[SessionInfo]:
    """Forward only kwargs the SessKit scanner actually accepts."""
    params = inspect.signature(scan_fn).parameters
    kwargs: dict[str, Any] = {"limit": limit}
    if keep_ids is not None and "keep_ids" in params:
        kwargs["keep_ids"] = keep_ids
    if include_missing_cwd and "include_missing_cwd" in params:
        kwargs["include_missing_cwd"] = True
    return scan_fn(**kwargs)
