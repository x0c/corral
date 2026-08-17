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
    process_start_time,
)

PI_HOME = os.path.expanduser("~/.pi/agent")
SESSIONS_DIR = os.path.join(PI_HOME, "sessions")
# `{ISO时间戳把 :. 换成 -}_{sessionId}.jsonl`
_SESSION_BASENAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z_(.+)\.jsonl$",
    re.IGNORECASE,
)

# 会话头时间戳在进程启动后一两秒才写入；只允许「创建时间 ≥ 启动时间 - 这个余量」。
_CREATE_AFTER_START_SLACK = 2.0
_NON_TUI_SUBCOMMANDS = frozenset(
    ("install", "remove", "uninstall", "update", "list", "config", "auth")
)
_NON_TUI_FLAGS = frozenset({
    "-p", "--print", "--export", "--list-models", "--help", "-h", "--version", "-v",
})
_VALUE_FLAGS = frozenset(
    {
        "--session", "--session-id", "--fork", "--session-dir",
        "--provider", "--model", "--api-key", "--system-prompt",
        "--append-system-prompt", "--name", "-n", "--mode", "--models",
    }
)


def message_text(content: object) -> str:
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


def read_entries(path: str) -> list[dict]:
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


def active_messages(entries: list[dict]) -> list[dict]:
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


# 以下旧私有名保留别名：transcript 等核心层已改用公共名，模块内部仍引用。
_text = message_text
_read_entries = read_entries
_active_messages = active_messages


def _build_session_info(path: str) -> tuple[SessionInfo, float] | None:
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
    created = parse_timestamp(header.get("timestamp")) or 0.0
    info = make_session_info(
        source="pi", id=session_id, short_id=session_id[:12], cwd=cwd, mtime=mtime,
        time_source=time_source, event_time=event_time, file_mtime=stat.st_mtime,
        size_bytes=stat.st_size, native_title=native_title,
        fallback_title=(native_title or first_user)[:60], status_tag=status, path=path,
        first_user_msg=first_user, last_user_msg=last_user, last_agent_msg=last_agent,
    )
    return info, created


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
    created_ts: dict[str, float] = {}
    for _mtime, path in sorted(candidates, reverse=True):
        built = _build_session_info(path)
        if built is None:
            continue
        info, created = built
        if cwd_filter and not info["cwd"].startswith(cwd_filter):
            continue
        if created > 0:
            created_ts[str(info["id"])] = created
        results.append(info)
        if len(results) >= limit:
            break
    _apply_live_flags(results, created_ts)
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


def _cmdline_parts_before_prompt(cmdline: str) -> list[str]:
    """第一个非旗标词起是提问；其后不能再当 argv 去取 ``--session`` / ``-c``。

    npm 包装后常见 ``node …/cli.js --approve --session <path>``：脚本路径是位置
    参数，但不是提问，必须跳过，否则恢复旗标会被裁掉。
    """
    parts = str(cmdline or "").split()
    if not parts:
        return []
    kept = [parts[0]]
    index = 1
    argv0 = os.path.basename(parts[0])
    if argv0 in {"node", "nodejs", "bun"} and index < len(parts) and not parts[index].startswith("-"):
        kept.append(parts[index])
        index += 1
    while index < len(parts):
        token = parts[index]
        if token.startswith("--system-prompt=") or token.startswith("--append-system-prompt="):
            kept.append(token)
            break
        if token in _VALUE_FLAGS:
            kept.append(token)
            if index + 1 < len(parts):
                kept.append(parts[index + 1])
                index += 2
            else:
                index += 1
            continue
        if token.startswith("-"):
            kept.append(token)
            index += 1
            continue
        break
    return kept


def _flag_value(cmdline: str, names: tuple[str, ...]) -> str | None:
    """取命令行里 ``--session <值>`` 这类「旗标 + 下一个非旗标参数」。"""
    parts = _cmdline_parts_before_prompt(cmdline)
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


def _is_continue_cmdline(cmdline: str) -> bool:
    return bool(set(_cmdline_parts_before_prompt(cmdline)).intersection({"-c", "--continue"}))


def is_pi_tui_cmdline(cmdline: str) -> bool:
    """交互 TUI 才算会话进程；``-p`` 打印模式与 ``auth`` / ``install`` 等子命令排除。

    跨助手接力把说明写在位置参数里，正文常有 ``list`` / ``install`` 等词；进程
    命令行是空格拼接的，第一个非旗标词若不是子命令，后面整段都是提示词，
    不能再拿去撞子命令表。
    """
    parts = str(cmdline or "").split()
    if not parts:
        return False
    index = 1
    while index < len(parts):
        token = parts[index]
        if token in _NON_TUI_FLAGS or token.startswith("--print="):
            return False
        if token.startswith("--system-prompt=") or token.startswith("--append-system-prompt="):
            return True
        if token in _NON_TUI_SUBCOMMANDS:
            return False
        if token in _VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        # 位置参数是初始提问，不再往后扫子命令。
        return True
    return True


def _mark_live(session: dict, pid: int) -> bool:
    if session.get("live"):
        return False
    session["live"] = True
    session["pid"] = pid
    return True


# 进程内 ``/new`` / ``/resume`` / ``/fork`` 会换一份 jsonl，但启动时的
# ``--session-id`` 与 ``PICKUP_SESSION_ID`` 仍指向旧 ident。Pi 用
# ``appendFileSync`` 写完即关，扫描经常赶不上打开瞬间；TUI 长驻时把「上次
# 看到这个 pid 在写哪条会话」记住，避免侧栏标题停在旧卡、新历史被标成 Ended。
_pid_session_override: dict[int, str] = {}


def _remember_live_session(pid: int, session_id: str) -> None:
    if session_id:
        _pid_session_override[pid] = session_id


def reset_live_session_overrides() -> None:
    """单测隔离：清掉进程内记住的 Pi 会话切换。"""
    _pid_session_override.clear()


def _apply_live_flags(sessions: list[dict], created_ts: dict[str, float]) -> None:
    """给 Pi 会话列表就地标注 live/pid。

    裸 ``pi`` 不长期持有 jsonl、命令行也不带会话参数，旧实现四条正向路径
    全部落空，侧边栏就把仍在跑的会话当成已结束历史。同目录又常会同时跑
    多个 TUI，禁止再按「cwd → 最新一条」猜测。

    绑定优先级（正向证据优先，禁止「同目录只留最新一条」）：
    1. 进程正打开的 ``*.jsonl``（进程内 ``/new`` 后的当前文件）；
    2. 本进程先前观察到该 pid 打开过的会话（文件已关上仍跟上切换）；
    3. ``--session <path|id>``（原生恢复）；
    4. ``--session-id <id>``（托管新建/分叉钉死的占位 ident）；
    5. 环境变量 ``PICKUP_SESSION_ID`` / ``SC_SESSION_ID`` **精确**等于会话 id；
    6. ``-c`` / ``--continue`` → 该 cwd 尚未标记的最新一条；
    7. 其余 TUI：同一 cwd 里，按「进程启动 ≤ 会话创建」一对一认领。
    """
    processes = list(live_processes("pi"))
    live_pid_set = {pid for pid, _cwd in processes}
    for stale in [pid for pid in _pid_session_override if pid not in live_pid_set]:
        _pid_session_override.pop(stale, None)
    if not sessions or not processes:
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
    cmdlines = {pid: process_command_line(pid) for pid, _cwd in processes}
    tui_procs: list[tuple[int, str]] = []
    for pid, cwd in processes:
        cmdline = cmdlines.get(pid) or ""
        if not is_pi_tui_cmdline(cmdline):
            continue
        tui_procs.append((pid, cwd))
    if not tui_procs:
        return
    open_paths = open_file_paths([pid for pid, _cwd in tui_procs])

    bound_pids: set[int] = set()

    def bind_by_id_or_path(pid: int, value: str) -> dict | None:
        text = str(value or "").strip()
        if not text:
            return None
        session = None
        if text in by_id:
            session = by_id[text]
        else:
            try:
                real = os.path.realpath(os.path.expanduser(text))
            except OSError:
                real = text
            session = by_path.get(real)
            if session is None:
                file_id = _session_id_from_path(text)
                if file_id and file_id in by_id:
                    session = by_id[file_id]
            if session is None:
                matches = [item for item_id, item in by_id.items() if item_id.startswith(text)]
                if len(matches) == 1:
                    session = matches[0]
        if session is None:
            return None
        if session.get("live") and session.get("pid") != pid:
            return None
        _mark_live(session, pid)
        return session

    def bind_and_stop(pid: int, session: dict | None, *, remember: bool = False) -> None:
        if remember and session is not None:
            _remember_live_session(pid, str(session.get("id") or ""))
        bound_pids.add(pid)

    def bind_open_jsonl(pid: int) -> bool:
        for path in open_paths.get(pid) or []:
            session = bind_by_id_or_path(pid, path)
            if session is not None:
                bind_and_stop(pid, session, remember=True)
                return True
        return False

    def bind_exact(pid: int) -> None:
        if bind_open_jsonl(pid):
            return
        override_id = _pid_session_override.get(pid)
        if override_id and override_id in by_id:
            session = by_id[override_id]
            if session.get("live") and session.get("pid") != pid:
                _pid_session_override.pop(pid, None)
            else:
                _mark_live(session, pid)
                bind_and_stop(pid, session)
                return
        cmdline = cmdlines.get(pid) or ""
        session_arg = _flag_value(cmdline, ("--session",))
        if session_arg:
            bind_and_stop(pid, bind_by_id_or_path(pid, session_arg))
            return
        session_id_arg = _flag_value(cmdline, ("--session-id",))
        if session_id_arg:
            session = by_id.get(session_id_arg)
            if session is not None:
                _mark_live(session, pid)
            bind_and_stop(pid, session)
            return
        env = process_environ(pid)
        ident = env.get("PICKUP_SESSION_ID") or env.get("SC_SESSION_ID") or ""
        if ident in by_id:
            session = by_id[ident]
            _mark_live(session, pid)
            bind_and_stop(pid, session)
            return
        if ident:
            # 托管占位 ident 尚未落盘、或不在本轮扫描窗口：不要回落到 cwd 配对。
            bound_pids.add(pid)

    for pid, _cwd in tui_procs:
        bind_exact(pid)

    remaining = [(pid, cwd) for pid, cwd in tui_procs if pid not in bound_pids]
    if not remaining:
        return

    continue_by_cwd: dict[str, list[int]] = {}
    unmatched: list[tuple[int, str, float]] = []
    for pid, cwd in remaining:
        cmdline = cmdlines.get(pid) or ""
        if _is_continue_cmdline(cmdline):
            continue_by_cwd.setdefault(cwd, []).append(pid)
            continue
        started = process_start_time(pid)
        if started is None:
            continue
        unmatched.append((pid, cwd, started))

    unbound_by_cwd: dict[str, list[dict]] = {}
    for session in sessions:
        if session.get("live"):
            continue
        cwd = session.get("cwd") or ""
        if not cwd:
            continue
        try:
            real = os.path.realpath(cwd)
        except OSError:
            real = cwd
        unbound_by_cwd.setdefault(real, []).append(session)

    for cwd, pids in continue_by_cwd.items():
        candidates = unbound_by_cwd.get(cwd) or []
        candidates.sort(key=lambda item: item.get("mtime") or 0, reverse=True)
        for pid, session in zip(pids, candidates, strict=False):
            if _mark_live(session, pid):
                bound_pids.add(pid)

    unbound_by_cwd = {
        cwd: [item for item in items if not item.get("live")]
        for cwd, items in unbound_by_cwd.items()
    }
    procs_by_cwd: dict[str, list[tuple[int, float]]] = {}
    for pid, cwd, started in unmatched:
        if pid in bound_pids:
            continue
        procs_by_cwd.setdefault(cwd, []).append((pid, started))

    for cwd, items in unbound_by_cwd.items():
        procs = procs_by_cwd.get(cwd) or []
        if not procs:
            continue
        used: set[int] = set()
        items.sort(key=lambda item: created_ts.get(str(item.get("id") or ""), 0.0))
        for session in items:
            created = created_ts.get(str(session.get("id") or ""), 0.0)
            if created <= 0:
                continue
            eligible = [
                (pid, started) for pid, started in procs
                if pid not in used and started <= created + _CREATE_AFTER_START_SLACK
            ]
            if not eligible:
                continue
            pid, _started = max(eligible, key=lambda item: item[1])
            if _mark_live(session, pid):
                used.add(pid)
