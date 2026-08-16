"""会话托管存活性与扫描后托管标注（运行时无关）。

本模块不感知任何具体运行时，和 `keepalive` / `embed` 同属运行时无关层。
判活查询（`is_alive` / `note_alive` / `forget_alive`）与扫描后按 pid 祖先链
贴上 `keepalive_name`（`annotate`）都在这里；启动包装仍在 keepalive。

匹配保活会话到已扫描出的会话时，不能只靠 tmux 会话名：`claude --resume`
之类的原生恢复可能在内部 fork/重新注册进程，pane 里的顶层 pid 未必等于
运行时自己记录的"活跃 pid"。因此用一次 `ps -eo pid,ppid` 建出整机父子
关系表，逐个候选 pid 向上追祖先链，只要能追到某个 tmux pane 的顶层 pid，
就判定命中——对是否发生过 fork 免疫。
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

from pickup import keepalive

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
            [*keepalive.BASE_ARGV, "has-session", "-t", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_CALL_TIMEOUT, check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        forget_alive(name)
        return False
    note_alive(name)
    return True


def _list_tmux_sessions(fields: str) -> list[list[str]]:
    """列出保活 socket 上的所有会话；socket 尚不存在（还没人保活过）时静默返回空列表。"""
    if shutil.which("tmux") is None:
        return []
    try:
        out = subprocess.check_output(
            [*keepalive.BASE_ARGV, "list-sessions", "-F", fields],
            stderr=subprocess.DEVNULL,
            timeout=keepalive.SUBPROCESS_TIMEOUT,
        ).decode()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("|")
        if not parts or not parts[0].startswith((keepalive.SESSION_PREFIX, keepalive.LEGACY_SESSION_PREFIX)):
            continue
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


def annotate(sessions) -> None:
    """给命中保活的会话就地加上 `keepalive_name` 字段；不生成新列表，不改变顺序。"""
    candidates = {s.get("pid"): s for s in sessions if s.get("pid")}
    if not candidates:
        return  # 没有任何会话带存活 pid，不值得为此打一次 tmux/ps 子进程

    tmux_sessions = _list_tmux_sessions("#{session_name}|#{pane_pid}")
    if not tmux_sessions:
        return

    ppid_map = _build_ppid_map()
    for row in tmux_sessions:
        if len(row) < 2:
            continue
        name, pane_pid_text = row[0], row[1]
        try:
            pane_pid = int(pane_pid_text)
        except ValueError:
            continue
        for pid, session in candidates.items():
            if _is_descendant(pid, pane_pid, ppid_map):
                session["keepalive_name"] = name
                break


# 在 liveness 加载完成后绑定，避免与 keepalive 顶层互相 import 形成环。
keepalive.annotate = annotate
