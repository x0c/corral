"""从各运行时的本地历史中提取会话关注状态证据。

这里刻意只识别结构化事件，不根据自然语言、问号或文件更新时间猜测状态。
JSONL 只读有界尾部，SQLite 只查询指定会话的少量尾部记录；任何未知格式均
返回 ``unknown``，不能影响正常的会话扫描。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pickup.attention import AttentionEvidence

_JSONL_TAIL_BYTES = 512 * 1024
_JSONL_TAIL_ENTRIES = 768
_DB_TAIL_ROWS = 192
_QUESTION_TOOLS = frozenset({"AskUserQuestion", "request_user_input", "question", "AskQuestion"})


def _evidence(
    phase: str = "unknown",
    *,
    activity_token: str | None = None,
    question_token: str | None = None,
    observed_at: float = 0.0,
) -> AttentionEvidence:
    return AttentionEvidence(
        phase=phase,
        activity_token=activity_token,
        question_token=question_token,
        observed_at=observed_at,
        source="history",
    )


def _token(runtime: str, kind: str, native: Any) -> str | None:
    """只对原生标识或时间做摘要，绝不把正文写入状态存储。"""
    if native is None or native == "":
        return None
    raw = f"{runtime}\0{kind}\0{native}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:24]


def _event_native(entry: dict, *keys: str) -> Any:
    for key in keys:
        value = entry.get(key)
        if value is not None and value != "":
            return value
    return None


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        # Kimi/OpenCode 常用毫秒 epoch；秒 epoch 远小于此阈值。
        return float(value) / 1000 if value > 10_000_000_000 else float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _stable_observed_at(session: dict, path: str = "") -> float:
    """返回不会随重复扫描漂移的历史时间。"""
    for key in ("event_time", "file_mtime", "mtime"):
        value = _timestamp(session.get(key))
        if value is not None:
            return value
    if path:
        try:
            return os.path.getmtime(path)
        except OSError:
            pass
    return 0.0


def _advance_observed(current: float, *values: Any) -> float:
    for value in values:
        parsed = _timestamp(value)
        if parsed is not None:
            return parsed
    return current


def _read_jsonl_tail(path: str) -> list[dict]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as file:
            offset = max(0, size - _JSONL_TAIL_BYTES)
            file.seek(offset)
            data = file.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    lines = data.splitlines()
    if offset and lines:
        lines = lines[1:]
    entries: list[dict] = []
    for line in lines[-_JSONL_TAIL_ENTRIES:]:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _content_parts(entry: dict) -> Iterable[dict]:
    message = entry.get("message")
    if not isinstance(message, dict):
        return ()
    content = message.get("content")
    if not isinstance(content, list):
        return ()
    return (part for part in content if isinstance(part, dict))


def _is_human_claude_user(entry: dict) -> bool:
    if entry.get("type") != "user":
        return False
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") not in (None, "user"):
        return False
    origin = entry.get("origin")
    if origin is None:
        origin = message.get("origin")
    origin_kind = origin.get("kind") if isinstance(origin, dict) else None
    if origin_kind not in (None, "human"):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(part.get("type") == "text" for part in content if isinstance(part, dict))
    return False


def _inspect_claude(session: dict) -> AttentionEvidence:
    path = str(session.get("path") or "")
    entries = _read_jsonl_tail(path)
    if not entries:
        return _evidence(observed_at=_stable_observed_at(session, path))

    live = session.get("live") is True
    phase = "unknown"
    pending: dict[str, str] = {}
    activity_token = None
    observed_at = _stable_observed_at(session, path)

    for entry in entries:
        entry_type = entry.get("type")
        native = _event_native(entry, "uuid", "id", "timestamp")
        if _is_human_claude_user(entry):
            observed_at = _advance_observed(observed_at, entry.get("timestamp"))
            # Claude 的中断标记是固定协议值，不做自然语言近似匹配。
            message = entry.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            if content == "[Request interrupted by user]":
                phase = "idle"
                activity_token = _token("claude", "interrupted", native)
            else:
                phase = "working"

        if entry_type == "assistant":
            has_agent_output = False
            for part in _content_parts(entry):
                part_type = part.get("type")
                if part_type == "text":
                    has_agent_output = True
                if part_type == "tool_use" and part.get("name") == "AskUserQuestion":
                    call_id = str(part.get("id") or "")
                    if call_id:
                        pending[call_id] = _token("claude", "question", call_id) or call_id
                        has_agent_output = True
                elif part_type == "tool_result":
                    pending.pop(str(part.get("tool_use_id") or ""), None)
            if has_agent_output:
                phase = "working"
                activity_token = _token("claude", "assistant", native) or activity_token
                observed_at = _advance_observed(observed_at, entry.get("timestamp"))

        # tool_result 在 Claude 历史中通常包在 type=user 的 content 数组里。
        for part in _content_parts(entry):
            if part.get("type") == "tool_result":
                if pending.pop(str(part.get("tool_use_id") or ""), None) is not None:
                    phase = "working"
                    observed_at = _advance_observed(observed_at, entry.get("timestamp"))

        subtype = entry.get("subtype")
        hook_name = entry.get("hook_name") or entry.get("hookName")
        if (entry_type == "system" and subtype == "turn_duration") or hook_name in {"Stop", "StopFailure"}:
            phase = "idle"
            activity_token = _token("claude", "stop", native) or activity_token
            observed_at = _advance_observed(observed_at, entry.get("timestamp"))

    if pending and live:
        question_token = next(reversed(pending.values()))
        return _evidence(
            "waiting",
            activity_token=activity_token,
            question_token=question_token,
            observed_at=observed_at,
        )
    if phase in {"working", "waiting"} and not live:
        phase = "idle"
    return _evidence(phase, activity_token=activity_token, observed_at=observed_at)


def _inspect_codex(session: dict) -> AttentionEvidence:
    path = str(session.get("path") or "")
    entries = _read_jsonl_tail(path)
    if not entries:
        return _evidence(observed_at=_stable_observed_at(session, path))

    live = session.get("live") is True
    phase = "unknown"
    pending: dict[str, str] = {}
    activity_token = None
    observed_at = _stable_observed_at(session, path)

    for entry in entries:
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("type")
        native = _event_native(payload, "turn_id", "call_id", "id", "completed_at", "started_at")
        if native is None:
            native = entry.get("timestamp")

        relevant = False

        if entry.get("type") == "event_msg" and payload_type == "user_message":
            phase = "working"
            relevant = True
        elif payload_type == "task_started":
            phase = "working"
            relevant = True
        elif payload_type == "task_complete":
            phase = "idle"
            activity_token = _token("codex", "complete", native) or activity_token
            relevant = True
        elif payload_type == "turn_aborted":
            phase = "idle"
            activity_token = _token("codex", "aborted", native) or activity_token
            relevant = True
        elif payload_type == "agent_message":
            phase = "working"
            activity_token = _token("codex", "assistant", native) or activity_token
            relevant = True
        elif payload_type == "function_call" and payload.get("name") == "request_user_input":
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            if call_id:
                pending[call_id] = _token("codex", "question", call_id) or call_id
                relevant = True
        elif payload_type in {"function_call_output", "custom_tool_call_output"}:
            if pending.pop(str(payload.get("call_id") or ""), None) is not None:
                phase = "working"
                relevant = True
        if relevant:
            observed_at = _advance_observed(
                observed_at,
                entry.get("timestamp"),
                payload.get("completed_at"),
                payload.get("started_at"),
            )

    if pending and live:
        return _evidence(
            "waiting",
            activity_token=activity_token,
            question_token=next(reversed(pending.values())),
            observed_at=observed_at,
        )
    if phase in {"working", "waiting"} and not live:
        phase = "idle"
    return _evidence(phase, activity_token=activity_token, observed_at=observed_at)


def _inspect_kimi(session: dict) -> AttentionEvidence:
    path = str(session.get("path") or "")
    try:
        before_stat = os.stat(path)
        before_signature = (before_stat.st_size, before_stat.st_mtime_ns)
    except OSError:
        before_signature = None
    entries = _read_jsonl_tail(path)
    try:
        after_stat = os.stat(path)
        stable_read = before_signature == (after_stat.st_size, after_stat.st_mtime_ns)
    except OSError:
        stable_read = False
    if not entries:
        return _evidence(observed_at=_stable_observed_at(session, path))

    live = session.get("live") is True
    phase = "unknown"
    pending: dict[str, str] = {}
    activity_token = None
    last_structured_type = None
    last_structured_native = None
    observed_at = _stable_observed_at(session, path)

    for entry in entries:
        top_type = entry.get("type")
        event = entry.get("event")
        event = event if isinstance(event, dict) else {}
        event_type = event.get("type")
        native = _event_native(event, "uuid", "toolCallId", "turnId", "messageId")
        if native is None:
            native = _event_native(entry, "uuid", "time")

        if top_type == "turn.prompt":
            phase = "working"
            last_structured_type = top_type
            last_structured_native = native
            observed_at = _advance_observed(observed_at, entry.get("time"))
        elif top_type == "turn.cancel":
            phase = "idle"
            last_structured_type = top_type
            last_structured_native = native
            activity_token = _token("kimi", "cancel", native) or activity_token
            observed_at = _advance_observed(observed_at, entry.get("time"))
        elif top_type == "context.append_loop_event":
            last_structured_type = event_type
            last_structured_native = native
            if event_type in {"tool.call", "tool.result", "content.part", "step.end"}:
                observed_at = _advance_observed(observed_at, entry.get("time"))
            if event_type == "tool.call" and event.get("name") == "AskUserQuestion":
                phase = "working"
                call_id = str(event.get("toolCallId") or event.get("uuid") or "")
                if call_id:
                    pending[call_id] = _token("kimi", "question", call_id) or call_id
            elif event_type == "tool.result":
                if pending.pop(str(event.get("toolCallId") or ""), None) is not None:
                    phase = "working"
            elif event_type == "content.part":
                part = event.get("part")
                if isinstance(part, dict) and part.get("type") == "text":
                    phase = "working"
                    activity_token = _token("kimi", "assistant", native) or activity_token

    # step.end 既可能是中间工具步，也可能是本轮末尾。只有它确为尾部最新结构化
    # 事件且读取前后文件签名稳定时，才保守视为整轮停止；mtime 绝不单独产生状态。
    if last_structured_type == "step.end":
        if stable_read:
            phase = "idle"
            activity_token = _token("kimi", "step_end", last_structured_native) or activity_token

    if pending and live:
        return _evidence(
            "waiting",
            activity_token=activity_token,
            question_token=next(reversed(pending.values())),
            observed_at=observed_at,
        )
    if phase in {"working", "waiting"} and not live:
        phase = "idle"
    return _evidence(phase, activity_token=activity_token, observed_at=observed_at)


def _inspect_pi(session: dict) -> AttentionEvidence:
    """从 Pi 已落盘的完整消息与工具调用判断会话关注状态。

    Pi 只在一轮助手输出完成后写入 assistant 消息，因此 ``stop``、``error`` 和
    ``aborted`` 都是稳定的空闲证据；用户消息、工具调用和工具结果后的下一轮则只在
    进程仍存活时显示为执行中。自定义扩展若使用统一的结构化提问工具名，同样可得到
    等待回答提示。
    """
    path = str(session.get("path") or "")
    entries = _read_jsonl_tail(path)
    if not entries:
        return _evidence(observed_at=_stable_observed_at(session, path))

    live = session.get("live") is True
    phase = "unknown"
    pending: dict[str, str] = {}
    activity_token = None
    observed_at = _stable_observed_at(session, path)

    for entry in entries:
        if entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        native = _event_native(entry, "id", "timestamp")
        timestamp = message.get("timestamp") or entry.get("timestamp")

        if role == "user":
            phase = "working"
            observed_at = _advance_observed(observed_at, timestamp)
            continue

        if role == "toolResult":
            call_id = str(message.get("toolCallId") or "")
            if pending.pop(call_id, None) is not None:
                phase = "working"
                observed_at = _advance_observed(observed_at, timestamp)
            continue

        if role != "assistant":
            continue

        tool_calls = [
            part for part in _content_parts(entry) if part.get("type") == "toolCall"
        ]
        for tool_call in tool_calls:
            if tool_call.get("name") not in _QUESTION_TOOLS:
                continue
            call_id = str(tool_call.get("id") or "")
            if call_id:
                pending[call_id] = _token("pi", "question", call_id) or call_id

        stop_reason = str(message.get("stopReason") or "")
        if tool_calls or stop_reason == "toolUse":
            phase = "working"
        elif stop_reason in {"stop", "error", "aborted", "length"}:
            phase = "idle"
            activity_token = _token("pi", "assistant", native) or activity_token
        observed_at = _advance_observed(observed_at, timestamp)

    if pending and live:
        return _evidence(
            "waiting",
            activity_token=activity_token,
            question_token=next(reversed(pending.values())),
            observed_at=observed_at,
        )
    if phase in {"working", "waiting"} and not live:
        phase = "idle"
    return _evidence(phase, activity_token=activity_token, observed_at=observed_at)


def _connect_ro(path: str, *, immutable: bool = False) -> sqlite3.Connection | None:
    try:
        suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
        connection = sqlite3.connect(f"file:{os.path.abspath(path)}{suffix}", uri=True, timeout=0.15)
        connection.row_factory = sqlite3.Row
        return connection
    except (OSError, sqlite3.Error):
        return None


def _json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, memoryview):
        value = bytes(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return {}
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _inspect_opencode(session: dict) -> AttentionEvidence:
    db_path = str(session.get("path") or "")
    session_id = str(session.get("id") or "")
    observed_at = _stable_observed_at(session, db_path)
    if not db_path or not session_id or not os.path.isfile(db_path):
        return _evidence(observed_at=observed_at)
    connection = _connect_ro(db_path)
    if connection is None:
        return _evidence(observed_at=observed_at)
    try:
        messages = connection.execute(
            "SELECT id, time_created, time_updated, data FROM message "
            "WHERE session_id = ? ORDER BY time_created DESC, id DESC LIMIT ?",
            (session_id, _DB_TAIL_ROWS),
        ).fetchall()
        parts = connection.execute(
            "SELECT id, message_id, time_created, time_updated, data FROM part "
            "WHERE session_id = ? ORDER BY time_created DESC, id DESC LIMIT ?",
            (session_id, _DB_TAIL_ROWS),
        ).fetchall()
    except sqlite3.Error:
        return _evidence(observed_at=observed_at)
    finally:
        connection.close()

    live = session.get("live") is True
    pending: dict[str, str] = {}
    activity_token = None
    relevant_times: list[float] = []
    # 逆序恢复时间顺序，确保回答/完成能消掉此前的问题。
    for row in reversed(parts):
        part = _json_object(row["data"])
        if part.get("type") == "tool" and part.get("tool") in _QUESTION_TOOLS:
            row_time = _timestamp(row["time_updated"]) or _timestamp(row["time_created"])
            if row_time is not None:
                relevant_times.append(row_time)
            call_id = str(part.get("callID") or row["id"] or "")
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = state.get("status")
            if status in {"pending", "running"} and call_id:
                pending[call_id] = _token("opencode", "question", call_id) or call_id
            elif call_id:
                pending.pop(call_id, None)

    phase = "unknown"
    if messages:
        newest = messages[0]
        message = _json_object(newest["data"])
        role = message.get("role")
        native = newest["id"] or newest["time_updated"] or newest["time_created"]
        message_time = _timestamp(newest["time_updated"]) or _timestamp(newest["time_created"])
        if role in {"assistant", "user"} and message_time is not None:
            relevant_times.append(message_time)
        if role == "assistant":
            completed = (message.get("time") or {}).get("completed") if isinstance(message.get("time"), dict) else None
            if message.get("error") or completed is not None or message.get("finish") == "stop":
                phase = "idle"
            elif live:
                phase = "working"
            activity_token = _token("opencode", "assistant", native)
        elif role == "user" and live:
            phase = "working"

    if relevant_times:
        observed_at = max(relevant_times)

    if pending and live:
        return _evidence(
            "waiting",
            activity_token=activity_token,
            question_token=next(reversed(pending.values())),
            observed_at=observed_at,
        )
    return _evidence(phase, activity_token=activity_token, observed_at=observed_at)


def _cursor_store_path(path: str) -> str:
    return os.path.join(path, "store.db") if os.path.isdir(path) else path


def _pb_read_varint(data: bytes, index: int) -> tuple[int, int] | None:
    shift = 0
    value = 0
    while index < len(data):
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, index
        shift += 7
        if shift > 63:
            return None
    return None


def _pb_fields(data: bytes) -> dict[int, list[bytes | int]]:
    """解析一层 protobuf 字段；损坏输入返回已读到的部分，不抛错。"""
    fields: dict[int, list[bytes | int]] = {}
    index = 0
    while index < len(data):
        parsed = _pb_read_varint(data, index)
        if parsed is None:
            break
        tag, index = parsed
        field = tag >> 3
        wire = tag & 7
        if wire == 0:
            parsed = _pb_read_varint(data, index)
            if parsed is None:
                break
            value, index = parsed
            fields.setdefault(field, []).append(value)
        elif wire == 2:
            parsed = _pb_read_varint(data, index)
            if parsed is None:
                break
            length, index = parsed
            end = index + length
            if end > len(data):
                break
            fields.setdefault(field, []).append(data[index:end])
            index = end
        elif wire == 1:
            index += 8
        elif wire == 5:
            index += 4
        else:
            break
    return fields


def _cursor_ask_question_id(data: bytes) -> str | None:
    """从 Cursor 提问专用 protobuf 取出 toolCallId。

    等待用户作答时，AskQuestion 往往只写 field 2 包裹的记录（内层 field 23 为
    题目、field 57 为调用标识），JSON tool-call 要等用户选完才落盘。其它工具的
    同类 field 2 记录没有 field 23，不能当成提问。二进制 DAG（首字节 0x0A）不含
    这两字段，解析后会自然忽略。
    """
    if not data or data[:1] == b"{":
        return None

    def from_fields(fields: dict[int, list[bytes | int]]) -> str | None:
        questions = fields.get(23)
        call_ids = fields.get(57)
        if not questions or not call_ids:
            return None
        raw = call_ids[-1]
        if not isinstance(raw, bytes):
            return None
        call_id = raw.decode("utf-8", errors="replace").strip("\0")
        return call_id or None

    fields = _pb_fields(data)
    found = from_fields(fields)
    if found:
        return found
    for wrapped in fields.get(2, []):
        if isinstance(wrapped, bytes):
            found = from_fields(_pb_fields(wrapped))
            if found:
                return found
    return None


def _inspect_cursor(session: dict) -> AttentionEvidence:
    live = session.get("live") is True
    if not live and session.get("signal_probe") is not True:
        return _evidence(observed_at=_stable_observed_at(session))
    store_path = _cursor_store_path(str(session.get("path") or ""))
    observed_at = _stable_observed_at(session, store_path)
    if not store_path or not os.path.isfile(store_path):
        return _evidence(observed_at=observed_at)
    # 有 WAL 时绝不能 immutable：冷会话若刚结束、最新轮次还在 wal 里，
    # immutable 会读到过期尾巴，关注圆点/已读基线都会偏。
    has_wal = os.path.isfile(store_path + "-wal")
    connection = _connect_ro(store_path, immutable=not live and not has_wal)
    if connection is None:
        return _evidence(observed_at=observed_at)
    try:
        json_rows = connection.execute(
            "SELECT rowid, data FROM blobs WHERE substr(data, 1, 1) = X'7B' "
            "ORDER BY rowid DESC LIMIT ?",
            (_DB_TAIL_ROWS,),
        ).fetchall()
        # 提问等待态常只在 field-2 protobuf 里；与 JSON 分查，避免当前提问被
        # 大量 JSON 尾巴挤出窗口。旧提问的 JSON 结果滚出窗口后，不能只凭还在
        # protobuf 尾巴里就判成仍在等待——见下方 continuation / JSON 窗口过滤。
        proto_rows = connection.execute(
            "SELECT rowid, data FROM blobs WHERE substr(data, 1, 1) = X'12' "
            "ORDER BY rowid DESC LIMIT ?",
            (_DB_TAIL_ROWS,),
        ).fetchall()
    except sqlite3.Error:
        return _evidence(observed_at=observed_at)
    finally:
        connection.close()

    answered: set[str] = set()
    questions: dict[str, str] = {}
    question_rowids: dict[str, int] = {}
    activity_token = None
    continuation_max = 0
    min_json_rowid = min((row["rowid"] for row in json_rows), default=0)

    def _note_question(call_id: str, rowid: int) -> None:
        questions[call_id] = _token("cursor", "question", call_id) or call_id
        question_rowids[call_id] = max(question_rowids.get(call_id, 0), rowid)

    for row in reversed(json_rows):
        rowid = row["rowid"]
        message = _json_object(row["data"])
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, list):
            content = []
        has_text = False
        has_ask_question = False
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            tool_name = part.get("toolName")
            call_id = str(part.get("toolCallId") or "")
            if part_type == "tool-result" and call_id:
                answered.add(call_id)
                if tool_name != "AskQuestion":
                    continuation_max = max(continuation_max, rowid)
            elif part_type == "tool-call" and tool_name == "AskQuestion" and call_id:
                has_ask_question = True
                _note_question(call_id, rowid)
            elif part_type == "tool-call" and call_id:
                continuation_max = max(continuation_max, rowid)
            elif part_type == "text" and str(part.get("text") or "").strip():
                has_text = True
        if has_text and not has_ask_question:
            continuation_max = max(continuation_max, rowid)
        terminal = (
            message.get("stopReason")
            or message.get("finishReason")
            or message.get("stop_reason")
            or message.get("status")
        )
        is_terminal = terminal in {"stop", "stopped", "error", "abort", "aborted", "cancelled"}
        if role == "assistant" and (has_text or message.get("error") or is_terminal):
            kind = "terminal" if message.get("error") or is_terminal else "assistant"
            activity_token = _token("cursor", kind, rowid)

    for row in reversed(proto_rows):
        raw = row["data"]
        if isinstance(raw, memoryview):
            raw = bytes(raw)
        if not isinstance(raw, (bytes, bytearray)):
            continue
        rowid = row["rowid"]
        call_id = _cursor_ask_question_id(bytes(raw))
        if call_id:
            _note_question(call_id, rowid)
        else:
            # 其它工具的 field-2 记录：助手已经在提问之后继续干活。
            continuation_max = max(continuation_max, rowid)

    pending: list[str] = []
    for call_id, token in questions.items():
        if call_id in answered:
            continue
        question_rowid = question_rowids.get(call_id, 0)
        # JSON 窗口已经前移时，更早的 protobuf 提问无法核对是否已答，
        # 不能当成仍在等待。正在等答时，提问记录会新于当前 JSON 尾巴。
        if json_rows and question_rowid < min_json_rowid:
            continue
        if question_rowid < continuation_max:
            continue
        pending.append(token)
    if pending and live:
        return _evidence(
            "waiting",
            activity_token=activity_token,
            question_token=pending[-1],
            observed_at=observed_at,
        )
    # Cursor 的历史库没有可靠执行中标志，永远不从数据库推导 working。
    return _evidence("unknown", activity_token=activity_token, observed_at=observed_at)


_INSPECTORS = {
    "claude": _inspect_claude,
    "codex": _inspect_codex,
    "kimi": _inspect_kimi,
    "opencode": _inspect_opencode,
    "cursor": _inspect_cursor,
    "pi": _inspect_pi,
}


def inspect_session(session: dict) -> AttentionEvidence:
    """提取单个会话的结构化状态证据；未知来源或损坏输入安全降级。"""
    if not isinstance(session, dict):
        return _evidence()
    inspector = _INSPECTORS.get(str(session.get("source") or ""))
    if inspector is None:
        return _evidence()
    try:
        return inspector(session)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return _evidence()
