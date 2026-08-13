"""Pi coding agent 运行时适配器。"""

from __future__ import annotations

from pickup.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from pickup.runtime.base import BaseRuntime, usable_cwd
from pickup.scan import pi as scan_pi


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
