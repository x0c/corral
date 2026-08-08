"""运行中终端主题变化监听。

Textual 目前只负责应用自身主题切换，不会把终端返回的 OSC 11 背景色或
DEC 2031 深浅色通知转成事件。本模块在 Unix 终端驱动的输入解析入口补一层：
保留 Textual 原有解析行为，同时把这两类应答变成应用消息。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

from textual.message import Message

_OSC_BACKGROUND_MARKER = "\x1b]11;rgb:"
_MODE_MARKER = "\x1b[?997;"
_OSC_BACKGROUND_RE = re.compile(
    r"\x1b\]11;rgb:[0-9a-fA-F]+/[0-9a-fA-F]+/[0-9a-fA-F]+(?:\x07|\x1b\\)"
)
_MODE_RE = re.compile(r"\x1b\[\?997;([12])n")
_THEME_MARKERS = (_OSC_BACKGROUND_MARKER, _MODE_MARKER)

# iTerm2 不支持 DEC 2031 主动通知，只能定期查询 OSC 11。两秒足够贴近日落切换，
# 同时查询只是很短的终端控制序列，不起进程、不访问网络。
BACKGROUND_POLL_INTERVAL = 2.0


class TerminalBackgroundReport(Message):
    """终端报告了新的背景色或深浅模式。"""

    def __init__(
        self,
        *,
        osc_report: bytes | None = None,
        is_light: bool | None = None,
    ) -> None:
        super().__init__()
        self.osc_report = osc_report
        self.is_light = is_light


if os.name != "nt":
    from textual._xterm_parser import XTermParser
    from textual.drivers import linux_driver
    from textual.drivers.linux_driver import LinuxDriver

    class TerminalThemeParser(XTermParser):
        """从 Textual 输入流中提取主题应答，其余输入原样交回框架。"""

        def __init__(self, debug: bool = False) -> None:
            super().__init__(debug)
            self._theme_pending = ""

        @staticmethod
        def _trailing_marker_prefix(data: str) -> str:
            """返回可能在下一次读取中补全的主题消息开头。"""
            for size in range(min(len(data), max(map(len, _THEME_MARKERS))), 0, -1):
                suffix = data[-size:]
                if any(marker.startswith(suffix) for marker in _THEME_MARKERS):
                    return suffix
            return ""

        def feed(self, data: str) -> Iterable[Message]:
            # 驱动关闭时会用空串让原解析器收尾；未完整的主题应答也交回去，
            # 避免我们吞掉无法确认的普通输入。
            if not data:
                pending, self._theme_pending = self._theme_pending, ""
                if pending:
                    yield from super().feed(pending)
                yield from super().feed(data)
                return

            pending = self._theme_pending + data
            self._theme_pending = ""
            while pending:
                osc_at = pending.find(_OSC_BACKGROUND_MARKER)
                mode_at = pending.find(_MODE_MARKER)
                starts = [index for index in (osc_at, mode_at) if index >= 0]
                if not starts:
                    # TTY 读取不保证控制消息一次到齐。若 ESC / ESC[ / ESC[?997;
                    # 这类前缀被先交给原解析器，后半段会变成普通按键并泄漏到
                    # 当前托管助手，Cursor 会把它画成 ^[[?997;2n。
                    prefix = self._trailing_marker_prefix(pending)
                    if prefix:
                        body = pending[:-len(prefix)]
                        if body:
                            yield from super().feed(body)
                        self._theme_pending = prefix
                        return
                    yield from super().feed(pending)
                    return

                start = min(starts)
                if start:
                    yield from super().feed(pending[:start])
                    pending = pending[start:]

                if pending.startswith(_OSC_BACKGROUND_MARKER):
                    match = _OSC_BACKGROUND_RE.match(pending)
                    if match is None:
                        if (
                            "\x07" in pending
                            or "\x1b\\" in pending
                            or len(pending) > 128
                        ):
                            yield from super().feed(pending)
                            return
                        # 已确认是 OSC 11 应答开头，但结尾可能在下一次 read 才到。
                        self._theme_pending = pending
                        return
                    raw = match.group(0)
                    yield TerminalBackgroundReport(osc_report=raw.encode("ascii"))
                    pending = pending[match.end():]
                    continue

                match = _MODE_RE.match(pending)
                if match is None:
                    if len(pending) >= len(_MODE_MARKER) + 2:
                        yield from super().feed(pending)
                        return
                    self._theme_pending = pending
                    return
                yield TerminalBackgroundReport(is_light=match.group(1) == "2")
                pending = pending[match.end():]


    class TerminalThemeLinuxDriver(LinuxDriver):
        """复用 Textual Unix 驱动，只替换其输入解析器。"""

        def start_application_mode(self) -> None:
            super().start_application_mode()
            enable_runtime_theme_tracking(self)

        def stop_application_mode(self) -> None:
            disable_runtime_theme_tracking(self)
            super().stop_application_mode()

        def run_input_thread(self) -> None:
            original_parser = linux_driver.XTermParser
            linux_driver.XTermParser = TerminalThemeParser
            try:
                super().run_input_thread()
            finally:
                linux_driver.XTermParser = original_parser

else:  # pragma: no cover - Windows 使用 Textual 原生驱动，当前不发送主题查询
    TerminalThemeParser = None  # type: ignore[assignment,misc]
    TerminalThemeLinuxDriver = None  # type: ignore[assignment,misc]


def enhance_driver(driver_class):
    """Unix 默认驱动加上主题应答解析；自定义/Windows 驱动保持原样。"""
    if os.name == "nt":
        return driver_class
    return TerminalThemeLinuxDriver if driver_class is LinuxDriver else driver_class


def supports_runtime_theme_tracking(driver) -> bool:
    """当前真实终端驱动是否支持运行中主题应答。"""
    return os.name != "nt" and isinstance(driver, TerminalThemeLinuxDriver)


def _write(driver, data: str) -> None:
    try:
        driver.write(data)
        driver.flush()
    except Exception:  # noqa: BLE001 - 终端查询失败不能影响主界面
        return


def enable_runtime_theme_tracking(driver) -> None:
    """订阅支持 DEC 2031 的终端，并立即查询一次当前背景。"""
    query = "\x1b[?2031h\x1b[?996n\x1b]11;?\x07"
    if os.environ.get("TMUX"):
        # tmux 的缓存色可能仍是启动时的旧值；同时穿透查询真实外层终端。
        query += (
            "\x1bPtmux;\x1b\x1b[?2031h\x1b\\"
            "\x1bPtmux;\x1b\x1b[?996n\x1b\\"
            "\x1bPtmux;\x1b\x1b]11;?\x07\x1b\\"
        )
    _write(driver, query)


def query_runtime_theme(driver) -> None:
    """查询当前深浅模式与实际背景色；不阻塞等待应答。"""
    query = "\x1b[?996n\x1b]11;?\x07"
    if os.environ.get("TMUX"):
        query += (
            "\x1bPtmux;\x1b\x1b[?996n\x1b\\"
            "\x1bPtmux;\x1b\x1b]11;?\x07\x1b\\"
        )
    _write(driver, query)


def disable_runtime_theme_tracking(driver) -> None:
    """退出前关闭 DEC 2031 主动通知。"""
    sequence = "\x1b[?2031l"
    if os.environ.get("TMUX"):
        sequence += "\x1bPtmux;\x1b\x1b[?2031l\x1b\\"
    _write(driver, sequence)
