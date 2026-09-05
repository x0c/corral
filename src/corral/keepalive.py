"""会话保活：把启动计划包进专用 tmux 后端，SSH 断开后进程继续跑。

运行时无关的启动包装层，地位类似 titles.py——不属于任何 runtime 适配器，
`runtime/registry.py` 只负责生成 `LaunchPlan`，本模块负责在执行前后包一层
tmux。使用独立 socket（`-L corral-keepalive`）和专属配置，与用户自己的 tmux
会话/配置完全隔离，不互相污染。

扫描后按 pid 祖先链贴 `keepalive_name` 已迁到 `liveness.annotate`；本模块仍导出
`annotate` 兼容别名。启动包装（wrap / attach / kill / reap_idle）仍在这里。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid

from corral import titles
from corral.legacy_names import LEGACY_SESSION_PREFIX as LEGACY_SESSION_PREFIX
from corral.legacy_names import SESSION_PREFIX as SESSION_PREFIX
from corral.legacy_names import SOCKET_NAME as SOCKET_NAME
from corral.legacy_names import (
    env_is_disabled,
    getenv,
    hosted_env_pairs,
    tmux_argv_for_session,
    tmux_base_argv,
)
from corral.models import LaunchPlan

_DEFAULT_IDLE_HOURS = 2.0
_DEFAULT_MAX_SESSIONS = 12
_DEFAULT_PRESSURE_IDLE_MINUTES = 10.0
_SUBPROCESS_TIMEOUT = 1.5
SUBPROCESS_TIMEOUT = _SUBPROCESS_TIMEOUT

_BASE_ARGV = tmux_base_argv()
BASE_ARGV = _BASE_ARGV


def tmux_argv(session_name: str | None = None) -> tuple[str, ...]:
    """新建用新 socket；对已有会话按名字选对过渡期 socket。"""
    if session_name:
        return tmux_argv_for_session(session_name)
    return _BASE_ARGV

# tmux -f 配置内容内联在代码里，而不是仓库里独立的 .conf 文件：安装产物只包含
# 明确纳入包的数据，独立配置不能依赖源码目录相对路径（曾用独立配置实测过，安装后
# 文件完全缺失，wrap_plan 在真实安装环境里会直接报 `-f` 文件不存在）。改动只改这个
# 常量即可，_ensure_config_file() 会在下次调用时自动把新内容重新落盘覆盖旧文件。
_TMUX_CONFIG = """\
# corral 保活会话专用 tmux 配置：只在 `tmux -L corral-keepalive` 这个独立 socket 上生效，
# 不读取、不影响用户自己的 ~/.tmux.conf。目标是让接入的会话看起来和原生终端
# 一样，感觉不到自己在 tmux 里。

set -g status off
set -g mouse on
set -g default-terminal "tmux-256color"
set -ga terminal-overrides ",*256col*:Tc"
# 内嵌从不 attach 可视客户端，只 capture。控制通道 `tmux -C attach` 走管道，
# 客户端尺寸常是默认 80x24。window-size latest 会把托管窗打回 80 列，右栏格子
# 仍是分屏全宽，观感就是「Claude 只占约 1/3、右侧大块空白」。改成 manual：
# 只有 embed.resize-window 改尺寸。aggressive-resize 对每会话单窗没有意义。
set -g window-size manual
setw -g aggressive-resize off
set -sg escape-time 0
set -g history-limit 10000

# 无前缀直接脱离（保留标准 prefix+d 作为备用）：Ctrl-\\ 在 tmux 接管终端时不会
# 触发本地 SIGQUIT，可以放心用作"离开但保持后台运行"的快捷键。
bind-key -n C-\\\\ detach-client
"""


def _ensure_config_file() -> str:
    """把内联的 tmux 配置落盘到本地缓存目录（`~/.cache/corral`），返回文件路径；内容有变化才重写。"""
    os.makedirs(titles.CACHE_DIR, exist_ok=True)
    path = os.path.join(titles.CACHE_DIR, "keepalive.tmux.conf")
    try:
        with open(path, encoding="utf-8") as f:
            current = f.read()
    except OSError:
        current = None
    if current != _TMUX_CONFIG:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(_TMUX_CONFIG)
        except OSError:
            pass
    return path


ensure_config_file = _ensure_config_file


def _env_disabled(*names: str) -> bool:
    """兼容旧调用：任一完整变量名被置 0 即视为禁用。新代码请用 env_is_disabled。"""
    return any((os.environ.get(name) or "").strip() == "0" for name in names)


env_disabled = _env_disabled


def enabled(disabled_flag: bool = False) -> bool:
    """保活默认开启；命令行开关、环境变量或已身处 tmux/screen 中时关闭。"""
    if disabled_flag:
        return False
    if env_is_disabled("KEEPALIVE"):
        return False
    if os.environ.get("TMUX") or os.environ.get("STY"):
        return False
    if shutil.which("tmux") is None:
        return False
    return True


def _session_name(runtime_id: str, ident: str) -> str:
    return f"{SESSION_PREFIX}{runtime_id}-{ident[:8]}"


session_name = _session_name


def new_session_ident() -> str:
    """空白新会话或跨运行时接力目标在launch前还没有历史会话 id，生成一个临时标识用于命名。"""
    return uuid.uuid4().hex[:8]


def wrap_plan(plan: LaunchPlan, runtime_id: str, ident: str) -> LaunchPlan:
    """把原始启动计划包进 tmux `new-session -A`：会话不存在则创建，已存在则直接接入。"""
    identity_env: list[str] = []
    if runtime_id == "pi":
        from corral import pi_identity
        from corral.runtime.pi import bind_hosted_ident

        plan = bind_hosted_ident(plan, ident)
        # 身份桥：首次需要 Pi 前幂等安装 corral-session-identity 扩展，并给这个
        # pane 注入稳定 instance；claim 由扩展写入，Corral 只按 claim 精确绑定。
        # 安装失败直接抛错中止启动，禁止静默退回 cwd/mtime 猜测。
        pi_identity.ensure_extension_installed()
        identity_env = pi_identity.instance_env_pairs(pi_identity.new_instance_id())
    name = _session_name(runtime_id, ident)
    argv = [*_BASE_ARGV, "-f", _ensure_config_file(), "new-session", "-A", "-s", name]
    if plan.cwd:
        argv += ["-c", plan.cwd]
    argv += hosted_env_pairs(runtime_id, ident)
    argv += identity_env
    argv += ["--", *plan.argv]
    return LaunchPlan(argv=tuple(argv), cwd=None)


def attach_plan(session: dict) -> LaunchPlan | None:
    """会话已在保活中时，返回直接接回现场的启动计划；否则返回 None。"""
    name = session.get("keepalive_name")
    if not name:
        return None
    return LaunchPlan(argv=(*tmux_argv_for_session(name), "attach-session", "-t", name), cwd=None)


def _idle_threshold_hours() -> float:
    raw = getenv("KEEPALIVE_IDLE_HOURS")
    if raw is None:
        return _DEFAULT_IDLE_HOURS
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_IDLE_HOURS


def _max_sessions() -> int:
    raw = getenv("KEEPALIVE_MAX_SESSIONS")
    if raw is None:
        return _DEFAULT_MAX_SESSIONS
    try:
        return int(float(raw))
    except ValueError:
        return _DEFAULT_MAX_SESSIONS


def _pressure_idle_seconds() -> float:
    raw = getenv("KEEPALIVE_PRESSURE_IDLE_MINUTES")
    if raw is None:
        return _DEFAULT_PRESSURE_IDLE_MINUTES * 60.0
    try:
        return max(0.0, float(raw) * 60.0)
    except ValueError:
        return _DEFAULT_PRESSURE_IDLE_MINUTES * 60.0


def _ident_matches_session_id(ident: str, session_id: str) -> bool:
    """托管名末段 ident 是否对得上会话 id（与 liveness 贴名口径一致）。"""
    if not ident or not session_id:
        return False
    if session_id == ident or session_id.startswith(ident):
        return True
    compact = session_id.replace("-", "")
    return compact.startswith(ident) or ident.startswith(compact[:8])


def _is_working_keepalive(name: str, working_pairs: list[tuple[str, str]]) -> bool:
    """关注状态 phase=working 的会话不得被压力回收。"""
    from corral import liveness

    parsed = liveness._parse_managed_session_name(name)
    if parsed is None:
        return False
    runtime_id, ident = parsed
    for rid, sid in working_pairs:
        if rid != runtime_id:
            continue
        if _ident_matches_session_id(ident, sid):
            return True
    return False


def _load_working_pairs() -> list[tuple[str, str]]:
    from corral.attention import AttentionStore

    return AttentionStore().working_pairs()


def kill(name: str) -> bool:
    """手动/自动回收指定保活会话；不存在或 tmux 不可用时静默失败。"""
    if shutil.which("tmux") is None:
        return False
    try:
        subprocess.run(
            [*tmux_argv_for_session(name), "kill-session", "-t", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_SUBPROCESS_TIMEOUT, check=False,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def reap_idle(now: float | None = None) -> list[str]:
    """关闭空闲超过阈值（默认 2 小时，`CORRAL_KEEPALIVE_IDLE_HOURS=0` 禁用）的保活会话。

    会话历史仍在各自运行时的磁盘记录里，关闭的只是 tmux 后台进程，不丢数据。
    """
    threshold_hours = _idle_threshold_hours()
    if threshold_hours <= 0:
        return []
    from corral import liveness

    rows = liveness._list_tmux_sessions("#{session_name}|#{session_activity}")
    if not rows:
        return []
    if now is None:
        now = time.time()
    reaped = []
    for row in rows:
        if len(row) < 2:
            continue
        name, activity_text = row[0], row[1]
        try:
            activity = float(activity_text)
        except ValueError:
            continue
        if now - activity > threshold_hours * 3600 and kill(name):
            reaped.append(name)
    return reaped


def reap_pressure(now: float | None = None) -> list[str]:
    """托管数超过软上限时，关掉闲置够久且非「执行中」的会话。

    默认上限 12（`CORRAL_KEEPALIVE_MAX_SESSIONS`，`0` 禁用）；候选须 tmux
    无活动超过默认 10 分钟（`CORRAL_KEEPALIVE_PRESSURE_IDLE_MINUTES`），且关注
    状态不是 working。按空闲最久优先，关到 ≤ 上限或没有合格候选为止——软上限，
    不会拦新建。
    """
    max_sessions = _max_sessions()
    if max_sessions <= 0:
        return []
    from corral import liveness

    rows = liveness._list_tmux_sessions("#{session_name}|#{session_activity}")
    if not rows:
        return []
    if now is None:
        now = time.time()
    sessions: list[tuple[str, float]] = []
    for row in rows:
        if len(row) < 2:
            continue
        name, activity_text = row[0], row[1]
        try:
            activity = float(activity_text)
        except ValueError:
            continue
        sessions.append((name, activity))
    if len(sessions) <= max_sessions:
        return []

    idle_needed = _pressure_idle_seconds()
    working_pairs = _load_working_pairs()
    candidates = [
        (name, activity)
        for name, activity in sessions
        if (now - activity) > idle_needed and not _is_working_keepalive(name, working_pairs)
    ]
    candidates.sort(key=lambda item: item[1])  # 空闲最久（activity 最小）优先

    reaped: list[str] = []
    remaining = len(sessions)
    for name, _activity in candidates:
        if remaining <= max_sessions:
            break
        if kill(name):
            reaped.append(name)
            remaining -= 1
    return reaped


def reap(now: float | None = None) -> list[str]:
    """先按时长空闲回收，再按软上限压力回收；返回本轮关掉的托管名。"""
    return [*reap_idle(now=now), *reap_pressure(now=now)]


def __getattr__(name: str):
    """`annotate` 已迁到 liveness；按需导入以保持 `keepalive.annotate is liveness.annotate`。"""
    if name == "annotate":
        from corral import liveness

        value = liveness.annotate
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

