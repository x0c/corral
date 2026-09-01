"""会话托管存活性与扫描后托管标注（运行时无关）。

本模块不感知任何具体运行时，和 `keepalive` / `embed` 同属运行时无关层。
判活查询（`is_alive` / `note_alive` / `forget_alive`）与扫描后按 pid 祖先链
贴上 `keepalive_name`（`annotate`）都在这里；启动包装仍在 keepalive。

匹配保活会话到已扫描出的会话时，优先走 pid 祖先链：`claude --resume`
之类的原生恢复可能在内部 fork/重新注册进程，pane 里的顶层 pid 未必等于
运行时自己记录的"活跃 pid"。因此用一次 `ps -eo pid,ppid` 建出整机父子
关系表，逐个候选 pid 向上追祖先链，只要能追到某个 tmux pane 的顶层 pid，
就判定命中——对是否发生过 fork 免疫。扫描没标出 pid 时（Pi claim 过期、
jsonl 已关）仍按 ``corral-<runtime>-<ident>`` 唯一命中贴名，避免把还在跑
的托管会话画成 Enter restart。
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

from corral import keepalive
from corral.legacy_names import (
    ALL_SESSION_PREFIXES,
    ALL_SOCKET_NAMES,
    is_managed_session,
    tmux_argv_for_session,
    tmux_base_argv,
)

_CALL_TIMEOUT = 1.5
_MAX_ANCESTOR_DEPTH = 20

# 会话名 → 最近一次「确认它还活着」的单调时钟读数。抓帧、状态查询、开通道
# 成功本身就是存活证据，记下来给界面层复用：切换会话时的活跃判定不必再 fork
# 一次 `has-session`（实测每次约 5ms，分屏几格就乘几，全压在 Textual 主线程上）。
_alive_marks: dict[str, float] = {}
_alive_lock = threading.Lock()


def note_alive(name: str) -> None:
    """登记一次「刚刚确认它活着」。只有真正拿到 tmux 正常响应时才调用。"""
    if not name:
        return
    with _alive_lock:
        _alive_marks[name] = time.monotonic()


def forget_alive(name: str) -> None:
    """确认会话已不存在时清除存活证据，避免缓存把死会话续命。"""
    with _alive_lock:
        _alive_marks.pop(name, None)


def is_alive(name: str, *, max_age: float | None = None) -> bool:
    """托管会话是否还活着（pane 里的进程退出后 tmux 会话随之消失）。

    `max_age` 给出可接受的证据陈旧上限（秒）：这段时间内有过成功抓帧 / 状态查询
    就直接返回 True，不再 fork。判定「会话是否已结束」这类必须拿准的场景一律
    不要传 `max_age`——缓存只能加速「确认活着」，不能替代宣告死亡。
    """
    if max_age is not None and name:
        with _alive_lock:
            marked = _alive_marks.get(name)
        if marked is not None and (time.monotonic() - marked) <= max_age:
            return True
    if shutil.which("tmux") is None:
        return False
    try:
        subprocess.run(
            [*tmux_argv_for_session(name), "has-session", "-t", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_CALL_TIMEOUT, check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        forget_alive(name)
        return False
    note_alive(name)
    return True


def _list_tmux_sessions(fields: str) -> list[list[str]]:
    """列出新旧保活 socket 上的托管会话；某个 socket 还不存在时跳过，不报错。"""
    if shutil.which("tmux") is None:
        return []
    rows: list[list[str]] = []
    seen: set[str] = set()
    for socket in ALL_SOCKET_NAMES:
        try:
            out = subprocess.check_output(
                [*tmux_base_argv(socket), "list-sessions", "-F", fields],
                stderr=subprocess.DEVNULL,
                timeout=keepalive.SUBPROCESS_TIMEOUT,
            ).decode()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        for line in out.splitlines():
            parts = line.split("|")
            if not parts or not is_managed_session(parts[0]) or parts[0] in seen:
                continue
            seen.add(parts[0])
            rows.append(parts)
    return rows


def _build_ppid_map() -> dict[int, int]:
    """一次 `ps -eo pid,ppid` 拿到整机父子关系，供祖先链匹配复用；跨 macOS/Linux 通用。"""
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,ppid"], stderr=subprocess.DEVNULL, timeout=keepalive.SUBPROCESS_TIMEOUT
        ).decode()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}
    mapping: dict[int, int] = {}
    for line in out.splitlines()[1:]:  # 跳过表头
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            mapping[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return mapping


def _is_descendant(pid: int, ancestor_pid: int, ppid_map: dict[int, int]) -> bool:
    current = pid
    for _ in range(_MAX_ANCESTOR_DEPTH):
        if current == ancestor_pid:
            return True
        parent = ppid_map.get(current)
        if parent is None or parent <= 1:
            return False
        current = parent
    return False


def _parse_managed_session_name(name: str) -> tuple[str, str] | None:
    """从 ``corral-pi-abcd1234`` / ``sc-claude-…`` 拆出 (runtime_id, ident)。"""
    for prefix in ALL_SESSION_PREFIXES:
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix):]
        runtime, sep, ident = rest.rpartition("-")
        if sep and runtime and ident:
            return runtime, ident
    return None


def _name_matches_session(name: str, session: dict) -> bool:
    """托管名末段 ident 是否对得上这条会话 id（占位 8 位或完整 id）。

    与 ``store._session_matches_keepalive_ident`` 同口径；liveness 不得 import store。
    """
    ident = str(name or "").rsplit("-", 1)[-1]
    sid = str(session.get("id") or "")
    if not ident or not sid:
        return False
    if sid == ident or sid.startswith(ident):
        return True
    compact = sid.replace("-", "")
    return compact.startswith(ident) or ident.startswith(compact[:8])


def annotate(sessions) -> None:
    """给命中保活的会话就地加上 `keepalive_name` 字段；不生成新列表，不改变顺序。

    同一时刻一个进程只属于一个 pane：已经对上某个 pane 的 pid 不再参与后续
    pane 的匹配（否则进程树嵌套时后通到的 pane 会把名字改写成别人的）。同
    一 pane 也只挂一条会话：扫描若把父进程和子进程绑到两张卡上，祖先链会让
    两张卡都命中同一份 tmux 画面，分屏两格就会一模一样。同一 pane 有多个
    候选时优先「pid 恰好就是 pane 顶层进程」的精确命中，再考虑祖先链。

    扫描没标出 pid 时仍要按 tmux 名贴回去。Pi 的 jsonl 写完即关、claim 过期
    后 `_apply_live_flags` 经常拿不到 pid；旧逻辑这里直接 return，侧栏就把还
    在跑的托管会话画成 Enter restart，回车却走 ``new-session -A`` 接回原进程。
    """
    if not sessions:
        return
    candidates = {s.get("pid"): s for s in sessions if s.get("pid")}
    tmux_sessions = _list_tmux_sessions("#{session_name}|#{pane_pid}")
    if not tmux_sessions:
        return

    assigned_pids: set[int] = set()
    assigned_names: set[str] = set()

    def _claim(name: str, pane_pid: int, *, exact_only: bool) -> None:
        if name in assigned_names:
            return
        matches = [
            (pid, session)
            for pid, session in candidates.items()
            if pid not in assigned_pids
            and (pid == pane_pid or (not exact_only and _is_descendant(pid, pane_pid, ppid_map)))
        ]
        if not matches:
            return
        pid, session = matches[0]
        session["keepalive_name"] = name
        assigned_pids.add(pid)
        assigned_names.add(name)

    if candidates:
        ppid_map = _build_ppid_map()
        # 第一遍只认「pid 恰好就是 pane 顶层进程」的精确命中（进程树嵌套时，深祖
        # 先链命中的可能是外层 pane，精确命中才是进程真正性所在的那个 pane）。
        for row in tmux_sessions:
            if len(row) < 2:
                continue
            name, pane_pid_text = row[0], row[1]
            try:
                pane_pid = int(pane_pid_text)
            except ValueError:
                continue
            _claim(name, pane_pid, exact_only=True)
        # 第二遍祖先链兜底；已对上 pane 的 pid / 名字不再改绑：一个进程只挂一个
        # 名字，一个 pane 也只挂一条会话。
        for row in tmux_sessions:
            if len(row) < 2:
                continue
            name, pane_pid_text = row[0], row[1]
            try:
                pane_pid = int(pane_pid_text)
            except ValueError:
                continue
            _claim(name, pane_pid, exact_only=False)

    _annotate_unmatched_by_session_name(sessions, tmux_sessions, assigned_names)


def _annotate_unmatched_by_session_name(
    sessions, tmux_sessions: list[list[str]], assigned_names: set[str],
) -> None:
    """pid 没贴上时，用 wrap_plan 同款的 ``corral-<runtime>-<ident>`` 名接回还活着的 pane。"""
    unnamed = [session for session in sessions if not session.get("keepalive_name")]
    if not unnamed:
        return
    for row in tmux_sessions:
        if not row:
            continue
        name = row[0]
        if name in assigned_names:
            continue
        parsed = _parse_managed_session_name(name)
        if parsed is None:
            continue
        runtime, _ident = parsed
        matches = [
            session
            for session in unnamed
            if str(session.get("source") or "") == runtime
            and _name_matches_session(name, session)
        ]
        if len(matches) != 1:
            continue
        matches[0]["keepalive_name"] = name
        assigned_names.add(name)
        unnamed.remove(matches[0])


# 在 liveness 加载完成后绑定，避免与 keepalive 顶层互相 import 形成环。
keepalive.annotate = annotate
