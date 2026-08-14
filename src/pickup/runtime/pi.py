"""Pi coding agent 运行时适配器。"""

from __future__ import annotations

from pickup.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from pickup.runtime.base import BaseRuntime, usable_cwd
from pickup.scan import pi as scan_pi

# 与 `--session` / `--continue` / `--resume` / `--no-session` 互斥；`--fork` 可以一起用。
_SESSION_ID_BLOCKING = {"--session", "--continue", "-c", "--resume", "-r", "--no-session"}


def bind_hosted_ident(plan: LaunchPlan, ident: str) -> LaunchPlan:
    """把托管临时 ident 钉成 Pi 的 `--session-id`，让落盘会话 id 与占位卡相同。

    Pi 新建时自己分配 uuidv7，占位卡却用 `keepalive.new_session_ident()` 的 8 位
    ident。历史一落盘，扫描键从 `pi:<ident>` 变成 `pi:<uuid>`：分屏组还记着旧键，
    真实卡出现在组外，侧边栏就变成「组里跑丢 + 组外重复」。`--session-id` 是 Pi
    官方支持的精确 id（可与 `--fork` 并用，不能与 `--session` 并用）。
    """
    if not ident or not plan.argv or plan.argv[0] != "pi":
        return plan
    if "--session-id" in plan.argv or _SESSION_ID_BLOCKING.intersection(plan.argv):
        return plan
    insert_at = 1
    if len(plan.argv) > 1 and plan.argv[1] in ("--approve", "-a"):
        insert_at = 2
    argv = (*plan.argv[:insert_at], "--session-id", ident, *plan.argv[insert_at:])
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
