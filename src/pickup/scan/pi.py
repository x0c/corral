"""读取 Pi coding agent 的 JSONL 会话（默认位于 ``~/.pi/agent/sessions``）。"""

from __future__ import annotations

import json
import os

from pickup import titles
from pickup.models import ConversationMessage, SessionInfo, effective_session_time, make_session_info
from pickup.scan.common import parse_timestamp

PI_HOME = os.path.expanduser("~/.pi/agent")
SESSIONS_DIR = os.path.join(PI_HOME, "sessions")


def _text(content: object) -> str:
    """只取 Pi message content 中可展示的 text 分片，忽略 thinking 和工具调用。"""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        value = part.get("text")
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n\n".join(parts)


def _read_entries(path: str) -> list[dict]:
    entries: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            for line in file:
                try:
                    item = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(item, dict):
                    entries.append(item)
    except OSError:
        pass
    return entries


def _active_messages(entries: list[dict]) -> list[dict]:
    """从当前叶子沿 parentId 回溯，避免预览已分叉出去的旧分支。"""
    by_id = {str(item["id"]): item for item in entries if isinstance(item.get("id"), str)}
    parents = {str(item["parentId"]) for item in entries if isinstance(item.get("parentId"), str)}
    leaves = [item for item in by_id.values() if str(item.get("id")) not in parents]
    if not leaves:
        return []
    leaf = max(leaves, key=lambda item: str(item.get("timestamp") or ""))
    path: list[dict] = []
    while isinstance(leaf, dict):
        path.append(leaf)
        parent_id = leaf.get("parentId")
        leaf = by_id.get(parent_id) if isinstance(parent_id, str) else None
    path.reverse()
    return [item for item in path if item.get("type") == "message" and isinstance(item.get("message"), dict)]


def _build_session_info(path: str) -> SessionInfo | None:
    entries = _read_entries(path)
    if not entries or entries[0].get("type") != "session":
        return None
    header = entries[0]
    session_id = str(header.get("id") or "")
    if not session_id:
        return None
    cwd = str(header.get("cwd") or "")
    branch = _active_messages(entries)
    first_user = last_user = last_agent = None
    last_role = None
    event_time = parse_timestamp(header.get("timestamp"))
    for item in branch:
        message = item["message"]
        role = message.get("role")
        text = _text(message.get("content"))
        timestamp = parse_timestamp(item.get("timestamp")) or parse_timestamp(message.get("timestamp"))
        if timestamp is not None:
            event_time = timestamp
        if role == "user" and text:
            first_user = first_user or text
            last_user = text
            last_role = "user"
        elif role == "assistant" and text:
            last_agent = text
            last_role = "assistant"
    if not first_user:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    native_title = next(
        (str(item.get("name")) for item in entries if item.get("type") == "session_info" and item.get("name")),
        None,
    )
    mtime, time_source = effective_session_time(stat.st_mtime, event_time)
    status = (
        titles.STATUS_PENDING
        if last_role == "user"
        else titles.STATUS_DONE
        if last_role == "assistant"
        else titles.STATUS_NONE
    )
    return make_session_info(
        source="pi", id=session_id, short_id=session_id[:12], cwd=cwd, mtime=mtime,
        time_source=time_source, event_time=event_time, file_mtime=stat.st_mtime,
        size_bytes=stat.st_size, native_title=native_title,
        fallback_title=(native_title or first_user)[:60], status_tag=status, path=path,
        first_user_msg=first_user, last_user_msg=last_user, last_agent_msg=last_agent,
    )


def scan_sessions(cwd_filter: str | None = None, limit: int = 50) -> list[SessionInfo]:
    if not os.path.isdir(SESSIONS_DIR):
        return []
    candidates: list[tuple[float, str]] = []
    for root, _dirs, names in os.walk(SESSIONS_DIR):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(root, name)
            try:
                candidates.append((os.stat(path).st_mtime, path))
            except OSError:
                continue
    results: list[SessionInfo] = []
    for _mtime, path in sorted(candidates, reverse=True):
        info = _build_session_info(path)
        if info is None or (cwd_filter and not info["cwd"].startswith(cwd_filter)):
            continue
        results.append(info)
        if len(results) >= limit:
            break
    return results


def load_conversation(path: str) -> list[ConversationMessage]:
    result: list[ConversationMessage] = []
    for item in _active_messages(_read_entries(path)):
        message = item["message"]
        role = message.get("role")
        text = _text(message.get("content"))
        if role not in ("user", "assistant") or not text:
            continue
        timestamp = parse_timestamp(item.get("timestamp")) or parse_timestamp(message.get("timestamp"))
        result.append(ConversationMessage(role, text, timestamp))
    return result
