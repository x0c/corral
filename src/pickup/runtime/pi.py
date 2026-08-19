"""Pi coding agent 运行时适配器。"""

from __future__ import annotations

import os

from pickup.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from pickup.runtime.base import BaseRuntime, usable_cwd
from pickup.scan import pi as scan_pi

# 与 `--session` / `--continue` / `--resume` / `--no-session` 互斥；`--fork` 可以一起用。
_SESSION_ID_BLOCKING = {"--session", "--continue", "-c", "--resume", "-r", "--no-session"}


def _insert_after_approve(argv: tuple[str, ...], *items: str) -> tuple[str, ...]:
    insert_at = 1
    if len(argv) > 1 and argv[1] in ("--approve", "-a"):
        insert_at = 2
    return (*argv[:insert_at], *items, *argv[insert_at:])


def _argv_option(argv: tuple[str, ...] | list[str], flag: str) -> str | None:
    args = list(argv)
    try:
        index = args.index(flag)
    except ValueError:
        return None
    if index + 1 >= len(args) or str(args[index + 1]).startswith("-"):
        return None
    return str(args[index + 1])


def resolve_hosted_session_dir(argv: tuple[str, ...], cwd: str | None, ident: str) -> str | None:
    """托管 Pi 应写入的 session-dir：恢复用历史文件所在目录，新建用 pickup-<ident>。"""
    if not argv or argv[0] != "pi" or "--no-session" in argv:
        return None
    existing = _argv_option(argv, "--session-dir")
    if existing:
        return os.path.expanduser(existing)
    session_path = _argv_option(argv, "--session")
    if session_path:
        directory = scan_pi.session_file_dir(session_path)
        if directory:
            return directory
        return os.path.dirname(os.path.abspath(os.path.expanduser(session_path))) or None
    if ident and cwd:
        return scan_pi.hosted_session_dir(cwd, ident)
    return None


def hosted_session_dir_from_plan(plan: LaunchPlan) -> str:
    """从已绑定的启动计划取出 ``--session-dir``，供 tmux 注入环境变量。"""
    return _argv_option(plan.argv, "--session-dir") or ""


def bind_hosted_ident(plan: LaunchPlan, ident: str) -> LaunchPlan:
    """钉托管 ident，并隔离这份进程能写出的 jsonl 目录。

    ``--session-id`` 让首份落盘 id 与占位卡相同（与 ``--session`` 互斥，可与
    ``--fork`` 并用）。``--session-dir`` 是 Pi 官方开关：新建/接力写到
    ``pickup-<ident>/``，恢复则沿用历史文件所在目录，避免同 cwd 多 pane 挤进
    默认堆后被空闲认领串台。
    """
    if not plan.argv or plan.argv[0] != "pi":
        return plan
    argv = plan.argv
    if ident and "--session-id" not in argv and not _SESSION_ID_BLOCKING.intersection(argv):
        argv = _insert_after_approve(argv, "--session-id", ident)
    if "--session-dir" not in argv:
        session_dir = resolve_hosted_session_dir(argv, plan.cwd, ident)
        if session_dir:
            argv = _insert_after_approve(argv, "--session-dir", session_dir)
    return LaunchPlan(argv, plan.cwd)


class PiRuntime(BaseRuntime):
    id = "pi"
    display_name = "Pi"
    executable = "pi"
    history_reading_hint = "Pi coding agent JSONL；首行 session header，沿 message entry 的 parentId 回溯当前活动分支。"
    auto_approve_args = ("--approve",)

    def scan_sessions(self, limit: int) -> list[SessionInfo]:
        return scan_pi.scan_sessions(limit=limit)

    def load_conversation(self, session: SessionInfo) -> list[ConversationMessage]:
        return scan_pi.load_conversation(str(session.get("path") or ""))

    def delete_session(self, session: SessionInfo) -> None:
        scan_pi.delete_session(str(session.get("path") or ""))

    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        return LaunchPlan(
            (self.executable, *self.auto_approve_args, "--session", str(session["path"])),
            usable_cwd(str(session.get("cwd") or "")),
        )

    def build_fork_plan(self, session: SessionInfo) -> LaunchPlan | None:
        return LaunchPlan(
            (self.executable, *self.auto_approve_args, "--fork", str(session["path"])),
            usable_cwd(str(session.get("cwd") or "")),
        )

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        return LaunchPlan(
            (self.executable, *self.auto_approve_args, handoff.render_prompt()),
            usable_cwd(handoff.original_cwd),
        )

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan((self.executable, *self.auto_approve_args), usable_cwd(cwd))
