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
# ``appendFileSync`` 写完即关，扫描经常赶不上打开瞬间。记忆必须落到磁盘：
# pickup 一重启内存表是空的，否则侧栏标题停在旧卡、新历史被标成 Ended。
_pid_session_override: dict[int, str] = {}
_pid_override_started: dict[int, float] = {}
_prev_write_bytes: dict[int, int] = {}
_prev_cpu_ticks: dict[int, int] = {}
_prev_file_mtimes: dict[str, float] = {}
_LIVE_MAP_NAME = "pi-live-pids.json"
# 空闲进程认领 /new：新文件创建距旧 ident 最后活动不超过这个间隔。
_IDLE_NEW_MAX_GAP = 90 * 60
# CPU 仍在跑、但绑着的旧 ident 已明显比另一条未绑定历史更旧，才跟过去。
_STALE_NEWER_SLACK = 60.0


def _live_map_path() -> str:
    override = os.environ.get("PICKUP_CACHE_DIR")
    if override:
        return os.path.join(os.path.expanduser(override), _LIVE_MAP_NAME)
    root = os.environ.get("XDG_CACHE_HOME")
    base = os.path.expanduser(root) if root else os.path.expanduser("~/.cache")
    return os.path.join(base, "pickup", _LIVE_MAP_NAME)


def _read_live_map() -> dict[str, str]:
    path = _live_map_path()
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if key and value}


def _write_live_map(data: dict[str, str]) -> None:
    path = _live_map_path()
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=0)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _persist_key(pid: int, started: float) -> str:
    return f"{pid}:{int(started)}"


def _lookup_persisted_id(data: dict[str, str], pid: int, started: float) -> str | None:
    exact = data.get(_persist_key(pid, started))
    if exact:
        return exact
    prefix = f"{pid}:"
    best: str | None = None
    best_gap = 3.0
    for key, value in data.items():
        if not key.startswith(prefix):
            continue
        try:
            stored = int(key.split(":", 1)[1])
        except (ValueError, IndexError):
            continue
        gap = abs(float(stored) - started)
        if gap < best_gap:
            best_gap = gap
            best = value
    return best


def _pid_write_bytes(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/io", encoding="utf-8") as file:
            for line in file:
                if line.startswith("write_bytes:"):
                    return int(line.split(":", 1)[1])
    except (OSError, ValueError):
        return None
    return None


def _pid_cpu_ticks(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as file:
            parts = file.read().split()
        return int(parts[13]) + int(parts[14])
    except (OSError, ValueError, IndexError):
        return None


def _session_last_ts(session: dict) -> float:
    for key in ("event_time", "file_mtime", "mtime"):
        value = session.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return 0.0


def _session_cwd(session: dict) -> str:
    cwd = str(session.get("cwd") or "")
    if not cwd:
        return ""
    try:
        return os.path.realpath(cwd)
    except OSError:
        return cwd


def _remember_live_session(pid: int, session_id: str, started: float | None = None) -> None:
    if not session_id:
        return
    _pid_session_override[pid] = session_id
    if started is None:
        started = _pid_override_started.get(pid)
    if started is None:
        started = process_start_time(pid)
    if started is None:
        return
    _pid_override_started[pid] = started
    data = _read_live_map()
    prefix = f"{pid}:"
    data = {key: value for key, value in data.items() if not key.startswith(prefix)}
    data[_persist_key(pid, started)] = session_id
    _write_live_map(data)


def _load_persisted_overrides(pids: list[int], starts: dict[int, float]) -> None:
    data = _read_live_map()
    if not data:
        return
    live = set(pids)
    keep: dict[str, str] = {}
    for pid in pids:
        started = starts.get(pid)
        if started is None:
            continue
        session_id = _lookup_persisted_id(data, pid, started)
        if not session_id:
            continue
        _pid_session_override.setdefault(pid, session_id)
        _pid_override_started[pid] = started
        keep[_persist_key(pid, started)] = session_id
    stale_keys = [
        key for key in data
        if key.split(":", 1)[0].isdigit() and int(key.split(":", 1)[0]) not in live
    ]
    if stale_keys:
        for key in stale_keys:
            data.pop(key, None)
        data.update(keep)
        _write_live_map(data)


def reset_live_session_overrides() -> None:
    """单测隔离：清掉进程内记住的 Pi 会话切换与跨轮 IO 快照。"""
    _pid_session_override.clear()
    _pid_override_started.clear()
    _prev_write_bytes.clear()
    _prev_cpu_ticks.clear()
    _prev_file_mtimes.clear()


def _follow_switched_sessions(
    sessions: list[dict],
    tui_procs: list[tuple[int, str]],
    created_ts: dict[str, float],
    starts: dict[int, float],
    bound_source: dict[int, str],
    rebind_to,
) -> None:
    """启动 ident 绑上之后，把进程内 /new 换出来的新文件认领回来。

    appendFileSync 太短，单轮经常看不到打开的 jsonl；pickup 重启后内存表也是
    空的。用三层不靠猜「同目录最新一条」的证据：跨轮写字节对上刚更新的文件、
    仍在跑 CPU 且旧 ident 已明显更旧、空闲进程在 90 分钟窗口内一对一认领。
    """
    file_mtimes = {
        str(session.get("id") or ""): float(session.get("file_mtime") or session.get("mtime") or 0)
        for session in sessions
        if session.get("id")
    }
    write_now = {pid: _pid_write_bytes(pid) for pid, _cwd in tui_procs}
    cpu_now = {pid: _pid_cpu_ticks(pid) for pid, _cwd in tui_procs}

    grown_pids = [
        pid for pid, _cwd in tui_procs
        if write_now.get(pid) is not None
        and pid in _prev_write_bytes
        and int(write_now[pid] or 0) > _prev_write_bytes[pid]
    ]
    grown_sessions = [
        session for session in sessions
        if str(session.get("id") or "")
        and file_mtimes.get(str(session.get("id") or ""), 0)
        > _prev_file_mtimes.get(str(session.get("id") or ""), 0)
    ]
    if len(grown_pids) == 1 and len(grown_sessions) == 1:
        pid = grown_pids[0]
        session = grown_sessions[0]
        current = next((item for item in sessions if item.get("pid") == pid), None)
        if current is not session:
            rebind_to(pid, session)

    _prev_write_bytes.clear()
    _prev_write_bytes.update(
        {pid: value for pid, value in write_now.items() if value is not None}
    )
    _prev_file_mtimes.clear()
    _prev_file_mtimes.update(file_mtimes)

    live_by_pid = {item.get("pid"): item for item in sessions if item.get("pid")}
    cpu_active = [
        pid for pid, _cwd in tui_procs
        if cpu_now.get(pid) is not None
        and pid in _prev_cpu_ticks
        and int(cpu_now[pid] or 0) > _prev_cpu_ticks[pid]
    ]
    _prev_cpu_ticks.clear()
    _prev_cpu_ticks.update(
        {pid: value for pid, value in cpu_now.items() if value is not None}
    )

    def unbound_in_cwd(cwd: str) -> list[dict]:
        return [
            session for session in sessions
            if not session.get("live") and _session_cwd(session) == cwd
        ]

    for pid, cwd in tui_procs:
        if pid not in cpu_active:
            continue
        current = live_by_pid.get(pid)
        if current is None:
            continue
        bound_last = _session_last_ts(current)
        started = starts.get(pid) or 0.0
        candidates = []
        for session in unbound_in_cwd(cwd):
            created = created_ts.get(str(session.get("id") or ""), 0.0)
            if created > 0 and started and created + _CREATE_AFTER_START_SLACK < started:
                continue
            last_ts = _session_last_ts(session)
            if last_ts > bound_last + _STALE_NEWER_SLACK:
                candidates.append(session)
        if len(candidates) != 1 and candidates:
            candidates = [max(candidates, key=_session_last_ts)]
        if len(candidates) == 1:
            rebind_to(pid, candidates[0])
            live_by_pid[pid] = candidates[0]

    claimed: set[int] = set()
    newcomers = [
        session for session in sessions
        if not session.get("live")
    ]
    newcomers.sort(key=lambda item: created_ts.get(str(item.get("id") or ""), 0.0))
    for session in newcomers:
        created = created_ts.get(str(session.get("id") or ""), 0.0)
        if created <= 0:
            continue
        cwd = _session_cwd(session)
        eligible: list[tuple[int, float]] = []
        for pid, proc_cwd in tui_procs:
            if pid in claimed:
                continue
            if proc_cwd != cwd:
                continue
            started = starts.get(pid)
            if started is None or created + _CREATE_AFTER_START_SLACK < started:
                continue
            current = live_by_pid.get(pid)
            if current is None:
                continue
            bound_last = _session_last_ts(current)
            if bound_last <= 0 or created <= bound_last:
                continue
            gap = created - bound_last
            if gap > _IDLE_NEW_MAX_GAP:
                continue
            eligible.append((pid, gap))
        if not eligible:
            continue
        pid, _gap = min(eligible, key=lambda item: item[1])
        rebind_to(pid, session)
        live_by_pid[pid] = session
        claimed.add(pid)


def _apply_live_flags(sessions: list[dict], created_ts: dict[str, float]) -> None:
    """给 Pi 会话列表就地标注 live/pid。

    裸 ``pi`` 不长期持有 jsonl、命令行也不带会话参数，旧实现四条正向路径
    全部落空，侧边栏就把仍在跑的会话当成已结束历史。同目录又常会同时跑
    多个 TUI，禁止再按「cwd → 最新一条」猜测。

    绑定优先级（正向证据优先，禁止「同目录只留最新一条」）：
    1. 进程正打开的 ``*.jsonl``（进程内 ``/new`` 后的当前文件）；
    2. 本进程或磁盘记住的「该 pid 上次在写哪条」（pickup 重启后仍跟上）；
    3. ``--session <path|id>``（原生恢复）；
    4. ``--session-id <id>``（托管新建/分叉钉死的占位 ident）；
    5. 环境变量 ``PICKUP_SESSION_ID`` / ``SC_SESSION_ID`` **精确**等于会话 id；
    6. 仍绑在启动 ident 上时，用跨轮写字节/CPU 与空闲窗口把 /new 后的新文件认领回来；
    7. ``-c`` / ``--continue`` → 该 cwd 尚未标记的最新一条；
    8. 其余 TUI：同一 cwd 里，按「进程启动 ≤ 会话创建」一对一认领。
    """
    processes = list(live_processes("pi"))
    live_pid_set = {pid for pid, _cwd in processes}
    for stale in [pid for pid in _pid_session_override if pid not in live_pid_set]:
        _pid_session_override.pop(stale, None)
        _pid_override_started.pop(stale, None)
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
    starts = {
        pid: started
        for pid, started in ((pid, process_start_time(pid)) for pid, _cwd in tui_procs)
        if started is not None
    }
    _load_persisted_overrides([pid for pid, _cwd in tui_procs], starts)
    open_paths = open_file_paths([pid for pid, _cwd in tui_procs])

    bound_pids: set[int] = set()
    bound_source: dict[int, str] = {}

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

    def bind_and_stop(
        pid: int, session: dict | None, *, remember: bool = False, source: str = "",
    ) -> None:
        if remember and session is not None:
            _remember_live_session(pid, str(session.get("id") or ""), starts.get(pid))
        bound_pids.add(pid)
        if source:
            bound_source[pid] = source

    def rebind_to(pid: int, session: dict) -> None:
        for item in sessions:
            if item.get("pid") == pid and item is not session:
                item["live"] = False
                item["pid"] = None
        session["live"] = True
        session["pid"] = pid
        bind_and_stop(pid, session, remember=True, source="follow")

    def bind_open_jsonl(pid: int) -> bool:
        for path in open_paths.get(pid) or []:
            session = bind_by_id_or_path(pid, path)
            if session is not None:
                bind_and_stop(pid, session, remember=True, source="open")
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
                bind_and_stop(pid, session, source="override")
                return
        cmdline = cmdlines.get(pid) or ""
        session_arg = _flag_value(cmdline, ("--session",))
        if session_arg:
            bind_and_stop(pid, bind_by_id_or_path(pid, session_arg), source="session")
            return
        session_id_arg = _flag_value(cmdline, ("--session-id",))
        if session_id_arg:
            session = by_id.get(session_id_arg)
            if session is not None:
                _mark_live(session, pid)
            bind_and_stop(pid, session, source="session-id")
            return
        env = process_environ(pid)
        ident = env.get("PICKUP_SESSION_ID") or env.get("SC_SESSION_ID") or ""
        if ident in by_id:
            session = by_id[ident]
            _mark_live(session, pid)
            bind_and_stop(pid, session, source="env")
            return
        if ident:
            # 托管占位 ident 尚未落盘、或不在本轮扫描窗口：不要回落到 cwd 配对。
            bound_pids.add(pid)

    for pid, _cwd in tui_procs:
        bind_exact(pid)

    _follow_switched_sessions(
        sessions, tui_procs, created_ts, starts, bound_source, rebind_to,
    )

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
        started = starts.get(pid)
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
