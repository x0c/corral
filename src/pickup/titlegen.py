#!/usr/bin/env python3
"""标题生成器抽象:把「用哪个 CLI 无头生成标题」与标题业务逻辑解耦。

标题生成是独立服务,不属于任何运行时适配器(见 AGENTS.md 架构约束),
本模块只依赖标准库,不 import runtime/。titles.py 负责批量 prompt 构建、
结果解析和缓存;本模块的每个生成器只负责一次无头 CLI 调用并交回原始文本。

覆盖范围与 pickup 默认运行时注册表对齐：claude / codex / opencode / kimi / cursor。
本机装了哪个助手，标题生成就可以用哪个；首选失败时按优先级自动切换到下一个。

选择策略:
- 环境变量 PICKUP_TITLE_GENERATOR 显式指定首选（旧名 SC_TITLE_GENERATOR 仍生效）；
- 未指定或指定的不可用时,按注册顺序取本机已安装的全部候选；
- 环境变量 PICKUP_TITLE_MODEL 覆盖生成器默认模型（旧名 SC_TITLE_MODEL 仍生效）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod

ENV_GENERATOR = "PICKUP_TITLE_GENERATOR"
ENV_MODEL = "PICKUP_TITLE_MODEL"
LEGACY_ENV_GENERATOR = "SC_TITLE_GENERATOR"  # 项目改名 sessionContinue → pickup 前的旧变量名
LEGACY_ENV_MODEL = "SC_TITLE_MODEL"


def _env(*names: str) -> str:
    """按优先级取第一个非空的环境变量值（新名在前、旧名兜底）。"""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _run(argv: list[str], input_text: str | None, timeout: int) -> str | None:
    """执行一次 CLI 调用并返回 stdout;非零退出、超时或无法启动一律返回 None。"""
    try:
        proc = subprocess.run(argv, input=input_text, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


class TitleGenerator(ABC):
    """每个标题生成后端需要实现的最小能力。"""

    id: str
    executable: str
    default_model: str | None = None

    def is_available(self) -> bool:
        return shutil.which(self.executable) is not None

    def _model(self) -> str | None:
        return _env(ENV_MODEL, LEGACY_ENV_MODEL) or self.default_model

    @abstractmethod
    def generate(self, prompt: str, timeout: int) -> str | None:
        """无头调用一次 CLI,返回模型原始文本输出;失败返回 None。"""


class ClaudeTitleGenerator(TitleGenerator):
    id = "claude"
    executable = "claude"
    default_model = "haiku"  # 标题生成用最便宜的模型即可

    def generate(self, prompt: str, timeout: int) -> str | None:
        # 标题是一次性派生数据，不得写进 Claude 会话历史污染用户的真实会话列表。
        return _run([
            "claude", "-p", "--no-session-persistence",
            "--model", self._model(),
        ], prompt, timeout)


class CodexTitleGenerator(TitleGenerator):
    id = "codex"
    executable = "codex"
    default_model = None  # 不带 -m,用用户 codex 配置里的默认模型

    def generate(self, prompt: str, timeout: int) -> str | None:
        # stdout 混着事件日志,最终答复用 -o 落到临时文件读取;
        # --ephemeral 不落盘会话文件,避免自产噪音污染 Codex 历史扫描。
        fd, out_path = tempfile.mkstemp(prefix="pickup-title-", suffix=".txt")
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


class OpenCodeTitleGenerator(TitleGenerator):
    id = "opencode"
    executable = "opencode"
    default_model = None

    def generate(self, prompt: str, timeout: int) -> str | None:
        # `opencode run` 无头执行一次；--auto 跳过权限问询。
        # 每次调用会真实落一条会话，扫描侧必须用 titles.PROMPT_MARKER 过滤。
        argv = ["opencode", "run", "--auto"]
        model = self._model()
        if model:
            argv += ["-m", model]
        argv.append(prompt)
        return _run(argv, None, timeout)


class KimiTitleGenerator(TitleGenerator):
    id = "kimi"
    executable = "kimi"
    default_model = None

    def generate(self, prompt: str, timeout: int) -> str | None:
        # `-p` 非交互打印；`-y` 跳过权限问询。会落盘会话，扫描侧过滤 PROMPT_MARKER。
        argv = ["kimi", "-y", "-p", prompt]
        model = self._model()
        if model:
            argv = ["kimi", "-y", "--model", model, "-p", prompt]
        return _run(argv, None, timeout)


class CursorTitleGenerator(TitleGenerator):
    id = "cursor"
    executable = "agent"
    # 与 CursorRuntime.executable_aliases 对齐：官方主名 agent，兼容名 cursor-agent。
    _aliases = ("agent", "cursor-agent")
    default_model = None

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


# 顺序与 runtime/registry.default_registry 对齐：本机有哪些就按这个优先序用。
_GENERATORS: tuple[TitleGenerator, ...] = (
    ClaudeTitleGenerator(),
    CodexTitleGenerator(),
    OpenCodeTitleGenerator(),
    KimiTitleGenerator(),
    CursorTitleGenerator(),
)


def available_generators() -> tuple[TitleGenerator, ...]:
    """返回可用生成器的优先顺序，首选失败时可继续降级。"""
    configured = _env(ENV_GENERATOR, LEGACY_ENV_GENERATOR).lower()
    available = tuple(generator for generator in _GENERATORS if generator.is_available())
    preferred = next((generator for generator in available if generator.id == configured), None)
    if preferred is None:
        return available
    return (preferred, *(generator for generator in available if generator is not preferred))


def resolve_generator() -> TitleGenerator | None:
    """兼容旧调用：返回当前优先级最高的可用生成器。"""
    generators = available_generators()
    return generators[0] if generators else None
