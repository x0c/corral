"""读取 Pi coding agent 的 JSONL 会话（默认位于 ``~/.pi/agent/sessions``）。"""

from __future__ import annotations

import json
import os
import re

from pickup import titles
from pickup.models import ConversationMessage, SessionInfo, effective_session_time, make_session_info
from pickup.scan.common import (
    live_processes,
    open_file_paths,
    parse_timestamp,
    process_command_line,
    process_environ,
)

PI_HOME = os.path.expanduser("~/.pi/agent")
SESSIONS_DIR = os.path.join(PI_HOME, "sessions")
# `{ISO时间戳把 :. 换成 -}_{sessionId}.jsonl`
_SESSION_BASENAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z_(.+)\.jsonl$",
    re.IGNORECASE,
)


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
    _apply_live_flags(results)
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


def delete_session(path: str) -> None:
    """彻底删除一条 Pi 会话的独立 JSONL 历史。

    Pi 每条会话各自对应一个 JSONL 文件，不与其他会话共享存储；删除该文件不会
    连带删除同一工作目录下的其他会话。文件已不存在时视为操作已经完成。
    """
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _flag_value(cmdline: str, names: tuple[str, ...]) -> str | None:
    """取命令行里 ``--session <值>`` 这类「旗标 + 下一个非旗标参数」。"""
    parts = str(cmdline or "").split()
    wanted = set(names)
    for index, part in enumerate(parts):
        if part not in wanted or index + 1 >= len(parts):
            continue
        value = parts[index + 1]
        if value.startswith("-"):
            return None
        return value
    return None


def _session_id_from_path(path: str) -> str | None:
    """从 Pi JSONL 文件名取出 session id；认不出返回 None。"""
    match = _SESSION_BASENAME_RE.match(os.path.basename(path.replace("\\", "/")))
    return match.group(1) if match else None


def _mark_live(session: dict, pid: int) -> bool:
    if session.get("live"):
        return False
    session["live"] = True
    session["pid"] = pid
    return True


def _apply_live_flags(sessions: list[dict]) -> None:
    """给 Pi 会话列表就地标注 live/pid。

    绑定只认正向证据，禁止按 cwd/mtime 猜测（同目录两个新建 Pi 分屏会串台）：
    1. ``--session <path|id>``（原生恢复）；
    2. ``--session-id <id>``（托管新建/分叉钉死的占位 ident）；
    3. 进程正打开的 ``*.jsonl``；
    4. 环境变量 ``PICKUP_SESSION_ID`` / ``SC_SESSION_ID`` **精确**等于会话 id。
    """
    if not sessions:
        return
    processes = list(live_processes("pi"))
    if not processes:
        return
    by_id = {str(session.get("id") or ""): session for session in sessions if session.get("id")}
    by_path: dict[str, dict] = {}
    for session in sessions:
        path = str(session.get("path") or "")
        if not path:
            continue
        try:
            by_path[os.path.realpath(path)] = session
        except OSError:
            by_path[path] = session
    pids = [pid for pid, _cwd in processes]
    open_paths = open_file_paths(pids)
    cmdlines = {pid: process_command_line(pid) for pid, _cwd in processes}

    def bind_by_id_or_path(pid: int, value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        if text in by_id:
            return _mark_live(by_id[text], pid)
        try:
            real = os.path.realpath(os.path.expanduser(text))
        except OSError:
            real = text
        session = by_path.get(real)
        if session is not None:
            return _mark_live(session, pid)
        file_id = _session_id_from_path(text)
        if file_id and file_id in by_id:
            return _mark_live(by_id[file_id], pid)
        matches = [item for item_id, item in by_id.items() if item_id.startswith(text)]
        if len(matches) == 1:
            return _mark_live(matches[0], pid)
        return False

    for pid, _cwd in processes:
        cmdline = cmdlines.get(pid) or ""
        session_arg = _flag_value(cmdline, ("--session",))
        if session_arg and bind_by_id_or_path(pid, session_arg):
            continue
        session_id_arg = _flag_value(cmdline, ("--session-id",))
        if session_id_arg and session_id_arg in by_id and _mark_live(by_id[session_id_arg], pid):
            continue
        bound = False
        for path in open_paths.get(pid) or []:
            if bind_by_id_or_path(pid, path):
                bound = True
                break
        if bound:
            continue
        env = process_environ(pid)
        ident = env.get("PICKUP_SESSION_ID") or env.get("SC_SESSION_ID") or ""
        if ident in by_id:
            _mark_live(by_id[ident], pid)
