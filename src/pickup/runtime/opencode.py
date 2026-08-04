"""OpenCode CLI 运行时适配器。"""

from __future__ import annotations

import dataclasses
import os

from pickup.scan import opencode as scan_opencode
from pickup.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from pickup.runtime.base import BaseRuntime, usable_cwd


class OpenCodeRuntime(BaseRuntime):
    id = "opencode"
    display_name = "OpenCode"
    executable = "opencode"
    history_reading_hint = (
        "OpenCode 会话历史存在 SQLite 数据库 opencode.db 里（session/message/part 三表，"
        "正文在 part.data 的 JSON text 字段，助手消息元数据在 message.data）；优先运行 "
        "`opencode export <会话ID>` 导出该会话完整 JSON 阅读，或用只读方式"
        "（sqlite3 \"file:<路径>?mode=ro\"）查询，不要写这个库。"
    )
    # `--auto` 是 OpenCode 官方的放行参数（自动批准所有未被显式拒绝的权限请求），
    # 主命令（TUI）与 `run` 子命令都正式声明了它，因此和其他适配器一样统一走
    # auto_approve_args。历史上这里曾留空：早期版本只有隐藏的
    # --dangerously-skip-permissions 且仅 `run` 接受，主命令的 yargs 严格校验会
    # 因未声明的 flag 直接退出。该限制在现版本已不存在（1.18.9 实测两条命令都声明
    # 了 --auto，旧名作为隐藏别名折叠进同一个开关），装不上 --auto 的旧版本按
    # 「该升级 opencode」处理，不为它保留降级分支。
    auto_approve_args = ("--auto",)
    # OpenCode 的全部子命令（1.18.9 `--help`）。直启 `pickup opencode <词>` 时
    # 首个词命中这里就按透传处理，否则会被当成项目名去模糊匹配。
    SUBCOMMANDS = frozenset(
        (
            "completion", "acp", "mcp", "attach", "run", "debug", "providers",
            "agent", "upgrade", "uninstall", "serve", "web", "models", "stats",
            "export", "import", "github", "pr", "session", "plugin", "db",
        )
    )
    # 认 --auto 的子命令（其余子命令带上它会用法错误退出），见 compose_passthrough_argv。
    _AUTO_APPROVE_SUBCOMMANDS = ("run",)

    def scan_signature(self) -> object | None:
        return scan_opencode.scan_signature()

    def scan_sessions(self, limit: int) -> list[SessionInfo]:
        return scan_opencode.scan_sessions(limit=limit)

    def load_conversation(self, session: SessionInfo) -> list[ConversationMessage]:
        return scan_opencode.load_conversation(
            str(session.get("path") or ""), str(session.get("id") or "")
        )

    def delete_session(self, session: SessionInfo) -> None:
        scan_opencode.delete_session(
            str(session.get("path") or ""), str(session.get("id") or "")
        )

    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "-s",
                str(session["id"]),
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_continue_plan(self, session: SessionInfo, instruction: str) -> LaunchPlan:
        """构造供外部执行器使用的非交互式 OpenCode 原生续接计划。"""
        return LaunchPlan(
            argv=(
                self.executable,
                "run",
                *self.auto_approve_args,
                "-s",
                str(session["id"]),
                instruction,
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        # OpenCode 主命令的位置参数是项目路径，提示词只能通过 --prompt 传入；也没有
        # --add-dir 等价物，读取源历史落在工作目录外时仍会命中「外部目录」权限，
        # 靠 --auto 自动放行。
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "--prompt",
                handoff.render_prompt(),
            ),
            cwd=usable_cwd(handoff.original_cwd),
        )

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan(
            argv=(self.executable, *self.auto_approve_args),
            cwd=usable_cwd(cwd),
        )

    def compose_passthrough_argv(self, user_args: tuple[str, ...]) -> tuple[str, ...]:
        """`pickup opencode …` 直启透传：`--auto` 的位置在 OpenCode 上是有讲究的。

        1. **必须排在子命令之后**。主命令的第一个位置参数是项目路径，`--auto` 一旦
           前置，yargs 就不再把后面的词当子命令：`opencode --auto run …` 会变成
           「在名为 run 的目录里开 TUI」（1.18.9 实测 `opencode --auto stats` 报
           `Failed to change directory to <当前目录>/stats`），用户的命令被静默改成
           了另一件事。
        2. **只有主命令和 `run` 声明了它**。`stats`/`export`/`auth` 等子命令带上它
           会被严格校验判为未知参数、用法错误退出，所以这些路径一律不垫。

        第一个参数不像子命令（是路径、或本来就是 flag、或干脆没有参数）时按主命令
        处理，前置即可。
        """
        if any(arg in self.auto_approve_args for arg in user_args):
            return (self.executable, *user_args)
        head = user_args[0] if user_args else ""
        if head and not head.startswith("-") and not self._looks_like_path(head):
            if head not in self._AUTO_APPROVE_SUBCOMMANDS:
                return (self.executable, *user_args)
            return (self.executable, head, *self.auto_approve_args, *user_args[1:])
        return (self.executable, *self.auto_approve_args, *user_args)

    @staticmethod
    def _looks_like_path(token: str) -> bool:
        """区分「项目路径位置参数」和「子命令名」，前者仍按主命令垫放行参数。"""
        return (
            os.sep in token
            or token.startswith((".", "~"))
            or os.path.isdir(token)
        )

    def export_handoff(self, session: SessionInfo, title: str) -> Handoff:
        """在基类通用实现之上补充会话 ID：历史 db 是全库共享的，没有 ID 无法定位会话。"""
        handoff = super().export_handoff(session, title)
        return dataclasses.replace(
            handoff,
            history_reading_hint=(
                f"{self.history_reading_hint}本次要读取的会话 ID：{session['id']}。"
            ),
        )
