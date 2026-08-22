#!/usr/bin/env python3
"""标题生成器抽象:把「用哪个 CLI 无头生成标题」与标题业务逻辑解耦。

标题生成是独立服务,不属于任何运行时适配器(见 AGENTS.md 架构约束),
本模块只依赖标准库,不 import runtime/。titles.py 负责批量 prompt 构建、
结果解析和缓存;本模块的每个生成器只负责一次无头 CLI 调用并交回原始文本。

覆盖范围与 corral 默认运行时注册表对齐：claude / codex / opencode / kimi / pi / cursor。
本机装了哪个助手，标题生成就可以用哪个；每批随机起点轮转，失败时自动切换到其余可用助手。

选择策略:
- 环境变量 CORRAL_TITLE_GENERATOR 显式指定首选（旧名 SC_TITLE_GENERATOR 仍生效）；
- 未指定或指定的不可用时,返回本机已安装的全部候选；实际批次由 titles.py 随机起点轮转，不设默认优先级；
- 环境变量 CORRAL_TITLE_MODEL 显式指定标题生成使用的模型（旧名 SC_TITLE_MODEL 仍生效）；
  未设置时继承各助手自己的全局默认。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod

from corral.legacy_names import getenv

ENV_GENERATOR = "CORRAL_TITLE_GENERATOR"
ENV_MODEL = "CORRAL_TITLE_MODEL"


def _env(suffix: str) -> str:
    """按 CORRAL_ → PICKUP_ → SC_ 取第一个已设置的值。"""
    return (getenv(suffix) or "").strip()


def _run(
    argv: list[str],
    input_text: str | None,
    timeout: int,
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> str | None:
    """执行一次 CLI 调用并返回 stdout;非零退出、超时或无法启动一律返回 None。"""
    try:
        proc = subprocess.run(
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


class TitleGenerator(ABC):
    """每个标题生成后端需要实现的最小能力。"""

    id: str
    executable: str

    def is_available(self) -> bool:
        return shutil.which(self.executable) is not None

    def _model(self) -> str | None:
        return _env("TITLE_MODEL") or None

    @abstractmethod
    def generate(self, prompt: str, timeout: int) -> str | None:
        """无头调用一次 CLI,返回模型原始文本输出;失败返回 None。"""


class ClaudeTitleGenerator(TitleGenerator):
    id = "claude"
    executable = "claude"

    def generate(self, prompt: str, timeout: int) -> str | None:
        # 标题是一次性派生数据，不得写进 Claude 会话历史污染用户的真实会话列表。
        argv = ["claude", "-p", "--no-session-persistence"]
        model = self._model()
        if model:
            argv += ["--model", model]
        return _run(argv, prompt, timeout)


class CodexTitleGenerator(TitleGenerator):
    id = "codex"
    executable = "codex"

    def generate(self, prompt: str, timeout: int) -> str | None:
        # stdout 混着事件日志,最终答复用 -o 落到临时文件读取;
        # --ephemeral 不落盘会话文件,避免自产噪音污染 Codex 历史扫描。
        fd, out_path = tempfile.mkstemp(prefix="corral-title-", suffix=".txt")
        os.close(fd)
        try:
            argv = [
                "codex", "exec",
                "--skip-git-repo-check", "--ephemeral",
                "-s", "read-only", "--color", "never",
                "-o", out_path,
            ]
            model = self._model()
            if model:
                argv += ["-m", model]
            argv.append("-")  # prompt 从 stdin 读
            if _run(argv, prompt, timeout) is None:
                return None
            try:
                with open(out_path, encoding="utf-8") as f:
                    return f.read()
            except OSError:
                return None
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass


def _opencode_user_data_dir() -> str:
    """解析用户真实的 OpenCode 数据目录（登录凭证所在处），不读扫描器。"""
    data_dir = os.environ.get("OPENCODE_DATA_DIR", "").strip()
    if data_dir:
        return data_dir.split(",")[0].strip()
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = xdg if xdg else os.path.expanduser("~/.local/share")
    return os.path.join(base, "opencode")


def _seed_isolated_opencode_dir(dest: str) -> None:
    """把登录凭证拷进隔离目录，避免改数据目录后标题生成找不到账号。"""
    src = _opencode_user_data_dir()
    for name in ("auth.json", "mcp-auth.json"):
        origin = os.path.join(src, name)
        if os.path.isfile(origin):
            try:
                shutil.copy2(origin, os.path.join(dest, name))
            except OSError:
                pass


class OpenCodeTitleGenerator(TitleGenerator):
    id = "opencode"
    executable = "opencode"

    def generate(self, prompt: str, timeout: int) -> str | None:
        # `opencode run` 无头执行一次；--auto 跳过权限问询。
        # 官方没有 --ephemeral：默认会写入用户的 opencode.db，一次性任务
        # 会变成侧边栏新卡（还常套用被总结那条的标题），滤掉后又消失，
        # 表现为列表自己乱跳。每次调用改到临时数据目录 + `--dir`，
        # 登录凭证从用户目录拷过来。
        with tempfile.TemporaryDirectory(prefix="corral-title-opencode-") as tmp:
            _seed_isolated_opencode_dir(tmp)
            env = os.environ.copy()
            env["OPENCODE_DATA_DIR"] = tmp
            argv = ["opencode", "run", "--auto", "--dir", tmp]
            model = self._model()
            if model:
                argv += ["-m", model]
            argv.append(prompt)
            return _run(argv, None, timeout, env=env, cwd=tmp)


class KimiTitleGenerator(TitleGenerator):
    id = "kimi"
    executable = "kimi"

    def generate(self, prompt: str, timeout: int) -> str | None:
        # `-p` 非交互打印；`-y` 跳过权限问询。会落盘会话，扫描侧过滤 PROMPT_MARKER。
        argv = ["kimi", "-y", "-p", prompt]
        model = self._model()
        if model:
            argv = ["kimi", "-y", "--model", model, "-p", prompt]
        return _run(argv, None, timeout)


class PiTitleGenerator(TitleGenerator):
    id = "pi"
    executable = "pi"

    def generate(self, prompt: str, timeout: int) -> str | None:
        # --no-session 不落盘会话；--no-tools 保证标题请求不调用工具；
        # --approve 遵循免权限打断的产品默认，--print 只输出一次结果。
        return _run(
            ["pi", "--approve", "--no-session", "--no-tools", "--print", prompt],
            None,
            timeout,
        )


class CursorTitleGenerator(TitleGenerator):
    id = "cursor"
    executable = "agent"
    # 与 CursorRuntime.executable_aliases 对齐：官方主名 agent，兼容名 cursor-agent。
    _aliases = ("agent", "cursor-agent")
    def is_available(self) -> bool:
        return any(shutil.which(name) for name in self._aliases)

    def _exe(self) -> str:
        for name in self._aliases:
            if shutil.which(name):
                return name
        return self.executable

    def generate(self, prompt: str, timeout: int) -> str | None:
        # `-p` 无头打印；`--mode ask` 只读问答，避免标题请求去改文件；
        # `--force`/`--trust` 跳过权限与工作区信任问询（用户约定默认免打断）。
        argv = [
            self._exe(),
            "-p",
            "--mode", "ask",
            "--output-format", "text",
            "--trust",
            "--force",
        ]
        model = self._model()
        if model:
            argv += ["--model", model]
        argv.append(prompt)
        return _run(argv, None, timeout)


# 候选集合与 runtime/registry.default_registry 对齐；实际生成时由调用方公平轮转。
_GENERATORS: tuple[TitleGenerator, ...] = (
    ClaudeTitleGenerator(),
    CodexTitleGenerator(),
    OpenCodeTitleGenerator(),
    KimiTitleGenerator(),
    CursorTitleGenerator(),
    PiTitleGenerator(),
)


def available_generators() -> tuple[TitleGenerator, ...]:
    """返回全部可用生成器；显式指定时仅将其置前，其余保持同等候选资格。"""
    configured = _env("TITLE_GENERATOR").lower()
    available = tuple(generator for generator in _GENERATORS if generator.is_available())
    preferred = next((generator for generator in available if generator.id == configured), None)
    if preferred is None:
        return available
    return (preferred, *(generator for generator in available if generator is not preferred))


def resolve_generator() -> TitleGenerator | None:
    """兼容旧调用：返回候选列表的首项。批量生成不使用此函数决定选路。"""
    generators = available_generators()
    return generators[0] if generators else None
