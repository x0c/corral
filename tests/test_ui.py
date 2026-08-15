"""ui/ 包（Textual 界面层）的 Pilot 交互测试。

取代旧版 test_session_scanning.py 里那些直接 mock curses/stdscr 的界面测试
（_run/_draw*/鼠标 SGR 解析等，随 curses 一起删除）。这里用 Textual 官方支持的
无终端 Pilot（App.run_test()）驱动真实的 App/Screen/Widget 事件循环，覆盖会话
导航、项目筛选、启动/内嵌流程、预览页、各类弹窗。

内嵌面板（EmbedPane）对 tmux 的依赖在这里用真实 tmux 会话验证（而不是 mock
embed.* 调用）：项目已有的 embed.py 单测负责纯函数层，selftest.sh 负责完整
真机冒烟；这里介于两者之间，用真实但轻量的 tmux 会话验证 MainScreen ↔ EmbedPane
↔ embed.py 的接线是否正确（这条接线正是本次从 curses 迁移到 Textual 的核心）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import select
import shutil
import subprocess
import threading
import time
import unittest
from unittest import mock

from pickup import i18n

# 界面测试固定英文，避免 CI/本机 LANG=zh* 时断言漂移
i18n.set_lang("en")

# 侧边栏记忆（会话组/置顶/折叠/显隐）隔离到临时目录，避免读到、更避免改到本机
# ~/.cache/pickup 里机主真实的状态。`PICKUP_CACHE_DIR` 是唯一的隔离开关：少了它，
# split_layout 会去真实家目录找旧版 JSON 做一次性迁移。
import tempfile

from pickup import split_layout as _split_layout

_SIDEBAR_STATE_DIR = tempfile.mkdtemp(prefix="pickup-test-sidebar-state-")
os.environ["PICKUP_CACHE_DIR"] = _SIDEBAR_STATE_DIR
_split_layout.reset_default_layout_db()
_SIDEBAR_STATE_DB = os.path.join(_SIDEBAR_STATE_DIR, "sidebar-layout.sqlite3")

from textual import events
from textual.color import Color
from textual.geometry import Offset, Size
from textual.widgets import Footer, Input, Label, ListView, TextArea

import pickup
from pickup import ui_prefs as _ui_prefs
from pickup.models import LaunchPlan
from pickup.ui.app import PickupApp
from pickup.ui.embed_pane import EmbedPane
from pickup.ui.modals import (
    ConfirmModal,
    NewSessionModal,
    RuntimeChoice,
    RuntimePickerModal,
)
from pickup.ui.pointer_shape import (
    enable_tmux_passthrough,
    reset_sequence,
    restore_tmux_passthrough,
    sequence,
)
from pickup.ui.runtime_top_bar import _SidebarToggleChip, _TopBarSpacer
from pickup.ui.search_modal import FullTextSearchModal, SearchResultRow
from pickup.ui.session_list import (
    GROUP_ID_PREFIX,
    NEW_SESSION_ID,
    PIN_SEP_ID,
    TODAY_SEP_ID,
    NewSessionCard,
    PinSeparatorCard,
    SessionCard,
    SessionGroupCard,
    SessionListView,
    _session_in_today_window,
)
from pickup.ui.split_pane_area import SplitPaneArea
from pickup.ui.terminal_theme import TerminalBackgroundReport, TerminalThemeParser

HAS_TMUX = shutil.which("tmux") is not None


def _primary_embed_pane(screen) -> EmbedPane:
    """MainScreen 多分屏右栏里取第一个 EmbedPane（单格测试沿用此入口）。"""
    area = screen.query_one(SplitPaneArea)
    for cell in area._cells():  # noqa: SLF001
        pane = cell.embed_pane()
        if pane is not None:
            return pane
    raise AssertionError("没有可用的内嵌面板")


async def _wait_for_embed_pane(screen) -> EmbedPane:
    """等 SplitPaneArea 异步挂载完成后再取 EmbedPane。"""

    def _ready() -> bool:
        area = screen.query_one(SplitPaneArea)
        for cell in area._cells():  # noqa: SLF001
            if cell.embed_pane() is not None:
                return True
        return False

    await _wait_until(_ready)
    return _primary_embed_pane(screen)


async def _wait_for_embed_session(
    screen, session_name: str, *, tries: int = 500, interval: float = 0.01,
) -> EmbedPane:
    """右栏异步替换格子时反复取当前 Widget，直到它已绑定目标托管会话。"""
    for _ in range(tries):
        try:
            pane = _primary_embed_pane(screen)
        except AssertionError:
            pane = None
        if pane is not None and pane.session_name == session_name:
            return pane
        await asyncio.sleep(interval)
    raise AssertionError(
        f"等待 {tries * interval:.2f}s 后仍未挂载托管会话：{session_name}"
    )



async def _wait_until(predicate, *, tries: int = 500, interval: float = 0.01) -> None:
    """等待后台 worker 达到断言条件；成功立即返回，慢速 Runner 最多等五秒。"""
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"等待 {tries * interval:.2f}s 后条件仍未满足")


async def _wait_for_pane_text(pane, text: str, *, tries: int = 60, interval: float = 0.1) -> None:
    """抓帧现在跑在后台线程（见 embed_pane.py 的性能修复：滚轮/输入处理不再
    同步调用 embed.capture，避免卡住主线程），测试里不能再直接同步调用一个
    "_capture_now" 方法强制抓一帧，改成轮询等待后台线程把新画面渲染出来。"""
    import asyncio

    for _ in range(tries):
        if pane._grid is not None and text in pane.render().plain:
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"等待 {tries * interval:.1f}s 后仍未看到文本：{text!r}；"
                          f"当前画面：{pane.render().plain!r}")


async def _wait_for_session_name(pane, *, tries: int = 60, interval: float = 0.1) -> None:
    """等待 MainScreen 的托管 worker 完成。`embed.host_session`（真正的阻塞 tmux
    子进程调用）现在跑在 `@work(thread=True)` worker 里，通过 call_from_thread 把
    结果异步写回 `pane.session_name`，不再和按键处理同步完成，测试拿到 pane 后
    不能立即读 session_name，要轮询等它就绪。"""
    import asyncio

    for _ in range(tries):
        if pane.session_name is not None:
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"等待 {tries * interval:.1f}s 后 pane.session_name 仍为 None")


def _make_store(sessions=None, extra_runtimes=()):
    # 每个界面用例从空会话组状态开始，避免置顶/分组跨用例串扰；使用 unittest
    # discover 时不会执行 pytest fixture，因此隔离必须放在共用夹具入口。
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(f"{_SIDEBAR_STATE_DB}{suffix}")
    _split_layout.reset_default_layout_db()
    sessions = sessions if sessions is not None else [
        {
            "source": "claude", "id": f"s{i}", "short_id": f"s{i}",
            "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
            "native_title": None, "fallback_title": f"会话{i}",
            "cwd": "/tmp", "live": False,
        }
        for i in range(3)
    ]
    claude = mock.Mock()
    claude.id = "claude"
    claude.display_name = "Claude"
    claude.is_available.return_value = True
    claude.scan_sessions.return_value = sessions
    claude.load_conversation.return_value = [
        pickup.ConversationMessage("user", "测试问题"),
        pickup.ConversationMessage("assistant", "测试回复"),
    ]
    registry = pickup.RuntimeRegistry((claude, *extra_runtimes))
    with mock.patch.object(pickup.titles, "load_cache", return_value={}):
        store = pickup.SessionStore(limit=20, registry=registry)
        store.load()
    return store, registry


def _claude_session(
    sid: str,
    mtime: float,
    title: str | None = None,
    *,
    live: bool = False,
) -> dict:
    return {
        "source": "claude",
        "id": sid,
        "short_id": sid,
        "mtime": mtime,
        "size_bytes": 1,
        "size_kb": 1,
        "native_title": None,
        "fallback_title": title or sid,
        "cwd": "/tmp",
        "live": live,
    }


class KittyKeyboardProtocolTests(unittest.TestCase):
    """回归：pickup 必须默认关闭 Textual 的 Kitty 键盘协议，否则 iTerm2/Ghostty/kitty
    等支持它的终端会把按键当转义码原样上报、绕过操作系统输入法，用户在内嵌 Agent
    里根本打不出中文（真机反馈：iTerm2 + SSH 下内嵌 Agent 无法输入中文，同一 SSH
    的 nano 却正常，唯一差别就是 pickup 开了这个协议）。pickup 顶层用
    os.environ.setdefault 在任何 textual 导入前关掉它。"""

    def test_kitty_keyboard_protocol_disabled_by_default(self) -> None:
        import os

        # import pickup 已在模块顶部发生，setdefault 应已生效
        self.assertEqual(os.environ.get("TEXTUAL_DISABLE_KITTY_KEY"), "1")
        import textual.constants as constants
        self.assertTrue(
            constants.DISABLE_KITTY_KEY,
            "Textual 必须把 Kitty 键盘协议判定为禁用；开着会绕过 IME 导致内嵌 Agent 打不了中文",
        )


@contextlib.contextmanager
def _draining_pty_master(master: int):
    """持续读空 pty master，模拟「真实终端会把程序写出去的字节收走」。

    **没有这个读者，测试会在 macOS 上永久挂死。** 探测把 OSC 查询写给 slave，
    这些字节堆在输出队列里等对端来读；`_probe_osc_colours` 收尾用
    `tcsetattr(fd, TCSADRAIN, old)` 恢复终端属性，而 `TCSADRAIN` 的语义正是
    「先等输出队列排空再生效」——没人读 master，队列永远不空，这一行就永远不返回。
    Linux 的排空语义不同、不会卡，所以本机怎么跑都复现不了。2026-07-31 GitHub
    macOS runner 上真实挂死，作业空烧 6 小时才被平台上限杀掉，线程栈精确停在
    `theme.py` 的那一行 `tcsetattr`。真实终端不存在这个问题（终端一直在读）。
    """
    stop = threading.Event()

    def _drain() -> None:
        while not stop.is_set():
            try:
                ready, _, _ = select.select([master], [], [], 0.02)
                if ready and not os.read(master, 4096):
                    return
            except OSError:
                return  # slave 已关闭

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    try:
        yield
    finally:
        stop.set()
        reader.join(timeout=1.0)


class OscProbeFlushTests(unittest.TestCase):
    """回归：OSC 探测结束必须清空终端输入队列，否则没读完的应答尾巴会漏进 Textual
    被当键盘输入注入搜索框——真机现象是启动时先闪过一行 `...rgb:xxxx/...`、搜索框
    乱码、且乱字符实时筛选把侧边栏会话列表整个过滤空（tmux/SSH 下 60ms 超时太短、
    多段应答读不全时高发）。见 theme._probe_osc_colours 的 finally tcflush。"""

    def test_probe_flushes_unread_input_tail(self) -> None:
        import pty
        import select
        import threading

        from pickup import theme

        master, slave = pty.openpty()

        class _FakeStd:
            """伪造 stdin/stdout 指向 pty slave（isatty 为真，探测才会真正跑）。"""

            def __init__(self, fd: int) -> None:
                self._fd = fd

            def fileno(self) -> int:
                return self._fd

            def isatty(self) -> bool:
                return True

        # 模拟真实终端：探测进入读循环（已 setraw）后才把应答送来，避免被探测自身
        # 的 tty.setraw(TCSAFLUSH) 提前清掉。两段完整应答（OSC 10+11，凑够计数 2 让
        # 读循环提前退出）后再跟一大段尾巴——单次 os.read(256) 读不完，尾巴留在输入
        # 队列，正是没有 flush 时会漏进 Textual 变成搜索框乱码的那部分。
        resp = b"\x1b]10;rgb:1e1e/1e1e/2e2e\x07\x1b]11;rgb:1e1e/1e1e/2e2e\x07"
        tail = b"stray-osc-tail-" + b"x" * 400

        def _respond() -> None:
            time.sleep(0.04)  # 等探测完成 setraw 并进入 select 等待
            os.write(master, resp + tail)

        writer = threading.Thread(target=_respond)
        try:
            writer.start()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TMUX", None)
                os.environ.pop("PICKUP_OSC_REPORT", None)
                with mock.patch.object(theme.sys, "stdin", _FakeStd(slave)), \
                        mock.patch.object(theme.sys, "stdout", _FakeStd(slave)), \
                        _draining_pty_master(master):
                    report = theme._probe_osc_colours(timeout=0.5)
            writer.join()

            # 应答本身要被正确解出（探测仍然有效，没有误伤合法应答）
            self.assertIsNotNone(report)
            self.assertIn(b"rgb:", report)

            # 关键断言：探测返回后输入队列必须已被清空，尾巴不会漏进 TUI
            os.set_blocking(slave, False)
            leftover = b""
            while True:
                r, _, _ = select.select([slave], [], [], 0.1)
                if not r:
                    break
                chunk = os.read(slave, 4096)
                if not chunk:
                    break
                leftover += chunk
            self.assertEqual(
                leftover, b"",
                f"探测后输入队列仍有残留会漏进 TUI 变成搜索框乱码：{leftover!r}",
            )
        finally:
            writer.join()
            os.close(master)
            os.close(slave)

    def test_tmux_settle_drains_late_passthrough_pair(self) -> None:
        """回归：TMUX 下裸应答到齐后立刻退出会漏掉晚到的 passthrough 应答。

        v0.24.3 的 flush 只能清「此刻已在队列里」的字节；passthrough 若在 flush
        之后才到，仍会漏进 Textual。直启默认焦点在搜索框时就会复现乱码+空列表。
        settle 窗口必须在 flush 前把这对迟到应答读掉。
        """
        import pty
        import select
        import threading

        from pickup import theme

        master, slave = pty.openpty()

        class _FakeStd:
            def __init__(self, fd: int) -> None:
                self._fd = fd

            def fileno(self) -> int:
                return self._fd

            def isatty(self) -> bool:
                return True

        bare = b"\x1b]10;rgb:1111/1111/1111\x07\x1b]11;rgb:2222/2222/2222\x07"
        late = b"\x1b]10;rgb:aaaa/aaaa/aaaa\x07\x1b]11;rgb:bbbb/bbbb/bbbb\x07"

        def _respond() -> None:
            time.sleep(0.04)  # 等探测完成 setraw 并进入 select
            os.write(master, bare)
            time.sleep(0.05)  # 仍在 tmux settle(0.12s) 内
            os.write(master, late)

        writer = threading.Thread(target=_respond)
        try:
            writer.start()
            with mock.patch.dict(os.environ, {"TMUX": "1"}, clear=False):
                os.environ.pop("PICKUP_OSC_REPORT", None)
                with mock.patch.object(theme.sys, "stdin", _FakeStd(slave)), \
                        mock.patch.object(theme.sys, "stdout", _FakeStd(slave)), \
                        _draining_pty_master(master):
                    report = theme._probe_osc_colours(timeout=0.5)
            writer.join(timeout=2.0)

            self.assertIsNotNone(report)
            self.assertIn(b"rgb:bbbb", report)

            os.set_blocking(slave, False)
            leftover = b""
            while True:
                r, _, _ = select.select([slave], [], [], 0.05)
                if not r:
                    break
                chunk = os.read(slave, 4096)
                if not chunk:
                    break
                leftover += chunk
            self.assertEqual(leftover, b"", f"settle 后仍有残留：{leftover!r}")
        finally:
            writer.join(timeout=1.0)
            os.close(master)
            os.close(slave)


@unittest.skipIf(TerminalThemeParser is None, "Windows 不使用 Unix 终端主题解析器")
class RuntimeThemeParserTests(unittest.TestCase):
    """运行中的主题应答必须被单独提取，不能再变成搜索框键盘输入。"""

    def test_extracts_chunked_osc_background_and_preserves_normal_keys(self) -> None:
        parser = TerminalThemeParser()
        parsed = []
        parsed.extend(parser.feed("a\x1b]11;rgb:ffff/"))
        parsed.extend(parser.feed("eeee/dddd\x07b"))

        self.assertEqual(parsed[0].key, "a")
        report = parsed[1]
        self.assertIsInstance(report, TerminalBackgroundReport)
        self.assertEqual(report.osc_report, b"\x1b]11;rgb:ffff/eeee/dddd\x07")
        self.assertEqual(parsed[2].key, "b")

    def test_extracts_dec_2031_light_and_dark_notifications(self) -> None:
        parser = TerminalThemeParser()
        light = list(parser.feed("\x1b[?997;2n"))
        dark = list(parser.feed("\x1b[?997;1n"))
        self.assertTrue(light[0].is_light)
        self.assertFalse(dark[0].is_light)

    def test_extracts_dec_2031_notifications_split_at_every_byte(self) -> None:
        """主题回复被 TTY 拆包时也不能泄漏成 Cursor 的普通输入。"""
        sequence = "\x1b[?997;2n"
        for split_at in range(1, len(sequence)):
            parser = TerminalThemeParser()
            parsed = [*parser.feed(sequence[:split_at]), *parser.feed(sequence[split_at:])]
            self.assertEqual(len(parsed), 1, f"拆在第 {split_at} 个字符后不应产生按键")
            self.assertIsInstance(parsed[0], TerminalBackgroundReport)
            self.assertTrue(parsed[0].is_light)

    def test_pickup_app_uses_runtime_theme_driver_on_unix(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        self.assertEqual(app.driver_class.__name__, "TerminalThemeLinuxDriver")


def _pointer_env(*drop: str, **extra: str):
    """构造一份干净环境：去掉 TMUX / XTERM_VERSION 等，再按需补回。"""
    env = {key: value for key, value in os.environ.items() if key not in drop and key not in extra}
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=True)


class PointerShapeSequenceTests(unittest.TestCase):
    """OSC 22 序列必须按终端 / tmux / xterm 四种组合发出正确的形状名。"""

    def test_bare_css_name_without_tmux(self) -> None:
        with _pointer_env("TMUX", "XTERM_VERSION"):
            self.assertEqual(sequence("pointer"), "\x1b]22;pointer\x07")
            self.assertEqual(sequence("default"), "\x1b]22;default\x07")
            self.assertEqual(sequence("text"), "\x1b]22;text\x07")

    def test_tmux_passthrough_appends_dcs_copy(self) -> None:
        with _pointer_env("XTERM_VERSION", TMUX="1,0,0"):
            raw = sequence("pointer")
            self.assertTrue(raw.startswith("\x1b]22;pointer\x07"))
            self.assertIn("\x1bPtmux;\x1b\x1b]22;pointer\x07\x1b\\", raw)

    def test_xterm_uses_x11_cursor_names(self) -> None:
        with _pointer_env("TMUX", XTERM_VERSION="XTerm(379)"):
            self.assertEqual(sequence("default"), "\x1b]22;left_ptr\x07")
            self.assertEqual(sequence("pointer"), "\x1b]22;hand2\x07")
            self.assertEqual(sequence("text"), "\x1b]22;xterm\x07")
            self.assertEqual(sequence("wait"), "\x1b]22;watch\x07")

    def test_xterm_inside_tmux_wraps_x11_name(self) -> None:
        with _pointer_env(TMUX="1,0,0", XTERM_VERSION="XTerm(379)"):
            raw = sequence("pointer")
            self.assertTrue(raw.startswith("\x1b]22;hand2\x07"))
            self.assertIn("\x1bPtmux;\x1b\x1b]22;hand2\x07\x1b\\", raw)

    def test_reset_sequence_uses_empty_shape(self) -> None:
        with _pointer_env("TMUX", "XTERM_VERSION"):
            self.assertEqual(reset_sequence(), "\x1b]22;\x07")
        with _pointer_env("XTERM_VERSION", TMUX="1,0,0"):
            self.assertIn("\x1bPtmux;\x1b\x1b]22;\x07\x1b\\", reset_sequence())

    def test_passthrough_skipped_without_tmux(self) -> None:
        with _pointer_env("TMUX", "TMUX_PANE"):
            with mock.patch("pickup.ui.pointer_shape.subprocess.run") as run:
                enable_tmux_passthrough()
                restore_tmux_passthrough()
                run.assert_not_called()

    def test_passthrough_sets_and_unsets_pane_option(self) -> None:
        with _pointer_env(TMUX="1,0,0", TMUX_PANE="%0"):
            with mock.patch("pickup.ui.pointer_shape.subprocess.run") as run:
                enable_tmux_passthrough()
                restore_tmux_passthrough()
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["tmux", "set", "-p", "allow-passthrough", "on"],
        )
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["tmux", "set", "-pu", "allow-passthrough"],
        )


class PointerShapeUiTests(unittest.IsolatedAsyncioTestCase):
    """悬停可点区域是手型，内嵌终端是 I 型，空白处是箭头。"""

    async def test_hover_updates_pointer_shape_by_widget(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            card = app.screen.query_one(SessionCard)
            self.assertEqual(str(card.styles.pointer), "pointer")
            await pilot.hover(card)
            await pilot.pause()
            self.assertEqual(app.screen._pointer_shape, "pointer")  # noqa: SLF001

            pane = app.screen.query_one(EmbedPane)
            self.assertEqual(str(pane.styles.pointer), "text")
            await pilot.hover(pane)
            await pilot.pause()
            self.assertEqual(app.screen._pointer_shape, "text")  # noqa: SLF001

            spacer = app.screen.query_one(_TopBarSpacer)
            await pilot.hover(spacer)
            await pilot.pause()
            self.assertEqual(app.screen._pointer_shape, "default")  # noqa: SLF001

    async def test_set_pointer_shape_does_not_raise_in_headless(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            app._set_pointer_shape("pointer")
            app._set_pointer_shape("text")
            app._set_pointer_shape("default")


class AppThemeTests(unittest.IsolatedAsyncioTestCase):
    """pickup 自身界面配色应跟随外层终端探测到的深浅色（真机反馈：浅色终端下
    配色不对——此前只处理了托管会话内的深浅色注入，没接 pickup 自己的界面）。"""

    async def test_theme_follows_detected_terminal_background(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False, osc_report=b"\x1b]11;rgb:ffff/ffff/ffff\x07")
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            self.assertEqual(app.theme, "pickup-light")

    async def test_widget_css_survives_a_builtin_theme(self) -> None:
        """自有主题变量必须有兜底值，否则整个应用起不来（v0.24.29 真机事故）。

        widget 的 DEFAULT_CSS 是各自第一次挂载时才并入样式表做变量代换的，那一刻
        当前主题不保证已经是 pickup 自有主题。只把变量写在 Theme 里，换个终端探测
        结果或 Textual 版本就可能在代换时找不到它，Textual 直接报
        `reference to undefined variable` 并中止启动，而不是退化成默认颜色。
        这里强行切到 Textual 内置主题再挂载整屏，等价于"最坏时序"。
        """
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            app.theme = "textual-dark"  # 内置主题里没有 pickup 自有变量
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(delay=0.3)
            # 能取到颜色就说明变量代换成功；解析失败时 Textual 早已中止应用
            from pickup.ui.app import _SIDEBAR_SPLIT_LADDER

            for name in ("pane-active-background", *_SIDEBAR_SPLIT_LADDER):
                with self.subTest(variable=name):
                    self.assertIn(name, app.get_css_variables())

    def test_theme_variable_defaults_cover_every_custom_variable(self) -> None:
        """自有主题里定义的变量，兜底表必须一个不落地覆盖。"""
        from pickup.ui.app import (
            _PICKUP_DARK,
            _PICKUP_LIGHT,
            _THEME_VARIABLE_DEFAULTS,
        )

        builtin = {"block-cursor-background", "block-cursor-blurred-background"}
        for theme in (_PICKUP_DARK, _PICKUP_LIGHT):
            custom = set(theme.variables) - builtin
            missing = custom - set(_THEME_VARIABLE_DEFAULTS)
            self.assertEqual(
                missing, set(),
                f"{theme.name} 定义了自有变量但没进兜底表：{missing}",
            )

    async def test_dark_background_uses_dark_theme(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False, osc_report=b"\x1b]11;rgb:1e1e/1e1e/2e2e\x07")
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            self.assertEqual(app.theme, "pickup-dark")

    async def test_missing_report_falls_back_to_default_dark(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False, osc_report=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            self.assertEqual(app.theme, "pickup-dark")

    async def test_running_app_switches_theme_when_terminal_background_changes(self) -> None:
        store, _ = _make_store()
        old_report = b"\x1b]10;rgb:0000/0000/0000\x07\x1b]11;rgb:ffff/ffff/ffff\x07"
        app = PickupApp(store, embed_ok=True, osc_report=old_report)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            self.assertEqual(app.theme, "pickup-light")

            area = app.screen.query_one(SplitPaneArea)
            session = store.all_sessions()[0]
            area.show_hosted_group("/tmp", [(session, None, lambda: "")])
            await _wait_until(lambda: len(area.cells()) == 1)
            pane = area.cells()[0].embed_pane()
            self.assertIsNotNone(pane)

            dark_report = b"\x1b]11;rgb:1111/2222/3333\x07"
            app.post_message(TerminalBackgroundReport(osc_report=dark_report))
            await pilot.pause(delay=0.2)

            self.assertEqual(app.theme, "pickup-dark")
            self.assertEqual(app.screen.osc_report, b"\x1b]10;rgb:0000/0000/0000\x07" + dark_report)
            self.assertEqual(area._osc_report, app.screen.osc_report)  # noqa: SLF001
            self.assertEqual(pane._osc_report, app.screen.osc_report)  # noqa: SLF001
            self.assertEqual(pane.styles.background.rgb, (17, 34, 51))

    async def test_dec_mode_notification_switches_theme_before_osc_reply(self) -> None:
        store, _ = _make_store()
        app = PickupApp(
            store,
            embed_ok=False,
            osc_report=b"\x1b]11;rgb:ffff/ffff/ffff\x07",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            app.post_message(TerminalBackgroundReport(is_light=False))
            await pilot.pause(delay=0.1)
            self.assertEqual(app.theme, "pickup-dark")

    async def test_runtime_top_bar_matches_footer_and_aligns_left(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            top_bar = app.screen.query_one("#runtime-top-bar")
            footer = app.screen.query_one(Footer)
            # 左侧侧栏开关 + spacer 把助手 chip 顶到右侧；容器本身左对齐。
            self.assertEqual(top_bar.styles.align_horizontal, "left")
            self.assertEqual(top_bar.styles.background, footer.styles.background)
            self.assertIsNotNone(app.screen.query_one("#sidebar-toggle", _SidebarToggleChip))

    async def test_sidebar_and_split_panes_use_one_cell_blank_gaps(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            area = app.screen.query_one(SplitPaneArea)
            self.assertEqual(area.styles.margin.left, 1)
            sessions = store.all_sessions()[:2]
            area.show_hosted_group(
                "/tmp",
                [(session, None, lambda: "") for session in sessions],
            )
            await _wait_until(lambda: len(area._cells()) == 2)  # noqa: SLF001
            first, second = area._cells()  # noqa: SLF001
            self.assertEqual(first.styles.margin.left, 0)
            self.assertEqual(second.styles.margin.left, 1)
            self.assertEqual(first.styles.border_left[0], "")
            self.assertEqual(second.styles.border_left[0], "")
            self.assertEqual(second.styles.border_top[0], "")
            self.assertEqual(second.styles.border_right[0], "")
            self.assertEqual(second.styles.border_bottom[0], "")

    async def test_mouse_down_on_detached_widget_does_not_crash(self) -> None:
        """全量重建的中间态：合成器命中表还留着刚被移出 DOM 的控件。

        Textual 8.2.8 的文本选择分支会对它取 `parent.region`，parent 已是 None
        → 未捕获 AttributeError 直接掀掉整个 TUI（真机 2026-08-03 / 08-05 各闪退
        一次，都发生在启动首屏重建期间点鼠标）。
        """
        from textual.widgets import Static

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            screen = app.screen
            hint = Static("待选文本")
            await screen.mount(hint)
            await pilot.pause()
            self.assertTrue(hint.allow_select)  # 命中前提：该控件允许文本选择
            hint._parent = None  # noqa: SLF001  模拟「已移出 DOM、命中表未更新」
            with mock.patch.object(
                screen._compositor,  # noqa: SLF001
                "get_widget_and_offset_at",
                return_value=(hint, Offset(0, 0)),
            ):
                await pilot.mouse_down(offset=(60, 10))
                await pilot.pause()
                await pilot.mouse_up(offset=(60, 10))
                await pilot.pause()
            self.assertTrue(app.is_running)
            hint._parent = screen  # noqa: SLF001  还回去，别让拆卸阶段踩空

    async def test_split_supports_max_panes_and_refuses_one_more(self) -> None:
        """分屏上限（当前 4 格）：满格都要能均分挂上，且不再允许加格。"""
        from pickup.split_layout import MAX_PANES

        sessions = [
            {
                "source": "claude", "id": f"m{i}", "short_id": f"m{i}",
                "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"会话{i}",
                "cwd": "/tmp", "live": False,
            }
            for i in range(MAX_PANES)
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause(delay=0.2)
            area = app.screen.query_one(SplitPaneArea)
            self.assertTrue(area.can_add_pane())
            area.show_hosted_group(
                "/tmp",
                [(session, None, lambda: "") for session in sessions],
            )
            await _wait_until(lambda: len(area._cells()) == MAX_PANES)  # noqa: SLF001
            await pilot.pause()
            widths = [cell.size.width for cell in area._cells()]  # noqa: SLF001
            self.assertTrue(all(w > 0 for w in widths), widths)
            self.assertLessEqual(max(widths) - min(widths), 1, widths)
            self.assertFalse(area.can_add_pane())

    async def test_footer_does_not_bind_n_for_new_session(self) -> None:
        """底栏不再暴露 n 新建快捷键；新建只走侧边栏项 / 顶栏加格。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            keys = {b.key for b in app.screen.BINDINGS}
            self.assertNotIn("n", keys)
            actions = {b.action for b in app.screen.BINDINGS}
            self.assertNotIn("new_session", actions)

    async def test_focusing_split_pane_highlights_matching_sidebar_session(self) -> None:
        """多分屏时聚焦某一格，侧边栏高亮必须切到该格对应会话。"""
        sessions = [
            {
                "source": "claude", "id": f"s{i}", "short_id": f"s{i}",
                "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"会话{i}",
                "cwd": "/tmp", "live": True,
                "keepalive_name": f"pickup-claude-s{i}",
            }
            for i in range(2)
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            area = app.screen.query_one(SplitPaneArea)
            list_view = app.screen.query_one(SessionListView)
            key0 = pickup.session_key(sessions[0])
            key1 = pickup.session_key(sessions[1])
            # 写入分屏记忆，避免后续列表高亮回调把两格收成单格
            app.screen._apply_layout_change(  # noqa: SLF001
                lambda s: s.set_group("/tmp", [key0, key1], focus_key=key0)
            )
            area.show_hosted_group(
                "/tmp",
                [
                    (session, session["keepalive_name"], lambda: "")
                    for session in sessions
                ],
                focus_key=key0,
            )
            await _wait_until(lambda: len(area._cells()) == 2)  # noqa: SLF001
            # 先保证侧边栏停在第一格会话（不动 focus，避免抢焦点）
            list_view.index = 1
            await pilot.pause()
            self.assertEqual(len(area._cells()), 2)  # noqa: SLF001
            self.assertEqual(pickup.session_key(list_view.selected_session()), key0)
            second = area._cells()[1].embed_pane()  # noqa: SLF001
            self.assertIsNotNone(second)
            second.focus()
            await pilot.pause()
            await _wait_until(
                lambda: list_view.index == 2
                and list_view.selected_session() is not None
                and pickup.session_key(list_view.selected_session()) == key1,
            )
            self.assertEqual(area.focus_key, key1)

    async def test_try_restore_startup_layout_skips_prune_before_store_loaded(self) -> None:
        """扫描未完成时不得 prune+save，否则会把磁盘分屏记忆清空。"""
        from pickup import split_layout

        sessions = [
            {
                "source": "claude", "id": f"s{i}", "short_id": f"s{i}",
                "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"会话{i}",
                "cwd": "/tmp", "live": True,
                "keepalive_name": f"pickup-claude-s{i}",
            }
            for i in range(2)
        ]
        store, _ = _make_store(sessions=sessions)
        key0 = pickup.session_key(sessions[0])
        key1 = pickup.session_key(sessions[1])
        # 先写入一份「两格组合」到库；再装 App（__init__ 会读一份快照）
        split_layout.default_layout_db().set_group("/tmp", [key0, key1], focus_key=key0)
        store.loaded = False  # 模拟异步首扫尚未完成
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.05)
            # 显式再调一次：即便 on_mount 漏调，契约也必须守住
            app.screen._try_restore_startup_layout()  # noqa: SLF001
            loaded = split_layout.default_layout_db().read()
            group = loaded.get_group(key0)
            self.assertIsNotNone(group)
            assert group is not None
            self.assertEqual(group.session_keys, [key0, key1])

    async def test_closing_one_split_pane_keeps_sibling_widget(self) -> None:
        """关一格只卸该格，同伴 EmbedPane 实例不得被整排 remount 换掉。"""
        sessions = [
            {
                "source": "claude", "id": f"s{i}", "short_id": f"s{i}",
                "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"会话{i}",
                "cwd": "/tmp", "live": True,
                "keepalive_name": f"pickup-claude-s{i}",
            }
            for i in range(2)
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            area = app.screen.query_one(SplitPaneArea)
            key0 = pickup.session_key(sessions[0])
            key1 = pickup.session_key(sessions[1])
            app.screen._apply_layout_change(  # noqa: SLF001
                lambda s: s.set_group("/tmp", [key0, key1], focus_key=key0)
            )
            area.show_hosted_group(
                "/tmp",
                [
                    (session, session["keepalive_name"], lambda: "")
                    for session in sessions
                ],
                focus_key=key0,
            )
            await _wait_until(
                lambda: len(area.cells()) == 2
                and all(cell.embed_pane() is not None for cell in area.cells()),
            )
            keeper = area.cells()[1]
            keeper_pane = keeper.embed_pane()
            self.assertIsNotNone(keeper_pane)
            area._close_spec(area.pane_specs()[0])  # noqa: SLF001
            await _wait_until(lambda: len(area.cells()) == 1)
            self.assertIs(area.cells()[0], keeper)
            self.assertIs(area.cells()[0].embed_pane(), keeper_pane)
            self.assertEqual(area.ordered_session_keys(), [key1])

    async def test_same_hosted_identity_skips_remount_keeps_live_grid(self) -> None:
        """同 (session_key, keepalive) 再 show_hosted_group 不得整排 remount 清掉 live 画面。"""
        from pickup.embed import Cell

        sessions = [
            {
                "source": "claude", "id": "s0", "short_id": "s0",
                "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "会话0",
                "cwd": "/tmp", "live": True,
                "keepalive_name": "pickup-claude-s0",
            }
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            area = app.screen.query_one(SplitPaneArea)
            key0 = pickup.session_key(sessions[0])
            # 阻止列表跟随在断言窗口内另行 remount
            with mock.patch.object(app.screen, "_follow_current_selection"):
                area.show_hosted_group(
                    "/tmp",
                    [
                        (sessions[0], sessions[0]["keepalive_name"], lambda: "FALLBACK-TOP"),
                    ],
                    focus_key=key0,
                )
                await _wait_until(lambda: len(area.cells()) == 1)
                pane = area.cells()[0].embed_pane()
                self.assertIsNotNone(pane)
                cell_widget = area.cells()[0]
                fake_grid = [[Cell("L")] for _ in range(3)]
                pane._grid = fake_grid  # noqa: SLF001
                pane.session_name = sessions[0]["keepalive_name"]
                pane._capture_generation = 7  # noqa: SLF001

                with mock.patch.object(
                    area, "_schedule_mount", wraps=area._schedule_mount,
                ) as mount_mock:
                    area.show_hosted_group(
                        "/tmp",
                        [
                            (
                                sessions[0],
                                sessions[0]["keepalive_name"],
                                lambda: "FALLBACK-UPDATED",
                            ),
                        ],
                        focus_key=key0,
                    )
                    mount_mock.assert_not_called()

                self.assertIs(area.cells()[0], cell_widget)
                self.assertIs(area.cells()[0].embed_pane(), pane)
                self.assertIs(pane._grid, fake_grid)  # noqa: SLF001
                self.assertEqual(pane._capture_generation, 7)  # noqa: SLF001
                # 就地更新必须保留实时画面，但托管会话绝不能残留对话预览；
                # 否则抓帧重排的空档会闪一下消息内容。
                self.assertIsNone(pane._detail_renderer)  # noqa: SLF001

    async def test_hosted_registration_keeps_session_active_without_is_alive(self) -> None:
        """store.hosted 仍登记时，is_alive 假阴性不得把会话判为不活跃。"""
        sessions = [
            {
                "source": "claude", "id": "s0", "short_id": "s0",
                "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "会话0",
                "cwd": "/tmp", "live": False,
                "keepalive_name": "pickup-claude-s0",
            }
        ]
        store, _ = _make_store(sessions=sessions)
        key = pickup.session_key(sessions[0])
        store.hosted[key] = "pickup-claude-s0"
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            with mock.patch("pickup.embed.is_alive", return_value=False):
                self.assertTrue(app.screen._is_session_active(key))  # noqa: SLF001
                self.assertTrue(app.screen._session_is_active(sessions[0]))  # noqa: SLF001

    async def test_reconcile_split_keys_after_provisional_becomes_real(self) -> None:
        """占位卡转正后，分屏和侧边栏选择都必须迁移到真实会话。"""
        provisional_id = "abcd1234"
        real_id = "real-session-uuid"
        kname = "pickup-claude-abcd1234"
        provisional = {
            "source": "claude", "id": provisional_id, "short_id": provisional_id,
            "mtime": time.time(), "size_bytes": 0, "size_kb": 0,
            "native_title": None, "fallback_title": "新 Claude 会话",
            "cwd": "/tmp", "live": True, "keepalive_name": kname, "provisional": True,
        }
        real = {
            "source": "claude", "id": real_id, "short_id": real_id[:12],
            "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
            "native_title": "真实会话", "fallback_title": "真实会话",
            "cwd": "/tmp", "live": True, "keepalive_name": kname,
        }
        companion = {
            "source": "claude", "id": "companion", "short_id": "companion",
            "mtime": time.time() - 1, "size_bytes": 1, "size_kb": 1,
            "native_title": "同伴会话", "fallback_title": "同伴会话",
            "cwd": "/tmp", "live": True,
            "keepalive_name": "pickup-claude-companion",
        }
        store, _ = _make_store(sessions=[provisional, companion])
        old_key = pickup.session_key(provisional)
        new_key = pickup.session_key(real)
        companion_key = pickup.session_key(companion)
        app = PickupApp(store, embed_ok=True)
        # 本测手动模拟一次扫描替换；禁止后台定时重扫把 fixture 又写回占位卡。
        with mock.patch.object(store, "refresh", return_value=False):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                area = app.screen.query_one(SplitPaneArea)
                app.screen._apply_layout_change(  # noqa: SLF001
                    lambda s: s.set_group(
                        "/tmp", [old_key, companion_key], focus_key=old_key
                    )
                )
                with mock.patch("pickup.embed.is_alive", return_value=True):
                    area.show_hosted_group(
                        "/tmp",
                        [(provisional, kname, lambda: "")],
                        focus_key=old_key,
                    )
                    list_view = app.screen.query_one(SessionListView)
                    await list_view.rebuild(select_key=old_key)
                    self.assertEqual(
                        pickup.session_key(list_view.selected_session()), old_key
                    )
                    store.sessions["claude"] = [real, companion]
                    store._order = [new_key, companion_key]  # noqa: SLF001 — 模拟重扫把占位卡替换成真实卡
                    store.hosted[new_key] = kname
                    await app.screen._rebuild_list()  # noqa: SLF001
                    group = app.screen._split_store.get_group(new_key)  # noqa: SLF001
                    self.assertIsNotNone(group)
                    self.assertEqual(group.session_keys, [new_key, companion_key])
                    self.assertEqual(area.pane_specs()[0].session_key, new_key)
                    self.assertEqual(
                        pickup.session_key(list_view.selected_session()), new_key,
                        "占位卡转成真实卡后仍应选中同一份运行中会话",
                    )
                    self.assertEqual(area.ordered_session_keys(), [new_key])

    async def test_pi_provisional_refresh_keeps_split_group_without_duplicate(self) -> None:
        """Pi 历史落盘后占位卡转正：分屏组跟上新键，侧边栏不得在组外再挂一张。"""
        kname = "pickup-pi-abcd1234"
        companion_kname = "pickup-claude-companion"
        companion = {
            "source": "claude", "id": "companion", "short_id": "companion",
            "mtime": time.time() - 1, "size_bytes": 1, "size_kb": 1,
            "native_title": "同伴", "fallback_title": "同伴",
            "cwd": "/tmp/proj", "live": True, "keepalive_name": companion_kname,
        }
        pi_runtime = mock.Mock()
        pi_runtime.id = "pi"
        pi_runtime.display_name = "Pi"
        pi_runtime.is_available.return_value = True
        pi_runtime.scan_signature.return_value = None
        pi_runtime.scan_sessions.return_value = []
        pi_runtime.load_conversation.return_value = []
        store, _ = _make_store(sessions=[companion], extra_runtimes=(pi_runtime,))
        provisional = store.register_hosted_session(
            runtime_id="pi",
            keepalive_name=kname,
            title="新Pi会话",
            cwd="/tmp/proj",
            ident="abcd1234",
        )
        old_key = pickup.session_key(provisional)
        real_id = "019ffa0b-6679-7e5e-bfd9-1615e07cf643"
        new_key = f"pi:{real_id}"
        companion_key = pickup.session_key(companion)
        real = {
            "source": "pi", "id": real_id, "short_id": real_id[:12],
            "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
            "native_title": "真会话", "fallback_title": "真会话",
            "cwd": "/tmp/proj", "live": False,
        }
        app = PickupApp(store, embed_ok=True)
        real_refresh = store.refresh
        with mock.patch.object(store, "refresh", return_value=False):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                area = app.screen.query_one(SplitPaneArea)
                app.screen._apply_layout_change(  # noqa: SLF001
                    lambda s: s.set_group(
                        "/tmp/proj", [old_key, companion_key], focus_key=old_key
                    )
                )
                with mock.patch("pickup.embed.is_alive", return_value=True), mock.patch.object(
                    pickup.keepalive, "annotate"
                ):
                    area.show_hosted_group(
                        "/tmp/proj",
                        [(provisional, kname, lambda: ""), (companion, companion_kname, lambda: "")],
                        focus_key=old_key,
                    )
                    list_view = app.screen.query_one(SessionListView)
                    await list_view.rebuild(select_key=old_key)
                    pi_runtime.scan_sessions.return_value = [real]
                    real_refresh()
                    await app.screen._rebuild_list()  # noqa: SLF001
                    group = app.screen._split_store.get_group(new_key)  # noqa: SLF001
                    self.assertIsNotNone(group)
                    self.assertEqual(set(group.session_keys), {new_key, companion_key})
                    self.assertIsNone(store.find_session(old_key))
                    grouped = set(group.session_keys)
                    visible = [
                        pickup.session_key(session)
                        for session in list_view.visible_sessions()
                    ]
                    self.assertIn(new_key, visible)
                    self.assertNotIn(old_key, visible)
                    ungrouped_pi = [
                        key for key in visible
                        if key.startswith("pi:") and key not in grouped
                    ]
                    self.assertEqual(ungrouped_pi, [])

    async def test_resize_full_repaint_is_debounced(self) -> None:
        """连续缩放手势只在停稳后触发一次整屏全量重绘，不能每次尺寸变化都狂刷。"""
        import pickup.ui.app as app_mod

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        # CI 上 pilot.pause 的墙钟开销可能让 0.12s 防抖在「拖动断言」前就到期；
        # 本测把窗口拉长，只验证「拖动中重置、停稳后恰好一次」的契约。
        debounce = 0.5
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            calls: list[Size] = []
            original = app._force_full_repaint

            def _tracking_force() -> None:
                calls.append(app.size)
                original()

            with (
                mock.patch.object(app_mod, "_RESIZE_FULL_REPAINT_DEBOUNCE", debounce),
                mock.patch.object(app, "_force_full_repaint", side_effect=_tracking_force),
            ):
                # 快速连续缩放：防抖计时器应被反复重置，到期前不应触发全量重绘
                await pilot.resize_terminal(90, 28)
                await pilot.pause(delay=0.02)
                await pilot.resize_terminal(80, 24)
                await pilot.pause(delay=0.02)
                await pilot.resize_terminal(70, 22)
                await pilot.pause(delay=0.02)
                self.assertEqual(calls, [], "拖动过程中不应触发整屏全量重绘")
                self.assertIsNotNone(app._resize_full_repaint_timer)
                # 停稳超过防抖窗口后应恰好一次，且尺寸为最后一次目标
                await pilot.pause(delay=debounce + 0.05)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0], Size(70, 22))
                self.assertIsNone(app._resize_full_repaint_timer)

    def test_compositor_index_error_recovers_instead_of_exiting(self) -> None:
        """窗口缩放时 Textual chops/spans 行数竞态：IndexError 应自愈，不退出 TUI。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        app._compositor_recovery_budget = 2
        forced: list[int] = []

        def _fake_force() -> None:
            forced.append(1)

        with mock.patch.object(app, "_force_full_repaint", side_effect=_fake_force):
            app._handle_exception(IndexError("list index out of range"))
        self.assertEqual(forced, [1])
        self.assertEqual(app._compositor_recovery_budget, 1)
        self.assertNotEqual(getattr(app, "_return_code", None), 1)

        # 额度耗尽后仍走默认致命路径，并落盘供 diagnose 读取
        app._compositor_recovery_budget = 0
        with (
            mock.patch.object(app, "_force_full_repaint", side_effect=_fake_force),
            mock.patch("textual.app.App._handle_exception") as fatal,
            mock.patch("pickup.observe.log_exception") as logged,
        ):
            app._handle_exception(IndexError("list index out of range"))
        fatal.assert_called_once()
        logged.assert_called_once()
        self.assertEqual(logged.call_args.args[0], "TUI 未捕获异常")
        self.assertEqual(forced, [1], "额度耗尽后不应再尝试整屏重绘")

    def test_lru_cache_set_survives_desynced_eviction(self) -> None:
        """Textual LRUCache 链表/dict 不同步时，set 驱逐不得再 KeyError 掀掉 TUI。

        真机 2026-08-06：双分屏 PaneCell._get_box_model 写入 _box_model_cache
        时在 textual/cache.py:126 的 del self._cache[last[2]] 闪退。
        """
        from textual.cache import LRUCache

        from pickup.ui.textual_patches import install_textual_patches

        install_textual_patches()
        cache = LRUCache(2)
        cache["a"] = 1
        cache["b"] = 2
        # 模拟上游偶发的不同步：dict 里没有最老项，链表里还挂着。
        del cache._cache["a"]  # noqa: SLF001
        cache._full = True  # noqa: SLF001
        # 无补丁时下一行必炸 KeyError；有补丁应清空后写入新项。
        cache["c"] = 3
        self.assertEqual(cache["c"], 3)
        self.assertIn("c", cache)
        self.assertNotIn("a", cache)

    def test_fatal_tui_exception_is_logged_before_exit(self) -> None:
        """非 compositor 自愈的致命异常必须写入 observe，不能只闪在终端。"""
        import tempfile

        from pickup import observe

        store, _ = _make_store()
        with tempfile.TemporaryDirectory() as tmp:
            events_path = os.path.join(tmp, "events.log")
            embed_path = os.path.join(tmp, "embed-error.log")
            with (
                mock.patch.object(observe, "CACHE_DIR", tmp),
                mock.patch.object(observe, "EVENTS_LOG", events_path),
                mock.patch.object(observe, "EMBED_ERROR_LOG", embed_path),
            ):
                observe.reset_for_tests()
                observe.init(debug=False)
                app = PickupApp(store, embed_ok=False)

                def _boom() -> None:
                    raise NameError("name '_project_groups' is not defined")

                try:
                    _boom()
                except NameError as inner:
                    class WorkerFailed(Exception):
                        def __init__(self, error: BaseException) -> None:
                            self.error = error
                            super().__init__(f"Worker raised exception: {error!r}")

                    wrapped = WorkerFailed(inner)
                with mock.patch("textual.app.App._handle_exception"):
                    app._handle_exception(wrapped)
                last = observe.read_last_error()
                self.assertIsNotNone(last)
                assert last is not None
                self.assertEqual(last["where"], "TUI 未捕获异常")
                self.assertEqual(last["exc_type"], "NameError")
                self.assertIn("_project_groups", last["traceback"])
                self.assertIn("_boom", last["traceback"])
                self.assertIn("via WorkerFailed", last["traceback"])

    async def test_f12_saves_screenshot_under_cache(self) -> None:
        import tempfile

        from pickup import observe

        store, _ = _make_store()
        with tempfile.TemporaryDirectory() as tmp:
            events_path = os.path.join(tmp, "events.log")
            with (
                mock.patch.object(observe, "CACHE_DIR", tmp),
                mock.patch.object(observe, "EVENTS_LOG", events_path),
                mock.patch.object(observe, "EMBED_ERROR_LOG", os.path.join(tmp, "embed-error.log")),
            ):
                observe.reset_for_tests()
                observe.init(debug=False)
                app = PickupApp(store, embed_ok=False)
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause(delay=0.2)
                    # 直接调 action，避免 Pilot 对 F12 键名在部分环境下不派发到 Screen。
                    app.screen.action_save_screenshot()
                    await pilot.pause(delay=0.2)
                    keys = {b.key for b in app.screen.BINDINGS}
                    self.assertIn("f12", keys)
                shots = os.path.join(tmp, "screenshots")
                files = os.listdir(shots) if os.path.isdir(shots) else []
                self.assertTrue(any(name.endswith(".svg") for name in files), files)
                self.assertTrue(os.path.isfile(events_path))
                with open(events_path, encoding="utf-8") as fh:
                    body = fh.read()
                self.assertIn('"name": "screenshot"', body)

    async def test_embed_pane_background_matches_real_terminal_bg(self) -> None:
        """回归测试：内嵌 Agent 画面里的"默认背景"格子（tmux 报 bg=-1）必须垫在
        外层终端真实底色上，不能透出 Textual 主题的中性灰——否则整个托管画面看
        着变灰（真机反馈：内嵌 agent tui 背景变中性灰）。断言面板底色 == OSC 11
        探到的真实 RGB。"""

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True, osc_report=b"\x1b]11;rgb:1e1e/1e1e/2e2e\x07")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            pane = _primary_embed_pane(app.screen)
            self.assertEqual(pane.styles.background.rgb, (0x1e, 0x1e, 0x2e))


class MainScreenWorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """主屏退出时不能被长驻刷新线程或首屏等待线程拖住。"""

    async def test_background_refresh_worker_is_cancelled_on_normal_exit(self) -> None:
        store, _ = _make_store()
        store.refresh = mock.Mock(return_value=False)
        app = PickupApp(store, embed_ok=False)

        started_at = time.monotonic()
        with (
            mock.patch("pickup.ui.main_screen.REFRESH_INTERVAL", 0.01),
            mock.patch("pickup.ui.main_screen.REFRESH_INTERVAL_MAX", 0.02),
        ):
            async with app.run_test(size=(100, 30)) as pilot:
                await _wait_until(lambda: store.refresh.call_count > 0)
                worker = next(w for w in app.workers if w.group == "session-refresh")
                await pilot.press("escape")
                await pilot.pause()

        self.assertTrue(worker.is_cancelled)
        self.assertLess(time.monotonic() - started_at, 8.0)

    async def test_initial_load_wait_worker_is_cancelled_on_normal_exit(self) -> None:
        runtime = mock.Mock(id="claude", display_name="Claude")
        registry = pickup.RuntimeRegistry((runtime,))
        with mock.patch.object(pickup.titles, "load_cache", return_value={}):
            store = pickup.SessionStore(limit=20, registry=registry)
        app = PickupApp(store, embed_ok=False)

        started_at = time.monotonic()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.05)
            worker = next(w for w in app.workers if w.group == "initial-load")
            await pilot.press("escape")
            await pilot.pause()

        self.assertTrue(worker.is_cancelled)
        self.assertLess(time.monotonic() - started_at, 8.0)


class SessionStoreFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_failure_reaches_terminal_state_and_refresh_recovers(self) -> None:
        runtime = mock.Mock(id="claude", display_name="Claude")
        registry = mock.MagicMock()
        registry.ids = ("claude",)
        registry.get.return_value = runtime
        registry.__iter__.side_effect = lambda: iter((runtime,))
        registry.scan_all.side_effect = [RuntimeError("历史目录暂时不可读"), {"claude": []}]
        with mock.patch.object(pickup.titles, "load_cache", return_value={}):
            store = pickup.SessionStore(limit=20, registry=registry)

        store.load()

        self.assertTrue(store.loaded)
        self.assertTrue(store.wait_loaded(timeout=0))
        self.assertIn("Failed to load sessions", store.get_load_error())
        self.assertIn("历史目录暂时不可读", store.get_load_error())

        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.1)
            search = app.screen.query_one("#project-search", Input)
            self.assertIn("Failed to load sessions", search.placeholder)
            self.assertIn("retrying", search.placeholder)

            self.assertFalse(store.refresh())
            self.assertIsNone(store.get_load_error())
            app.screen._update_header()
            self.assertNotIn("Failed", search.placeholder)
            self.assertNotIn("retrying", search.placeholder)


class SessionStoreRemoveSessionTests(unittest.TestCase):
    """store.remove_session：删除动作成功后立即摘除内存状态，不必等下一轮 refresh()。"""

    def test_remove_session_clears_every_key_indexed_structure(self) -> None:
        store, _ = _make_store()
        key = "claude:s0"
        session = store.find_session(key)
        self.assertIsNotNone(session)
        # 人为塞满所有按 key 索引的结构，验证 remove_session 逐一清干净。
        store.display_titles[key] = "标题"
        store.generating.add(key)
        store.conversations[key] = (1.0, [])
        store.hosted[key] = "pickup-claude-fake"
        store._provisional[key] = dict(session)
        store._force_ended.add(key)

        store.remove_session(key)

        self.assertIsNone(store.find_session(key))
        self.assertNotIn(key, store._order)
        self.assertNotIn(key, store.display_titles)
        self.assertNotIn(key, store.generating)
        self.assertNotIn(key, store.conversations)
        self.assertNotIn(key, store.hosted)
        self.assertNotIn(key, store._provisional)
        self.assertNotIn(key, store._force_ended)

    def test_remove_session_leaves_other_sessions_untouched(self) -> None:
        store, _ = _make_store()
        store.remove_session("claude:s0")
        self.assertIsNotNone(store.find_session("claude:s1"))
        self.assertIsNotNone(store.find_session("claude:s2"))

    def test_mark_deleted_blocks_merge_scanned_reinsert(self) -> None:
        store, _ = _make_store()
        key = "claude:s0"
        session = store.find_session(key)
        self.assertIsNotNone(session)
        scanned = {
            runtime_id: list(bucket)
            for runtime_id, bucket in store.sessions.items()
        }
        store.mark_deleted(key)
        self.assertIsNone(store.find_session(key))
        store._merge_scanned(scanned)
        self.assertIsNone(store.find_session(key))

    def test_deleted_tombstone_survives_later_stale_merges(self) -> None:
        """删除成功后 tombstone 不解除，晚到的旧扫描结果不得把卡片灌回来。

        后台重扫是「先读磁盘、后合并」两段式，删除很容易落在中间；tombstone 若
        随删除成功解除，那轮旧数据合并回来就是用户看到的「删掉的会话又冒出来、
        过几秒才真的消失」。
        """
        store, _ = _make_store()
        key = "claude:s0"
        stale = {
            runtime_id: list(bucket)
            for runtime_id, bucket in store.sessions.items()
        }
        store.mark_deleted(key)
        for _ in range(3):  # 模拟后续多轮扫描仍带着删除前的快照
            store._merge_scanned(stale)
            self.assertIsNone(store.find_session(key))

    def test_abort_delete_allows_merge_scanned_restore(self) -> None:
        store, _ = _make_store()
        key = "claude:s0"
        scanned = {
            runtime_id: list(bucket)
            for runtime_id, bucket in store.sessions.items()
        }
        store.mark_deleted(key)
        store.abort_delete(key)
        store._merge_scanned(scanned)
        self.assertIsNotNone(store.find_session(key))


class SessionCardVisualTests(unittest.TestCase):
    """侧边栏两行卡片的列布局和状态样式不能随刷新优化再次回退。"""

    @staticmethod
    def _card(
        *,
        live=False,
        keepalive_name=None,
        source="opencode",
        display_name="OpenCode",
        display_title="修复侧边栏展示",
        cwd="/tmp/pickup",
    ) -> SessionCard:
        runtime = mock.Mock(id=source, display_name=display_name)
        store = mock.Mock()
        store.registry.get.return_value = runtime
        session = {
            "source": source,
            "id": "visual-check",
            "fallback_title": display_title,
            "cwd": cwd,
            "mtime": time.time(),
            "live": live,
        }
        if keepalive_name is not None:
            session["keepalive_name"] = keepalive_name
        return SessionCard(
            session,
            store,
            display_title=display_title,
        )

    def test_runtime_is_right_aligned_on_second_line_at_fixed_width(self) -> None:
        card = self._card()
        with mock.patch.object(
            SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 3),
        ):
            rendered = card.render()

        lines = rendered.plain.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertNotIn("OpenCode", lines[0])
        self.assertTrue(lines[1].endswith("OpenCode"))
        self.assertEqual(pickup._text_width(lines[0]), 39)
        self.assertEqual(pickup._text_width(lines[1]), 39)
        relative_time = pickup._format_relative_time(card.session["mtime"])
        self.assertTrue(lines[2].endswith(relative_time))

    def test_just_now_time_is_bold_older_relative_time_is_not(self) -> None:
        """「刚刚」加粗；跨过 3 分钟阈值后的相对时间保持常规字重。"""
        now = time.time()
        fresh = self._card()
        fresh.session["mtime"] = now - 30
        older = self._card()
        older.session["mtime"] = now - 240  # 4 分钟前 → "4m ago"
        with mock.patch.object(
            SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 3),
        ):
            fresh_text = fresh.render()
            older_text = older.render()

        just_now = pickup._format_relative_time(fresh.session["mtime"], now)
        self.assertEqual(just_now, "just now")
        just_idx = fresh_text.plain.rfind(just_now)
        self.assertGreaterEqual(just_idx, 0)
        just_spans = [
            span for span in fresh_text.spans
            if span.start <= just_idx < span.end
        ]
        self.assertTrue(
            any("bold" in str(span.style).lower() for span in just_spans),
            f"just now should be bold, spans={just_spans}",
        )

        older_label = pickup._format_relative_time(older.session["mtime"], now)
        self.assertEqual(older_label, "4m ago")
        older_idx = older_text.plain.rfind(older_label)
        self.assertGreaterEqual(older_idx, 0)
        older_spans = [
            span for span in older_text.spans
            if span.start <= older_idx < span.end
        ]
        self.assertFalse(
            any("bold" in str(span.style).lower() for span in older_spans),
            f"older relative time should not be bold, spans={older_spans}",
        )

    def test_long_title_is_hard_truncated_without_ellipsis(self) -> None:
        """标题放不下时直接截断，不写 `...`：省略号只会白占三格。"""
        card = self._card(
            display_title="这是一个非常非常非常长的侧边栏标题用来验证省略",
        )
        with mock.patch.object(
            SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 3),
        ):
            rendered = card.render()

        lines = rendered.plain.splitlines()
        self.assertNotIn("OpenCode", lines[0])
        self.assertTrue(lines[1].endswith("OpenCode"))
        self.assertNotIn("...", lines[0])
        self.assertNotIn("…", lines[0])
        # 截断处仍是标题正文的字符，且整行铺满可用宽度
        self.assertIn("这是一个非常", lines[0])
        self.assertEqual(pickup._text_width(lines[0]), 39)

    def test_runtime_label_uses_distinct_brand_colors(self) -> None:
        cases = (
            ("claude", "Claude", "#D97757"),
            ("codex", "Codex", "#60A5FA"),
            ("cursor", "Cursor", "#A78BFA"),
            ("kimi", "Kimi", "#F472B6"),
            ("opencode", "OpenCode", "#34D399"),
        )
        for source, display_name, color in cases:
            card = self._card(source=source, display_name=display_name)
            with self.subTest(source=source), mock.patch.object(
                SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 3),
            ):
                rendered = card.render()
            runtime_start = rendered.plain.index(display_name)
            runtime_spans = [
                span for span in rendered.spans
                if span.start <= runtime_start < span.end
            ]
            self.assertTrue(
                any(color.lower() in str(span.style).lower() for span in runtime_spans),
                f"{source} runtime should use {color}, spans={runtime_spans}",
            )
            self.assertTrue(
                any("bold" in str(span.style).lower() for span in runtime_spans),
                f"{source} runtime label should be bold, spans={runtime_spans}",
            )

    def test_project_name_is_one_shade_lighter_than_title(self) -> None:
        """首行两段的分工：同为 bold 拉开与下面两行的层级，但项目名 dim 一档。

        项目名是定位用的前缀，同亮度时会和标题抢视线；标题本身必须保持不 dim。
        """
        card = self._card(cwd="/tmp/pickup", display_title="修复侧边栏展示")
        with mock.patch.object(
            SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 3),
        ):
            rendered = card.render()

        lines = rendered.plain.splitlines()
        first_line = lines[0]
        project_start = first_line.index("pickup")
        project_end = project_start + len("pickup")
        title_start = first_line.index("修复侧边栏展示")

        project_spans = [
            span for span in rendered.spans
            if span.start <= project_start and span.end >= project_end
        ]
        self.assertTrue(
            any("bold" in str(span.style).lower() for span in project_spans),
            f"project name should be bold, spans={project_spans}",
        )
        self.assertTrue(
            any("dim" in str(span.style).lower() for span in project_spans),
            f"project name should be one shade lighter than the title, spans={project_spans}",
        )
        title_spans = [
            span for span in rendered.spans
            if span.start <= title_start < span.end and span.end <= len(first_line)
        ]
        self.assertTrue(
            any("bold" in str(span.style).lower() for span in title_spans),
            f"title should be bold like the project name, spans={title_spans}",
        )
        self.assertFalse(
            any("dim" in str(span.style).lower() for span in title_spans),
            f"title should not be dim, spans={title_spans}",
        )

    def test_group_member_title_omits_project_name(self) -> None:
        """组内子项挂在已写项目的组卡下，标题前不再重复项目名。"""
        card = self._card(cwd="/tmp/pickup", display_title="修复侧边栏展示")
        card.tree_position = "last"
        with mock.patch.object(
            SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 3),
        ):
            rendered = card.render()

        first_line = rendered.plain.splitlines()[0]
        self.assertTrue(first_line.startswith("└─ "))
        self.assertNotIn("pickup", first_line)
        self.assertIn("修复侧边栏展示", first_line)

    def test_group_tree_prefix_is_not_dim(self) -> None:
        """树线贴左缘后只剩两列，再用 dim 会糊成灰影；不得用 dim。"""
        card = self._card(display_title="修复侧边栏展示")
        card.tree_position = "middle"
        with mock.patch.object(
            SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 3),
        ):
            rendered = card.render()

        # 未挂载时树线吃默认前景（无 span 也行）；一旦有覆盖第 0 列的 span，不能是 dim。
        covering = [
            span for span in rendered.spans
            if span.start <= 0 < span.end
        ]
        self.assertFalse(
            any("dim" in str(span.style).lower() for span in covering),
            f"tree prefix must not be dim, spans={covering}",
        )
        first_line, runtime_line, time_line = rendered.plain.splitlines()
        self.assertTrue(first_line.startswith("├─ "))
        # 三行第 0 列都是竖向框线，终端里才能连成一条不断的树干。
        self.assertTrue(runtime_line.startswith("│"))
        self.assertTrue(time_line.startswith("│"))

    def test_sidebar_shows_no_generating_spinner(self) -> None:
        """标题生成期间侧边栏不再显示任何「加载中」转圈动画：无关注圆点时首行
        直接以项目名开头，不出现 braille spinner 帧或任何转圈占位字符。"""
        card = self._card()
        with mock.patch.object(
            SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 3),
        ):
            rendered = card.render()

        first_line = rendered.plain.splitlines()[0]
        self.assertTrue(first_line.startswith("pickup "))
        for frame in pickup.SPINNER_FRAMES:
            self.assertNotIn(frame, first_line)

    def test_title_color_is_uniform_across_lifecycle_states(self) -> None:
        """运行阶段由第二行圆点表达；标题不再整行变绿，也不展示状态文案。"""
        cases = (
            (self._card(live=True), "live"),
            (self._card(keepalive_name="pickup-opencode-visual"), "hosted"),
            (self._card(), "ended"),
        )
        for card, label in cases:
            with self.subTest(status=label), mock.patch.object(
                SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 3),
            ):
                rendered = card.render()

            plain = rendered.plain
            self.assertNotIn("Running", plain)
            self.assertNotIn("Ended", plain)
            self.assertNotIn("hosted", plain.lower())
            title_end = plain.index("\n")
            green_spans = [
                span for span in rendered.spans
                if "#3f9a6a" in str(span.style).lower()
                and span.start < title_end
            ]
            self.assertFalse(green_spans)


class SidebarVisualLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_and_card_spacing_are_explicit(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            search = app.screen.query_one("#project-search", Input)
            list_view = app.screen.query_one(SessionListView)
            items = list(list_view.list_children)
            cards = list(app.screen.query(SessionCard))

            # 筛选条用表面层弱化文字，不再白字铺高饱和主色底
            self.assertEqual(search.styles.background, Color.parse("#1C2430"))
            self.assertLess(search.styles.color.a, 1.0)
            self.assertEqual(search.region.height, 2)  # 正文 1 + 末行间隔 1
            self.assertGreaterEqual(len(items), 3)
            # 分隔空行画在卡片自身内，两项 region 紧挨、无外边距空隙
            self.assertEqual(items[1].region.y - items[0].region.bottom, 0)
            self.assertEqual(items[2].region.y - items[1].region.bottom, 0)
            self.assertTrue(cards)
            self.assertTrue(all(card.region.height == 3 for card in cards))
            new_card = app.screen.query_one(NewSessionCard)
            self.assertEqual(new_card.region.height, 2)
            # 搜索框底边紧贴新建项顶边（搜索的末行间隔已含在 height: 2 内）
            self.assertEqual(items[0].region.y - search.region.bottom, 0)

    async def test_ended_card_title_is_dimmed_to_80_percent(self) -> None:
        """已结束会话标题吃卡片基础色：主题前景压到 8 成，不用满亮白字铺整栏。

        进行中标题（显式成功绿）和运行时名（品牌色）自带颜色，不受这条影响。
        """
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            card = next(iter(app.screen.query(SessionCard)))

            self.assertAlmostEqual(card.styles.color.a, 0.8, places=2)
            theme_fg = Color.parse(app.current_theme.foreground)
            self.assertEqual(
                (card.styles.color.r, card.styles.color.g, card.styles.color.b),
                (theme_fg.r, theme_fg.g, theme_fg.b),
                "基础色必须跟随主题前景，不能写死某个灰值",
            )
            # 与背景混合后真的比满亮前景暗
            resolved = card.rich_style.color.triplet
            self.assertLess(sum(resolved), theme_fg.r + theme_fg.g + theme_fg.b)

    async def test_time_line_brightness_steps_down_with_age(self) -> None:
        """第三行时间按新鲜度分四档亮度：半小时内与标题同亮，越旧越暗。"""
        now = time.time()
        ages = [60, 3600, 7 * 3600, 3 * 86400]  # fresh / recent / today / old
        sessions = [
            {
                "source": "claude", "id": f"t{i}", "short_id": f"t{i}",
                "mtime": now - age, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"会话{i}",
                "cwd": "/tmp", "live": False,
            }
            for i, age in enumerate(ages)
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            cards = list(app.screen.query(SessionCard))
            self.assertEqual(len(cards), len(ages))
            brightness = []
            for card in cards:
                tier = card._time_tier()
                style = card._time_style(tier)
                self.assertIsNotNone(style.color, "挂载后必须解析出真实档位色")
                self.assertIsNone(
                    style.bgcolor,
                    "时间行不能自带背景色，否则会盖掉整行的选中/分屏底色",
                )
                brightness.append(sum(style.color.triplet))
            self.assertEqual(
                [card._time_tier() for card in cards],
                ["fresh", "recent", "today", "old"],
            )
            # 严格递减：四档必须肉眼可分，不能两档撞成同一个颜色
            self.assertEqual(sorted(brightness, reverse=True), brightness)
            self.assertEqual(len(set(brightness)), len(ages))
            # 最新一档与标题同色（都吃卡片基础色 $foreground 80%）
            self.assertEqual(
                cards[0]._time_style("fresh").color.triplet,
                cards[0].rich_style.color.triplet,
            )


class SidebarSplitHighlightTests(unittest.IsolatedAsyncioTestCase):
    """右栏分屏时整组铺底，当前激活子会话再重一档。"""

    @staticmethod
    def _items(app):
        list_view = app.screen.query_one(SessionListView)
        return list_view, list_view._session_items()

    async def _seed_group(self, list_view: SessionListView) -> list[str]:
        keys = [
            pickup.session_key(session)
            for session in list_view.store.all_sessions()
        ][:2]
        list_view.on_layout_change(lambda s: s.set_group("/tmp", keys, focus_key=keys[0]))
        await list_view.rebuild()
        return keys

    async def test_split_marks_group_and_active_session(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view, _ = self._items(app)
            keys = await self._seed_group(list_view)

            list_view.set_split_marks(keys[:2], keys[1])
            # 光标停在激活子会话上，避免「列表选中高亮」沾到未激活成员上干扰底色断言。
            list_view.select_session_key(keys[1])
            await pilot.pause()
            group_row = list_view._group_items()[0][0]
            session_rows = {
                pickup.session_key(card.session): item
                for item, card in list_view._session_items()
            }
            active_row = session_rows[keys[1]]
            inactive_row = session_rows[keys[0]]
            self.assertTrue(group_row.has_class("-in-split"))
            self.assertTrue(active_row.has_class("-split-active"))
            self.assertTrue(inactive_row.has_class("-in-split"))
            self.assertFalse(inactive_row.has_class("-split-active"))
            # 组标题与未激活成员同档铺底，激活子会话再重一档
            plain_bg = app.screen.query_one(SessionListView).styles.background
            active_bg = active_row.styles.background
            listed_bg = group_row.styles.background
            inactive_bg = inactive_row.styles.background
            self.assertGreater(
                self._weight(active_bg, app), self._weight(listed_bg, app)
            )
            self.assertGreater(
                self._weight(listed_bg, app), self._weight(plain_bg, app)
            )
            self.assertEqual(inactive_bg, listed_bg)

    async def test_selecting_group_card_highlights_whole_group(self) -> None:
        """光标停在会话组卡上时整组铺底，激活格对应成员仍格外高光。"""
        sessions = [
            {
                "source": "claude", "id": f"s{i}", "short_id": f"s{i}",
                "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"会话{i}",
                "cwd": "/tmp", "live": True,
                "keepalive_name": f"pickup-claude-s{i}",
            }
            for i in range(2)
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.is_alive", return_value=True):
            async with app.run_test(size=(160, 30)) as pilot:
                await pilot.pause(delay=0.2)
                list_view = app.screen.query_one(SessionListView)
                area = app.screen.query_one(SplitPaneArea)
                keys = [pickup.session_key(s) for s in sessions]
                list_view.on_layout_change(lambda s: s.set_group("/tmp", keys, focus_key=keys[0]))
                await list_view.rebuild()
                area.show_hosted_group(
                    "/tmp",
                    [(s, s["keepalive_name"], lambda: "") for s in sessions],
                    focus_key=keys[0],
                )
                await _wait_until(lambda: len(area._cells()) == 2)  # noqa: SLF001

                # 先停在某个成员上：该成员应有 -split-active
                list_view.select_session_key(keys[0])
                await pilot.pause()
                app.screen._sync_split_marks()  # noqa: SLF001
                member0 = next(
                    item
                    for item, card in list_view._session_items()
                    if pickup.session_key(card.session) == keys[0]
                )
                self.assertTrue(member0.has_class("-split-active"))

                # 点到 / 选中组卡：整组（Group 行 + 成员）高光，激活格再重一档
                group_row = list_view._group_items()[0][0]
                list_view.index = list(list_view.list_children).index(group_row)
                await pilot.pause()
                app.screen._sync_split_marks()  # noqa: SLF001
                self.assertTrue(group_row.has_class("-in-split"))
                self.assertTrue(group_row.has_class("-group-selected"))
                session_rows = {
                    pickup.session_key(card.session): item
                    for item, card in list_view._session_items()
                }
                self.assertTrue(session_rows[keys[0]].has_class("-in-split"))
                self.assertTrue(session_rows[keys[0]].has_class("-group-selected"))
                self.assertTrue(session_rows[keys[0]].has_class("-split-active"))
                self.assertTrue(session_rows[keys[1]].has_class("-in-split"))
                self.assertTrue(session_rows[keys[1]].has_class("-group-selected"))
                self.assertFalse(session_rows[keys[1]].has_class("-split-active"))
                group_bg = group_row.styles.background
                inactive_bg = session_rows[keys[1]].styles.background
                active_bg = session_rows[keys[0]].styles.background
                self.assertEqual(inactive_bg, group_bg)
                self.assertGreater(
                    self._weight(active_bg, app), self._weight(group_bg, app)
                )

                # 改选某个成员：整组选中态收回，只剩分屏铺底 + 该行光标
                list_view.select_session_key(keys[1])
                await pilot.pause()
                app.screen._sync_split_marks()  # noqa: SLF001
                self.assertFalse(group_row.has_class("-group-selected"))
                self.assertFalse(session_rows[keys[0]].has_class("-group-selected"))
                self.assertFalse(session_rows[keys[1]].has_class("-group-selected"))

    @staticmethod
    def _weight(color, app) -> float:
        """底色的「显著程度」：深色主题下越亮越重，浅色主题下越深越重。"""
        total = color.r + color.g + color.b
        return -total if app.current_theme.dark is False else total

    async def test_keyboard_cursor_on_a_marked_row_never_dims_it(self) -> None:
        """光标停到组合行上必须更重，不能被列表自身的选中底色压回去。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view, _ = self._items(app)
            keys = await self._seed_group(list_view)
            list_view.set_split_marks(keys[:2], keys[1])
            list_view.focus()
            # 先停在「＋ 新建」，避免组选中把成员也抬到光标档，污染四级阶梯的「静止」色。
            list_view.index = 0
            await pilot.pause()

            group_row = list_view._group_items()[0][0]
            active_row = next(
                item
                for item, card in list_view._session_items()
                if pickup.session_key(card.session) == keys[1]
            )
            listed_bg = group_row.styles.background
            active_bg = active_row.styles.background
            list_view.index = list(list_view.list_children).index(group_row)
            await pilot.pause()
            listed_cursor_bg = group_row.styles.background
            list_view.index = list(list_view.list_children).index(active_row)
            await pilot.pause()
            active_cursor_bg = active_row.styles.background

            ladder = [listed_bg, listed_cursor_bg, active_bg, active_cursor_bg]
            weights = [self._weight(c, app) for c in ladder]
            self.assertEqual(sorted(weights), weights, f"四级底色必须单调：{ladder}")
            self.assertEqual(len(set(weights)), 4, f"四级底色不能重复：{ladder}")

    async def test_single_pane_and_placeholder_keys_are_not_marked(self) -> None:
        """单格不标（列表光标本身就指着它），新建提示这类占位键也不参与。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view, items = self._items(app)
            keys = [pickup.session_key(card.session) for _, card in items]

            list_view.set_split_marks([keys[0]], keys[0])
            await pilot.pause()
            self.assertEqual(list_view.split_marks(), ([], None))
            self.assertFalse(any(item.has_class("-split-active") for item, _ in items))

            list_view.set_split_marks(["__hint__", keys[0]], keys[0])
            await pilot.pause()
            self.assertEqual(list_view.split_marks(), ([], None))

    async def test_marks_survive_list_rebuild(self) -> None:
        """后台重扫会重建全部列表项，分屏底色必须跟着重新贴上。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view, _ = self._items(app)
            keys = await self._seed_group(list_view)
            list_view.set_split_marks(keys[:2], keys[0])
            await pilot.pause()

            # 制造一次真正的全量重建：会话集合变了（少一条）
            store.sessions["claude"] = store.sessions["claude"][:-1]
            await list_view.rebuild()
            await pilot.pause()

            group_row = list_view._group_items()[0][0]
            active_row = next(
                item
                for item, card in list_view._session_items()
                if pickup.session_key(card.session) == keys[0]
            )
            self.assertTrue(group_row.has_class("-in-split"))
            self.assertTrue(active_row.has_class("-split-active"))


class SidebarStripeTests(unittest.IsolatedAsyncioTestCase):
    """侧边栏块级斑马纹：独立会话一块、会话组一块，分隔线后重置。"""

    def _stripe_by_identity(self, list_view: SessionListView) -> dict[str, bool]:
        return {row.identity: row.stripe for row in list_view._sidebar_rows()}

    def _assert_dom_matches_rows(self, list_view: SessionListView) -> None:
        rows = list_view._sidebar_rows()
        new_item = list_view.list_children[0]
        self.assertEqual(new_item.id, NEW_SESSION_ID)
        self.assertFalse(new_item.children[0].has_class("-stripe"))
        for item, row in zip(
            [child for child in list_view.list_children if child.id != NEW_SESSION_ID],
            rows,
            strict=True,
        ):
            card = item.children[0]
            if isinstance(card, (SessionCard, SessionGroupCard)):
                self.assertEqual(
                    card.has_class("-stripe"),
                    row.stripe,
                    f"{row.identity} DOM 条纹与 rows 不一致",
                )
            else:
                self.assertFalse(card.has_class("-stripe"))
                self.assertFalse(row.stripe)

    async def test_independent_sessions_alternate_by_block(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            keys = [
                pickup.session_key(session) for session in store.all_sessions()
            ]
            self.assertGreaterEqual(len(keys), 3)
            stripes = self._stripe_by_identity(list_view)
            self.assertEqual(stripes[keys[0]], False)
            self.assertEqual(stripes[keys[1]], True)
            self.assertEqual(stripes[keys[2]], False)
            self._assert_dom_matches_rows(list_view)

    async def test_group_and_members_share_phase(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            sessions = store.all_sessions()
            group_keys = [
                pickup.session_key(session) for session in sessions[:2]
            ]
            independent_key = pickup.session_key(sessions[2])
            list_view.on_layout_change(
                lambda s: s.set_group("/tmp", group_keys, focus_key=group_keys[0])
            )
            await list_view.rebuild()
            rows = list_view._sidebar_rows()
            self.assertEqual(rows[0].kind, "group")
            self.assertFalse(rows[0].stripe)
            members = [row for row in rows if row.kind == "session" and row.tree_position]
            self.assertEqual(len(members), 2)
            self.assertTrue(all(not row.stripe for row in members))
            independent = next(row for row in rows if row.identity == independent_key)
            self.assertTrue(independent.stripe)
            self._assert_dom_matches_rows(list_view)

    async def test_collapse_does_not_flip_later_blocks(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            sessions = store.all_sessions()
            group_keys = [
                pickup.session_key(session) for session in sessions[:2]
            ]
            independent_key = pickup.session_key(sessions[2])
            list_view.on_layout_change(
                lambda s: s.set_group("/tmp", group_keys, focus_key=group_keys[0])
            )
            await list_view.rebuild()
            expanded = self._stripe_by_identity(list_view)
            group_id = list_view.group_store.get_group(group_keys[0]).group_id
            list_view.on_layout_change(lambda s: s.set_collapsed(group_id, True))
            await list_view.rebuild()
            collapsed = self._stripe_by_identity(list_view)
            self.assertEqual(expanded[independent_key], collapsed[independent_key])
            self.assertTrue(collapsed[independent_key])
            self.assertEqual(
                collapsed[f"{GROUP_ID_PREFIX}{group_id}"],
                expanded[f"{GROUP_ID_PREFIX}{group_id}"],
            )
            self._assert_dom_matches_rows(list_view)

    async def test_separator_resets_phase(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            sessions = store.all_sessions()
            pinned_key = pickup.session_key(sessions[0])
            unpinned = [pickup.session_key(session) for session in sessions[1:]]
            list_view.on_layout_change(lambda s: s.toggle_session_pin(pinned_key))
            await list_view.rebuild()
            rows = list_view._sidebar_rows()
            kinds = [row.kind for row in rows]
            self.assertEqual(kinds[0], "session")
            self.assertEqual(kinds[1], "separator")
            self.assertFalse(rows[0].stripe)
            self.assertFalse(rows[1].stripe)
            self.assertEqual(rows[2].identity, unpinned[0])
            self.assertFalse(rows[2].stripe)
            self.assertEqual(rows[3].identity, unpinned[1])
            self.assertTrue(rows[3].stripe)
            self._assert_dom_matches_rows(list_view)

    async def test_stripes_survive_in_place_and_full_rebuild(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            before = self._stripe_by_identity(list_view)
            store.all_sessions()[0]["mtime"] = time.time() + 10
            await list_view.rebuild()
            self.assertEqual(self._stripe_by_identity(list_view), before)
            self._assert_dom_matches_rows(list_view)

            store.sessions["claude"] = store.sessions["claude"][:-1]
            await list_view.rebuild()
            after = self._stripe_by_identity(list_view)
            self.assertEqual(len(after), len(before) - 1)
            remaining = [
                pickup.session_key(session) for session in store.all_sessions()
            ]
            self.assertEqual(after[remaining[0]], False)
            self.assertEqual(after[remaining[1]], True)
            self._assert_dom_matches_rows(list_view)

    async def test_stripe_background_is_translucent(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            striped = next(
                card
                for card in list_view.query(SessionCard)
                if card.has_class("-stripe")
            )
            alpha = striped.styles.background.a
            self.assertGreater(alpha, 0)
            self.assertLess(alpha, 1)


class SessionGroupSidebarTests(unittest.IsolatedAsyncioTestCase):
    """会话组在侧边栏按三行组卡 + 缩进子会话展示，并支持折叠与置顶。"""

    async def _grouped_app(self):
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        return store, app

    async def test_collapsed_group_card_summarizes_member_attention(self) -> None:
        store, app = await self._grouped_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            sessions = store.all_sessions()
            sessions[0]["attention_kind"] = "working"
            sessions[1]["attention_kind"] = "waiting"
            keys = [pickup.session_key(session) for session in sessions[:2]]
            list_view.on_layout_change(lambda s: s.set_group("/tmp", keys, focus_key=keys[0]))
            await list_view.rebuild()

            group_cards = list(list_view.query(SessionGroupCard))
            self.assertEqual(len(group_cards), 1)
            group_text = group_cards[0].render().plain
            lines = group_text.splitlines()
            self.assertEqual(len(lines), 3)
            emoji = _split_layout.group_emoji(group_cards[0].group.name)
            self.assertTrue(emoji, "水果组名必须能取到对应 emoji")
            self.assertTrue(group_text.startswith(f"▼ {emoji} Group "))
            self.assertNotIn("●", lines[0], "会话组标题不能重复显示会话状态圆点")
            # 第二行项目名与第一行 Group 文字同列起笔（按终端显示宽度比较，
            # emoji 是宽字符：1 个 Python 字符占 2 格，不能直接比字符下标）。
            from rich.cells import cell_len

            group_col = cell_len(lines[0][: lines[0].index("Group")])
            self.assertTrue(lines[1].startswith("│"))
            self.assertTrue(lines[2].startswith("│"))
            tmp_col = cell_len(lines[1][: lines[1].find("tmp")])
            self.assertEqual(tmp_col, group_col)
            # 组标题下的树干要无缝接到首个成员分叉。
            self.assertEqual(lines[2].strip(), "│")

            group_cards[0].group.collapsed = True
            collapsed_lines = group_cards[0].render().plain.splitlines()
            self.assertIn("● Waiting 1", collapsed_lines[2])
            self.assertIn("● Working 1", collapsed_lines[2])

            child_cards = [
                card
                for card in list_view.query(SessionCard)
                if pickup.session_key(card.session) in keys
            ]
            self.assertEqual(len(child_cards), 2)
            first_child_line = child_cards[0].render().plain.splitlines()[0]
            last_child_line = child_cards[1].render().plain.splitlines()[0]
            self.assertTrue(first_child_line.startswith("├─ "))
            self.assertTrue(last_child_line.startswith("└─ "))
            self.assertIn("●", first_child_line)
            # 组卡第二行已经写了项目，子项标题前不再重复「tmp 」前缀。
            self.assertNotRegex(first_child_line, r"^[├└]─\s*(?:●\s+)?tmp\s")
            self.assertNotRegex(last_child_line, r"^[├└]─\s*(?:●\s+)?tmp\s")
            # 中间项续行竖线与分叉同列，避免三行高度把树干扯断。
            mid_lines = child_cards[0].render().plain.splitlines()
            self.assertTrue(mid_lines[1].startswith("│"))
            self.assertTrue(mid_lines[2].startswith("│"))
            self.assertEqual(len(list(list_view.query(SessionCard))), len(sessions))

    async def test_space_collapses_and_expands_selected_group(self) -> None:
        store, app = await self._grouped_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            keys = [
                pickup.session_key(session)
                for session in store.all_sessions()[:2]
            ]
            list_view.on_layout_change(lambda s: s.set_group("/tmp", keys, focus_key=keys[0]))
            await list_view.rebuild()
            list_view.focus()
            list_view.index = 1

            await pilot.press("space")
            await _wait_until(
                lambda: list_view.group_store.get_group(keys[0]).collapsed
                and len(list(list_view.query(SessionCard))) == 1
            )
            self.assertTrue(
                list(list_view.query(SessionGroupCard))[0].render().plain.startswith("▶")
            )

            await pilot.press("space")
            await _wait_until(
                lambda: not list_view.group_store.get_group(keys[0]).collapsed
                and len(list(list_view.query(SessionCard))) == 3
            )

    async def test_follows_sidebar_memory_changed_by_another_window(self) -> None:
        """另一个 pickup 窗口改了置顶/分组，本窗口要自动跟上，且不覆盖对方。

        侧边栏记忆是多窗口共享的：这里用一个独立的库句柄模拟另一个窗口，本窗口靠
        版本号轮询发现改动。断言两件事——对方的改动进得来，本窗口自己的改动还在。
        """
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            screen = app.screen
            list_view = screen.query_one(SessionListView)
            keys = [
                pickup.session_key(session)
                for session in store.all_sessions()[:3]
            ]
            # 本窗口先置顶一条
            list_view.on_layout_change(lambda s: s.set_group("/tmp", keys[:2], focus_key=keys[0]))
            list_view.on_layout_change(lambda s: s.toggle_session_pin(keys[2]))
            await list_view.rebuild()

            # 另一个窗口：折叠这个组
            other_window = _split_layout.SidebarLayoutDB()
            group_id = list_view.group_store.get_group(keys[0]).group_id
            other_window.set_collapsed(group_id, True)

            screen._poll_layout_state()  # noqa: SLF001 定时器每秒会调，这里直接触发以免等
            await _wait_until(
                lambda: list_view.group_store.groups[group_id].collapsed
                and len(list(list_view.query(SessionCard))) == 1
            )
            # 本窗口自己的置顶没被对方那次写入抹掉
            self.assertIn(keys[2], list_view.group_store.pinned_session_keys)

    async def test_filter_by_group_name_reveals_all_members(self) -> None:
        store, app = await self._grouped_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            keys = [
                pickup.session_key(session)
                for session in store.all_sessions()[:2]
            ]
            list_view.on_layout_change(lambda s: s.set_group("/tmp", keys, focus_key=keys[0]))
            group = list_view.group_store.get_group(keys[0])
            group.collapsed = True
            list_view.nav.project_query = group.name.lower()
            await list_view.rebuild()
            self.assertEqual(len(list(list_view.query(SessionGroupCard))), 1)
            self.assertEqual(len(list(list_view.query(SessionCard))), 2)

    async def test_refresh_keeps_ended_sessions_in_the_group(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            keys = [
                pickup.session_key(session)
                for session in store.all_sessions()[:2]
            ]
            list_view.on_layout_change(lambda s: s.set_group("/tmp", keys, focus_key=keys[0]))
            await app.screen._rebuild_list()  # noqa: SLF001
            self.assertIsNotNone(list_view.group_store.get_group(keys[0]))
            self.assertEqual(len(list(list_view.query(SessionGroupCard))), 1)

    async def test_p_pins_independent_session_and_whole_group(self) -> None:
        store, app = await self._grouped_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            sessions = store.all_sessions()
            keys = [pickup.session_key(session) for session in sessions[:2]]
            independent_key = pickup.session_key(sessions[2])
            list_view.on_layout_change(lambda s: s.set_group("/tmp", keys, focus_key=keys[0]))
            await list_view.rebuild()
            list_view.focus()

            independent_item = next(
                item
                for item, card in list_view._session_items()
                if pickup.session_key(card.session) == independent_key
            )
            list_view.index = list(list_view.list_children).index(independent_item)
            await pilot.press("p")
            await _wait_until(
                lambda: independent_key in list_view.group_store.pinned_session_keys
            )
            first_card = list_view.list_children[1].children[0]
            self.assertIsInstance(first_card, SessionCard)
            self.assertIn("↑", first_card.render().plain.splitlines()[0])

            group_item = list_view._group_items()[0][0]
            list_view.index = list(list_view.list_children).index(group_item)
            await pilot.press("p")
            group = list_view.group_store.get_group(keys[0])
            await _wait_until(
                lambda: group.group_id in list_view.group_store.pinned_group_ids
            )
            first_card = list_view.list_children[1].children[0]
            self.assertIsInstance(first_card, SessionGroupCard)
            self.assertIn("↑", first_card.render().plain.splitlines()[0])

    async def test_pin_separator_between_pinned_and_unpinned(self) -> None:
        """置顶与未置顶都非空时画蓝色横线分隔；仅一侧时不画。"""
        store, app = await self._grouped_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            sessions = store.all_sessions()
            self.assertGreaterEqual(len(sessions), 2)
            pinned_key = pickup.session_key(sessions[0])
            other_key = pickup.session_key(sessions[1])

            # 无置顶 → 无分隔
            self.assertEqual(len(list(list_view.query(PinSeparatorCard))), 0)
            self.assertNotIn(PIN_SEP_ID, list_view._current_row_identities())

            list_view.on_layout_change(lambda s: s.toggle_session_pin(pinned_key))
            await list_view.rebuild()
            seps = list(list_view.query(PinSeparatorCard))
            self.assertEqual(len(seps), 1)
            sep_item = list_view.query_one(f"#{PIN_SEP_ID}")
            self.assertTrue(sep_item.disabled)
            plain = seps[0].render().plain
            self.assertIn("Pinned", plain)
            self.assertNotIn("Today", plain)
            self.assertNotIn("其他", plain)
            self.assertNotIn("Other", plain)

            identities = list_view._current_row_identities()
            self.assertIn(PIN_SEP_ID, identities)
            self.assertNotIn(TODAY_SEP_ID, identities)
            sep_at = identities.index(PIN_SEP_ID)
            self.assertEqual(identities[0], pinned_key)
            self.assertEqual(identities[sep_at + 1], other_key)

            # 键盘从置顶项 ↓ 直接落到未置顶项，跳过分隔
            list_view.focus()
            list_view.index = 1  # 置顶会话（新建项之下）
            list_view.action_cursor_down()
            self.assertEqual(
                pickup.session_key(list_view.selected_session()),
                other_key,
            )
            self.assertNotEqual(
                getattr(list_view.highlighted_child, "id", None),
                PIN_SEP_ID,
            )

            # 全部置顶后分隔消失
            remaining = [
                pickup.session_key(session)
                for session in sessions
                if pickup.session_key(session) != pinned_key
            ]
            for key in remaining:
                list_view.on_layout_change(lambda s, k=key: s.toggle_session_pin(k))
            await list_view.rebuild()
            self.assertEqual(len(list(list_view.query(PinSeparatorCard))), 0)

            # 取消一条置顶后分隔回来，选中不丢
            list_view.select_session_key(pinned_key)
            list_view.on_layout_change(lambda s: s.toggle_session_pin(other_key))
            await list_view.rebuild()
            self.assertEqual(len(list(list_view.query(PinSeparatorCard))), 1)
            self.assertEqual(
                pickup.session_key(list_view.selected_session()),
                pinned_key,
            )

    async def test_filter_new_and_pinned_stay_fixed_when_unpinned_scrolls(self) -> None:
        """筛选框、＋新建、置顶块固定不可滚；指针在固定头上滚轮仍带动未置顶区。"""
        now = time.time()
        sessions = [
            _claude_session("pin-me", now - 60, "置顶会话"),
            *[
                _claude_session(
                    f"old-{i}",
                    now - 3 * pickup.TODAY_SECONDS - i,
                    f"更早{i}",
                )
                for i in range(18)
            ],
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            list_view.on_layout_change(
                lambda s: s.toggle_session_pin("claude:pin-me")
            )
            await list_view.rebuild()
            await pilot.pause()
            search = app.screen.query_one("#project-search", Input)
            new_card = app.screen.query_one(NewSessionCard)
            pinned_card = next(
                card
                for card in app.screen.query(SessionCard)
                if pickup.session_key(card.session) == "claude:pin-me"
            )
            scroll = list_view.query_one("#sidebar-scroll")
            search_y = search.region.y
            new_y = new_card.region.y
            pinned_y = pinned_card.region.y
            self.assertGreater(
                scroll.max_scroll_y, 0, "未置顶区必须长到能滚，否则测不到固定头"
            )
            sticky = list_view.query_one("#sidebar-sticky")
            wheel = events.MouseScrollDown(
                None, 1, 1, 0, 0, 0, False, False, False
            )
            sticky._on_mouse_scroll_down(wheel)
            app.screen.on_mouse_scroll_down(
                events.MouseScrollDown(
                    search, 0, 0, 0, 0, 0, False, False, False
                )
            )
            await pilot.pause()
            self.assertEqual(search.region.y, search_y)
            self.assertEqual(new_card.region.y, new_y)
            self.assertEqual(pinned_card.region.y, pinned_y)
            self.assertGreater(scroll.scroll_y, 0)

    def test_live_or_recent_mtime_counts_as_today(self) -> None:
        """滚动 24 小时界：live 优先，与时间行 today 档共用 TODAY_SECONDS。"""
        now = time.time()
        old = now - 10 * pickup.TODAY_SECONDS
        self.assertTrue(
            _session_in_today_window({"live": True, "mtime": old}, now)
        )
        self.assertFalse(
            _session_in_today_window({"live": False, "mtime": old}, now)
        )
        self.assertTrue(
            _session_in_today_window({"live": False, "mtime": now - 60}, now)
        )
        self.assertTrue(
            _session_in_today_window({"live": False, "mtime": now + 30}, now)
        )
        self.assertFalse(_session_in_today_window(None, now))
        self.assertFalse(_session_in_today_window({}, now))

    def test_separator_render_centers_label(self) -> None:
        card = PinSeparatorCard("list.sep_pinned")
        plain = card.render().plain
        self.assertIn("Pinned", plain)
        self.assertTrue(plain.startswith("─"))
        after = plain.split("Pinned", 1)[1]
        self.assertTrue(after.lstrip().startswith("─") or after.startswith("─"))
        self.assertNotIn("Other", plain)
        self.assertNotIn("其他", plain)

    async def test_today_separator_between_recent_and_older(self) -> None:
        """今天与更早都有时画 Today 线；today 全在线前、older 全在线后。"""
        now = time.time()
        sessions = [
            _claude_session("new-a", now - 60, "新A"),
            _claude_session("new-b", now - 120, "新B"),
            _claude_session("old-c", now - 180, "旧C"),
        ]
        store, _ = _make_store(sessions=sessions)
        store.find_session("claude:old-c")["mtime"] = now - 3 * pickup.TODAY_SECONDS
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            await list_view.rebuild()
            identities = [row.identity for row in list_view._sidebar_rows()]
            self.assertIn(TODAY_SEP_ID, identities)
            self.assertNotIn(PIN_SEP_ID, identities)
            sep_at = identities.index(TODAY_SEP_ID)
            self.assertEqual(
                identities[:sep_at],
                ["claude:new-a", "claude:new-b"],
            )
            self.assertEqual(identities[sep_at + 1 :], ["claude:old-c"])
            plains = [card.render().plain for card in list_view.query(PinSeparatorCard)]
            joined = "\n".join(plains)
            self.assertIn("Today", joined)
            self.assertNotIn("Pinned", joined)
            self.assertNotIn("Other", joined)
            self.assertNotIn("其他", joined)
            self.assertEqual(
                list_view._current_row_identities(),
                identities,
            )

    async def test_today_separator_absent_when_all_recent(self) -> None:
        store, app = await self._grouped_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            identities = [row.identity for row in list_view._sidebar_rows()]
            self.assertNotIn(TODAY_SEP_ID, identities)
            self.assertEqual(len(list(list_view.query(PinSeparatorCard))), 0)

    async def test_today_separator_absent_when_all_old(self) -> None:
        now = time.time()
        sessions = [
            _claude_session("old-a", now - 60, "旧A"),
            _claude_session("old-b", now - 120, "旧B"),
        ]
        store, _ = _make_store(sessions=sessions)
        age = now - 3 * pickup.TODAY_SECONDS
        for session in store.all_sessions():
            session["mtime"] = age
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            await list_view.rebuild()
            identities = [row.identity for row in list_view._sidebar_rows()]
            self.assertNotIn(TODAY_SEP_ID, identities)
            self.assertEqual(len(list(list_view.query(PinSeparatorCard))), 0)

    async def test_unpinned_bucket_keeps_store_relative_order(self) -> None:
        """夹在两个今天会话之间的更早项，分桶后 today 桶仍跟 store 原序。"""
        now = time.time()
        sessions = [
            _claude_session("new-a", now - 60, "新A"),
            _claude_session("mid-old", now - 120, "中间旧"),
            _claude_session("new-b", now - 180, "新B"),
        ]
        store, _ = _make_store(sessions=sessions)
        store.find_session("claude:mid-old")["mtime"] = (
            now - 3 * pickup.TODAY_SECONDS
        )
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            await list_view.rebuild()
            identities = [row.identity for row in list_view._sidebar_rows()]
            self.assertEqual(
                identities,
                [
                    "claude:new-a",
                    "claude:new-b",
                    TODAY_SEP_ID,
                    "claude:mid-old",
                ],
            )

    async def test_group_with_one_recent_member_stays_above_today_line(self) -> None:
        """组内一个新成员 + 两个旧成员 → 整组在 Today 线前。"""
        now = time.time()
        sessions = [
            _claude_session("g-new", now - 60, "新成员"),
            _claude_session("g-old1", now - 120, "旧成员1"),
            _claude_session("g-old2", now - 180, "旧成员2"),
            _claude_session("solo-old", now - 200, "独立旧"),
        ]
        store, _ = _make_store(sessions=sessions)
        age = now - 3 * pickup.TODAY_SECONDS
        for key in ("claude:g-old1", "claude:g-old2", "claude:solo-old"):
            store.find_session(key)["mtime"] = age
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            keys = ["claude:g-new", "claude:g-old1", "claude:g-old2"]
            list_view.on_layout_change(
                lambda s: s.set_group("/tmp", keys, focus_key=keys[0])
            )
            await list_view.rebuild()
            identities = [row.identity for row in list_view._sidebar_rows()]
            self.assertIn(TODAY_SEP_ID, identities)
            sep_at = identities.index(TODAY_SEP_ID)
            before = identities[:sep_at]
            self.assertTrue(before[0].startswith(GROUP_ID_PREFIX))
            self.assertEqual(
                before[1:],
                ["claude:g-new", "claude:g-old1", "claude:g-old2"],
            )
            self.assertEqual(identities[sep_at + 1 :], ["claude:solo-old"])

    async def test_keyboard_skips_both_separators(self) -> None:
        now = time.time()
        sessions = [
            _claude_session("pin-me", now - 60, "置顶"),
            _claude_session("today-b", now - 120, "今天"),
            _claude_session("old-c", now - 180, "更早"),
        ]
        store, _ = _make_store(sessions=sessions)
        store.find_session("claude:old-c")["mtime"] = (
            now - 3 * pickup.TODAY_SECONDS
        )
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            list_view.on_layout_change(
                lambda s: s.toggle_session_pin("claude:pin-me")
            )
            await list_view.rebuild()
            identities = list_view._current_row_identities()
            self.assertEqual(
                identities,
                [
                    "claude:pin-me",
                    PIN_SEP_ID,
                    "claude:today-b",
                    TODAY_SEP_ID,
                    "claude:old-c",
                ],
            )
            list_view.focus()
            list_view.index = 1  # 置顶会话
            list_view.action_cursor_down()
            self.assertEqual(
                pickup.session_key(list_view.selected_session()),
                "claude:today-b",
            )
            self.assertNotIn(
                getattr(list_view.highlighted_child, "id", None),
                {PIN_SEP_ID, TODAY_SEP_ID},
            )
            list_view.action_cursor_down()
            self.assertEqual(
                pickup.session_key(list_view.selected_session()),
                "claude:old-c",
            )
            self.assertNotIn(
                getattr(list_view.highlighted_child, "id", None),
                {PIN_SEP_ID, TODAY_SEP_ID},
            )

    async def test_separator_labels_follow_language(self) -> None:
        now = time.time()
        sessions = [
            _claude_session("pin-me", now - 60, "置顶会话"),
            _claude_session("today-b", now - 120, "今天会话"),
            _claude_session("old-c", now - 180, "更早会话"),
        ]
        store, _ = _make_store(sessions=sessions)
        store.find_session("claude:old-c")["mtime"] = (
            now - 3 * pickup.TODAY_SECONDS
        )
        i18n.set_lang("zh")
        try:
            app = PickupApp(store, embed_ok=False)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(delay=0.2)
                list_view = app.screen.query_one(SessionListView)
                list_view.on_layout_change(
                    lambda s: s.toggle_session_pin("claude:pin-me")
                )
                await list_view.rebuild()
                plains = [
                    card.render().plain for card in list_view.query(PinSeparatorCard)
                ]
                joined = "\n".join(plains)
                self.assertIn("置顶", joined)
                self.assertIn("今天", joined)
                self.assertNotIn("Pinned", joined)
                self.assertNotIn("Today", joined)
                self.assertNotIn("其他", joined)
                self.assertNotIn("Other", joined)
        finally:
            i18n.set_lang("en")

    async def test_newer_independent_session_sorts_above_unpinned_group(self) -> None:
        """未置顶组不霸榜：比组成员更新的独立会话应排在组前面。"""
        now = time.time()
        sessions = [
            {
                "source": "claude", "id": "old-a", "short_id": "old-a",
                "mtime": now - 3600, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "旧成员 A",
                "cwd": "/tmp", "live": False,
            },
            {
                "source": "claude", "id": "old-b", "short_id": "old-b",
                "mtime": now - 3500, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "旧成员 B",
                "cwd": "/tmp", "live": False,
            },
            {
                "source": "claude", "id": "fresh", "short_id": "fresh",
                "mtime": now, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "新建会话",
                "cwd": "/tmp", "live": False,
            },
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            keys = ["claude:old-a", "claude:old-b"]
            list_view.on_layout_change(lambda s: s.set_group("/tmp", keys, focus_key=keys[0]))
            await list_view.rebuild()

            rows = list_view._sidebar_rows()
            kinds = [row.kind for row in rows]
            identities = [row.identity for row in rows]
            self.assertEqual(kinds[0], "session")
            self.assertEqual(identities[0], "claude:fresh")
            self.assertEqual(kinds[1], "group")
            self.assertTrue(identities[1].startswith(GROUP_ID_PREFIX))

    async def test_sidebar_order_stable_when_member_mtime_updates(self) -> None:
        """进入后组成员 mtime 变新，不得把整组顶到侧边栏上方（位置应相对固定）。"""
        now = time.time()
        sessions = [
            {
                "source": "claude", "id": "solo", "short_id": "solo",
                "mtime": now, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "独立会话",
                "cwd": "/tmp", "live": False,
            },
            {
                "source": "claude", "id": "g1", "short_id": "g1",
                "mtime": now - 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "组成员 1",
                "cwd": "/tmp", "live": True,
                "keepalive_name": "pickup-g1",
            },
            {
                "source": "claude", "id": "g2", "short_id": "g2",
                "mtime": now - 200, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "组成员 2",
                "cwd": "/tmp", "live": True,
                "keepalive_name": "pickup-g2",
            },
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            keys = ["claude:g1", "claude:g2"]
            list_view.on_layout_change(lambda s: s.set_group("/tmp", keys, focus_key=keys[0]))
            await list_view.rebuild()
            before = [row.identity for row in list_view._sidebar_rows()]
            self.assertEqual(before[0], "claude:solo")
            self.assertTrue(before[1].startswith(GROUP_ID_PREFIX))

            # 模拟运行中成员写盘：mtime 顶到最新，但 store 顺序不变。
            g1 = store.find_session("claude:g1")
            self.assertIsNotNone(g1)
            g1["mtime"] = now + 10
            await list_view.rebuild()
            after = [row.identity for row in list_view._sidebar_rows()]
            self.assertEqual(after, before, "mtime 更新不得重排侧边栏")


class MainScreenNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_selection_and_project_search_filter(self) -> None:
        """侧边栏顶部搜索框：大小写无关模糊匹配项目名，并同步过滤会话列表。"""
        sessions = [
            {
                "source": "claude", "id": "a", "short_id": "a",
                "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "节点选择",
                "cwd": "/Users/x/ProxyAgent", "live": False,
            },
            {
                "source": "claude", "id": "b", "short_id": "b",
                "mtime": time.time() - 10, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "界面打磨",
                "cwd": "/Users/x/pickup", "live": False,
            },
            {
                "source": "claude", "id": "c", "short_id": "c",
                "mtime": time.time() - 20, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "字幕优化",
                "cwd": "/Users/x/LiveCaptionMac", "live": False,
            },
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            search = app.screen.query_one("#project-search", Input)
            self.assertEqual(list_view.index, 1)  # 默认落在第一条会话，跳过「＋新建」
            self.assertEqual(list_view.selected_session()["id"], "a")
            self.assertEqual(len(list_view.visible_sessions()), 3)
            self.assertIn("Filter groups / projects / titles", search.placeholder)
            self.assertFalse(search.has_class("-active"))

            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(list_view.index, 2)

            await pilot.press("slash")
            await pilot.pause()
            self.assertTrue(search.has_focus)
            await pilot.press("p", "r", "o", "x", "y")
            await pilot.pause()
            self.assertEqual(list_view.nav.project_query, "proxy")
            self.assertTrue(search.has_class("-active"))
            visible = list_view.visible_sessions()
            self.assertEqual(len(visible), 1)
            self.assertEqual(visible[0]["id"], "a")
            self.assertIn("ProxyAgent", visible[0]["cwd"])

            # 清空后恢复全部；Esc 在搜索框有内容时只清查询，不退出
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(list_view.nav.project_query, "")
            self.assertFalse(search.has_class("-active"))
            self.assertEqual(len(list_view.visible_sessions()), 3)
            self.assertIsNone(app.return_value)

            # 会话标题也可被模糊命中
            search.focus()
            await pilot.pause()
            search.value = "界面"
            await pilot.pause()
            visible = list_view.visible_sessions()
            self.assertEqual(len(visible), 1)
            self.assertEqual(visible[0]["id"], "b")

    async def test_switching_session_rebinds_pane_instead_of_remounting(self) -> None:
        """切到另一个会话必须就地改绑同一个格子，不能整排销毁重建。

        销毁重建除了控件开销，还会连带丢掉上一格的实时画面和控制通道——真机上
        表现为每换一个会话右栏都要先空一下再重新连。
        """
        from pickup.ui.split_pane_area import SplitPaneArea

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.open_channel", return_value=None), \
             mock.patch("pickup.embed.should_resize_host", return_value=False):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                area = app.screen.query_one(SplitPaneArea)
                sessions = store.sessions["claude"]
                first, second = sessions[0], sessions[1]
                # 选择跟随会把右栏换回静态预览，挡住它以免和本用例抢右栏
                mock.patch.object(app.screen, "_follow_current_selection").start()
                self.addCleanup(mock.patch.stopall)

                area.show_hosted_group("/tmp", [(first, "pickup-claude-one", None)])
                await _wait_until(
                    lambda: bool(area.cells())
                    and area.cells()[0].embed_pane() is not None
                    and area.cells()[0].embed_pane().session_name == "pickup-claude-one"
                )
                pane_before = area.cells()[0].embed_pane()
                cell_before = area.cells()[0]

                area.show_hosted_group("/tmp", [(second, "pickup-claude-two", None)])
                await _wait_until(
                    lambda: bool(area.cells())
                    and area.cells()[0].embed_pane() is not None
                    and area.cells()[0].embed_pane().session_name == "pickup-claude-two"
                )
                self.assertIs(area.cells()[0], cell_before, "格子被重建了")
                self.assertIs(area.cells()[0].embed_pane(), pane_before, "画面控件被重建了")
                self.assertEqual(area.cells()[0].spec.keepalive_name, "pickup-claude-two")

    async def test_pane_count_change_reuses_pool_without_remount(self) -> None:
        """2↔4 格切换必须复用格池控件，不得 remove/mount 已有格子。"""
        from pickup.split_layout import MAX_PANES
        from pickup.ui.embed_pane import EmbedPane

        sessions = [
            {
                "source": "claude", "id": f"p{i}", "short_id": f"p{i}",
                "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"会话{i}",
                "cwd": "/tmp", "live": True,
                "keepalive_name": f"pickup-claude-p{i}",
            }
            for i in range(MAX_PANES)
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.open_channel", return_value=None), \
             mock.patch("pickup.embed.should_resize_host", return_value=False):
            async with app.run_test(size=(160, 30)) as pilot:
                await pilot.pause(delay=0.2)
                area = app.screen.query_one(SplitPaneArea)
                mock.patch.object(app.screen, "_follow_current_selection").start()
                self.addCleanup(mock.patch.stopall)

                area.show_hosted_group(
                    "/tmp",
                    [(s, s["keepalive_name"], None) for s in sessions[:2]],
                )
                await _wait_until(lambda: len(area.cells()) == 2)
                await _wait_until(lambda: len(area._pool_cells()) == MAX_PANES)  # noqa: SLF001
                pool_before = area._pool_cells()  # noqa: SLF001
                active_before = list(area.cells())

                focus_calls: list[tuple[bool, bool, tuple[int, int] | None]] = []
                original_focus_session = EmbedPane.focus_session

                def _record_focus(pane, *args, **kwargs):
                    focus_calls.append((
                        kwargs.get("resize_immediately", True),
                        kwargs.get("discard_stale_screen", False),
                        kwargs.get("target_size"),
                    ))
                    return original_focus_session(pane, *args, **kwargs)

                with mock.patch.object(EmbedPane, "focus_session", new=_record_focus):
                    area.show_hosted_group(
                        "/tmp",
                        [(s, s["keepalive_name"], None) for s in sessions],
                    )
                    await _wait_until(lambda: len(area.cells()) == MAX_PANES)
                self.assertEqual(
                    [(True, True, call[2]) for call in focus_calls],
                    focus_calls,
                    "格数变化必须立即按最终尺寸重设终端，并禁止铺旧宽缓存",
                )
                self.assertTrue(all(call[2] is not None for call in focus_calls))
                self.assertEqual(area._pool_cells(), pool_before)  # noqa: SLF001
                self.assertIs(area.cells()[0], active_before[0])
                self.assertIs(area.cells()[1], active_before[1])

                area.show_hosted_group(
                    "/tmp",
                    [(s, s["keepalive_name"], None) for s in sessions[:2]],
                )
                await _wait_until(lambda: len(area.cells()) == 2)
                self.assertEqual(area._pool_cells(), pool_before)  # noqa: SLF001
                self.assertIs(area.cells()[0], active_before[0])

    async def test_pane_count_change_resizes_once_at_final_layout_size(self) -> None:
        """分屏格数变化要立即按最终尺寸 resize，布局落定后不得重复一次。"""
        from pickup.split_layout import MAX_PANES

        sessions = [
            {
                "source": "claude", "id": f"resize-{i}", "short_id": f"resize-{i}",
                "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"尺寸会话{i}",
                "cwd": "/tmp", "live": True,
                "keepalive_name": f"pickup-claude-resize-{i}",
            }
            for i in range(MAX_PANES)
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=True)
        resize_calls: list[tuple[str, int, int]] = []
        with (
            mock.patch("pickup.embed.open_channel", return_value=None),
            mock.patch("pickup.embed.should_resize_host", return_value=True),
            mock.patch(
                "pickup.embed.resize",
                side_effect=lambda name, width, height: resize_calls.append(
                    (name, width, height)
                ),
            ),
        ):
            async with app.run_test(size=(160, 30)) as pilot:
                await pilot.pause(delay=0.2)
                area = app.screen.query_one(SplitPaneArea)
                mock.patch.object(app.screen, "_follow_current_selection").start()
                self.addCleanup(mock.patch.stopall)

                area.show_hosted_group(
                    "/tmp",
                    [(s, s["keepalive_name"], None) for s in sessions[:2]],
                )
                await _wait_until(lambda: len(area.cells()) == 2)
                await pilot.pause(delay=0.4)
                resize_calls.clear()

                area.show_hosted_group(
                    "/tmp",
                    [(s, s["keepalive_name"], None) for s in sessions],
                )
                await _wait_until(lambda: len(area.cells()) == MAX_PANES)
                await pilot.pause(delay=0.05)

                expected = [
                    (
                        str(cell.spec.keepalive_name),
                        cell.embed_pane().size.width,
                        cell.embed_pane().size.height,
                    )
                    for cell in area.cells()
                    if cell.embed_pane() is not None
                ]
                self.assertEqual(sorted(resize_calls), sorted(expected))
                await pilot.pause(delay=0.4)
                self.assertEqual(
                    sorted(resize_calls), sorted(expected),
                    "布局落定后的 Resize 不得在 200ms 后再次调回相同尺寸",
                )

    async def test_two_to_one_discards_half_width_screen_before_first_frame(self) -> None:
        """双格切单格不可先把半宽缓存画在左半边，再等 200ms 防抖修正。"""
        from pickup.ui import embed_pane as embed_pane_mod

        sessions = [
            {
                "source": "claude", "id": f"single-{i}", "short_id": f"single-{i}",
                "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"单格会话{i}",
                "cwd": "/tmp", "live": True,
                "keepalive_name": f"pickup-claude-single-{i}",
            }
            for i in range(3)
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=True)
        resize_calls: list[tuple[str, int, int]] = []
        target_name = sessions[2]["keepalive_name"]
        # 这正是切回过双格会话时会命中的旧尺寸缓存；本次切单格必须不用它。
        embed_pane_mod._cache_screen(target_name, [["旧半宽画面"]], None)  # noqa: SLF001
        with (
            mock.patch("pickup.embed.open_channel", return_value=None),
            mock.patch("pickup.embed.should_resize_host", return_value=True),
            mock.patch(
                "pickup.embed.resize",
                side_effect=lambda name, width, height: resize_calls.append(
                    (name, width, height)
                ),
            ),
        ):
            async with app.run_test(size=(160, 30)) as pilot:
                await pilot.pause(delay=0.2)
                area = app.screen.query_one(SplitPaneArea)
                mock.patch.object(app.screen, "_follow_current_selection").start()
                self.addCleanup(mock.patch.stopall)

                area.show_hosted_group(
                    "/tmp",
                    [(s, s["keepalive_name"], None) for s in sessions[:2]],
                )
                await _wait_until(lambda: len(area.cells()) == 2)
                await pilot.pause(delay=0.3)
                resize_calls.clear()

                area.show_hosted_group("/tmp", [(sessions[2], target_name, None)])
                await _wait_until(lambda: len(area.cells()) == 1)
                pane = area.cells()[0].embed_pane()
                self.assertIsNotNone(pane)
                self.assertIsNone(pane._grid, "切单格前不得先恢复旧的半宽缓存")  # noqa: SLF001
                await pilot.pause(delay=0.05)
                expected = (target_name, pane.size.width, pane.size.height)
                self.assertEqual(resize_calls, [expected])
                await pilot.pause(delay=0.35)
                self.assertEqual(
                    resize_calls, [expected],
                    "布局落定后的尺寸回报不得在 200ms 后重复 resize",
                )

    def test_capture_uses_projected_single_pane_size_until_layout_arrives(self) -> None:
        """双格改单格时，首帧解析不得继续沿用旧半格宽度。"""
        pane = EmbedPane()
        pane._capture_size_override = (101, 28)  # noqa: SLF001 预测到的单格最终尺寸
        with mock.patch.object(pane, "_pane_size", return_value=(50, 28)):
            self.assertEqual(
                pane._capture_size(),  # noqa: SLF001 抓帧线程的唯一尺寸入口
                (101, 28),
                "布局尚未更新时，抓帧必须按单格全宽解析，不可补出右半边空白",
            )

    def test_projected_size_is_visible_before_new_session_can_be_captured(self) -> None:
        """新会话名一写入，抓帧线程就必须已经看见单格最终尺寸。"""
        pane = EmbedPane()
        pane.post_message = lambda *args, **kwargs: None  # type: ignore[method-assign]
        observed: list[tuple[int, int]] = []
        with (
            mock.patch.object(pane, "_pane_size", return_value=(50, 28)),
            mock.patch(
                "pickup.embed.open_channel",
                side_effect=lambda *_args, **_kwargs: observed.append(pane._capture_size()),
            ),
            mock.patch("pickup.embed.should_resize_host", return_value=False),
        ):
            pane.focus_session("pickup-claude-target", target_size=(101, 28))
        self.assertEqual(observed, [(101, 28)])

    def test_focus_session_without_target_size_clears_stale_override(self) -> None:
        """切会话未带预测尺寸时，必须丢掉上一格残留的 override。"""
        pane = EmbedPane()
        pane.post_message = lambda *args, **kwargs: None  # type: ignore[method-assign]
        pane._capture_size_override = (24, 28)  # noqa: SLF001
        with (
            mock.patch.object(pane, "_pane_size", return_value=(80, 28)),
            mock.patch("pickup.embed.open_channel", return_value=None),
            mock.patch("pickup.embed.should_resize_host", return_value=False),
        ):
            pane.focus_session("pickup-claude-cleared")
            self.assertIsNone(pane._capture_size_override)  # noqa: SLF001
            self.assertEqual(pane._capture_size(), (80, 28))  # noqa: SLF001

    def test_capture_size_prefers_tmux_real_size_over_widget(self) -> None:
        """稳态抓帧按 tmux 真实列数解析，不能按更宽的格子去补空白。"""
        pane = EmbedPane()
        pane._capture_size_override = None  # noqa: SLF001
        pane._tmux_pane_size = (40, 20)  # noqa: SLF001
        with mock.patch.object(pane, "_pane_size", return_value=(200, 20)):
            self.assertEqual(pane._capture_size(), (40, 20))  # noqa: SLF001

    def test_resize_clears_override_even_when_predicted_size_differs(self) -> None:
        """预测宽与 Textual 实际差 1 列时，Resize 也必须清掉 override。"""
        pane = EmbedPane()
        pane.session_name = "pickup-claude-mismatch"
        pane.dead = False
        pane._capture_size_override = (25, 18)  # noqa: SLF001
        pane._host_size = ("pickup-claude-mismatch", 24, 18)  # noqa: SLF001
        pane._on_resize(events.Resize(Size(24, 18), Size(24, 18)))
        self.assertIsNone(pane._capture_size_override)  # noqa: SLF001

    def test_projected_embed_sizes_match_textual_floor_accumulate(self) -> None:
        """四格余数必须按 Textual floor-accumulate 交错分配，不能堆给末格。"""
        from pickup.ui.split_pane_area import projected_embed_sizes

        sizes = projected_embed_sizes(101, 30, 4)
        self.assertEqual([w for w, _ in sizes], [24, 25, 24, 25])
        self.assertEqual({h for _, h in sizes}, {28})
        self.assertEqual(
            [w for w, _ in projected_embed_sizes(105, 30, 4)], [25, 26, 25, 26],
        )
        self.assertEqual(
            [w for w, _ in projected_embed_sizes(189, 30, 4)], [46, 47, 46, 47],
        )
        # 2 格余 0：均分
        self.assertEqual([w for w, _ in projected_embed_sizes(81, 24, 2)], [40, 40])
        # 3 格：宽度之和等于行宽减去两列间距
        self.assertEqual(sum(w for w, _ in projected_embed_sizes(100, 24, 3)), 98)

    def test_host_size_drift_retries_resize_with_backoff(self) -> None:
        """tmux 真实尺寸落后于格子时，抓帧线程按间隔重发 resize，同一目标最多 3 次。"""
        import pickup.ui.embed_pane as embed_pane_mod

        pane = EmbedPane()
        pane._capture_size_override = None  # noqa: SLF001
        with (
            mock.patch.object(pane, "_pane_size", return_value=(80, 24)),
            mock.patch("pickup.embed.should_resize_host", return_value=True),
            mock.patch("pickup.embed.resize") as resize,
            mock.patch("pickup.observe.event") as event,
        ):
            pane._heal_host_size_if_needed("pickup-claude-heal", (40, 24))  # noqa: SLF001
            self.assertEqual(resize.call_count, 1)
            resize.assert_called_with("pickup-claude-heal", 80, 24)
            event.assert_called()
            self.assertEqual(event.call_args.args[0], "host_size_drift")
            pane._heal_host_size_if_needed("pickup-claude-heal", (40, 24))  # noqa: SLF001
            self.assertEqual(resize.call_count, 1, "间隔内不得连发")
            pane._heal_last_at = 0.0  # noqa: SLF001
            pane._heal_host_size_if_needed("pickup-claude-heal", (40, 24))  # noqa: SLF001
            pane._heal_last_at = 0.0  # noqa: SLF001
            pane._heal_host_size_if_needed("pickup-claude-heal", (40, 24))  # noqa: SLF001
            self.assertEqual(resize.call_count, 3)
            pane._heal_last_at = 0.0  # noqa: SLF001
            pane._heal_host_size_if_needed("pickup-claude-heal", (40, 24))  # noqa: SLF001
            self.assertEqual(resize.call_count, 3, "同一目标超过上限即停")
            pane._heal_host_size_if_needed("pickup-claude-heal", (80, 24))  # noqa: SLF001
            self.assertEqual(pane._heal_count, 0)  # noqa: SLF001
        self.assertGreaterEqual(embed_pane_mod._HOST_SIZE_HEAL_MAX, 3)

    async def test_browsing_existing_groups_persists_focus_not_composition(self) -> None:
        """浏览已有会话组只 set_focus，不得 set_group（会抬 updated_at 并整表写盘）。"""
        sessions = [
            {
                "source": "claude", "id": f"g{i}", "short_id": f"g{i}",
                "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"组会话{i}",
                "cwd": "/tmp", "live": True,
                "keepalive_name": f"pickup-claude-g{i}",
            }
            for i in range(4)
        ]
        store, _ = _make_store(sessions=sessions)
        keys = [pickup.session_key(s) for s in sessions]
        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.open_channel", return_value=None), \
             mock.patch("pickup.embed.should_resize_host", return_value=False):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                screen = app.screen
                screen._apply_layout_change(  # noqa: SLF001
                    lambda s: s.set_group("/tmp", keys[:2], focus_key=keys[0])
                )
                screen._apply_layout_change(  # noqa: SLF001
                    lambda s: s.set_group("/tmp", keys[2:], focus_key=keys[2])
                )
                group_a = screen._split_store.get_group(keys[0])  # noqa: SLF001
                group_b = screen._split_store.get_group(keys[2])  # noqa: SLF001
                self.assertIsNotNone(group_a)
                self.assertIsNotNone(group_b)
                assert group_a is not None and group_b is not None
                updated_a = group_a.updated_at
                updated_b = group_b.updated_at

                focus_calls: list[str] = []
                composition_calls: list[str] = []
                real_focus = screen._persist_split_focus
                real_comp = screen._persist_split_composition

                def track_focus():
                    focus_calls.append("focus")
                    return real_focus()

                def track_comp():
                    composition_calls.append("comp")
                    return real_comp()

                screen._persist_split_focus = track_focus  # noqa: SLF001
                screen._persist_split_composition = track_comp  # noqa: SLF001

                screen._show_session_group(keys[0], include_inactive=True)  # noqa: SLF001
                await pilot.pause(delay=0.15)
                screen._show_session_group(keys[2], include_inactive=True)  # noqa: SLF001
                await pilot.pause(delay=0.15)

                self.assertGreaterEqual(len(focus_calls), 2, focus_calls)
                self.assertEqual(composition_calls, [], composition_calls)
                adopted = screen._split_store  # noqa: SLF001
                self.assertEqual(adopted.get_group(keys[0]).updated_at, updated_a)
                self.assertEqual(adopted.get_group(keys[2]).updated_at, updated_b)

    async def test_cold_hosted_switch_skips_markdown_fallback(self) -> None:
        """冷切换托管会话不得同步跑 Markdown 回退（空白画布等首帧）。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.open_channel", return_value=None), \
             mock.patch("pickup.embed.should_resize_host", return_value=False):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                pane = _primary_embed_pane(app.screen)
                called = {"n": 0}

                def heavy():
                    called["n"] += 1
                    return "SHOULD-NOT-RENDER"

                pane.focus_session("pickup-cold-x", heavy)
                self.assertIsNone(pane._grid)
                self.assertFalse(pane._is_hosted_fallback())
                self.assertEqual(called["n"], 0)
                self.assertEqual(pane.render().plain, "")

    async def test_active_pane_never_receives_a_message_preview_renderer(self) -> None:
        """活跃会话的首次挂载和就地刷新都不能残留消息预览。"""
        sessions = [{
            "source": "claude", "id": "runtime-only", "short_id": "runtime-only",
            "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
            "native_title": None, "fallback_title": "运行时优先",
            "cwd": "/tmp", "live": True, "keepalive_name": "pickup-runtime-only",
        }]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=True)
        seen_fallbacks: list[object | None] = []
        original = EmbedPane.focus_session

        def record(pane, name, fallback_renderer=None, **kwargs):
            seen_fallbacks.append(fallback_renderer)
            return original(pane, name, fallback_renderer, **kwargs)

        with (
            mock.patch("pickup.embed.open_channel", return_value=None),
            mock.patch.object(EmbedPane, "focus_session", new=record),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                app.screen.query_one(SplitPaneArea).show_hosted_group(
                    "/tmp",
                    [(sessions[0], "pickup-runtime-only", lambda: "不得闪现的消息预览")],
                )
                await _wait_until(
                    lambda: len(app.screen.query_one(SplitPaneArea).cells()) == 1,
                )
                area = app.screen.query_one(SplitPaneArea)
                pane = area.cells()[0].embed_pane()
                self.assertIsNotNone(pane)
                self.assertIsNone(pane._detail_renderer)
                # 同一会话的列表刷新走就地更新，曾经正是这里把预览重新塞回活跃格。
                area.show_hosted_group(
                    "/tmp",
                    [(sessions[0], "pickup-runtime-only", lambda: "仍不得闪现")],
                )
                self.assertIsNone(pane._detail_renderer)
        self.assertTrue(seen_fallbacks)
        self.assertTrue(all(fallback is None for fallback in seen_fallbacks))

    async def test_rapid_highlights_are_throttled_but_still_settle(self) -> None:
        """连按方向键翻找会话时，右栏不能每一步都重建一次。

        每次「选择跟随」都可能整排重建右栏（实测单次端到端约 180ms），按住方向键
        积压 N 条高亮就是 N 次重建。这里钉死两件事：积压的高亮被合并，且停下来
        之后一定收敛到最后选中的那一项——只快不准同样是回归。
        """
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            screen = app.screen
            list_view = screen.query_one(SessionListView)
            list_view.focus()
            await pilot.pause()

            runs: list[int] = []
            original = screen._follow_current_selection

            def counted():
                runs.append(list_view.index or 0)
                return original()

            screen._follow_current_selection = counted
            # 同步连续移动：Highlighted 全部排进队列后才被处理，等价于按键重复积压
            for _ in range(5):
                list_view.action_cursor_down()
            await pilot.pause()
            await pilot.pause(delay=0.3)

            self.assertLess(
                len(runs), 5,
                f"积压的高亮没有被合并，跟随了 {len(runs)} 次：{runs}",
            )
            self.assertTrue(runs, "节流不能把跟随整个吞掉")
            self.assertEqual(
                runs[-1], list_view.index,
                "停下来之后必须收敛到最后选中的那一项",
            )
            self.assertIsNone(screen._follow_timer, "收敛后不应留下待触发的定时器")

    async def test_enter_without_embed_exits_with_launch_request(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("enter")
            await pilot.pause()
        self.assertIsInstance(app.return_value, pickup.LaunchRequest)
        self.assertEqual(app.return_value.session["id"], "s0")

    async def test_startup_selects_first_session_not_new_row(self) -> None:
        """进入 pickup 默认高亮列表第一条会话/会话组，焦点在侧边栏。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.3)
            list_view = app.screen.query_one(SessionListView)
            self.assertTrue(list_view.has_focus)
            self.assertEqual(list_view.index, 1)
            self.assertFalse(list_view.is_new_session_selected())
            self.assertEqual(list_view.selected_session()["id"], "s0")
            area = app.screen.query_one(SplitPaneArea)
            self.assertEqual(area.ordered_session_keys(), ["claude:s0"])

    async def test_startup_selects_first_group_when_list_starts_with_group(self) -> None:
        """列表顶是会话组时，启动默认高亮组卡而不是「＋新建」。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            keys = [pickup.session_key(s) for s in store.all_sessions()[:2]]
            list_view.on_layout_change(lambda s: s.set_group("/tmp", keys, focus_key=keys[0]))
            # 清空后再重建，走「初次填充」分支（had_rows=False）。
            await list_view.clear()
            await list_view.rebuild(keep_selection=False)
            await pilot.pause(delay=0.2)
            self.assertEqual(list_view.index, 1)
            self.assertIsNotNone(list_view.selected_group())
            self.assertTrue(list_view.has_focus)

    async def test_escape_exits_with_no_result(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("escape")
            await pilot.pause()
        self.assertIsNone(app.return_value)

    async def test_clicking_session_card_selects_and_launches_without_crashing(self) -> None:
        """回归测试：真机实测过点击会话卡片直接闪退——Textual 默认给所有 Widget
        开启内置的鼠标拖拽文本选择（ALLOW_SELECT=True），会话卡片这类自定义
        展示型 Widget 被点击时触发该逻辑，在某些时序下 container 解析为 None，
        访问 .region 抛 AttributeError 崩溃整个应用。修法：子卡片 + 外层
        NoSelectListItem + SessionListView 都关 ALLOW_SELECT；EmbedPane 保留。
        这里钉死点击会话卡不能再回归成崩溃；已结束会话点击只选中、回车才恢复。"""
        from pickup.ui.session_list import NoSelectListItem

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            self.assertFalse(list_view.ALLOW_SELECT)
            self.assertTrue(
                all(isinstance(item, NoSelectListItem) for item in list_view.list_children)
            )
            cards = list(app.screen.query(SessionCard))
            clicked = await pilot.click(cards[1], offset=(5, 0))
            await pilot.pause()
            self.assertTrue(clicked)
            self.assertEqual(
                pickup.session_key(list_view.selected_session()),
                pickup.session_key(cards[1].session),
                "点击必须把选中挪到这张卡上",
            )
            self.assertIsNone(app.return_value, "点已结束会话只看历史，不许直接恢复")
            await pilot.press("enter")
            await pilot.pause()
        self.assertIsInstance(app.return_value, pickup.LaunchRequest)

    async def test_clicking_group_card_does_not_crash(self) -> None:
        """会话组卡同样包在会重建的 ListItem 里，拖选开着会在启动刷新时崩。"""
        from pickup.ui.session_list import NoSelectListItem

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            keys = [pickup.session_key(session) for session in store.all_sessions()[:2]]
            list_view.on_layout_change(lambda s: s.set_group("/tmp", keys, focus_key=keys[0]))
            await list_view.rebuild()
            group_card = list_view.query(SessionGroupCard).first()
            self.assertIsNotNone(group_card)
            self.assertIsInstance(group_card.parent, NoSelectListItem)
            await pilot.click(group_card, offset=(8, 0))
            await pilot.pause()
            # 再强制全量重建一次，模拟启动后后台重扫与点击交错；未崩即过。
            await list_view.rebuild()
            self.assertGreaterEqual(len(list(list_view.query(SessionGroupCard))), 1)
        self.assertIsNone(app.return_value)

    async def test_clicking_group_card_keeps_focus_on_sidebar(self) -> None:
        """点会话组卡：右栏跟随展示组合，键盘焦点必须留在侧边栏。"""
        store, _ = _make_store()
        for session in store.all_sessions()[:2]:
            session["live"] = True
            session["keepalive_name"] = f"pickup-{session['id']}"
        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.is_alive", return_value=True):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                list_view = app.screen.query_one(SessionListView)
                keys = [pickup.session_key(s) for s in store.all_sessions()[:2]]
                list_view.on_layout_change(lambda s: s.set_group("/tmp", keys, focus_key=keys[0]))
                await list_view.rebuild()
                await pilot.pause(delay=0.2)

                group_card = list_view.query(SessionGroupCard).first()
                self.assertIsNotNone(group_card)
                await pilot.click(group_card, offset=(8, 0))
                await pilot.pause(delay=0.3)

                area = app.screen.query_one(SplitPaneArea)
                self.assertTrue(list_view.has_focus)
                self.assertFalse(area.any_embed_focused())
                self.assertIsNotNone(list_view.selected_group())
                self.assertEqual(set(area.ordered_session_keys()), set(keys))
        self.assertIsNone(app.return_value)

    async def test_ctrl_click_multi_select_does_not_launch(self) -> None:
        """Ctrl/Cmd+点击只 toggle 多选，不等价 Enter，也不退出应用。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            cards = list(app.screen.query(SessionCard))
            self.assertGreaterEqual(len(cards), 2)
            await pilot.click(cards[0], control=True)
            await pilot.pause()
            await pilot.click(cards[1], control=True)
            await pilot.pause()
            self.assertEqual(list_view.multi_count(), 2)
            self.assertIn("▸", cards[0].render().plain)
            self.assertIn("▸", cards[1].render().plain)
        self.assertIsNone(app.return_value)

    async def test_enter_opens_split_for_multi_selected_sessions(self) -> None:
        """多选 ≥2 后 Enter 在右栏开分屏（已结束会话走预览格）。"""
        sessions = [
            {
                "source": "claude", "id": f"s{i}", "short_id": f"s{i}",
                "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"Session {i}",
                "cwd": "/tmp", "live": False,
            }
            for i in range(2)
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            cards = list(app.screen.query(SessionCard))
            await pilot.click(cards[0], control=True)
            await pilot.click(cards[1], control=True)
            await pilot.pause()
            self.assertEqual(list_view.multi_count(), 2)
            await pilot.press("enter")
            await pilot.pause()
            area = app.screen.query_one(SplitPaneArea)
            await _wait_until(lambda: len(area._cells()) == 2)  # noqa: SLF001
            keys = area.ordered_session_keys()
            self.assertEqual(len(keys), 2)
            self.assertEqual(list_view.multi_count(), 0)
        self.assertIsNone(app.return_value)

    async def test_space_toggles_multi_select_on_focused_session(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            list_view.index = 1
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            self.assertEqual(list_view.multi_count(), 1)
            await pilot.press("space")
            await pilot.pause()
            self.assertEqual(list_view.multi_count(), 0)

    async def test_escape_clears_multi_select_before_quit(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            cards = list(app.screen.query(SessionCard))
            await pilot.click(cards[0], control=True)
            await pilot.click(cards[1], control=True)
            await pilot.pause()
            self.assertEqual(list_view.multi_count(), 2)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(list_view.multi_count(), 0)
        self.assertIsNone(app.return_value)

    async def test_list_item_gap_padding_is_part_of_hit_area(self) -> None:
        """会话卡第三行（时间行）仍属本卡命中区；不要用 ListItem margin/padding 做分隔。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        self.assertNotIn("margin-bottom:", PickupApp.CSS)
        self.assertNotIn("padding-bottom:", PickupApp.CSS)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            cards = list(app.screen.query(SessionCard))
            self.assertEqual(cards[0].region.height, 3)
            # 点第三行时间行，应等价于点该会话卡（选中要挪过去）
            clicked = await pilot.click(cards[1], offset=(5, 2))
            await pilot.pause()
            self.assertTrue(clicked)
            self.assertEqual(
                pickup.session_key(list_view.selected_session()),
                pickup.session_key(cards[1].session),
            )

    async def test_rebuild_updates_in_place_when_session_set_unchanged(self) -> None:
        """性能优化回归：会话集合（顺序+成员）没变、只是某个会话内容变了（比如
        「运行中」翻转成「已结束」）时，`rebuild()` 必须走原地更新——不清空/
        重建 ListView 子项，只换 SessionCard 手上的 session 引用再按需
        `refresh()`。这里同时断言两件事：① 卡片 Widget 实例本身没有被销毁重建
        （identity 不变）；② 渲染出的实际文本确实反映了新状态——只断言内部
        状态不能证明渲染结果对，这是 docs/MAINTAINER_GUIDE.md 记录过的教训。
        """
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            cards_before = list_view._session_cards()
            self.assertEqual(len(cards_before), 3)
            # 已结束会话标题保持默认配色（不是进行中的绿色）
            self.assertFalse(
                any("#3F9A6A" in str(span.style) for span in cards_before[0].render().spans),
            )

            # 模拟一次后台重扫：s0 的会话字典被替换成新对象（和真实 _merge_scanned
            # 行为一致，扫描结果每次都是新 dict），但会话键集合/顺序没变，
            # 只是 live 从 False 翻到 True。
            old_session = store.sessions["claude"][0]
            new_session = dict(old_session, live=True)
            store.sessions["claude"][0] = new_session

            await list_view.rebuild()

            cards_after = list_view._session_cards()
            self.assertEqual(
                [id(c) for c in cards_before], [id(c) for c in cards_after],
                "会话集合没变时不应该重新 mount 任何 SessionCard 实例",
            )
            self.assertIs(cards_after[0].session, new_session)
            # live 翻到 True 后标题仍保持统一基础色，关注状态只由圆点表达。
            self.assertFalse(
                any("#3F9A6A" in str(span.style) for span in cards_after[0].render().spans),
            )

    async def test_refresh_detects_detail_changes_and_updates_card_and_pane_in_place(self) -> None:
        old_session = {
            "source": "claude", "id": "detail", "short_id": "detail",
            "mtime": 100.0, "size_bytes": 1, "size_kb": 1,
            "native_title": "旧标题", "fallback_title": "旧标题",
            "cwd": "/tmp/pickup", "live": False, "path": "/tmp/pickup-detail.jsonl",
            "first_user_msg": "旧首问", "last_user_msg": "旧问题",
            "last_agent_msg": "旧回复",
        }
        new_session = dict(
            old_session,
            mtime=200.0,
            native_title="新标题",
            fallback_title="新标题",
            last_user_msg="新问题",
            last_agent_msg="新回复",
        )
        runtime = mock.Mock(id="claude", display_name="Claude")
        runtime.scan_signature.return_value = None
        runtime.scan_sessions.side_effect = [[old_session], [new_session]]
        runtime.load_conversation.side_effect = [
            [
                pickup.ConversationMessage("user", "旧问题"),
                pickup.ConversationMessage("assistant", "旧回复"),
            ],
            [
                pickup.ConversationMessage("user", "新问题"),
                pickup.ConversationMessage("assistant", "新回复"),
            ],
        ]
        registry = pickup.RuntimeRegistry((runtime,))
        with (
            mock.patch.object(pickup.titles, "load_cache", return_value={}),
            mock.patch.object(pickup.keepalive, "annotate"),
        ):
            store = pickup.SessionStore(limit=20, registry=registry)
            store.load()

        original_signature = store._sessions_signature()
        store.sessions["claude"][0]["mtime"] = 101.0
        self.assertNotEqual(store._sessions_signature(), original_signature)
        store.sessions["claude"][0]["mtime"] = 100.0
        store.sessions["claude"][0]["last_user_msg"] = "另一条问题"
        self.assertNotEqual(store._sessions_signature(), original_signature)
        store.sessions["claude"][0]["last_user_msg"] = "旧问题"

        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            screen = app.screen
            list_view = screen.query_one(SessionListView)
            pane = _primary_embed_pane(screen)
            card_before = list_view._session_cards()[0]
            await _wait_until(lambda: "旧标题" in pane.render().plain)
            old_snapshot = store.sessions["claude"][0]

            with mock.patch.object(pickup.keepalive, "annotate"):
                self.assertTrue(store.refresh())
            # 历史 path 未变时对话缓存仍有效；清掉让右栏重新 warm 到新对话。
            store.conversations.clear()
            await screen._rebuild_list()
            await pilot.pause(delay=0.3)

            card_after = list_view._session_cards()[0]
            self.assertIs(card_after, card_before)
            self.assertIsNot(card_after.session, old_snapshot)
            self.assertEqual(card_after.session["mtime"], 200.0)
            await _wait_until(lambda: "新标题" in pane.render().plain and "新问题" in pane.render().plain)
            detail = pane.render().plain
            self.assertIn("新标题", detail)
            self.assertIn("新问题", detail)
            self.assertIn("新回复", detail)
            self.assertNotIn("旧问题", detail)
            self.assertNotIn("最近提问", detail)

    async def test_rebuild_falls_back_to_full_rebuild_when_session_set_changes(self) -> None:
        """回归测试：新增/删除会话导致集合真的变了时，`rebuild()` 必须仍然正确
        走批量清空重建路径，不能被上面的原地更新优化误伤。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            self.assertEqual(len(list_view._session_cards()), 3)

            new_session = {
                "source": "claude", "id": "s99", "short_id": "s99",
                "mtime": time.time() + 1000, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "全新会话",
                "cwd": "/tmp", "live": False,
            }
            store.sessions["claude"].append(new_session)

            await list_view.rebuild()

            cards = list_view._session_cards()
            self.assertEqual(len(cards), 4)
            self.assertIn("claude:s99", [pickup.session_key(c.session) for c in cards])

    async def test_screen_serializes_concurrent_list_rebuilds(self) -> None:
        """后台重扫和交互刷新同时到达时，列表重建必须串行，不能重复挂载条目。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            screen = app.screen
            await asyncio.gather(screen._rebuild_list(), screen._rebuild_list())

            list_view = screen.query_one(SessionListView)
            self.assertEqual(len(list_view.query(f"#{NEW_SESSION_ID}")), 1)
            self.assertEqual(len(list_view._session_cards()), 3)

    async def test_list_rebuild_serialized_across_message_pumps(self) -> None:
        """回归测试（2026-07-26 真机崩溃）：后台重扫的重建跑在 App 泵上，搜索框
        输入触发的重建跑在 Screen 泵上，MainScreen 自己那把锁挡不住后者。两条
        全量重建一旦在 clear()/extend() 之间交错，新建项会被挂第二次，Textual
        抛 DuplicateIds 打崩整个 TUI。`SessionListView.rebuild()` 内部必须自带
        闸门，无论调用方来自哪条泵都串行进 DOM。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            screen = app.screen
            list_view = screen.query_one(SessionListView)

            new_session = {
                "source": "claude", "id": "s98", "short_id": "s98",
                "mtime": time.time() + 1000, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "并发新会话",
                "cwd": "/tmp", "live": False,
            }
            store.sessions["claude"].append(new_session)

            async def interleaved_filter_rebuild() -> None:
                # 让后台侧那次先进到 clear() 的 await 点，再模拟用户改筛选词
                await asyncio.sleep(0)
                screen.nav.project_query = "tmp"
                await list_view.rebuild(keep_selection=True)

            await asyncio.gather(screen._rebuild_list(), interleaved_filter_rebuild())

            self.assertEqual(len(list_view.query(f"#{NEW_SESSION_ID}")), 1)
            keys = [pickup.session_key(c.session) for c in list_view._session_cards()]
            self.assertEqual(len(keys), len(set(keys)))

    async def test_rebuild_keeps_focus_on_same_session_when_new_session_appears(self) -> None:
        """回归测试：真实反馈——聚焦第三条会话时后台刷出一条新会话，高亮和
        右栏会跟着「串位」跳到相邻的第二条。根因是 `rebuild()` 曾用
        `selected_session()`（按刚重算过的 `visible_sessions()` 索引 DOM 下标）
        推导原选中键；新会话按 mtime 置顶插入后同一下标已指向别的会话。
        `_displayed_selected_key()` 改按已渲染的 DOM 卡片取键，必须确保新会话
        置顶插入后，原选中会话仍被选中（只是位置下移），不能串到相邻会话。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            list_view.index = 2  # 选中 s1（第二条，位置 1）
            await pilot.pause()
            target_key = pickup.session_key(list_view.selected_session())
            self.assertEqual(target_key, "claude:s1")

            new_session = {
                "source": "claude", "id": "s_new", "short_id": "s_new",
                "mtime": time.time() + 1000, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "新会话",
                "cwd": "/tmp", "live": False,
            }
            store.sessions["claude"].append(new_session)

            await list_view.rebuild()

            self.assertEqual(
                pickup.session_key(list_view.selected_session()), target_key,
                "新会话置顶插入后，原选中会话应仍被选中（只是位置下移），"
                "不能串到相邻会话",
            )

    async def test_rebuild_keeps_embed_pane_following_same_session_when_new_session_appears(
        self,
    ) -> None:
        """同一 bug 的右栏视角：右栏详情预览必须跟着原选中会话一起下移，
        不能因为高亮串位而展示成相邻会话的内容。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            list_view.index = 2  # 选中 s1
            await pilot.pause(delay=0.2)
            await _wait_until(lambda: "会话1" in _primary_embed_pane(app.screen).render().plain)

            new_session = {
                "source": "claude", "id": "s_new", "short_id": "s_new",
                "mtime": time.time() + 1000, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "新会话",
                "cwd": "/tmp", "live": False,
            }
            store.sessions["claude"].append(new_session)

            await list_view.rebuild()
            await app.screen._rebuild_list()
            await pilot.pause(delay=0.2)

            await _wait_until(lambda: "会话1" in _primary_embed_pane(app.screen).render().plain)
            self.assertNotIn("会话0", _primary_embed_pane(app.screen).render().plain)

    async def test_browsing_keeps_list_focus_enter_hands_input_to_pane(self) -> None:
        """上下浏览不抢焦点；回车（明确意图）才把输入交给右栏，Ctrl+\\ 回列表。

        浏览一抢焦点，列表就没法继续用了——所以自动聚焦只绑在「明确意图」上。
        """
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.embed.host_session", return_value="pickup-claude-s0"),
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                list_view = app.screen.query_one(SessionListView)

                self.assertTrue(list_view.has_focus, "浏览列表不得把焦点交给右栏")

                await pilot.press("enter")
                await _wait_until(lambda: app.screen._host_pending == 0)
                pane = await _wait_for_embed_session(app.screen, "pickup-claude-s0")
                await _wait_until(lambda: pane.has_focus)
                self.assertFalse(list_view.has_focus)
                self.assertFalse(pane.input_masked, "持有输入的格不该压暗")

                await pilot.press("ctrl+backslash")
                await pilot.pause()
                self.assertTrue(list_view.has_focus)
                await _wait_until(lambda: pane.input_masked)

    async def test_focus_intent_removes_input_mask_before_focus_lands(self) -> None:
        """点开托管会话时，灰色输入蒙版必须先于下一帧的真实落焦消失。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.embed.host_session", return_value="pickup-claude-s0"),
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("enter")
                await _wait_until(lambda: app.screen._host_pending == 0)
                pane = await _wait_for_embed_session(app.screen, "pickup-claude-s0")
                await _wait_until(lambda: pane.has_focus)

                await pilot.press("ctrl+backslash")
                await _wait_until(lambda: pane.input_masked)
                key = app.screen.query_one(SplitPaneArea).pane_specs()[0].session_key

                # 这里刻意不等待下一次刷新：这正是用户此前能看到的中间态。
                app.screen.query_one(SplitPaneArea)._request_pane_focus(key)  # noqa: SLF001
                self.assertFalse(pane.input_masked)
                await _wait_until(lambda: pane.has_focus)

    async def test_input_claim_survives_until_the_real_focus_event(self) -> None:
        """已调用聚焦、但真实焦点尚未落下时，迟到的蒙版同步不能闪灰一帧。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.embed.host_session", return_value="pickup-claude-s0"),
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("enter")
                await _wait_until(lambda: app.screen._host_pending == 0)
                pane = await _wait_for_embed_session(app.screen, "pickup-claude-s0")
                await _wait_until(lambda: pane.has_focus)

                await pilot.press("ctrl+backslash")
                await _wait_until(lambda: pane.input_masked)
                area = app.screen.query_one(SplitPaneArea)
                key = area.pane_specs()[0].session_key
                area._claim_pane_input(key)  # noqa: SLF001 模拟点击已登记输入归属

                # Widget.focus() 会延后到下一轮事件循环；在它真正生效前让一次蒙版
                # 同步插队，正是此前真机上能看见灰色闪现的时序。
                with mock.patch.object(type(area.cells()[0]), "focus_embed", autospec=True):
                    self.assertTrue(area._apply_focus_intent())  # noqa: SLF001
                self.assertIsNone(area._focus_intent_key)  # noqa: SLF001
                self.assertEqual(area._input_claim_key, key)  # noqa: SLF001
                area.sync_input_mask()
                self.assertFalse(pane.input_masked)

                self.assertTrue(area.focus_session_key(key, only_live=True))
                await _wait_until(lambda: pane.has_focus)
                await pilot.pause()
                self.assertIsNone(area._input_claim_key)  # noqa: SLF001
                self.assertFalse(pane.input_masked)

    async def test_remount_focus_intent_removes_input_mask_immediately(self) -> None:
        """需重排的会话切换也必须先撤蒙版，不能只覆盖同格聚焦路径。"""
        sessions = [
            {
                "source": "claude", "id": f"mask-{i}", "short_id": f"mask-{i}",
                "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"蒙版会话{i}",
                "cwd": "/tmp", "live": True,
                "keepalive_name": f"pickup-claude-mask-{i}",
            }
            for i in range(2)
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.embed.open_channel", return_value=None),
            mock.patch("pickup.embed.should_resize_host", return_value=False),
        ):
            async with app.run_test(size=(160, 30)) as pilot:
                await pilot.pause(delay=0.2)
                area = app.screen.query_one(SplitPaneArea)
                area.show_hosted_group(
                    "/tmp", [(sessions[0], sessions[0]["keepalive_name"], None)],
                )
                await _wait_until(lambda: len(area.cells()) == 1)
                pane = area.cells()[0].embed_pane()
                app.screen._focus_list()  # noqa: SLF001
                list_view = app.screen.query_one(SessionListView)
                # 挂载 EmbedPane 后 Textual 可能先把焦点落到右栏；等侧栏真正持焦，
                # 再同步蒙版，避免 pilot.pause 一帧里焦点还在路上就断言失败。
                # 全量套件负载高时，托管声明 / session_name 可能比焦点晚一拍，
                # 单次 sync 会看到「右栏仍持有输入」或「还不算 live」而不压暗。
                def _list_focused_and_masked() -> bool:
                    if not list_view.has_focus or pane.has_focus:
                        return False
                    if area._input_claim_key is not None:  # noqa: SLF001
                        return False
                    area.sync_input_mask()
                    return pane.input_masked

                await _wait_until(_list_focused_and_masked)
                self.assertTrue(pane.input_masked)

                area.show_hosted_group(
                    "/tmp",
                    [(s, s["keepalive_name"], None) for s in sessions],
                    focus_key=pickup.session_key(sessions[1]),
                    focus_pane=True,
                )
                self.assertFalse(pane.input_masked)

    async def test_click_pane_focuses_embed(self) -> None:
        """鼠标点右栏进入内嵌交互（自动聚焦之外的另一条路径）。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.embed.host_session", return_value="pickup-claude-s0"),
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                list_view = app.screen.query_one(SessionListView)

                await pilot.press("enter")
                await _wait_until(lambda: app.screen._host_pending == 0)
                pane = await _wait_for_embed_session(app.screen, "pickup-claude-s0")
                await _wait_until(lambda: pane.has_focus)
                await pilot.press("ctrl+backslash")
                await pilot.pause()
                self.assertTrue(list_view.has_focus)

                # 托管成功后列表重建仍可能排在下一帧；等 DOM 稳定并重新取当前 pane，
                # 避免点击刚被替换掉的旧 Widget。
                await pilot.pause(delay=0.2)
                pane = await _wait_for_embed_session(app.screen, "pickup-claude-s0")
                await pilot.click(pane)
                await pilot.pause()
                self.assertTrue(pane.has_focus)
                self.assertFalse(list_view.has_focus)

    async def test_click_sidebar_card_of_hosted_session_hands_input_to_pane(self) -> None:
        """回归：点侧边栏里「已托管」的会话卡，输入必须当场交给右栏那一格。

        点击 = 打开（和回车同一条 Selected 路径，真的会拉起/接管会话），所以必须
        跟回车一样自动聚焦。曾经的坑：点击先触发选择跟随排了一次异步 remount，
        它的收尾无条件「把焦点还给列表」，把紧随其后的自动聚焦又抢了回去——真机
        表现就是「点会话卡进不去右栏，还得再点一下右栏才能打字」。
        """
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.embed.host_session", return_value="pickup-claude-s0"),
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                list_view = app.screen.query_one(SessionListView)

                await pilot.press("enter")
                await _wait_until(lambda: app.screen._host_pending == 0)
                pane = await _wait_for_embed_session(app.screen, "pickup-claude-s0")
                await _wait_until(lambda: pane.has_focus)
                hosted_key = pickup.session_key(list_view.selected_session())

                await pilot.press("ctrl+backslash")
                await pilot.pause()
                self.assertTrue(list_view.has_focus)

                # 键盘挪到另一条已结束会话：右栏换成静态预览，焦点仍在列表。
                await pilot.press("down")
                await pilot.pause(delay=0.3)
                self.assertTrue(list_view.has_focus, "浏览不得抢焦点")
                self.assertNotEqual(
                    pickup.session_key(list_view.selected_session()), hosted_key
                )

                # 点回那张已托管的卡：右栏重新变实时终端，且输入直接交给它。
                card = next(
                    c for c in app.screen.query(SessionCard)
                    if pickup.session_key(c.session) == hosted_key
                )
                await pilot.click(card)
                await _wait_until(lambda: app.screen._host_pending == 0)
                pane = await _wait_for_embed_session(app.screen, "pickup-claude-s0")
                await _wait_until(lambda: pane.has_focus)
                self.assertFalse(list_view.has_focus, "点已托管的会话卡必须进右栏")
                self.assertFalse(pane.input_masked, "持有输入的格不该压暗")

    async def test_click_same_card_again_returns_focus_to_list(self) -> None:
        """点会话卡是对称开关：点开进右栏，再点同一张卡把输入撤回侧边栏。

        Textual 在 MouseDown 阶段就把焦点移到列表了，判定必须靠
        `SessionListView.focus_on_click()` 记下的「按下前焦点」，不能事后现查。
        """
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.embed.host_session", return_value="pickup-claude-s0"),
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                list_view = app.screen.query_one(SessionListView)

                await pilot.press("enter")
                await _wait_until(lambda: app.screen._host_pending == 0)
                pane = await _wait_for_embed_session(app.screen, "pickup-claude-s0")
                await _wait_until(lambda: pane.has_focus)
                hosted_key = pickup.session_key(list_view.selected_session())

                def hosted_card() -> SessionCard:
                    return next(
                        c for c in app.screen.query(SessionCard)
                        if pickup.session_key(c.session) == hosted_key
                    )

                # 点当前正持有输入的那张卡：焦点撤回列表，该格重新压暗。
                await pilot.click(hosted_card())
                await _wait_until(lambda: list_view.has_focus)
                await _wait_until(lambda: pane.input_masked)
                self.assertFalse(pane.has_focus)

                # 再点一次又进去：开关必须对称，不能一去不回。
                await pilot.click(hosted_card())
                await _wait_until(lambda: app.screen._host_pending == 0)
                pane = await _wait_for_embed_session(app.screen, "pickup-claude-s0")
                await _wait_until(lambda: pane.has_focus)
                self.assertFalse(list_view.has_focus)
                self.assertFalse(pane.input_masked)

    async def test_consecutive_clicks_on_other_cards_always_hand_input_to_pane(self) -> None:
        """回归：连续点不同的会话卡，输入必须每次都落到右栏，不能一次进一次不进。

        坑：点击后紧跟的选择跟随会把同一个面板控件**就地改绑**到刚点的会话
        （`PaneCell.rebind` 复用控件不重建），事后再按控件身份比对「按下前焦点」，
        第二次点击就会把「点了另一张卡」误判成「点了正持有输入的那张卡」，焦点
        被撤回侧边栏——真机表现是焦点在侧边栏和右栏之间来回跳。判定必须在鼠标
        按下当帧解析成会话键。
        """
        sessions = [
            {
                "source": "claude", "id": f"s{i}", "short_id": f"s{i}",
                "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"会话{i}",
                "cwd": "/tmp", "live": True, "keepalive_name": f"pickup-claude-s{i}",
            }
            for i in range(3)
        ]
        store, registry = _make_store(sessions)
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch(
                "pickup.embed.host_session",
                side_effect=AssertionError("已托管会话不该重新拉起"),
            ),
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                screen = app.screen
                list_view = screen.query_one(SessionListView)
                area = screen.query_one(SplitPaneArea)

                def card(index: int) -> SessionCard:
                    return next(
                        c for c in screen.query(SessionCard)
                        if c.session["id"] == f"s{index}"
                    )

                for index in (0, 1, 2, 0):
                    await pilot.click(card(index))
                    pane = await _wait_for_embed_session(
                        screen, f"pickup-claude-s{index}",
                    )
                    await _wait_until(lambda p=pane: p.has_focus)
                    self.assertFalse(
                        list_view.has_focus, f"点第 {index} 张卡后焦点没进右栏",
                    )

                # 对称开关仍然成立：点当前持有输入的那张卡才收回焦点。
                await pilot.click(card(0))
                await _wait_until(lambda: list_view.has_focus)
                self.assertFalse(area.any_embed_focused())

    async def test_stale_highlight_after_hosting_keeps_live_pane(self) -> None:
        """托管落库早于列表重建时，旧卡片高亮事件不能把实时终端盖回静态预览。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.is_alive", return_value=True):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                screen = app.screen
                list_view = screen.query_one(SessionListView)
                area = screen.query_one(SplitPaneArea)
                stale_session = dict(list_view.selected_session())
                list_view._session_cards()[0].session = stale_session
                self.assertEqual(stale_session["id"], "s0")
                self.assertNotIn("keepalive_name", stale_session)

                store.mark_hosted("claude:s0", "pickup-claude-s0")
                self.assertNotIn("keepalive_name", stale_session)
                with (
                    mock.patch.object(area, "show_hosted_group", wraps=area.show_hosted_group) as live,
                    mock.patch.object(area, "show_single_preview", wraps=area.show_single_preview) as static,
                ):
                    screen._follow_current_selection()

                live.assert_called_once()
                static.assert_not_called()

    async def test_right_pane_wheel_scrolls_while_list_focused(self) -> None:
        """焦点在侧边栏时，鼠标在右栏滚轮仍应滚动静态预览（与焦点无关）。"""
        store, registry = _make_store()
        long_body = "\n".join(f"行{i} " + ("内容" * 20) for i in range(80))
        registry.get("claude").load_conversation.return_value = [
            pickup.ConversationMessage("user", long_body),
            pickup.ConversationMessage("assistant", long_body),
        ]
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            pane = _primary_embed_pane(app.screen)
            # 预览默认钉在最新（底部）；等末行可见后再上滚看更早内容
            await _wait_until(
                lambda: pane._is_detail_view() and pane.detail_offset == pane._detail_max_offset() > 0,
            )
            self.assertTrue(list_view.has_focus)
            self.assertFalse(pane.has_focus)
            before = pane.detail_offset
            # 滚轮处理不检查 has_focus；列表聚焦时直接投递也应能滚右栏预览。
            pane._on_mouse_scroll_up(
                events.MouseScrollUp(None, 10, 5, 0, 0, 0, False, False, False),
            )
            await pilot.pause()
            self.assertLess(pane.detail_offset, before)
            self.assertTrue(list_view.has_focus)


class MainScreenHostWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_is_single_flight_and_success_updates_current_store_session(self) -> None:
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        started = threading.Event()
        release = threading.Event()

        def delayed_host(*args, **kwargs):
            started.set()
            # 全量套件下 Textual 调度可能挤占超过 1s；超时后若仍返回成功名，
            # 会在测试替换 store 会话对象之前就 mark_hosted 旧 dict，随后
            # dict(old, …) 把 keepalive_name 拷进新对象，断言两边都有名字。
            if not release.wait(timeout=5.0):
                raise TimeoutError("测试未能及时释放 delayed_host")
            return "pickup-claude-s0"

        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.host_session", side_effect=delayed_host) as host_mock:
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                old_request_session = store.sessions["claude"][0]

                await pilot.press("enter")
                await _wait_until(started.is_set)
                self.assertTrue(app.screen._host_pending > 0)

                # 第一次托管还没结束时重复确认，只响铃，不应再启动第二个进程。
                await pilot.press("enter")
                await pilot.pause(delay=0.05)
                self.assertEqual(host_mock.call_count, 1)

                current_session = dict(old_request_session, mtime=old_request_session["mtime"] + 1)
                store.sessions["claude"][0] = current_session
                release.set()
                await _wait_until(lambda: app.screen._host_pending == 0)

                self.assertEqual(host_mock.call_count, 1)
                self.assertEqual(current_session.get("keepalive_name"), "pickup-claude-s0")
                self.assertNotIn("keepalive_name", old_request_session)
                pane = await _wait_for_embed_pane(app.screen)
                await _wait_until(lambda: pane.session_name == "pickup-claude-s0")

    async def test_host_failure_releases_single_flight_guard(self) -> None:
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)

        with (
            mock.patch("pickup.embed.host_session", side_effect=RuntimeError("模拟启动失败")) as host_mock,
            mock.patch.object(pickup, "_log_embed_error"),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("enter")
                await _wait_until(lambda: host_mock.call_count == 1)
                await _wait_until(lambda: app.screen._host_pending == 0)
                self.assertEqual(app.screen._host_pending, 0)

    async def test_new_session_request_hosts_without_reading_session(self) -> None:
        """回归：NewSessionRequest 托管成功回调不得访问 request.session。

        底栏 n 快捷键已删除；空白新建仍经 `_embed_open(NewSessionRequest)`
        （侧边栏新建项 / 顶栏加格），这条回调契约必须继续成立。
        """
        store, registry = _make_store()
        registry.build_new_session_plan = lambda request: LaunchPlan(("claude",), "/tmp")
        app = PickupApp(store, embed_ok=True)

        with (
            mock.patch("pickup.embed.host_session", return_value="pickup-claude-new"),
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                app.screen._embed_open(
                    pickup.NewSessionRequest("claude", "/tmp"),
                    add_pane=False,
                )
                await _wait_until(lambda: app.screen._host_pending == 0)
                await _wait_until(
                    lambda: any(
                        s.get("keepalive_name") == "pickup-claude-new"
                        for s in app.screen.query_one(SessionListView).visible_sessions()
                    ),
                    tries=500,
                )
                await _wait_for_embed_session(app.screen, "pickup-claude-new")

    async def test_cross_runtime_handoff_shows_hosted_card_and_keeps_embed(self) -> None:
        """回归：Claude→Cursor 接力后左栏立刻出现托管卡，右栏与源会话并排分屏。

        真机实报：按 a 选 Cursor 后 host_session 成功，但 Cursor 卡在 Workspace Trust、
        尚未落盘 chat 时扫描器无条目；跨运行时路径又不 mark_hosted，左栏不冒新卡。
        同时 `_rebuild_list` → `_follow_current_selection` 因仍选中源 Claude，把右栏
        盖回对话预览，看起来像「什么都没发生」。

        产品默认：跨助手接力从被接力会话旁加一格，不得整屏换成新会话。
        """
        cursor = mock.Mock()
        cursor.id = "cursor"
        cursor.display_name = "Cursor"
        cursor.is_available.return_value = True
        cursor.scan_sessions.return_value = []
        cursor.load_conversation.return_value = []
        store, registry = _make_store(extra_runtimes=(cursor,))
        registry.build_launch_plan = lambda request: LaunchPlan(("agent", "--force", "prompt"), "/tmp")
        app = PickupApp(store, embed_ok=True)

        with (
            mock.patch(
                "pickup.embed.host_session", return_value="pickup-cursor-handoff"
            ) as host_mock,
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                list_view = app.screen.query_one(SessionListView)
                source = list_view.selected_session()
                self.assertIsNotNone(source)
                source_key = pickup.session_key(source)

                await pilot.press("a")
                await pilot.pause()
                self.assertIsInstance(app.screen, RuntimePickerModal)
                await pilot.press("down")  # claude 原生恢复 → cursor
                await pilot.press("enter")
                await _wait_until(lambda: host_mock.call_count == 1)
                await _wait_until(lambda: app.screen._host_pending == 0)
                # 等 call_next(_rebuild_list) 跑完
                await _wait_until(
                    lambda: any(
                        s.get("keepalive_name") == "pickup-cursor-handoff"
                        for s in list_view.visible_sessions()
                    )
                )
                await pilot.pause(delay=0.05)

                hosted = [
                    s for s in list_view.visible_sessions()
                    if s.get("keepalive_name") == "pickup-cursor-handoff"
                ]
                self.assertEqual(len(hosted), 1, "左栏应立刻出现 Cursor 托管占位卡")
                self.assertEqual(hosted[0].get("source"), "cursor")
                selected = list_view.selected_session()
                self.assertIsNotNone(selected)
                self.assertEqual(selected.get("keepalive_name"), "pickup-cursor-handoff")

                area = app.screen.query_one(SplitPaneArea)
                keys = area.ordered_session_keys()
                self.assertGreaterEqual(len(keys), 2, "接力应与源会话并排分屏，不得整屏替换")
                self.assertIn(source_key, keys, "被接力会话必须仍在右栏")

                await _wait_for_embed_pane(app.screen)
                await _wait_until(
                    lambda: any(
                        (c.embed_pane() and c.embed_pane().session_name == "pickup-cursor-handoff")
                        for c in area.cells()
                    )
                )
                handoff_cell = next(
                    c for c in area.cells()
                    if c.embed_pane() and c.embed_pane().session_name == "pickup-cursor-handoff"
                )
                handoff_pane = handoff_cell.embed_pane()
                # 接力托管成功 = 明确意图，输入直接落到新会话那一格
                await _wait_until(lambda: handoff_pane.has_focus)
                self.assertFalse(list_view.has_focus)

    async def test_same_runtime_advanced_action_opens_new_session_split(self) -> None:
        """高级操作选同一助手：读历史后新建并旁挂，不得原生恢复原会话。"""
        store, registry = _make_store()
        captured: list = []

        def capture_plan(request):
            captured.append(request)
            return LaunchPlan(("claude", "--dangerously-skip-permissions", "prompt"), "/tmp")

        registry.build_launch_plan = capture_plan
        app = PickupApp(store, embed_ok=True)

        with (
            mock.patch(
                "pickup.embed.host_session", return_value="pickup-claude-handoff-self"
            ) as host_mock,
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                list_view = app.screen.query_one(SessionListView)
                source = list_view.selected_session()
                self.assertIsNotNone(source)
                source_key = pickup.session_key(source)

                await pilot.press("a")
                await pilot.pause()
                self.assertIsInstance(app.screen, RuntimePickerModal)
                # 本用例只有 claude，默认即源助手；回车 = 同助手读历史后新建
                await pilot.press("enter")
                await _wait_until(lambda: host_mock.call_count == 1)
                await _wait_until(lambda: app.screen._host_pending == 0)
                await _wait_until(
                    lambda: any(
                        s.get("keepalive_name") == "pickup-claude-handoff-self"
                        for s in list_view.visible_sessions()
                    )
                )

                self.assertEqual(len(captured), 1)
                self.assertTrue(captured[0].force_new)
                self.assertEqual(captured[0].target_runtime_id, "claude")

                area = app.screen.query_one(SplitPaneArea)
                keys = area.ordered_session_keys()
                self.assertGreaterEqual(len(keys), 2, "同助手另起也应与源会话并排")
                self.assertIn(source_key, keys)
                hosted = [
                    s for s in list_view.visible_sessions()
                    if s.get("keepalive_name") == "pickup-claude-handoff-self"
                ]
                self.assertEqual(len(hosted), 1)
                self.assertNotEqual(
                    pickup.session_key(hosted[0]), source_key,
                    "同助手另起必须是新会话卡，不能 mark 到原会话",
                )

    async def test_copy_session_advanced_action_opens_fork_split(self) -> None:
        """高级操作选「复制会话」：走 copy_session 分叉计划，旁挂分屏。"""
        store, registry = _make_store()
        captured: list = []

        def capture_plan(request):
            captured.append(request)
            return LaunchPlan(
                ("claude", "--dangerously-skip-permissions", "--resume", "x", "--fork-session"),
                "/tmp",
            )

        registry.build_launch_plan = capture_plan
        app = PickupApp(store, embed_ok=True)

        with (
            mock.patch(
                "pickup.embed.host_session", return_value="pickup-claude-copy-fork"
            ) as host_mock,
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                list_view = app.screen.query_one(SessionListView)
                source = list_view.selected_session()
                self.assertIsNotNone(source)
                source_key = pickup.session_key(source)

                await pilot.press("a")
                await pilot.pause()
                self.assertIsInstance(app.screen, RuntimePickerModal)
                # 默认高亮在接力项；上移到第一项「复制会话」
                await pilot.press("up")
                await pilot.press("enter")
                await _wait_until(lambda: host_mock.call_count == 1)
                await _wait_until(lambda: app.screen._host_pending == 0)
                await _wait_until(
                    lambda: any(
                        s.get("keepalive_name") == "pickup-claude-copy-fork"
                        for s in list_view.visible_sessions()
                    )
                )

                self.assertEqual(len(captured), 1)
                self.assertTrue(captured[0].copy_session)
                self.assertFalse(captured[0].force_new)
                self.assertEqual(captured[0].target_runtime_id, "claude")

                area = app.screen.query_one(SplitPaneArea)
                keys = area.ordered_session_keys()
                self.assertGreaterEqual(len(keys), 2, "复制会话应与源会话并排")
                self.assertIn(source_key, keys)
                hosted = [
                    s for s in list_view.visible_sessions()
                    if s.get("keepalive_name") == "pickup-claude-copy-fork"
                ]
                self.assertEqual(len(hosted), 1)
                self.assertNotEqual(pickup.session_key(hosted[0]), source_key)


class PaneCellHeaderSyncTests(unittest.TestCase):
    """分栏标题栏在重建中间态可能缺失；焦点同步不得因此崩掉（真机：双击顶栏 OpenCode）。"""

    def test_pane_header_title_update_before_compose(self) -> None:
        from pickup.ui.split_pane_area import _PaneHeader

        header = _PaneHeader("旧标题", lambda: None)
        header.set_title("新标题")

        title_widget = list(header.compose())[0]
        self.assertEqual(str(title_widget.render()), "新标题")

    def test_sync_active_marker_tolerates_missing_header(self) -> None:
        from pickup.ui.split_pane_area import PaneCell, PaneSpec

        cell = PaneCell(
            PaneSpec(session_key="k", cell_id="c1"),
            title="t",
            on_close=lambda _spec: None,
            on_focus_list=lambda: None,
            osc_report=None,
        )
        # 未 mount / 未 compose：子节点为空（标题栏与底条都缺）
        cell._sync_active_marker()  # noqa: SLF001 — 不得抛 NoMatches
        cell.set_title("new-title")
        self.assertEqual(cell._title, "new-title")  # noqa: SLF001


class FooterActionGatingTests(unittest.TestCase):
    """右栏实时终端持有输入时，列表侧快捷键必须整体让路。

    这既决定 Footer 显示什么，也决定按键派不派发——preview_* 是优先级绑定，不
    让路的话用户在助手里翻历史会被右栏预览滚动截胡。
    """

    def _screen(self, live: bool):
        from pickup.ui.main_screen import MainScreen

        store, _ = _make_store()
        screen = MainScreen(store, embed_ok=True)
        screen._live_embed_focused = lambda: live  # noqa: SLF001
        screen._any_embed_focused = lambda: live  # noqa: SLF001
        return screen

    def test_list_actions_step_aside_when_live_pane_focused(self) -> None:
        screen = self._screen(live=True)
        for action in (
            "handoff", "kill_keepalive", "delete_session", "close_pane",
            "quit_app", "preview_home", "preview_page_up", "preview_page_down",
        ):
            with self.subTest(action=action):
                self.assertIs(screen.check_action(action, ()), False)
        self.assertTrue(screen.check_action("focus_list", ()))
        # 壳层显隐侧栏：与 Ctrl+\ 同级，右栏持焦时仍可用
        self.assertTrue(screen.check_action("toggle_sidebar", ()))

    def test_list_actions_available_when_sidebar_focused(self) -> None:
        screen = self._screen(live=False)
        for action in ("handoff", "kill_keepalive", "quit_app", "preview_page_up"):
            with self.subTest(action=action):
                self.assertTrue(screen.check_action(action, ()))
        # 焦点已经在列表时不必展示"回列表"
        self.assertFalse(screen.check_action("focus_list", ()))
        self.assertTrue(screen.check_action("toggle_sidebar", ()))

    def test_toggle_sidebar_disabled_without_embed(self) -> None:
        from pickup.ui.main_screen import MainScreen

        store, _ = _make_store()
        screen = MainScreen(store, embed_ok=False)
        self.assertFalse(screen.check_action("toggle_sidebar", ()))


class FooterVersionTests(unittest.IsolatedAsyncioTestCase):
    """底栏右端常驻版本号，紧挨 `^p palette` 左侧。"""

    async def test_footer_shows_version_left_of_command_palette(self) -> None:
        from pickup.ui.footer import PickupFooter

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)):
            footer = app.screen.query_one(Footer)
            self.assertIsInstance(footer, PickupFooter)
            await _wait_until(lambda: bool(footer.query("#footer-version")))
            version = footer.query_one("#footer-version", Label)
            self.assertEqual(str(version.content), f"v{pickup.__version__}")
            right = footer.query_one("#footer-right")
            kids = list(right.children)
            self.assertEqual(kids[0].id, "footer-version")
            self.assertTrue(any("-command-palette" in c.classes for c in kids[1:]))
            self.assertLess(version.region.x, kids[1].region.x)
            self.assertLessEqual(version.region.x + version.region.width, kids[1].region.x)


class SidebarToggleTests(unittest.IsolatedAsyncioTestCase):
    """Ctrl+Shift+B / 顶栏开关显隐侧栏；偏好落盘；藏起后仍能点回来。

    不用 Ctrl+B：机主在 Claude Code 里按 Ctrl+B 是「把任务转后台」，会与 pickup
    抢键（2026-08-04 冲突实报后改键）。
    """

    async def test_ctrl_shift_b_toggles_list_pane_display(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            list_pane = app.screen.query_one("#list-pane")
            chip = app.screen.query_one("#sidebar-toggle", _SidebarToggleChip)
            self.assertTrue(list_pane.display)
            self.assertEqual(chip.render().plain, "◀")

            await pilot.press("ctrl+shift+b")
            self.assertFalse(app.screen.sidebar_visible)
            self.assertFalse(list_pane.display)
            self.assertEqual(chip.render().plain, "▶")
            self.assertFalse(_ui_prefs.load_sidebar_visible(default=True))

            await pilot.press("ctrl+shift+b")
            self.assertTrue(app.screen.sidebar_visible)
            self.assertTrue(list_pane.display)
            self.assertEqual(chip.render().plain, "◀")

    async def test_top_bar_chip_click_restores_sidebar(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            list_pane = app.screen.query_one("#list-pane")
            await pilot.press("ctrl+shift+b")
            self.assertFalse(list_pane.display)

            await pilot.click("#sidebar-toggle")
            self.assertTrue(app.screen.sidebar_visible)
            self.assertTrue(list_pane.display)

    async def test_persisted_hidden_sidebar_restored_on_mount(self) -> None:
        # 顺序不能反：_make_store() 会整份重置侧边栏记忆库（含这条显隐偏好）。
        store, _ = _make_store()
        _ui_prefs.save_sidebar_visible(False)
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)):
            list_pane = app.screen.query_one("#list-pane")
            chip = app.screen.query_one("#sidebar-toggle", _SidebarToggleChip)
            self.assertFalse(app.screen.sidebar_visible)
            self.assertFalse(list_pane.display)
            self.assertEqual(chip.render().plain, "▶")
        # 不影响后续用例默认态
        _ui_prefs.save_sidebar_visible(True)


class InputMaskFilterTests(unittest.TestCase):
    """输入蒙版：把整格画面拉向面板底色，任何颜色形态都不能算崩。"""

    def _apply(self, style):
        from rich.segment import Segment
        from rich.terminal_theme import DEFAULT_TERMINAL_THEME
        from textual.color import Color

        from pickup.ui.embed_pane import _InputMaskFilter

        f = _InputMaskFilter(DEFAULT_TERMINAL_THEME)
        out = f.apply([Segment("x", style)], Color(0, 0, 0))
        return out[0].style

    def test_masks_ansi_default_and_truecolor(self) -> None:
        from rich.color import Color as RichColor
        from rich.style import Style

        cases = [
            None,  # 无样式（终端默认前景/背景）
            Style(),  # 有样式但全默认
            Style(color=RichColor.from_ansi(2), bgcolor=RichColor.from_ansi(4)),
            Style(color=RichColor.from_rgb(255, 255, 255), bold=True),
        ]
        for style in cases:
            with self.subTest(style=style):
                masked = self._apply(style)
                # 压暗后前景/背景都必须是可直接输出的真彩色，且被拉向黑色底
                self.assertIsNotNone(masked.color)
                self.assertIsNotNone(masked.bgcolor)
                self.assertIsNotNone(masked.color.triplet)
                self.assertIsNotNone(masked.bgcolor.triplet)

    def test_mask_dims_white_text_toward_background(self) -> None:
        from rich.color import Color as RichColor
        from rich.style import Style

        masked = self._apply(Style(color=RichColor.from_rgb(255, 255, 255)))
        self.assertLess(masked.color.triplet.red, 255)
        self.assertGreater(masked.color.triplet.red, 0)

    def test_mask_keeps_text_attributes(self) -> None:
        from rich.color import Color as RichColor
        from rich.style import Style

        masked = self._apply(Style(color=RichColor.from_rgb(255, 0, 0), bold=True))
        self.assertTrue(masked.bold)


@unittest.skipUnless(HAS_TMUX, "内嵌面板依赖真实 tmux")
class MainScreenEmbedFlowTests(unittest.IsolatedAsyncioTestCase):
    """用真实（但轻量）tmux 会话验证 MainScreen ↔ EmbedPane ↔ embed.py 接线。"""

    def setUp(self) -> None:
        self._hosted_names: list[str] = []
        self.addCleanup(self._cleanup_hosted)

    def _cleanup_hosted(self) -> None:
        for name in self._hosted_names:
            subprocess.run(["tmux", "-L", "pickup-keepalive", "kill-session", "-t", name],
                            stderr=subprocess.DEVNULL)

    async def test_first_frame_never_exposes_connecting_state(self) -> None:
        """抓帧尚未完成时也要即时展示已有详情或空白终端，不能出现连接中间态。"""
        store, _registry = _make_store()
        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.open_channel", return_value=None):
            async with app.run_test(size=(120, 30)):
                pane = _primary_embed_pane(app.screen)

                pane.focus_session(
                    "已有会话", lambda: "即时会话详情", detail_until_frame=True,
                )
                self.assertEqual(pane.render().plain, "即时会话详情")

                pane.focus_session("刚启动的新会话")
                self.assertEqual(pane.render().plain, "")
                self.assertNotIn("连接中", pane.render().plain)

                # 默认冷切换不跑 Markdown 回退，避免跨组切屏卡顿/闪屏
                pane.focus_session("冷切换", lambda: "不应出现")
                self.assertEqual(pane.render().plain, "")
                self.assertFalse(pane._is_hosted_fallback())

    async def test_hosted_fallback_pins_long_conversation_to_bottom(self) -> None:
        """托管首帧前的长对话回退必须钉底，可见区不得出现最早消息。"""
        from rich.text import Text as RichText

        early = "EARLY-MSG-UNIQUE"
        late = "LATE-MSG-UNIQUE"
        lines = [early] + [f"mid-{i}" for i in range(80)] + [late]
        body = RichText("\n".join(lines))
        store, _registry = _make_store()
        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.open_channel", return_value=None), \
             mock.patch("pickup.embed.should_resize_host", return_value=False), \
             mock.patch("pickup.embed.capture", return_value=None):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                pane = _primary_embed_pane(app.screen)
                await _wait_until(lambda: pane.size.height >= 10 and pane.size.width >= 40)
                # 挡住列表跟随，避免 focus_session 后被盖回静态预览
                with mock.patch.object(app.screen, "_follow_current_selection"):
                    pane.focus_session(
                        "pickup-cursor-x", lambda: body, detail_until_frame=True,
                    )
                    self.assertTrue(pane._detail_stick_bottom)
                    self.assertTrue(pane._is_hosted_fallback())
                    pane._pin_detail_to_bottom()
                    strips = pane._ensure_static_strips()
                    visible = "\n".join(s.text for s in strips)
                    self.assertIn(late, visible)
                    self.assertNotIn(early, visible)

    async def test_enter_hosts_session_and_pane_shows_live_output(self) -> None:
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", "printf 'HELLO-UI-TEST\\n'; cat"), None
        )

        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            pane = await _wait_for_embed_pane(app.screen)
            await _wait_for_session_name(pane)
            self._hosted_names.append(pane.session_name)
            self.assertNotIn("连接中", pane.render().plain)
            await _wait_for_pane_text(pane, "HELLO-UI-TEST")
            list_view = app.screen.query_one(SessionListView)
            # 回车 = 明确意图：输入直接交给右栏那一格，无需再点鼠标
            await _wait_until(lambda: pane.has_focus)
            self.assertFalse(list_view.has_focus)
            cell = app.screen.query_one(SplitPaneArea)._cells()[0]  # noqa: SLF001
            title = cell.query_one(".title")
            header = cell.query_one(".header")
            footer = cell.query_one(".footer")
            self.assertTrue(header.has_class("-active"))
            self.assertTrue(footer.has_class("-active"))
            self.assertFalse(title.render().plain.startswith("● "))

            # 鼠标点右栏同样进入内嵌会话；ctrl+backslash 回列表，'c' 关闭分栏
            await pilot.click(pane)
            await pilot.pause()
            self.assertTrue(pane.has_focus)
            self.assertTrue(header.has_class("-active"))
            self.assertTrue(footer.has_class("-active"))
            self.assertFalse(title.render().plain.startswith("● "))
            await pilot.press("ctrl+backslash")
            await pilot.pause()
            self.assertFalse(header.has_class("-active"))
            self.assertFalse(footer.has_class("-active"))
            await pilot.press("c")
            await pilot.pause()
            area = app.screen.query_one(SplitPaneArea)
            self.assertEqual(area.pane_count(), 0)

        from pickup import embed
        self.assertTrue(embed.is_alive(self._hosted_names[0]))

    async def test_ctrl_shift_b_toggles_sidebar_while_pane_has_input(self) -> None:
        """实时终端持有输入时 Ctrl+Shift+B 仍可显隐侧栏，旧键 Ctrl+B 不再截胡。

        改键背景（2026-08-04 机主实报）：Claude Code 里 Ctrl+B 是「把任务转后台」，
        pickup 截走会把侧栏藏起来。新键与 Ctrl+\\ 同级属壳层键，EmbedPane 先拦截
        不进托管会话；旧键则原样转发给助手。
        """
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", "printf 'HELLO-UI-TEST\\n'; cat"), None
        )
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            pane = await _wait_for_embed_pane(app.screen)
            await _wait_for_session_name(pane)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "HELLO-UI-TEST")
            await _wait_until(lambda: pane.has_focus)

            list_pane = app.screen.query_one("#list-pane")
            self.assertTrue(list_pane.display)
            # 壳层键穿透：pane 持焦时仍能显隐侧栏。
            await pilot.press("ctrl+shift+b")
            self.assertFalse(list_pane.display)
            await pilot.press("ctrl+shift+b")
            self.assertTrue(list_pane.display)
            # 旧键不再截胡：pane 持焦时按 Ctrl+B 转发给托管会话，侧栏不动。
            await pilot.press("ctrl+b")
            self.assertTrue(list_pane.display)

    async def test_reselecting_static_session_keeps_live_frame(self) -> None:
        """重复高亮同一个静止会话不能清空画面后永久停在“连接中…”。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", "printf 'STATIC-RESELECT-TEST\\n'; cat"), None
        )

        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("enter")
            pane = _primary_embed_pane(app.screen)
            await _wait_for_session_name(pane)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "STATIC-RESELECT-TEST")

            generation = pane._capture_generation
            pane.focus_session(pane.session_name)
            await pilot.pause(delay=0.4)

            self.assertEqual(pane._capture_generation, generation)
            self.assertIn("STATIC-RESELECT-TEST", pane.render().plain)
            self.assertNotIn("连接中", pane.render().plain)

    async def test_fast_detail_round_trip_forces_static_frame_reparse(self) -> None:
        """抓帧线程来不及观察中间态时，版本变化也必须让同名静止帧重新解析。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", "printf 'STATIC-ROUND-TRIP-TEST\\n'; cat"), None
        )

        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("enter")
            pane = _primary_embed_pane(app.screen)
            await _wait_for_session_name(pane)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "STATIC-ROUND-TRIP-TEST")

            name = pane.session_name
            pane.show_detail(lambda: "临时详情")
            pane.focus_session(name)
            await _wait_for_pane_text(pane, "STATIC-ROUND-TRIP-TEST")

            self.assertIn("STATIC-ROUND-TRIP-TEST", pane.render().plain)

    async def test_stale_capture_callback_cannot_overwrite_new_view(self) -> None:
        """旧会话已排队的抓帧回调不能覆盖随后打开的详情页。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", "printf 'STALE-CALLBACK-TEST\\n'; cat"), None
        )

        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("enter")
            pane = _primary_embed_pane(app.screen)
            await _wait_for_session_name(pane)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "STALE-CALLBACK-TEST")

            old_generation = pane._capture_generation
            old_name = pane.session_name
            old_grid = pane._grid
            pane.show_detail(lambda: "新的详情页")
            pane._apply_capture(old_generation, old_name, old_grid, None, None)

            self.assertEqual(pane.render().plain, "新的详情页")

    async def test_capture_thread_recovers_after_unexpected_parse_error(self) -> None:
        """单帧解析异常只能丢一帧，抓帧线程必须继续并自动重试。"""
        from pickup import embed

        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", "printf 'CAPTURE-RECOVERY-TEST\\n'; cat"), None
        )
        original_parse_screen = embed.parse_screen_rows
        parse_calls = 0

        def flaky_parse_screen(*args, **kwargs):
            nonlocal parse_calls
            parse_calls += 1
            if parse_calls == 1:
                raise RuntimeError("模拟单帧解析失败")
            return original_parse_screen(*args, **kwargs)

        app = PickupApp(store, embed_ok=True)
        with (mock.patch("pickup.embed.parse_screen_rows", side_effect=flaky_parse_screen),
              mock.patch("pickup._log_embed_error") as log_error):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("enter")
                pane = _primary_embed_pane(app.screen)
                await _wait_for_session_name(pane)
                self._hosted_names.append(pane.session_name)
                await _wait_for_pane_text(pane, "CAPTURE-RECOVERY-TEST")

            self.assertGreaterEqual(parse_calls, 2)
            log_error.assert_called_once()

    async def test_focus_shows_real_cursor_blur_hides_it(self) -> None:
        """IME 回归：聚焦内嵌 pane 且有可见光标时必须显式打开外层真实硬件光标
        （`\\e[?25h`），失焦时收起。Textual 全屏运行期默认藏掉真实光标，只移动一个
        看不见的光标——位置再准，IME 也没有可见锚点，用户打不出中文（真机反馈）。
        这里断言 EmbedPane._real_cursor_shown 随焦点/光标状态正确翻转（selftest.sh
        另有真实外层终端 `#{cursor_flag}` 断言，覆盖真正写没写出转义）。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", "printf 'CURSOR-TEST\\n'; cat"), None
        )
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("enter")
            await pilot.pause(delay=0.5)
            pane = _primary_embed_pane(app.screen)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "CURSOR-TEST")
            list_view = app.screen.query_one(SessionListView)
            # 回车即把输入交给右栏，真实光标随之打开（IME 锚点）
            await _wait_until(lambda: pane.has_focus)
            await _wait_until(lambda: pane._real_cursor_shown)
            self.assertTrue(pane._real_cursor_shown, "聚焦活会话时应显示外层真实光标")

            await pilot.press("ctrl+backslash")  # 焦点回列表
            await pilot.pause()
            self.assertTrue(list_view.has_focus)
            self.assertFalse(pane._real_cursor_shown, "失焦后应收起外层真实光标")

            # 回列表后实时画面压暗，提示此刻输入不会进到助手
            await _wait_until(lambda: pane.input_masked)
            await pilot.press("tab")  # Tab 也能进右栏（与 selftest 一致）
            await pilot.pause()
            self.assertTrue(pane.has_focus)
            await _wait_until(lambda: not pane.input_masked)

    async def test_drag_select_mouseup_auto_copies_to_clipboard(self) -> None:
        """划词抬起后应自动经 OSC 52 复制，不必再按 Ctrl+C。

        Textual Screen 在 MouseUp 时会发 TextSelected；MainScreen 有选区就
        copy_to_clipboard。用的是内置拖选（EmbedPane 未关 ALLOW_SELECT）。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", "printf 'HELLO-SELECT-ME\\n'; while true; do sleep 0.1; done"), None
        )
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("enter")
            await pilot.pause(delay=0.5)
            pane = _primary_embed_pane(app.screen)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "HELLO-SELECT-ME")

            await pilot.mouse_down(pane, offset=Offset(0, 0))
            await pilot.hover(pane, offset=Offset(14, 0))
            await pilot.mouse_up(pane, offset=Offset(14, 0))
            await pilot.pause(delay=0.2)
            self.assertEqual(app.screen.get_selected_text(), "HELLO-SELECT-ME")
            self.assertEqual(app._clipboard, "HELLO-SELECT-ME")

    async def test_drag_select_then_ctrl_c_still_copies_to_clipboard(self) -> None:
        """划词后 Ctrl+C 仍可再次复制（兼容习惯；无选区时仍转发中断）。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", "printf 'HELLO-SELECT-ME\\n'; while true; do sleep 0.1; done"), None
        )
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("enter")
            await pilot.pause(delay=0.5)
            pane = _primary_embed_pane(app.screen)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "HELLO-SELECT-ME")

            await pilot.mouse_down(pane, offset=Offset(0, 0))
            await pilot.hover(pane, offset=Offset(14, 0))
            await pilot.mouse_up(pane, offset=Offset(14, 0))
            await pilot.pause(delay=0.2)
            self.assertEqual(app.screen.get_selected_text(), "HELLO-SELECT-ME")
            # 清空后再按 Ctrl+C，确认手动复制路径仍可用
            app._clipboard = ""
            await pilot.press("ctrl+c")
            await pilot.pause(delay=0.3)
        self.assertEqual(app._clipboard, "HELLO-SELECT-ME")

    async def test_ctrl_c_without_selection_forwards_interrupt_to_hosted_program(self) -> None:
        """没有选中任何文本时，Ctrl+C 必须原样转发给托管会话（中断当前命令），
        不能被"复制选中文本"这个新功能吞掉——这是终端最基本的操作，回归测试
        钉死（真机排查过 Textual 的按键派发：widget 自己的 on_key 一旦处理并
        stop() 掉事件，BINDINGS 系统根本不会再被咨询，逻辑必须直接写在
        on_key 里，不能指望走 Screen 的 ctrl+c -> copy_text 绑定）。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", 'trap "echo GOT-SIGINT" INT; echo READY; while true; do sleep 0.1; done'),
            None,
        )
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("enter")
            await pilot.pause(delay=0.5)
            pane = _primary_embed_pane(app.screen)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "READY")

            # 列表仍持有焦点时 Ctrl+C 不会进托管会话；先 Tab 进入右栏再测中断转发
            await pilot.press("tab")
            await pilot.pause()
            self.assertTrue(pane.has_focus)

            await pilot.press("ctrl+c")
            await _wait_for_pane_text(pane, "GOT-SIGINT", tries=30)
            rendered_with_interrupt = pane.render().plain
        self.assertIn("GOT-SIGINT", rendered_with_interrupt)
        # 卸载后的无障碍/测试读取仍应安全返回基础画面，不再访问不存在的 Screen。
        pane.render()

    async def test_host_session_failure_bells_and_stays_in_list(self) -> None:
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)

        app = PickupApp(store, embed_ok=True)
        with mock.patch(
            "pickup.embed.host_session",
            side_effect=__import__("pickup.embed", fromlist=["EmbedError"]).EmbedError("boom"),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("enter")
                # host_session 现在跑在后台 worker 里，失败结果要经 call_from_thread
                # 回到主线程才会触发 bell；给够时间让这趟线程往返完成。
                await pilot.pause(delay=0.3)
        self.assertIsNone(app.return_value)  # 仍停留在应用内，没有异常退出


class EmbedPaneWheelTests(unittest.TestCase):
    """滚轮转发回归：2026-07-19 卡顿根因——主线程每事件同步 fork 两次 tmux
    （还多发了 xterm 规范里不存在的滚轮 release 序列），触控板惯性滚动把界面堵死。"""

    def test_wheel_forwards_press_only_via_background_sender(self):
        pane = EmbedPane()
        pane.session_name = "pickup-claude-x"
        pane._mouse_any = True
        with (mock.patch("pickup.embed.send_mouse_sequence") as send_mock,
              mock.patch("pickup.embed.send_literal") as literal_mock):
            pane._wheel(64, 10.0, 5.0, -3)
        # 只发一次 press 序列（64;11;6，坐标 1-based），经后台队列，不直接 fork
        send_mock.assert_called_once_with("pickup-claude-x", "\x1b[<64;11;6M")
        literal_mock.assert_not_called()

    def test_wheel_without_mouse_capture_uses_app_level_scroll(self):
        pane = EmbedPane()
        pane.session_name = "pickup-codex-x"
        pane._mouse_any = False
        pane._scroll = mock.Mock()
        with mock.patch("pickup.embed.send_mouse_sequence") as send_mock:
            pane._wheel(64, 10.0, 5.0, -3)
        pane._scroll.assert_called_once_with(-3)
        send_mock.assert_not_called()

    def test_scroll_handlers_move_app_history_in_expected_direction(self):
        pane = EmbedPane()
        pane.session_name = "pickup-codex-x"
        pane._mouse_any = False
        pane._history_size = 100
        scroll_up = events.MouseScrollUp(None, 10, 5, 0, 0, 0, False, False, False)
        scroll_down = events.MouseScrollDown(None, 10, 5, 0, 0, 0, False, False, False)

        pane._on_mouse_scroll_up(scroll_up)
        self.assertEqual(pane.history_offset, 3)
        pane._on_mouse_scroll_down(scroll_down)
        self.assertEqual(pane.history_offset, 0)

    def test_detail_wheel_follows_document_scroll_direction(self):
        """已结束会话预览：下滚应增大 detail_offset（看更晚内容），与 history_offset 相反。"""
        pane = EmbedPane()
        pane.show_detail(lambda: "预览正文")
        pane.scroll_detail = mock.Mock(return_value=True)
        scroll_down = events.MouseScrollDown(None, 10, 5, 0, 0, 0, False, False, False)
        scroll_up = events.MouseScrollUp(None, 10, 5, 0, 0, 0, False, False, False)

        pane._on_mouse_scroll_down(scroll_down)
        pane.scroll_detail.assert_called_with(3)
        pane._on_mouse_scroll_up(scroll_up)
        pane.scroll_detail.assert_called_with(-3)

    def test_show_detail_enables_stick_to_bottom(self):
        """选中静态预览时开启钉底；Home 取消，End 恢复。"""
        pane = EmbedPane()
        pane.show_detail(lambda: "预览正文")
        self.assertTrue(pane._detail_stick_bottom)
        with mock.patch.object(pane, "_is_detail_view", return_value=True), \
             mock.patch.object(pane, "_detail_max_offset", return_value=20), \
             mock.patch.object(pane, "refresh"):
            pane.detail_offset = 20
            pane.scroll_detail_home()
            self.assertFalse(pane._detail_stick_bottom)
            self.assertEqual(pane.detail_offset, 0)
            pane.scroll_detail_end()
            self.assertTrue(pane._detail_stick_bottom)
            self.assertEqual(pane.detail_offset, 20)

    def test_focus_session_without_fallback_stays_blank_canvas(self):
        """无 fallback 时仍空白画布，不出现连接中。"""
        pane = EmbedPane()
        with mock.patch("pickup.embed.open_channel", return_value=None), \
             mock.patch("pickup.embed.should_resize_host", return_value=False):
            pane.focus_session("pickup-cursor-new")
        self.assertFalse(pane._detail_stick_bottom)
        self.assertEqual(pane.render().plain, "")
        self.assertNotIn("连接中", pane.render().plain)

    def test_focus_session_with_fallback_enables_stick_bottom(self):
        """显式要求首帧前对话回退时 focus_session 必须开启钉底。"""
        pane = EmbedPane()
        with mock.patch("pickup.embed.open_channel", return_value=None), \
             mock.patch("pickup.embed.should_resize_host", return_value=False):
            pane.focus_session(
                "pickup-cursor-x", lambda: "fallback", detail_until_frame=True,
            )
        self.assertTrue(pane._detail_stick_bottom)
        self.assertTrue(pane._is_hosted_fallback())
        self.assertTrue(pane._uses_detail_window())

    def test_scroll_handlers_preserve_sgr_direction_without_local_scroll(self):
        pane = EmbedPane()
        pane.session_name = "pickup-claude-x"
        pane._mouse_any = True
        pane._history_size = 100
        pane.history_offset = 7
        scroll_up = events.MouseScrollUp(None, 10, 5, 0, 0, 0, False, False, False)
        scroll_down = events.MouseScrollDown(None, 10, 5, 0, 0, 0, False, False, False)

        with mock.patch("pickup.embed.send_mouse_sequence") as send_mock:
            pane._on_mouse_scroll_up(scroll_up)
            pane._on_mouse_scroll_down(scroll_down)

        self.assertEqual(
            send_mock.call_args_list,
            [
                mock.call("pickup-claude-x", "\x1b[<64;11;6M"),
                mock.call("pickup-claude-x", "\x1b[<65;11;6M"),
            ],
        )
        self.assertEqual(pane.history_offset, 7)


class EmbedPaneSelectionSpanTests(unittest.IsolatedAsyncioTestCase):
    """拖选高亮范围回归：全面覆盖纯英文 / 纯中文 / 中英混排 / 全角+半角 /
    多样式段 / 多行（end==-1），穷举每一个字符区间，断言两件事——
    ① 输出文本不丢字（选区边界落在宽字符中间时不能把该字符吃成空格）；
    ② 被高亮的字符正好等于选中的字符（高亮宽度不缩水、不错位）。

    背景（2026-07-20 一连串真机反馈 + 我自己 headless 复现）：Textual 的选区
    坐标系是"字符索引"（`get_span` 返回字符下标），但 `_apply_selection` 早期直接
    把它交给按"cell 列"裁切的 `Strip.crop`。CJK/全角一个字占 2 列，两套坐标不等，
    导致高亮缩水/错位、宽字符被吃成空格。修复：裁切前用 `cell_len(text[:idx])`
    把字符索引换算成 cell 列。此前只针对纯中文写过一个窄测试就发版，漏了英文/
    混排——这个类就是补齐"充分的测试用例设计"。"""

    def _spans_ok(self, pane, strip, text):
        """穷举 [s,e) 字符区间，逐个断言不丢字、高亮精确。strip 已带 offset 元数据。"""
        from unittest.mock import PropertyMock

        from textual.geometry import Offset
        from textual.selection import Selection

        n = len(text)
        for s in range(n + 1):
            for e in range(s, n + 1):
                sel = Selection(Offset(s, 0), Offset(e, 0))
                with mock.patch.object(
                    EmbedPane, "text_selection",
                    new_callable=PropertyMock, return_value=sel,
                ):
                    out = pane._apply_selection(strip, 0)
                self.assertEqual(out.text, strip.text, f"{text!r} 选区[{s}:{e}] 丢字了")
                highlighted = "".join(
                    seg.text for seg in out if seg.style and seg.style.bgcolor
                )
                self.assertEqual(highlighted, text[s:e], f"{text!r} 选区[{s}:{e}] 高亮错位")

    async def test_selection_spans_across_scripts(self):
        from rich.segment import Segment
        from rich.style import Style
        from textual.strip import Strip

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(120, 30)) as pilot:
            pane = EmbedPane()
            await app.screen.mount(pane)
            await pilot.pause()

            cases = [
                "hello world",          # 纯英文（半角）
                "另外提醒一件事",         # 纯中文（全角）
                "标点bug hello",         # 中前英后
                "hi 你好 bye",           # 英-中-英
                "ａｂｃabc你好x",         # 全角字母 + 半角字母 + 中文 + 半角
            ]
            for text in cases:
                # 单段
                single = Strip([Segment(text, Style(color="#e0e0e0"))]).apply_offsets(0, 0)
                self._spans_ok(pane, single, text)
                # 多段（每 3 个字符换一种颜色，模拟真实语法高亮）
                segs, i, palette = [], 0, ["#ff0000", "#00ff00", "#0088ff"]
                while i < len(text):
                    segs.append(Segment(text[i:i + 3], Style(color=palette[(i // 3) % 3])))
                    i += 3
                multi = Strip(segs).apply_offsets(0, 0)
                self._spans_ok(pane, multi, text)

    async def test_selection_to_end_of_line_uses_full_width(self):
        """多行选区里，非末行的 get_span 返回 (start, -1)（一直选到行尾）；
        end==-1 必须换算成整行 cell 宽度，且中英文都不能丢字。"""
        from unittest.mock import PropertyMock

        from rich.segment import Segment
        from rich.style import Style
        from textual.geometry import Offset
        from textual.selection import Selection
        from textual.strip import Strip

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(120, 30)) as pilot:
            pane = EmbedPane()
            await app.screen.mount(pane)
            await pilot.pause()
            for text in ["hello world", "另外a提b醒", "ｍｉｘ 混排 end"]:
                strip = Strip([Segment(text, Style(color="#e0e0e0"))]).apply_offsets(0, 0)
                for start in range(len(text) + 1):
                    sel = Selection(Offset(start, 0), None)  # None end -> get_span 给 (start,-1)
                    with mock.patch.object(
                        EmbedPane, "text_selection",
                        new_callable=PropertyMock, return_value=sel,
                    ):
                        out = pane._apply_selection(strip, 0)
                    self.assertEqual(out.text, strip.text, f"{text!r} 选到行尾[{start}:] 丢字了")
                    highlighted = "".join(
                        seg.text for seg in out if seg.style and seg.style.bgcolor
                    )
                    self.assertEqual(highlighted, text[start:], f"{text!r} 选到行尾[{start}:] 高亮错")

    async def test_selection_through_real_parse_pipeline(self):
        """走真实解析管线（parse_screen -> _row_to_strip -> adjust_cell_length ->
        apply_offsets），端到端验证 render_line 的选区渲染，覆盖英文与混排。"""
        from unittest.mock import PropertyMock

        from textual.geometry import Offset, Size
        from textual.selection import Selection

        from pickup import embed
        from pickup.ui.embed_pane import _row_to_strip

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        WIDTH = 40
        async with app.run_test(size=(80, 24)) as pilot:
            pane = EmbedPane()
            await app.screen.mount(pane)
            await pilot.pause()
            pane.session_name = "pickup-claude-x"
            pane.dead = False
            for line in ["the quick brown fox", "标点bug hello", "ｈｅｌｌｏ ab 你好"]:
                grid = embed.parse_screen(line, width=WIDTH, height=1)
                pane._grid = grid
                pane._strips = [_row_to_strip(grid[0])]
                n = len(line)
                for s in range(n + 1):
                    for e in range(s, n + 1):
                        sel = Selection(Offset(s, 0), Offset(e, 0))
                        with mock.patch.object(
                            EmbedPane, "text_selection",
                            new_callable=PropertyMock, return_value=sel,
                        ), mock.patch.object(
                            type(pane), "size",
                            new_callable=PropertyMock, return_value=Size(WIDTH, 24),
                        ):
                            out = pane.render_line(0)
                        # render_line 会把行补齐到面板宽度，取前 n 个可见字符比对
                        self.assertTrue(out.text.startswith(line), f"{line!r}[{s}:{e}] 可见文本被破坏")
                        highlighted = "".join(
                            seg.text for seg in out if seg.style and seg.style.bgcolor
                        )
                        self.assertEqual(highlighted, line[s:e], f"{line!r} 选区[{s}:{e}] 高亮错位")

    async def test_active_selection_does_not_corrupt_offset_metadata(self):
        """拖选进行中不能污染 offset 元数据——这是"从中间往右拖、起点左边反而
        被高亮"的真正根因（2026-07-20 真机反馈，中英文都中招）。

        Textual 在拖选过程中会反复回读 render_line 把屏幕列换算成字符位置。若
        render_line 先 apply_offsets 再 crop 出选区三段，`Strip.crop` 拆 Segment
        时只照抄原 offset 不重算，三段会全带原整段的 offset（单段行拆完三段都
        是 (0,0)），换算就崩到行首——选区反向。正确顺序是先 crop 再 apply_offsets。

        不变量：不论当前有没有选区，render_line 出来的每个 Segment 的 offset 起
        始下标都必须等于它前面所有 Segment 文本的累计字符数（即与"对同一文本重新
        apply_offsets"完全一致）。"""
        from unittest.mock import PropertyMock

        from rich.segment import Segment
        from rich.style import Style
        from textual.geometry import Offset, Size
        from textual.selection import Selection
        from textual.strip import Strip

        def offsets_consistent(strip):
            expect = 0
            for seg in strip:
                meta = seg.style.meta.get("offset") if (seg.style and seg.style._meta) else None
                if meta is None or meta[0] != expect:
                    return False, expect, meta
                expect += len(seg.text)
            return True, None, None

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(60, 8)) as pilot:
            pane = EmbedPane()
            await app.screen.mount(pane)
            await pilot.pause()
            pane.session_name = "x"
            pane.dead = False
            for text in ["the quick brown fox", "另外提醒一件事abc", "标点bug hello"]:
                W = max(1, len(text))
                pane._grid = [[object()] * len(text)]
                pane._strips = [Strip([Segment(text, Style(color="#e0e0e0"))])]
                n = len(text)
                # 模拟拖选进行中：从每个可能的起点选出一小段
                for anchor in range(n + 1):
                    sel = Selection(Offset(anchor, 0), Offset(min(anchor + 1, n), 0))
                    with mock.patch.object(
                        EmbedPane, "text_selection",
                        new_callable=PropertyMock, return_value=sel,
                    ), mock.patch.object(
                        type(pane), "size",
                        new_callable=PropertyMock, return_value=Size(W, 8),
                    ):
                        out = pane.render_line(0)
                    ok, expect, meta = offsets_consistent(out)
                    self.assertTrue(
                        ok,
                        f"{text!r} anchor={anchor}: 选区把 offset 元数据搞乱了，"
                        f"某段应起于字符 {expect} 实际 offset={meta}",
                    )


class EmbedPaneSelectionStyleTests(unittest.IsolatedAsyncioTestCase):
    """拖选高亮不能盖住文字：2026-07-20 真机反馈"高亮把选中的文字整个遮住看
    不见"。headless 启动真实 app 打印证据确认——Textual 默认
    screen-selection-foreground 是 transparent（保留原前景语义），但
    get_component_rich_style 会把它预解析成一个具体色且恰好等于选区背景色，
    整段套上去后前景==背景，文字隐形。修复：前景 transparent 时只染背景、
    保留每个 Segment 原本的前景色。"""

    async def test_selection_style_preserves_foreground(self) -> None:
        from rich.segment import Segment
        from rich.style import Style
        from textual.strip import Strip

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(120, 30)) as pilot:
            pane = EmbedPane()
            await app.screen.mount(pane)
            await pilot.pause()

            sel = pane._selection_style()
            # 关键断言：选区样式不覆盖前景（保留原文字色），但要有背景色
            self.assertIsNone(sel.color, "选区前景应留空以保留原文字色，否则会盖住文字")
            self.assertIsNotNone(sel.bgcolor, "选区必须有背景色才能体现选中")

            # 端到端：一段有明确前景的文字套上选区样式后，前景必须原样保留、
            # 且不等于背景（等于就等于看不见）
            base = Strip([Segment("Hello 中文", Style(color="#e0e0e0"))])
            highlighted = base.crop(0, base.cell_length).apply_style(sel)
            seg = list(highlighted)[0]
            self.assertEqual(seg.style.color.triplet.hex, "#e0e0e0")
            self.assertNotEqual(seg.style.color, seg.style.bgcolor)

    async def test_selection_overrides_cell_own_background(self) -> None:
        """自带 ANSI 背景色的单元格（tmux 帧里染了底色的格子）被选中时，选区高亮
        背景必须盖住原背景色，整段统一显示选中色——否则"选了带背景色的文字看不出
        选中效果"（本次真机反馈的根因）。

        根因：`Strip.apply_style` 是基础样式语义（`selection + cell`），单元格自带的
        背景色会把选区背景顶掉。`_apply_selection` 改用 `_overlay_style`（后置样式，
        `cell + selection`）后，选区背景强制覆盖，同时留空前景以保留原文字色。"""
        from unittest.mock import PropertyMock

        from rich.segment import Segment
        from rich.style import Style
        from textual.geometry import Offset
        from textual.selection import Selection
        from textual.strip import Strip

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(120, 30)) as pilot:
            pane = EmbedPane()
            await app.screen.mount(pane)
            await pilot.pause()

            sel_style = pane._selection_style()
            text = "STATUS"
            # 整段自带红底白字（模拟状态栏/语法高亮的背景块）
            strip = Strip(
                [Segment(text, Style(color="#ffffff", bgcolor="#aa0000"))]
            ).apply_offsets(0, 0)
            selection = Selection(Offset(0, 0), Offset(len(text), 0))
            with mock.patch.object(
                EmbedPane, "text_selection",
                new_callable=PropertyMock, return_value=selection,
            ):
                out = pane._apply_selection(strip, 0)

            self.assertEqual(out.text, text, "选中带背景色的文字不能丢字")
            for seg in out:
                if not seg.text:
                    continue
                self.assertEqual(
                    seg.style.bgcolor, sel_style.bgcolor,
                    "选区背景必须覆盖单元格自带的 ANSI 背景色",
                )
                self.assertNotEqual(
                    seg.style.bgcolor.triplet.hex if seg.style.bgcolor else None,
                    "#aa0000",
                    "原背景色不应残留",
                )
                # 前景保留原文字色（透明前景语义）
                self.assertEqual(
                    seg.style.color.triplet.hex, "#ffffff",
                    "选中后应保留原文字前景色",
                )


class EmbedPaneResizeTests(unittest.IsolatedAsyncioTestCase):
    """窗口缩放：行宽即时裁补；tmux resize + 抓帧必须防抖，不能拖动期狂刷。"""

    def test_sync_strips_accepts_native_parsed_rows(self) -> None:
        """原生解析器返回预编译行时，首帧和逐行更新都不能按 Cell 列表取长度。"""
        pane = EmbedPane()
        pane.refresh = mock.Mock()
        first = [pickup.embed.ParsedRow("abc   ", (), 1)]
        second = [pickup.embed.ParsedRow("abd   ", (), 2)]

        pane._sync_strips(first)
        self.assertEqual(pane._strips[0].cell_length, 6)
        pane._sync_strips(second)

        self.assertEqual(pane._strips[0].text, "abd   ")
        self.assertEqual(pane._grid, second)

    def test_render_line_adjusts_cached_strip_to_current_width(self) -> None:
        from rich.segment import Segment
        from textual.strip import Strip

        pane = EmbedPane()
        pane.session_name = "pickup-claude-x"
        # 模拟旧宽度缓存行（10 列），面板已缩到 6 列
        pane._grid = [[object()] * 10]  # 非空即可让 render_line 走 _strips 分支
        pane._strips = [Strip([Segment("abcdefghij")])]
        with mock.patch.object(type(pane), "size", new_callable=mock.PropertyMock) as size_mock:
            size_mock.return_value = Size(6, 1)
            strip = pane.render_line(0)
        self.assertEqual(strip.cell_length, 6)
        self.assertEqual(strip.text, "abcdef")

    async def test_switching_back_restores_cached_screen_immediately(self) -> None:
        """切走再切回同一个会话，必须立刻摆出上次那一屏。

        不缓存的话每次切回都要先退回静态对话回退、等首帧抓到才跳成实时画面，
        观感就是右栏闪一下；缓存命中时 `_grid` 在 focus_session 返回时就有了。
        """
        import pickup.ui.embed_pane as embed_pane_mod
        from pickup.embed import Cell

        embed_pane_mod._screen_cache.clear()
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.open_channel", return_value=None), \
             mock.patch("pickup.embed.should_resize_host", return_value=False):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                pane = _primary_embed_pane(app.screen)
                pane.focus_session("pickup-claude-cache-a")
                pane._sync_strips([[Cell("A")], [Cell("B")]])
                self.assertIsNotNone(pane._grid)

                pane.focus_session("pickup-claude-cache-b")
                self.assertIsNone(pane._grid, "切到别的会话必须先清掉旧画面")

                pane.focus_session("pickup-claude-cache-a")
                self.assertIsNotNone(pane._grid, "切回来应立刻恢复缓存画面")
                self.assertIsNotNone(pane._strips, "只恢复网格不重建 Strip 会渲染成空白")
                self.assertEqual(pane._strips[0].text.strip(), "A")

    async def test_prefetch_parses_capture_text_before_caching(self) -> None:
        """预抓帧必须 parse 成行网格；把 capture 原文塞进缓存会在恢复时崩掉。"""
        import pickup.ui.embed_pane as embed_pane_mod
        from pickup.embed import Cell

        embed_pane_mod._screen_cache.clear()
        ansi = "\x1b[38;5;29mhello\x1b[0m\nworld"
        with mock.patch("pickup.embed.capture", return_value=ansi), \
             mock.patch(
                 "pickup.embed.parse_screen_rows",
                 return_value=[[Cell("h")], [Cell("w")]],
             ) as parse_mock:
            self.assertTrue(
                embed_pane_mod.prefetch_cached_screen("pickup-prefetch-x", width=40, height=10)
            )
            parse_mock.assert_called_once()
        hit = embed_pane_mod._take_cached_screen("pickup-prefetch-x")
        self.assertIsNotNone(hit)
        grid, _ = hit
        self.assertIsInstance(grid, list)
        self.assertIsInstance(grid[0], list)

    async def test_restore_rejects_raw_capture_string_in_cache(self) -> None:
        """脏缓存（capture 原文）恢复时必须丢弃，不得 AttributeError 崩界面。"""
        import pickup.ui.embed_pane as embed_pane_mod

        embed_pane_mod._screen_cache.clear()
        # 模拟 v0.24.61 错误写入：把 ANSI 原文当 grid
        embed_pane_mod._screen_cache["pickup-dirty"] = ("\x1b[31mraw\x1b[0m", None)
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.open_channel", return_value=None), \
             mock.patch("pickup.embed.should_resize_host", return_value=False):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                pane = _primary_embed_pane(app.screen)
                pane.focus_session("pickup-dirty")
                self.assertIsNone(pane._grid)
                self.assertNotIn("pickup-dirty", embed_pane_mod._screen_cache)

    async def test_dead_session_drops_cached_screen(self) -> None:
        """确认结束的会话必须丢掉缓存画面，别用旧屏幕伪装成还在跑。"""
        import pickup.ui.embed_pane as embed_pane_mod
        from pickup.embed import Cell

        embed_pane_mod._screen_cache.clear()
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.open_channel", return_value=None), \
             mock.patch("pickup.embed.should_resize_host", return_value=False):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                pane = _primary_embed_pane(app.screen)
                pane.focus_session("pickup-claude-gone")
                pane._sync_strips([[Cell("X")]])
                pane._apply_dead(pane._capture_generation, "pickup-claude-gone")
                self.assertNotIn("pickup-claude-gone", embed_pane_mod._screen_cache)

    async def test_tmux_resize_and_capture_are_debounced(self) -> None:
        import pickup.ui.embed_pane as embed_pane_mod

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            pane = _primary_embed_pane(app.screen)
            pane.session_name = "pickup-claude-debounce"
            pane.dead = False
            resize_calls: list[tuple] = []
            poke_calls: list[int] = []

            def _fake_resize(name, w, h):
                resize_calls.append((name, w, h))

            with (
                mock.patch("pickup.embed.resize", side_effect=_fake_resize),
                mock.patch.object(pane._poke, "set", side_effect=lambda: poke_calls.append(1)),
            ):
                pane._on_resize(events.Resize(Size(50, 20), Size(50, 20)))
                await pilot.pause(delay=0.02)
                pane._on_resize(events.Resize(Size(40, 18), Size(40, 18)))
                await pilot.pause(delay=0.02)
                self.assertEqual(resize_calls, [], "拖动过程中不应立刻 resize-window")
                self.assertEqual(poke_calls, [], "拖动过程中不应立刻唤醒抓帧")
                await pilot.pause(delay=embed_pane_mod._RESIZE_TMUX_DEBOUNCE + 0.05)
                self.assertEqual(resize_calls, [("pickup-claude-debounce", 40, 18)])
                self.assertEqual(len(poke_calls), 1)

    async def test_resize_with_live_grid_starts_capture_hold(self) -> None:
        """已有直播画面时，防抖 resize 后必须冻结抓帧显示，避免镜像 Cursor 重排滚动。"""
        import pickup.ui.embed_pane as embed_pane_mod
        from pickup.embed import Cell

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            pane = _primary_embed_pane(app.screen)
            pane.session_name = "pickup-cursor-hold"
            pane.dead = False
            pane._grid = [[Cell("x")]]  # noqa: SLF001
            with (
                mock.patch("pickup.embed.resize"),
                mock.patch("pickup.embed.capture", return_value=None),
            ):
                pane._on_resize(events.Resize(Size(60, 22), Size(60, 22)))
                await pilot.pause(delay=embed_pane_mod._RESIZE_TMUX_DEBOUNCE + 0.05)
            self.assertTrue(pane._resize_hold_active)  # noqa: SLF001
            # 停掉抓帧线程对 hold 状态的并发改写，再单测放行条件
            pane.session_name = None
            pane._stop.set()  # noqa: SLF001
            # 重排中的变化帧不得放行
            self.assertFalse(pane._resize_hold_allows_display("frame-a"))  # noqa: SLF001
            self.assertFalse(pane._resize_hold_allows_display("frame-b"))  # noqa: SLF001
            # 等到最小 hold 之后，连续两帧相同才放行
            pane._resize_hold_until_min = time.monotonic() - 0.01  # noqa: SLF001
            self.assertFalse(pane._resize_hold_allows_display("stable"))  # noqa: SLF001
            self.assertTrue(pane._resize_hold_allows_display("stable"))  # noqa: SLF001
            self.assertFalse(pane._resize_hold_active)  # noqa: SLF001

    def test_resize_hold_deadline_forces_release(self) -> None:
        """超时后即使画面仍在变也必须放行，避免永久冻结。"""
        pane = EmbedPane()
        pane._begin_resize_capture_hold()  # noqa: SLF001
        pane._resize_hold_until_min = time.monotonic() - 1  # noqa: SLF001
        pane._resize_hold_deadline = time.monotonic() - 0.01  # noqa: SLF001
        self.assertTrue(pane._resize_hold_allows_display("still-changing"))  # noqa: SLF001
        self.assertFalse(pane._resize_hold_active)  # noqa: SLF001

    async def test_tmux_resize_skips_when_pane_too_narrow(self) -> None:
        """右栏短时缩到下限以下时不得 resize-window，避免窄折行烧进历史。"""
        import pickup.ui.embed_pane as embed_pane_mod

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            pane = _primary_embed_pane(app.screen)
            pane.session_name = "pickup-claude-narrow"
            pane.dead = False
            resize_calls: list[tuple] = []

            with mock.patch("pickup.embed.resize", side_effect=lambda *a: resize_calls.append(a)):
                pane._on_resize(events.Resize(Size(20, 18), Size(20, 18)))
                await pilot.pause(delay=embed_pane_mod._RESIZE_TMUX_DEBOUNCE + 0.05)
                self.assertEqual(resize_calls, [])


@unittest.skipUnless(HAS_TMUX, "内嵌面板依赖真实 tmux")
class DirectLaunchHostingTests(unittest.IsolatedAsyncioTestCase):
    """直启子命令（pickup claude ...）带进 TUI 的托管路径。"""

    def setUp(self) -> None:
        self._hosted_names: list[str] = []
        self.addCleanup(self._cleanup_hosted)

    def _cleanup_hosted(self) -> None:
        for name in self._hosted_names:
            subprocess.run(["tmux", "-L", "pickup-keepalive", "kill-session", "-t", name],
                            stderr=subprocess.DEVNULL)

    async def test_direct_launch_hosts_and_focuses_pane_without_stealing_focus_back(self) -> None:
        """直启托管成功后焦点应在右栏；且挂载时不能再调度列表 focus 把焦点抢回去。"""
        store, _ = _make_store()
        plan = LaunchPlan(("bash", "-c", "printf 'DIRECT-HELLO\\n'; cat"), None)
        direct = pickup._DirectLaunch(plan, "claude", "directtest01")

        app = PickupApp(store, embed_ok=True, direct=direct)
        async with app.run_test(size=(120, 30)) as pilot:
            area = app.screen.query_one(SplitPaneArea)
            await _wait_until(
                lambda: any(cell.embed_pane() is not None for cell in area.cells()),
            )
            pane = _primary_embed_pane(app.screen)
            # embed.host_session 现在跑在后台 worker 里（见 _host_direct_worker），
            # 不再保证固定延迟内一定完成，轮询等待比死等更稳。
            await _wait_for_session_name(pane)
            self.assertIsNotNone(pane.session_name)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "DIRECT-HELLO")
            await _wait_until(lambda: pane.has_focus)

            await _wait_until(lambda: store.find_session("claude:directtest01") is not None)
            provisional = store.find_session("claude:directtest01")
            self.assertIsNotNone(provisional)
            self.assertTrue(provisional["provisional"])
            self.assertTrue(provisional["live"])
            self.assertEqual(provisional["keepalive_name"], pane.session_name)
            self.assertEqual(provisional["fallback_title"], "新Claude会话")
            self.assertEqual(provisional["cwd"], os.getcwd())
            self.assertIn(
                "claude:directtest01",
                [pickup.session_key(session) for session in store.all_sessions()],
            )
            list_view = app.screen.query_one(SessionListView)
            await _wait_until(
                lambda: list_view.selected_session() is not None
                and pickup.session_key(list_view.selected_session()) == "claude:directtest01"
            )
            self.assertFalse(list_view.is_new_session_selected())
            self.assertTrue(pane.has_focus)

            await pilot.press(*"x")
            await _wait_for_pane_text(pane, "x")
            self.assertIn("x", pane.render().plain.split("DIRECT-HELLO")[-1])

    async def test_direct_launch_focuses_pane_even_when_list_already_has_focus(self) -> None:
        """真机直启：搜索框不可聚焦时默认焦点在侧边栏，托管成功仍须把输入交给新会话。"""
        store, _ = _make_store()
        release = threading.Event()
        real_host = pickup.embed.host_session

        def delayed_host(*args, **kwargs):
            if not release.wait(timeout=5.0):
                raise TimeoutError("测试未能及时释放 delayed_host")
            return real_host(*args, **kwargs)

        plan = LaunchPlan(("bash", "-c", "printf 'DIRECT-LIST\\n'; cat"), None)
        direct = pickup._DirectLaunch(plan, "claude", "directlist01")
        app = PickupApp(store, embed_ok=True, direct=direct)
        with mock.patch("pickup.embed.host_session", side_effect=delayed_host):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.05)
                app.screen._focus_list()
                await pilot.pause()
                list_view = app.screen.query_one(SessionListView)
                self.assertTrue(
                    list_view.has_focus,
                    "托管完成前侧边栏持有焦点，复现真实终端直启的默认落点",
                )
                release.set()
                area = app.screen.query_one(SplitPaneArea)
                await _wait_until(
                    lambda: any(cell.embed_pane() is not None for cell in area.cells()),
                )
                pane = _primary_embed_pane(app.screen)
                await _wait_for_session_name(pane)
                self._hosted_names.append(pane.session_name)
                await _wait_for_pane_text(pane, "DIRECT-LIST")
                await _wait_until(lambda: pane.has_focus)
                await _wait_until(
                    lambda: list_view.selected_session() is not None
                    and pickup.session_key(list_view.selected_session())
                    == "claude:directlist01"
                )
                self.assertFalse(list_view.is_new_session_selected())
                await pilot.press(*"y")
                await _wait_for_pane_text(pane, "y")

    async def test_direct_launch_disables_search_focus_until_hosted(self) -> None:
        """直启挂载期间搜索框不可聚焦，避免迟到 OSC 应答灌进筛选框滤空列表。"""
        from textual.widgets import Input

        store, _ = _make_store()
        # 故意拖慢 host_session，拉长「搜索框本可吞键」的窗口
        release = threading.Event()

        def delayed_host(*_a, **_k):
            if not release.wait(timeout=5.0):
                raise TimeoutError("测试未能及时释放 delayed_host")
            return "pickup-claude-directfocus1"

        plan = LaunchPlan(("true",), None)
        direct = pickup._DirectLaunch(plan, "claude", "directfocus1")
        app = PickupApp(store, embed_ok=True, direct=direct)
        with mock.patch("pickup.embed.host_session", side_effect=delayed_host):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.05)
                search = app.screen.query_one("#project-search", Input)
                self.assertFalse(
                    search.can_focus,
                    "直启托管完成前搜索框必须不可聚焦，否则 OSC 泄漏会滤空侧边栏",
                )
                release.set()
                await _wait_until(lambda: search.can_focus)
                # 模拟泄漏垃圾已写入：恢复焦点后应被清掉
                search.value = "\x1b]11;rgb:aaaa/bbbb/cccc\x07"
                app.screen._restore_direct_search_focus()
                self.assertEqual(search.value, "")
                self.assertEqual(app.screen.nav.project_query, "")

    async def test_search_rejects_osc_leak_garbage(self) -> None:
        """兜底：搜索框若已出现 OSC 泄漏特征，必须清空且不把列表滤空。"""
        from textual.widgets import Input

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            search = app.screen.query_one("#project-search", Input)
            list_view = app.screen.query_one(SessionListView)
            before = len(list_view.visible_sessions())
            self.assertGreater(before, 0)
            search.focus()
            search.value = "]11;rgb:1e1e/1e1e/2e2e"
            await pilot.pause(delay=0.2)
            self.assertEqual(search.value, "")
            self.assertEqual(app.screen.nav.project_query, "")
            self.assertEqual(len(list_view.visible_sessions()), before)


class RestartEndedSessionTests(unittest.IsolatedAsyncioTestCase):
    """已结束会话的重启入口：右栏预览格 / 已结束格 / 组内成员都得能回车重启。

    背景（2026-08-05 用户反馈）：已结束会话点开只有历史预览，重启没有任何可见
    入口——组内成员那条路当时更是彻底走不通（只会把会话组再摆一遍）。
    """

    async def test_enter_on_preview_pane_restarts_session(self) -> None:
        """焦点在右栏静态预览格上按回车 = 重启这条会话。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch(
                "pickup.embed.host_session", return_value="pickup-claude-s0",
            ) as host,
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.3)
                pane = _primary_embed_pane(app.screen)
                self.assertTrue(pane._is_restart_target())  # noqa: SLF001
                app.screen.set_focus(pane)
                await pilot.pause()

                await pilot.press("enter")
                await _wait_until(lambda: host.called)
                await _wait_until(lambda: app.screen._host_pending == 0)  # noqa: SLF001
                await _wait_for_embed_session(app.screen, "pickup-claude-s0")

    async def test_enter_on_ended_pane_restarts_and_clears_stale_hosting(self) -> None:
        """会话就在这一格里跑完退出：画面变「会话已结束」，回车原地重启。

        托管标记要等下一轮重扫才撤，重启前必须自己撤掉，否则会被当成"还托管着"
        直接把那张死画面又摆回来。
        """
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch(
                "pickup.embed.host_session", return_value="pickup-claude-again",
            ) as host,
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.3)
                key = pickup.session_key(store.all_sessions()[0])
                store.mark_hosted(key, "pickup-claude-old")
                pane = _primary_embed_pane(app.screen)
                pane.session_name = "pickup-claude-old"
                pane.dead = True
                app.screen.set_focus(pane)
                await pilot.pause()
                self.assertIn(i18n.t("detail.session_ended"), pane.render().plain)

                await pilot.press("enter")
                await _wait_until(lambda: host.called)
                await _wait_until(lambda: app.screen._host_pending == 0)  # noqa: SLF001
                await _wait_for_embed_session(app.screen, "pickup-claude-again")
                self.assertEqual(
                    store.find_session(key).get("keepalive_name"), "pickup-claude-again",
                )

    async def test_enter_restarts_ended_member_of_session_group(self) -> None:
        """会话组里的已结束成员：回车必须重启它，而不是把会话组再摆一遍。

        会话组在成员结束后仍然保留，所以这条路会被长期撞上；修复前它是死胡同。
        """
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch(
                "pickup.embed.host_session", return_value="pickup-claude-s0",
            ) as host,
            mock.patch("pickup.embed.is_alive", return_value=False),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                list_view = app.screen.query_one(SessionListView)
                keys = [pickup.session_key(s) for s in store.all_sessions()[:2]]
                list_view.on_layout_change(
                    lambda s: s.set_group("/tmp", keys, focus_key=keys[0])
                )
                await list_view.rebuild()
                await pilot.pause(delay=0.2)
                self.assertTrue(list_view.select_session_key(keys[0]))
                await pilot.pause(delay=0.2)

                await pilot.press("enter")
                await _wait_until(lambda: host.called)
                await _wait_until(lambda: app.screen._host_pending == 0)  # noqa: SLF001
                await _wait_for_embed_session(app.screen, "pickup-claude-s0")
                area = app.screen.query_one(SplitPaneArea)
                self.assertEqual(
                    set(area.ordered_session_keys()), set(keys),
                    "重启组成员不得把用户的分屏组合拆成单格",
                )

    async def test_live_pane_forwards_enter_but_ctrl_f_opens_search(self) -> None:
        """回车照常发给助手，但 Ctrl+F 必须由 pickup 打开全文搜索。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.embed.host_session", return_value="pickup-claude-s0"),
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("enter")
                await _wait_until(lambda: app.screen._host_pending == 0)  # noqa: SLF001
                pane = await _wait_for_embed_session(app.screen, "pickup-claude-s0")
                await _wait_until(lambda: pane.has_focus)
                pane._grid = [[]]  # noqa: SLF001 — 首帧已到达，不再是回退态
                self.assertFalse(pane._is_restart_target())  # noqa: SLF001
                with mock.patch("pickup.embed.send_key") as send_key:
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertTrue(send_key.called)

                with mock.patch("pickup.embed.send_key") as send_key:
                    await pilot.press("ctrl+f")
                    await _wait_until(lambda: isinstance(app.screen, FullTextSearchModal))
                    self.assertFalse(send_key.called)
                    await pilot.press("escape")

    async def test_clicking_ended_session_only_previews_enter_restarts(self) -> None:
        """进程早就没了的会话：单击只摆历史，回车才恢复（机主 2026-08-05 拍板）。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch(
                "pickup.embed.host_session", return_value="pickup-claude-s1",
            ) as host,
            mock.patch("pickup.embed.is_alive", return_value=False),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.3)
                list_view = app.screen.query_one(SessionListView)
                cards = list(app.screen.query(SessionCard))

                await pilot.click(cards[1])
                await pilot.pause(delay=0.3)
                self.assertFalse(host.called, "单击已结束会话不许启动助手进程")
                self.assertTrue(list_view.has_focus, "焦点应留在侧边栏")
                self.assertEqual(
                    pickup.session_key(list_view.selected_session()),
                    pickup.session_key(cards[1].session),
                )

                await pilot.press("enter")
                await _wait_until(lambda: host.called)
                await _wait_until(lambda: app.screen._host_pending == 0)  # noqa: SLF001
                await _wait_for_embed_session(app.screen, "pickup-claude-s1")

    async def test_clicking_live_session_still_opens_it(self) -> None:
        """还活着的会话不受影响：单击仍等于回车，直接接管那一格。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        for session in store.all_sessions():
            session["keepalive_name"] = f"pickup-{session['id']}"
        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.is_alive", return_value=True):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.3)
                cards = list(app.screen.query(SessionCard))
                await pilot.click(cards[1])
                await pilot.pause(delay=0.3)
                pane = await _wait_for_embed_session(app.screen, "pickup-s1")
                await _wait_until(lambda: pane.has_focus)

    async def test_preview_header_shows_restart_hint(self) -> None:
        """已结束会话的详情头要写明回车可重启，否则用户找不到入口。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.3)
            pane = _primary_embed_pane(app.screen)
            hint = i18n.t("detail.restart_hint")
            await _wait_until(lambda: hint in pane.render().plain)

    async def test_preview_pane_chrome_shows_enter_restart_hint(self) -> None:
        """预览默认钉底，详情头提示会滚出视野；顶/底 chrome 必须常驻 Enter 重启。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch(
                "pickup.embed.host_session", return_value="pickup-claude-s0",
            ),
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.3)
                cell = app.screen.query_one(SplitPaneArea)._cells()[0]  # noqa: SLF001
                header = cell.query_one(".header")
                footer = cell.query_one(".footer")
                short = i18n.t("pane.restart_hint")
                focused = i18n.t("pane.restart_focus_hint")

                await _wait_until(lambda: short in footer.render().plain)
                self.assertTrue(header.query_one(".restart-hint").display)
                self.assertEqual(
                    header.query_one(".restart-hint").render().plain.strip(),
                    short,
                )

                # 焦点进预览格：底栏换成「重启 + 回列表」
                pane = _primary_embed_pane(app.screen)
                pane.focus()
                await pilot.pause()
                await _wait_until(lambda: focused in footer.render().plain)

                # 托管起来后顶底提示都要消失（Enter 此时转发给助手）
                list_view = app.screen.query_one(SessionListView)
                list_view.focus()
                await pilot.press("enter")
                await _wait_until(lambda: app.screen._host_pending == 0)  # noqa: SLF001
                await _wait_for_embed_session(app.screen, "pickup-claude-s0")
                await _wait_until(
                    lambda: short not in footer.render().plain
                    and focused not in footer.render().plain
                )
                self.assertFalse(header.query_one(".restart-hint").display)


class RightPanePreviewTests(unittest.IsolatedAsyncioTestCase):
    """选中即完整预览：右栏展示对话全文。"""

    async def test_right_pane_shows_full_conversation_not_last_qa_blurb(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.pause(delay=0.3)
            pane = _primary_embed_pane(app.screen)

            await _wait_until(lambda: "测试问题" in pane.render().plain and "测试回复" in pane.render().plain)
            detail = pane.render().plain
            self.assertIn("● You", detail)
            self.assertIn("测试问题", detail)
            self.assertIn("测试回复", detail)
            self.assertNotIn("最近提问", detail)
            self.assertNotIn("最近回复", detail)

    async def test_space_no_longer_opens_fullscreen_preview(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("space")
            await pilot.pause()
            self.assertIs(app.screen, app.screen)
            self.assertEqual(type(app.screen).__name__, "MainScreen")

    async def test_a_key_opens_handoff_modal_from_main_list(self) -> None:
        codex = mock.Mock()
        codex.id = "codex"
        codex.display_name = "Codex"
        codex.is_available.return_value = True
        codex.scan_sessions.return_value = []
        store, _ = _make_store(extra_runtimes=(codex,))
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("a")
            await pilot.pause()
            self.assertIsInstance(app.screen, RuntimePickerModal)
            await pilot.press("down")  # claude(原生恢复) -> codex
            await pilot.press("enter")
            await pilot.pause()
        self.assertIsInstance(app.return_value, pickup.LaunchRequest)
        self.assertEqual(app.return_value.target_runtime_id, "codex")

    async def test_right_pane_detail_scrolls_with_page_and_end(self) -> None:
        """长对话预览：默认钉底；PgUp / Home 可回到更早内容；End 再回最新。"""
        long_msgs = []
        for i in range(40):
            long_msgs.append(pickup.ConversationMessage("user", f"问题行-{i}-" + ("x" * 20)))
            long_msgs.append(pickup.ConversationMessage("assistant", f"回复行-{i}-" + ("y" * 20)))
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0",
            "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
            "native_title": None, "fallback_title": "长对话",
            "cwd": "/tmp", "live": False,
        }]
        store, registry = _make_store(sessions=sessions)
        registry.get("claude").load_conversation.return_value = long_msgs
        # 清掉 store.load() 时预热的短对话缓存，强制按新返回值重读
        store.conversations.clear()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            pane = _primary_embed_pane(app.screen)
            await _wait_until(
                lambda: (
                    pane._is_detail_view()
                    and "回复行-39" in pane.render().plain
                    and pane.detail_offset == pane._detail_max_offset() > 0
                ),
                tries=300,
                interval=0.02,
            )
            # 直接调滚动 API 验证窗口机制（再测键盘绑定）
            self.assertTrue(pane.scroll_detail_page(-1))
            self.assertLess(pane.detail_offset, pane._detail_max_offset())
            after_page_up = pane.detail_offset
            await pilot.press("home")
            await pilot.pause()
            self.assertEqual(pane.detail_offset, 0)
            self.assertIn("问题行-0", pane.render().plain)
            # 用户已离开底部后，invalidate 不得强行钉回底部
            pane.invalidate_detail()
            await pilot.pause()
            self.assertEqual(pane.detail_offset, 0)
            await pilot.press("end")
            await pilot.pause()
            self.assertEqual(pane.detail_offset, pane._detail_max_offset())
            self.assertGreater(pane.detail_offset, after_page_up)
            self.assertIn("回复行-39", pane.render().plain)

    async def test_detail_async_load_pins_to_bottom(self) -> None:
        """对话异步填入后仍应钉在最新；用户上滚后刷新保持当前位置。"""
        long_msgs = [
            pickup.ConversationMessage("user", f"早-{i}-" + ("u" * 40))
            for i in range(30)
        ] + [
            pickup.ConversationMessage("assistant", "最新答复-" + ("z" * 40)),
        ]
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0",
            "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
            "native_title": None, "fallback_title": "异步预览",
            "cwd": "/tmp", "live": False,
        }]
        store, registry = _make_store(sessions=sessions)
        # 首次 peek 为空：模拟暖加载前；随后 get_conversation 写入缓存
        registry.get("claude").load_conversation.return_value = long_msgs
        store.conversations.clear()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            pane = _primary_embed_pane(app.screen)
            await _wait_until(
                lambda: pane._is_detail_view() and "最新答复" in pane.render().plain,
                tries=300,
                interval=0.02,
            )
            self.assertEqual(pane.detail_offset, pane._detail_max_offset())
            self.assertTrue(pane.scroll_detail_home())
            self.assertEqual(pane.detail_offset, 0)
            # 模拟后台刷新（暖加载完成再次 invalidate）
            app.screen._refresh_preview_detail()
            await pilot.pause()
            self.assertEqual(pane.detail_offset, 0)


class ModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_session_modal_escape_cancels(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            result_holder = {}

            async def _open():
                result_holder["result"] = await app.push_screen_wait(
                    NewSessionModal(
                        [("/tmp/alpha", "alpha", "/tmp/alpha")],
                        [RuntimeChoice("claude", "Claude", "", True)],
                    )
                )

            app.run_worker(_open())
            await pilot.pause(delay=0.2)
            await pilot.press("escape")
            await pilot.pause(delay=0.2)
        self.assertIsNone(result_holder.get("result"))

    async def test_new_session_modal_picks_project_then_runtime(self) -> None:
        """一个弹窗内选完：左栏回车换到右栏，右栏回车才确认。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(delay=0.2)
            result_holder = {}

            async def _open():
                result_holder["result"] = await app.push_screen_wait(
                    NewSessionModal(
                        [("/tmp/alpha", "alpha", "/tmp/alpha"), ("/tmp/beta", "beta", "/tmp/beta")],
                        [
                            RuntimeChoice("claude", "Claude", "", True),
                            RuntimeChoice("codex", "Codex", "", True),
                        ],
                    )
                )

            app.run_worker(_open())
            await pilot.pause(delay=0.2)
            modal = app.screen
            self.assertIsInstance(modal, NewSessionModal)
            await pilot.press("down")  # alpha -> beta
            await pilot.press("enter")  # 项目定了，焦点交给运行时栏
            await pilot.pause()
            self.assertIsInstance(app.screen, NewSessionModal)  # 左栏回车不得关闭弹窗
            self.assertEqual(modal.query_one("#ns-runtimes").has_focus, True)
            await pilot.press("down")  # Claude -> Codex
            await pilot.press("enter")
            await pilot.pause(delay=0.2)
        self.assertEqual(result_holder.get("result"), ("/tmp/beta", "codex"))

    async def test_new_session_modal_arrow_keys_switch_columns(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(delay=0.2)

            async def _open():
                await app.push_screen_wait(
                    NewSessionModal(
                        [("/tmp/alpha", "alpha", "/tmp/alpha")],
                        [RuntimeChoice("claude", "Claude", "", True)],
                    )
                )

            app.run_worker(_open())
            await pilot.pause(delay=0.2)
            modal = app.screen
            self.assertTrue(modal.query_one("#ns-projects").has_focus)
            await pilot.press("right")
            await pilot.pause()
            self.assertTrue(modal.query_one("#ns-runtimes").has_focus)
            await pilot.press("left")
            await pilot.pause()
            self.assertTrue(modal.query_one("#ns-projects").has_focus)

    async def test_new_session_modal_bells_on_unavailable_runtime(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(delay=0.2)

            async def _open():
                await app.push_screen_wait(
                    NewSessionModal(
                        [("/tmp/alpha", "alpha", "/tmp/alpha")],
                        [RuntimeChoice("kimi", "Kimi", "", False)],
                    )
                )

            app.run_worker(_open())
            await pilot.pause(delay=0.2)
            await pilot.press("right")
            with mock.patch.object(app, "bell") as bell:
                await pilot.press("enter")
                await pilot.pause()
            bell.assert_called_once()
            self.assertIsInstance(app.screen, NewSessionModal)  # 未安装项不应关闭弹窗

    async def test_new_session_modal_filters_projects(self) -> None:
        """左栏筛选框按名/路径收窄项目列表，清空后还原。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(delay=0.2)
            result_holder = {}

            async def _open():
                result_holder["result"] = await app.push_screen_wait(
                    NewSessionModal(
                        [
                            ("/tmp/alpha", "alpha", "/tmp/alpha"),
                            ("/tmp/beta", "beta", "/tmp/beta"),
                            ("/Codes/pickup", "pickup", "/Codes/pickup"),
                        ],
                        [RuntimeChoice("claude", "Claude", "", True)],
                    )
                )

            app.run_worker(_open())
            await pilot.pause(delay=0.2)
            modal = app.screen
            self.assertIsInstance(modal, NewSessionModal)
            self.assertEqual(len(modal.query_one("#ns-projects").children), 3)
            await pilot.press("slash")
            await pilot.pause()
            self.assertTrue(modal.query_one("#ns-project-filter").has_focus)
            await pilot.press("p", "i", "c", "k")
            await pilot.pause()
            projects = modal.query_one("#ns-projects")
            self.assertEqual(len(projects.children), 1)
            self.assertEqual(modal._row("#ns-projects").value, "/Codes/pickup")
            await pilot.press("down")  # 筛选框 -> 项目列表
            await pilot.pause()
            self.assertTrue(projects.has_focus)
            filt = modal.query_one("#ns-project-filter")
            filt.focus()
            await pilot.pause()
            filt.value = ""
            await pilot.pause()
            self.assertEqual(len(modal.query_one("#ns-projects").children), 3)
            await pilot.press("escape")
            await pilot.pause(delay=0.2)
        self.assertIsNone(result_holder.get("result"))

    async def test_new_session_modal_initial_query_seeds_filter(self) -> None:
        """侧边栏 project_query 作初值时，打开即已收窄，且不写回 nav。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(delay=0.2)
            nav = app.screen.nav
            nav.project_query = "sidebar-seed"

            async def _open():
                await app.push_screen_wait(
                    NewSessionModal(
                        [
                            ("/tmp/alpha", "alpha", "/tmp/alpha"),
                            ("/tmp/beta", "beta", "/tmp/beta"),
                        ],
                        [RuntimeChoice("claude", "Claude", "", True)],
                        initial_query="beta",
                    )
                )

            app.run_worker(_open())
            await pilot.pause(delay=0.3)
            modal = app.screen
            self.assertIsInstance(modal, NewSessionModal)
            self.assertEqual(modal.query_one("#ns-project-filter").value, "beta")
            self.assertEqual(len(modal.query_one("#ns-projects").children), 1)
            self.assertEqual(modal._row("#ns-projects").value, "/tmp/beta")
            # 焦点偶发仍在 Input 上时再钉一次，避免套件并行压力下的竞态。
            if not modal.query_one("#ns-projects").has_focus:
                modal.set_focus(modal.query_one("#ns-projects"))
                await pilot.pause()
            self.assertTrue(modal.query_one("#ns-projects").has_focus)
            modal.query_one("#ns-project-filter").value = "alpha"
            await pilot.pause()
            self.assertEqual(len(modal.query_one("#ns-projects").children), 1)
            self.assertEqual(nav.project_query, "sidebar-seed")
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause(delay=0.2)

    async def test_new_session_modal_empty_filter_bells_on_confirm(self) -> None:
        """无命中时确认响铃、弹窗不关。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(delay=0.2)

            async def _open():
                await app.push_screen_wait(
                    NewSessionModal(
                        [("/tmp/alpha", "alpha", "/tmp/alpha")],
                        [RuntimeChoice("claude", "Claude", "", True)],
                        initial_query="zzz-no-match",
                    )
                )

            app.run_worker(_open())
            await pilot.pause(delay=0.2)
            modal = app.screen
            self.assertEqual(len(modal.query_one("#ns-projects").children), 0)
            await pilot.press("right")
            with mock.patch.object(app, "bell") as bell:
                await pilot.press("enter")
                await pilot.pause()
            bell.assert_called_once()
            self.assertIsInstance(app.screen, NewSessionModal)

    async def test_new_session_modal_escape_clears_filter_then_dismisses(self) -> None:
        """筛选框持焦且有内容时 Esc 先清空；再 Esc 才关窗。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(delay=0.2)
            result_holder = {}

            async def _open():
                result_holder["result"] = await app.push_screen_wait(
                    NewSessionModal(
                        [
                            ("/tmp/alpha", "alpha", "/tmp/alpha"),
                            ("/tmp/beta", "beta", "/tmp/beta"),
                        ],
                        [RuntimeChoice("claude", "Claude", "", True)],
                    )
                )

            app.run_worker(_open())
            await pilot.pause(delay=0.2)
            modal = app.screen
            await pilot.press("slash")
            await pilot.press("b", "e")
            await pilot.pause()
            self.assertEqual(len(modal.query_one("#ns-projects").children), 1)
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.screen, NewSessionModal)
            self.assertEqual(modal.query_one("#ns-project-filter").value, "")
            self.assertEqual(len(modal.query_one("#ns-projects").children), 2)
            await pilot.press("escape")
            await pilot.pause(delay=0.2)
        self.assertIsNone(result_holder.get("result"))

    async def test_sidebar_new_session_opens_single_modal(self) -> None:
        """回归：侧边栏「＋ 新建会话」只弹一个窗，项目与运行时在同一屏选完。"""
        codex = mock.Mock()
        codex.id = "codex"
        codex.display_name = "Codex"
        codex.is_available.return_value = True
        codex.scan_sessions.return_value = []
        store, _ = _make_store(extra_runtimes=(codex,))
        app = PickupApp(store, embed_ok=False)
        projects = [{"cwd_key": "/tmp", "label": "tmp", "count": 3, "latest_mtime": 0.0}]
        with mock.patch.object(store, "projects", return_value=projects):
            async with app.run_test(size=(110, 30)) as pilot:
                await pilot.pause(delay=0.2)
                app.screen.query_one(SessionListView).index = 0  # ＋ 新建会话
                await pilot.press("enter")
                await pilot.pause(delay=0.2)
                self.assertIsInstance(app.screen, NewSessionModal)
                await pilot.press("enter")  # 项目栏 -> 运行时栏
                await pilot.press("down")  # Claude -> Codex
                await pilot.press("enter")
                await pilot.pause(delay=0.3)
        self.assertIsInstance(app.return_value, pickup.NewSessionRequest)
        self.assertEqual(app.return_value.target_runtime_id, "codex")
        self.assertEqual(app.return_value.cwd, "/tmp")

    async def test_runtime_picker_modal_bells_on_unavailable_choice(self) -> None:
        kimi = mock.Mock()
        kimi.id = "kimi"
        kimi.display_name = "Kimi"
        kimi.is_available.return_value = False
        kimi.scan_sessions.return_value = []
        store, _ = _make_store(extra_runtimes=(kimi,))
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("a")
            await pilot.pause()
            self.assertIsInstance(app.screen, RuntimePickerModal)
            await pilot.press("down")  # 移到未安装的 kimi
            with mock.patch.object(app, "bell") as bell:
                await pilot.press("enter")
                await pilot.pause()
            bell.assert_called_once()
            self.assertIsInstance(app.screen, RuntimePickerModal)  # 未安装项不应关闭弹窗

    async def test_confirm_modal_other_key_cancels(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            result_holder = {}

            async def _open():
                result_holder["result"] = await app.push_screen_wait(ConfirmModal("确认？"))

            app.run_worker(_open())
            await pilot.pause(delay=0.2)
            await pilot.press("n")
            await pilot.pause(delay=0.2)
        self.assertFalse(result_holder.get("result"))

    async def test_confirm_modal_q_confirms(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            result_holder = {}

            async def _open():
                result_holder["result"] = await app.push_screen_wait(ConfirmModal("确认？"))

            app.run_worker(_open())
            await pilot.pause(delay=0.3)  # 等 ConfirmModal call_after_refresh 武装
            await pilot.press("q")
            await pilot.pause(delay=0.2)
        self.assertTrue(result_holder.get("result"))

    async def test_confirm_modal_custom_key_confirms_and_q_no_longer_does(self) -> None:
        """删除会话复用 ConfirmModal 但确认键换成 x；默认键 q 此时不应再生效。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            result_holder = {}

            async def _open():
                result_holder["result"] = await app.push_screen_wait(
                    ConfirmModal("删除？", confirm_key="x")
                )

            app.run_worker(_open())
            await pilot.pause(delay=0.3)
            await pilot.press("q")  # 不再是确认键，应按取消处理
            await pilot.pause(delay=0.2)
        self.assertFalse(result_holder.get("result"))

    async def test_confirm_modal_custom_key_x_confirms(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            result_holder = {}

            async def _open():
                result_holder["result"] = await app.push_screen_wait(
                    ConfirmModal("删除？", confirm_key="x")
                )

            app.run_worker(_open())
            await pilot.pause(delay=0.3)
            await pilot.press("x")
            await pilot.pause(delay=0.2)
        self.assertTrue(result_holder.get("result"))


class ModalOutsideClickTests(unittest.IsolatedAsyncioTestCase):
    """点弹窗主体以外的空白＝取消（与 Esc 等价）；点在弹窗内容上不得误关。

    内容那一半是真正的回归点：Click 会从子控件一路冒泡到弹窗，判定若不现查落点
    控件，弹窗会变成「点哪都关」。
    """

    async def _open(self, app, pilot, modal):
        """把弹窗推上来并等它挂好，返回收结果的字典。"""
        holder: dict = {}

        async def _run():
            holder["result"] = await app.push_screen_wait(modal)

        app.run_worker(_run())
        await pilot.pause(delay=0.3)  # 顺带跨过 ConfirmModal 的武装窗口
        return holder

    def _new_session_modal(self) -> NewSessionModal:
        return NewSessionModal(
            [("/tmp/alpha", "alpha", "/tmp/alpha")],
            [RuntimeChoice("claude", "Claude", "", True)],
        )

    async def test_runtime_picker_backdrop_click_cancels(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            modal = RuntimePickerModal("接力到", [RuntimeChoice("claude", "Claude", "", True)])
            holder = await self._open(app, pilot, modal)
            self.assertIsInstance(app.screen, RuntimePickerModal)
            await pilot.click(offset=(0, 0))
            await pilot.pause(delay=0.2)
            self.assertNotIsInstance(app.screen, RuntimePickerModal)
        self.assertIsNone(holder.get("result"))

    async def test_runtime_picker_click_inside_keeps_modal_open(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            modal = RuntimePickerModal("接力到", [RuntimeChoice("claude", "Claude", "", True)])
            await self._open(app, pilot, modal)
            await pilot.click(modal.query_one(Label))  # 标题行：内容区，不是背景
            await pilot.pause(delay=0.2)
            self.assertIsInstance(app.screen, RuntimePickerModal)

    async def test_new_session_backdrop_click_cancels(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(delay=0.2)
            holder = await self._open(app, pilot, self._new_session_modal())
            self.assertIsInstance(app.screen, NewSessionModal)
            await pilot.click(offset=(0, 0))
            await pilot.pause(delay=0.2)
            self.assertNotIsInstance(app.screen, NewSessionModal)
        self.assertIsNone(holder.get("result"))

    async def test_new_session_click_on_column_keeps_modal_open(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(delay=0.2)
            modal = self._new_session_modal()
            await self._open(app, pilot, modal)
            # 点项目栏的边框内侧：命中的是 ListView 不是背景，弹窗必须留着
            await pilot.click(modal.query_one("#ns-projects"))
            await pilot.pause(delay=0.2)
            self.assertIsInstance(app.screen, NewSessionModal)

    async def test_confirm_backdrop_click_cancels(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            holder = await self._open(app, pilot, ConfirmModal("确认？"))
            self.assertIsInstance(app.screen, ConfirmModal)
            await pilot.click(offset=(0, 0))
            await pilot.pause(delay=0.2)
            self.assertNotIsInstance(app.screen, ConfirmModal)
        self.assertIs(holder.get("result"), False)  # 取消，不是确认

    async def test_confirm_click_inside_keeps_modal_open(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            modal = ConfirmModal("确认？")
            await self._open(app, pilot, modal)
            await pilot.click(modal.query_one(Label))
            await pilot.pause(delay=0.2)
            self.assertIsInstance(app.screen, ConfirmModal)


class KillKeepaliveFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_q_key_confirm_kills_and_clears_keepalive_name(self) -> None:
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0", "mtime": time.time(),
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "会话0",
            "cwd": "/tmp", "live": True, "pid": 4242, "keepalive_name": "pickup-claude-fake",
        }]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=False)
        with mock.patch("pickup.keepalive.kill") as kill_mock:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("down")
                list_view = app.screen.query_one(SessionListView)
                card = list_view._session_cards()[0]
                # 托管运行中也不再整行染绿，关注状态只由圆点表达。
                self.assertFalse(
                    any("#3F9A6A" in str(span.style) for span in card.render().spans),
                )
                await pilot.press("q")
                await pilot.pause(delay=0.3)  # worker 推弹窗 + ConfirmModal 武装
                self.assertIsInstance(app.screen, ConfirmModal)
                await pilot.press("q")
                await pilot.pause(delay=0.2)
                # 确认后立刻应是已结束；标题生命周期前后都保持统一基础色。
                card = list_view._session_cards()[0]
                self.assertFalse(
                    any("#3F9A6A" in str(span.style) for span in card.render().spans),
                )
        kill_mock.assert_called_once_with("pickup-claude-fake")
        current = store.find_session("claude:s0")
        self.assertIsNotNone(current)
        self.assertNotIn("keepalive_name", current)
        self.assertFalse(current.get("live"))
        self.assertIsNone(current.get("pid"))


class DeleteSessionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_x_key_confirm_deletes_ended_session_and_removes_card(self) -> None:
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0", "mtime": time.time(),
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "会话0",
            "cwd": "/tmp", "live": False, "path": "/tmp/s0.jsonl",
        }]
        store, registry = _make_store(sessions=sessions)
        claude_runtime = registry.get("claude")
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("x")
            await pilot.pause(delay=0.3)  # worker 推弹窗 + ConfirmModal 武装
            self.assertIsInstance(app.screen, ConfirmModal)
            await pilot.press("x")
            await _wait_until(lambda: not isinstance(app.screen, ConfirmModal))
            list_view = app.screen.query_one(SessionListView)
            await _wait_until(lambda: not list_view._session_cards())
            self.assertEqual(list_view._session_cards(), [])
        claude_runtime.delete_session.assert_called_once_with(sessions[0])
        self.assertIsNone(store.find_session("claude:s0"))

    async def test_x_key_other_key_cancels_and_keeps_session(self) -> None:
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0", "mtime": time.time(),
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "会话0",
            "cwd": "/tmp", "live": False, "path": "/tmp/s0.jsonl",
        }]
        store, registry = _make_store(sessions=sessions)
        claude_runtime = registry.get("claude")
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("x")
            await pilot.pause(delay=0.3)
            await pilot.press("n")  # 非确认键，取消
            await pilot.pause(delay=0.2)
        claude_runtime.delete_session.assert_not_called()
        self.assertIsNotNone(store.find_session("claude:s0"))

    async def test_x_key_on_running_session_kills_keepalive_then_deletes(self) -> None:
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0", "mtime": time.time(),
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "会话0",
            "cwd": "/tmp", "live": True, "pid": 4242, "keepalive_name": "pickup-claude-fake",
            "path": "/tmp/s0.jsonl",
        }]
        store, registry = _make_store(sessions=sessions)
        claude_runtime = registry.get("claude")
        app = PickupApp(store, embed_ok=False)
        with mock.patch("pickup.keepalive.kill") as kill_mock:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("down")
                await pilot.press("x")
                await pilot.pause(delay=0.3)
                self.assertIsInstance(app.screen, ConfirmModal)
                await pilot.press("x")
                await _wait_until(
                    lambda: not isinstance(app.screen, ConfirmModal)
                    and not app.screen.query_one(SessionListView)._session_cards()
                )
                list_view = app.screen.query_one(SessionListView)
                self.assertEqual(list_view._session_cards(), [])
                # 结束进程与磁盘抹除都挪到了后台线程，必须在 App 存活期间等它跑完。
                await _wait_until(lambda: claude_runtime.delete_session.called)
        kill_mock.assert_called_once_with("pickup-claude-fake")
        claude_runtime.delete_session.assert_called_once()
        self.assertIsNone(store.find_session("claude:s0"))

    async def test_card_hides_before_slow_disk_delete_finishes(self) -> None:
        """确认删除即摘卡，不等磁盘抹除——OpenCode 写共享库等锁时最容易暴露。"""
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0", "mtime": time.time(),
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "会话0",
            "cwd": "/tmp", "live": False, "path": "/tmp/s0.jsonl",
        }]
        store, registry = _make_store(sessions=sessions)
        claude_runtime = registry.get("claude")
        released = threading.Event()
        claude_runtime.delete_session.side_effect = lambda *_: released.wait(5)
        app = PickupApp(store, embed_ok=False)
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("down")
                await pilot.press("x")
                await pilot.pause(delay=0.3)
                await pilot.press("x")
                list_view = app.screen.query_one(SessionListView)
                await _wait_until(lambda: not list_view._session_cards())
                # 磁盘删除仍卡着，卡片必须已经不在了。
                self.assertFalse(released.is_set())
                self.assertEqual(list_view._session_cards(), [])
                released.set()
                await _wait_until(lambda: store.find_session("claude:s0") is None)
        finally:
            released.set()

    async def test_delete_failure_keeps_card_and_notifies(self) -> None:
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0", "mtime": time.time(),
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "会话0",
            "cwd": "/tmp", "live": False, "path": "/tmp/s0.jsonl",
        }]
        store, registry = _make_store(sessions=sessions)
        claude_runtime = registry.get("claude")
        claude_runtime.delete_session.side_effect = OSError("模拟磁盘删除失败")
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("x")
            await pilot.pause(delay=0.3)
            await pilot.press("x")
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            self.assertEqual(len(list_view._session_cards()), 1)
        self.assertIsNotNone(store.find_session("claude:s0"))


class DeleteSessionGroupFlowTests(unittest.IsolatedAsyncioTestCase):
    """光标停在会话组标题上按 x：删的是整组，不是某一条成员。"""

    @staticmethod
    async def _grouped(app, pilot, count=2):
        """把前 count 条会话编成一组，并把光标停在组卡上。"""
        await pilot.pause(delay=0.2)
        list_view = app.screen.query_one(SessionListView)
        keys = [
            pickup.session_key(session)
            for session in app.screen.store.all_sessions()[:count]
        ]
        list_view.on_layout_change(lambda s: s.set_group("/tmp", keys, focus_key=keys[0]))
        await list_view.rebuild()
        list_view.focus()
        group_item = next(
            item
            for item in list_view.list_children
            if item.children and isinstance(item.children[0], SessionGroupCard)
        )
        list_view.index = list(list_view.list_children).index(group_item)
        return list_view, keys

    async def test_x_on_group_card_deletes_every_member(self) -> None:
        store, registry = _make_store()
        claude_runtime = registry.get("claude")
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            list_view, keys = await self._grouped(app, pilot)
            self.assertIsNotNone(list_view.selected_group())
            await pilot.press("x")
            await pilot.pause(delay=0.3)  # worker 推弹窗 + ConfirmModal 武装
            self.assertIsInstance(app.screen, ConfirmModal)
            await pilot.press("x")
            await _wait_until(
                lambda: all(store.find_session(key) is None for key in keys)
            )
            await _wait_until(
                lambda: not list(app.screen.query(SessionGroupCard))
            )
        self.assertEqual(claude_runtime.delete_session.call_count, len(keys))
        for key in keys:
            self.assertIsNone(store.find_session(key))
        # 组外的第三条会话不受牵连
        self.assertIsNotNone(store.find_session("claude:s2"))

    async def test_x_on_running_group_kills_every_keepalive_then_deletes(self) -> None:
        sessions = [
            {
                "source": "claude", "id": f"s{i}", "short_id": f"s{i}",
                "mtime": time.time() - i, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"会话{i}", "cwd": "/tmp",
                "live": True, "pid": 4242 + i, "keepalive_name": f"pickup-claude-fake{i}",
                "path": f"/tmp/s{i}.jsonl",
            }
            for i in range(2)
        ]
        store, registry = _make_store(sessions=sessions)
        claude_runtime = registry.get("claude")
        app = PickupApp(store, embed_ok=False)
        with mock.patch("pickup.keepalive.kill") as kill_mock:
            async with app.run_test(size=(100, 30)) as pilot:
                _, keys = await self._grouped(app, pilot)
                await pilot.press("x")
                await pilot.pause(delay=0.3)
                self.assertIsInstance(app.screen, ConfirmModal)
                await pilot.press("x")
                await _wait_until(
                    lambda: claude_runtime.delete_session.call_count == len(keys)
                )
        self.assertEqual(
            sorted(call.args[0] for call in kill_mock.call_args_list),
            ["pickup-claude-fake0", "pickup-claude-fake1"],
        )
        for key in keys:
            self.assertIsNone(store.find_session(key))

    async def test_x_on_group_card_cancel_keeps_every_member(self) -> None:
        store, registry = _make_store()
        claude_runtime = registry.get("claude")
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            _, keys = await self._grouped(app, pilot)
            await pilot.press("x")
            await pilot.pause(delay=0.3)
            await pilot.press("n")  # 非确认键，取消
            await pilot.pause(delay=0.2)
        claude_runtime.delete_session.assert_not_called()
        for key in keys:
            self.assertIsNotNone(store.find_session(key))

    async def test_group_delete_failure_only_restores_failed_member(self) -> None:
        store, registry = _make_store()
        claude_runtime = registry.get("claude")

        def fail_second(session):
            if session.get("id") == "s1":
                raise OSError("模拟磁盘删除失败")

        claude_runtime.delete_session.side_effect = fail_second
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._grouped(app, pilot)
            await pilot.press("x")
            await pilot.pause(delay=0.3)
            await pilot.press("x")
            await _wait_until(lambda: store.find_session("claude:s0") is None)
            await _wait_until(lambda: store.find_session("claude:s1") is not None)
        self.assertIsNone(store.find_session("claude:s0"))
        self.assertIsNotNone(store.find_session("claude:s1"))


class ExternalRunningSessionTests(unittest.IsolatedAsyncioTestCase):
    """在别的终端窗口里跑、没被 pickup 托管的会话。

    这类会话拿不到实时画面（画面只存在于那个窗口自己的终端连接里）。以前点进去
    会静默用原生恢复另起一个进程，右栏冒出一个刚从历史恢复的新界面，用户看到的
    就是"会话已中断"，而且两个进程写同一份历史有互相覆盖的风险。
    """

    @staticmethod
    def _external_sessions():
        return [{
            "source": "claude", "id": "s0", "short_id": "s0", "mtime": time.time(),
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "会话0",
            "cwd": "/tmp", "live": True, "pid": 4242,
        }]

    def test_is_external_running_only_for_live_untracked(self) -> None:
        from pickup.ui.main_screen import _status_key, is_external_running

        external = {"live": True}
        hosted = {"live": True, "keepalive_name": "pickup-claude-x"}
        ended = {"live": False}
        self.assertTrue(is_external_running(external))
        self.assertFalse(is_external_running(hosted))
        self.assertFalse(is_external_running(ended))
        self.assertEqual(_status_key(external), "status.running_external")
        self.assertEqual(_status_key(hosted), "status.running_hosted")
        self.assertEqual(_status_key(ended), "status.ended")

    async def test_detail_header_explains_why_no_live_screen(self) -> None:
        store, _registry = _make_store(sessions=self._external_sessions())
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            header = app.screen._detail_header(store.find_session("claude:s0")).plain
            self.assertIn(i18n.t("status.running_external"), header)
            self.assertIn(i18n.t("detail.running_external"), header)

    async def test_opening_external_session_never_starts_a_second_process(self) -> None:
        """确认前不得构造启动计划，取消后也不得启动任何进程。"""
        store, registry = _make_store(sessions=self._external_sessions())
        registry.build_launch_plan = mock.Mock(
            side_effect=AssertionError("确认前不该构造启动计划")
        )
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            self.assertNotIsInstance(app.screen, ConfirmModal)
        self.assertIsNone(app.return_value)
        registry.build_launch_plan.assert_not_called()

    async def test_external_running_session_stays_in_pickup(self) -> None:
        store, _registry = _make_store(sessions=self._external_sessions())
        app = PickupApp(store, embed_ok=False)  # embed 不可用 → 确认后退出交外层接管
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            self.assertNotIsInstance(app.screen, ConfirmModal)
        self.assertIsNone(app.return_value)

    async def test_pane_restart_never_starts_external_session(self) -> None:
        """右栏静态预览格回车同样不得另起外部会话的恢复进程（2026-08-08 裁定）。"""
        store, registry = _make_store(sessions=self._external_sessions())
        registry.build_launch_plan = mock.Mock(
            side_effect=AssertionError("外部会话不该构造启动计划")
        )
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.3)
            pane = _primary_embed_pane(app.screen)
            self.assertTrue(pane._is_restart_target())  # noqa: SLF001
            app.screen.set_focus(pane)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
        self.assertIsNone(app.return_value)
        registry.build_launch_plan.assert_not_called()

    async def test_transcript_keeps_being_reloaded_while_running_elsewhere(self) -> None:
        """助手仍在写历史 → mtime 变、缓存失效；右栏必须自己补读，否则正文会空掉。"""
        store, _registry = _make_store(sessions=self._external_sessions())
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.3)
            screen = app.screen
            warmed: list[str] = []
            with mock.patch.object(
                screen, "_warm_conversation",
                side_effect=lambda s, gen: warmed.append(pickup.session_key(s)),
            ):
                screen._build_hosted_entries(["claude:s0"])
            self.assertEqual(warmed, ["claude:s0"])

    async def test_hosted_session_is_not_reloaded_from_disk(self) -> None:
        """已托管会话右栏是实时画面，不该为它反复读历史文件。"""
        sessions = self._external_sessions()
        sessions[0]["keepalive_name"] = "pickup-claude-fake"
        store, _registry = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.3)
            screen = app.screen
            warmed: list[str] = []
            with mock.patch.object(
                screen, "_warm_conversation",
                side_effect=lambda s, gen: warmed.append(pickup.session_key(s)),
            ):
                screen._build_hosted_entries(["claude:s0"])
            self.assertEqual(warmed, [])

    async def test_hosted_running_session_opens_without_confirm(self) -> None:
        """已被 pickup 托管的运行中会话是老路径，不能被新确认框挡住。"""
        sessions = self._external_sessions()
        sessions[0]["keepalive_name"] = "pickup-claude-fake"
        store, _registry = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            self.assertNotIsInstance(app.screen, ConfirmModal)
        self.assertIsNotNone(app.return_value)


class FullTextSearchModalTests(unittest.IsolatedAsyncioTestCase):
    """Ctrl+F 全文搜索弹窗：搜对话正文、展示命中行、选中后跳回侧边栏定位。"""

    def _store(self):
        sessions = [
            {
                "source": "claude", "id": "a", "short_id": "a",
                "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "侧边栏改造",
                "cwd": "/Users/x/pickup", "live": False,
            },
            {
                "source": "claude", "id": "b", "short_id": "b",
                "mtime": time.time() - 10, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "字幕优化",
                "cwd": "/Users/x/LiveCaption", "live": False,
            },
        ]
        conversations = {
            "a": [pickup.ConversationMessage("user", "第一行\n这里聊到了红烧肉的做法\n第三行")],
            "b": [pickup.ConversationMessage("assistant", "字幕断句改好了")],
        }
        store, registry = _make_store(sessions=sessions)
        runtime = registry.get("claude")
        runtime.load_conversation.side_effect = lambda s: list(conversations[s["id"]])
        return store

    async def _open_search(self, pilot, app):
        await pilot.press("ctrl+f")
        await _wait_until(lambda: isinstance(app.screen, FullTextSearchModal))
        modal = app.screen
        await _wait_until(lambda: not modal._indexing)
        return modal

    async def _type(self, pilot, modal, text: str) -> None:
        modal.query_one("#search-query", TextArea).load_text(text)
        # 先 pause 一次让 TextArea.Changed 落地、把防抖定时器挂上，再等它跑完。
        # 少了这一步会在定时器还没建起来时就判定「已完成」，查询其实一次没跑。
        await pilot.pause()
        await _wait_until(lambda: modal._debounce_timer is None)
        await pilot.pause()

    async def test_search_matches_conversation_body_and_shows_the_hit_line(self) -> None:
        app = PickupApp(self._store(), embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            modal = await self._open_search(pilot, app)
            await self._type(pilot, modal, "红烧肉")

            self.assertEqual([m.session["id"] for m in modal._matches], ["a"])
            rows = modal.query(SearchResultRow)
            self.assertEqual(len(rows), 1)
            rendered = rows.first().render().plain
            self.assertIn("pickup: 侧边栏改造", rendered)
            self.assertIn("这里聊到了红烧肉的做法", rendered)
            # 只展示命中的那一行，不把整条消息倒出来
            self.assertNotIn("第三行", rendered)
            await pilot.press("escape")

    async def test_hit_keyword_is_highlighted(self) -> None:
        app = PickupApp(self._store(), embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            modal = await self._open_search(pilot, app)
            await self._type(pilot, modal, "红烧肉")

            row = modal.query(SearchResultRow).first()
            hit_style = row.get_component_rich_style("search-result--hit")
            highlighted = "".join(
                span_text
                for span_text, style in [
                    (row.render().plain[span.start:span.end], span.style)
                    for span in row.render().spans
                ]
                if style == hit_style
            )
            self.assertIn("红烧肉", highlighted)
            await pilot.press("escape")

    async def test_enter_reveals_session_and_clears_blocking_filter(self) -> None:
        """搜到的会话被侧边栏筛选词挡在外面时，选中它必须先把筛选清掉。"""
        app = PickupApp(self._store(), embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            search = app.screen.query_one("#project-search", Input)
            search.value = "LiveCaption"
            await pilot.pause(delay=0.2)
            self.assertEqual([s["id"] for s in list_view.visible_sessions()], ["b"])

            modal = await self._open_search(pilot, app)
            # 侧边栏筛选词会被带进弹窗当初始查询，先清掉再搜别的
            await self._type(pilot, modal, "红烧肉")
            await pilot.press("enter")
            await _wait_until(lambda: not isinstance(app.screen, FullTextSearchModal))
            await pilot.pause(delay=0.2)

            self.assertEqual(list_view.nav.project_query, "")
            self.assertEqual(search.value, "")
            selected = list_view.selected_session()
            self.assertIsNotNone(selected)
            self.assertEqual(selected["id"], "a")

    async def test_escape_closes_without_touching_the_sidebar(self) -> None:
        app = PickupApp(self._store(), embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            await pilot.press("down")
            await pilot.pause()
            before = list_view.index

            modal = await self._open_search(pilot, app)
            await self._type(pilot, modal, "红烧肉")
            await pilot.press("escape")
            await _wait_until(lambda: not isinstance(app.screen, FullTextSearchModal))
            await pilot.pause()

            self.assertEqual(list_view.index, before)
            self.assertIsNone(app.return_value)  # Esc 关弹窗不能顺手把程序也退了

    async def test_backdrop_click_closes_without_touching_the_sidebar(self) -> None:
        """点框外空白＝Esc：关弹窗、不动侧边栏、不退出程序。"""
        app = PickupApp(self._store(), embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            await pilot.press("down")
            await pilot.pause()
            before = list_view.index

            await self._open_search(pilot, app)
            await pilot.click(offset=(0, 0))
            await _wait_until(lambda: not isinstance(app.screen, FullTextSearchModal))
            await pilot.pause()

            self.assertEqual(list_view.index, before)
            self.assertIsNone(app.return_value)

    async def test_click_on_the_query_box_keeps_the_modal_open(self) -> None:
        """回归：Click 从输入框冒泡上来，不能被当成点在背景上。"""
        app = PickupApp(self._store(), embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            modal = await self._open_search(pilot, app)
            await pilot.click(modal.query_one("#search-query", TextArea))
            await pilot.pause(delay=0.2)
            self.assertIsInstance(app.screen, FullTextSearchModal)
            await pilot.press("escape")

    async def test_sidebar_filter_is_carried_into_the_modal(self) -> None:
        app = PickupApp(self._store(), embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            app.screen.query_one("#project-search", Input).value = "字幕"
            await pilot.pause(delay=0.2)

            modal = await self._open_search(pilot, app)
            self.assertEqual(modal.query_one("#search-query", TextArea).text, "字幕")
            await _wait_until(lambda: modal._debounce_timer is None)
            self.assertEqual([m.session["id"] for m in modal._matches], ["b"])
            await pilot.press("escape")

    async def test_results_are_sorted_newest_first(self) -> None:
        app = PickupApp(self._store(), embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            modal = await self._open_search(pilot, app)
            # 两个会话的项目路径都在 /Users/x 下，用它把两条都搜出来
            await self._type(pilot, modal, "/users/x")
            self.assertEqual([m.session["id"] for m in modal._matches], ["a", "b"])
            await pilot.press("escape")

    async def test_arrow_keys_move_results_while_input_keeps_focus(self) -> None:
        """输入框全程持有焦点，↑↓ 只挪结果高亮——用户不用在两个控件间切焦点。"""
        app = PickupApp(self._store(), embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            modal = await self._open_search(pilot, app)
            await self._type(pilot, modal, "/users/x")
            results = modal.query_one("#search-results", ListView)
            query = modal.query_one("#search-query", TextArea)
            self.assertTrue(query.has_focus)
            self.assertEqual(results.index, 0)

            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(results.index, 1)
            self.assertTrue(query.has_focus)  # 焦点没被抢走

            await pilot.press("enter")
            await _wait_until(lambda: not isinstance(app.screen, FullTextSearchModal))
            await pilot.pause(delay=0.2)
            selected = app.screen.query_one(SessionListView).selected_session()
            self.assertEqual(selected["id"], "b")

    def _bulk_store(self, count: int = 80):
        """结果多到一次重建挂不完的规模——只有这样才复现得了下面那个竞态。"""
        now = time.time()
        sessions = [
            {
                "source": "claude", "id": f"s{i}", "short_id": f"s{i}",
                "mtime": now - i, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"会话{i}",
                "cwd": "/Users/x/pickup", "live": False,
            }
            for i in range(count)
        ]
        store, registry = _make_store(sessions=sessions)
        registry.get("claude").load_conversation.side_effect = lambda s: [
            pickup.ConversationMessage("user", f"甲词 {s['id']}"),
            pickup.ConversationMessage("assistant", f"乙词 {s['id']}"),
        ]
        return store

    async def test_concurrent_rebuilds_do_not_stack_duplicate_rows(self) -> None:
        """两条消息泵同时要求重建时，结果列表不能把同一批结果叠着挂两遍。

        重建请求确实来自两条互不相让的泵：防抖定时器在 Screen 泵，建索引完成经
        `call_from_thread` 在 App 泵。`ListView.clear()` 是投递 Prune 消息异步移除、
        挂载却是同步进 DOM——不 await 且不串行就会新旧共存，`index` 指向旧子项，
        用户打完字立刻回车会打开一个他没在看的会话。这里直接并发调重建来锁住
        `_results_lock` + `await clear/extend` 这套约定。
        """
        app = PickupApp(self._bulk_store(), embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            modal = await self._open_search(pilot, app)
            await self._type(pilot, modal, "甲词")
            results = modal.query_one("#search-results", ListView)
            expected = len(modal._matches)
            self.assertGreater(expected, 1)

            await asyncio.gather(*(modal._rebuild_results() for _ in range(4)))
            await pilot.pause()

            rows = list(results.query(SearchResultRow))
            self.assertEqual(len(rows), expected, "并发重建把结果重复挂上去了")
            self.assertLess(results.index, len(rows))
            self.assertEqual(modal._selected_key(), rows[0].match.key)
            await pilot.press("escape")

    async def test_highlighted_row_always_matches_what_enter_would_open(self) -> None:
        """连续换查询词的整个过程中，高亮项和回车会打开的会话必须始终一致。

        `_selected_key()` 现在就是从高亮控件本身取的，所以这条在实现上是结构性
        成立的；用例的价值在于把这个约定钉死——将来谁把它改回「用 `index` 索引
        `_matches`」（两份可能不同步的数据），这里的采样就会在重建中间态上炸。
        """
        app = PickupApp(self._bulk_store(), embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            modal = await self._open_search(pilot, app)
            query = modal.query_one("#search-query", TextArea)
            results = modal.query_one("#search-results", ListView)

            checked = 0
            for text in ("甲词", "乙词", "甲词 s1", "乙词", "甲词"):
                query.load_text(text)
                # 不等收敛：在重建正在进行的中间态上反复核对不变式
                for _ in range(8):
                    await pilot.pause()
                    rows = list(results.query(SearchResultRow))
                    index = results.index
                    if index is None or not rows:
                        continue
                    checked += 1
                    self.assertLess(index, len(rows), f"高亮下标越过了实际子项（{text}）")
                    self.assertEqual(
                        modal._selected_key(),
                        rows[index].match.key,
                        f"高亮的会话与回车会打开的会话不一致（{text}）",
                    )
            self.assertGreater(checked, 10, "没有真正采样到重建中的状态，用例是空的")
            await pilot.press("escape")

    async def test_reopening_the_modal_picks_up_sessions_added_since_warmup(self) -> None:
        """索引不能建一次就不管了：开着不动期间新产生的会话必须搜得到。"""
        store = self._store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            screen = app.screen
            screen.search_index().refresh(store)  # 模拟首屏预热已经跑完

            new_session = {
                "source": "claude", "id": "c", "short_id": "c",
                "mtime": time.time() + 10, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "新会话",
                "cwd": "/Users/x/pickup", "live": False,
            }
            runtime = store.registry.get("claude")
            previous = runtime.load_conversation.side_effect
            runtime.load_conversation.side_effect = (
                lambda s: [pickup.ConversationMessage("user", "刚聊到的秘密暗号")]
                if s["id"] == "c"
                else previous(s)
            )
            with store.lock:
                store.sessions["claude"].insert(0, new_session)

            modal = await self._open_search(pilot, app)
            await self._type(pilot, modal, "秘密暗号")
            self.assertEqual([m.session["id"] for m in modal._matches], ["c"])
            await pilot.press("escape")

    async def test_ctrl_f_opens_search_when_a_live_pane_has_focus(self) -> None:
        """右栏实时终端持有输入时，Ctrl+F 仍是 pickup 的全文搜索入口。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            screen = app.screen
            self.assertTrue(screen.check_action("search_content", ()))
            with mock.patch.object(type(screen), "_live_embed_focused", return_value=True):
                self.assertTrue(screen.check_action("search_content", ()))

            await pilot.press("ctrl+f")
            await _wait_until(lambda: isinstance(app.screen, FullTextSearchModal))
            await pilot.press("escape")


class SessionHudSummaryTests(unittest.TestCase):
    """会话小窗的摘要提取：只取真人提问，从旧到新，多行压成一行。"""

    def _messages(self, count: int):
        out = []
        for i in range(count):
            out.append(pickup.ConversationMessage("user", f"问题{i}"))
            out.append(pickup.ConversationMessage("assistant", f"回复{i}"))
        return out

    def test_injected_runtime_prompts_are_dropped(self) -> None:
        """Your prompts 只列人敲的话；扫描层留下的注入轮次必须在这里丢掉。

        样本取自本机真实历史：Cursor 计划附件、任务收尾提示、pickup 接力词、
        Codex skill/中断包裹、OpenConductor 角色提示。用户自己敲的 `$doc-update`
        和带图提问要留下。
        """
        from pickup.ui.session_hud import is_injected_user_prompt, summarize_user_messages

        injected = [
            "Briefly inform the user about the task result"
            " and perform any follow-up actions (if needed).",
            "侧边栏块级斑马纹\n\nImplement the plan as specified,"
            " it is attached for your reference. Do NOT edit the plan file itself.",
            "任务：Subswap 余量不显示\n\n你正在接力一个来自 Cursor 的会话。"
            "请新建自己的会话继续工作；这不是对原会话的原生恢复。",
            "Implement the plan.",
            "<skill>\n<name>grilling</name>\n<path>/tmp/SKILL.md</path>",
            "<turn_aborted>\nThe user interrupted the previous turn on purpose.\n</turn_aborted>",
            "<subagent_notification>\n{\"status\":\"done\"}\n</subagent_notification>",
            "你是 OpenConductor 的管家 Agent，我的私人技术助理。",
            "【权威对话账本（按发生顺序；这是你与用户真实说过的话）】\n"
            "- 管家：在，说吧。\n【本轮回复契约】",
            "原始任务：\n接手验收收尾。\n用户最新补充：\n继续核对缓存。",
            "先按项目现有规则完成 build/test/lint 和真实路径验证；"
            "然后读取项目根 AGENTS.md 的发布要求。",
            "对本仓库当前的 git diff 做一次 code review，不要额外限制范围。",
            "Reply with exactly: ok",
            "只回复 OK。",
            "只回复 RESUMED。",
        ]
        for body in injected:
            self.assertTrue(is_injected_user_prompt(body), body[:60])

        kept = [
            "登录偶发失败，帮我定位",
            "$doc-update",
            "/grilling",
            "/model sol",
            "继续",
            "设计一下这项目的原型 使用 [$ui-prototyper](/tmp/SKILL.md) 生图",
        ]
        for body in kept:
            self.assertFalse(is_injected_user_prompt(body), body[:60])

        mixed = [
            pickup.ConversationMessage("user", injected[0]),
            pickup.ConversationMessage("user", "人敲的第一句"),
            pickup.ConversationMessage("assistant", "回复"),
            pickup.ConversationMessage("user", injected[2]),
            pickup.ConversationMessage("user", "人敲的第二句"),
        ]
        data = summarize_user_messages(mixed)
        self.assertEqual(data.count, 2)
        self.assertEqual([body for _stamp, body in data.entries], ["人敲的第一句", "人敲的第二句"])

    def test_image_wrapper_keeps_the_caption(self) -> None:
        from pickup.ui.session_hud import summarize_user_messages

        wrapped = (
            '<image name=[Image #1] path="/tmp/a.png">\n</image>\n'
            "[Image #1] 展开的时候这条线要连到三角形"
        )
        data = summarize_user_messages([pickup.ConversationMessage("user", wrapped)])
        self.assertEqual(data.count, 1)
        self.assertIn("展开的时候这条线要连到三角形", data.entries[0][1])
        self.assertNotIn("<image", data.entries[0][1])

    def test_only_user_messages_oldest_first(self) -> None:
        from pickup.ui.session_hud import summarize_user_messages

        data = summarize_user_messages(self._messages(3))
        self.assertEqual(data.count, 3)
        self.assertEqual([body for _stamp, body in data.entries], ["问题0", "问题1", "问题2"])
        self.assertEqual(data.oldest[1], "问题0")
        self.assertEqual(data.latest[1], "问题2")
        self.assertEqual(data.omitted, 0)

    def test_long_session_keeps_both_ends_and_drops_the_middle(self) -> None:
        """最早那条决定「这个会话本来要干嘛」，不能跟着中间那段一起被砍掉。"""
        from pickup.ui.session_hud import MAX_ENTRIES, summarize_user_messages

        data = summarize_user_messages(self._messages(20))
        self.assertEqual(data.count, 20)
        self.assertEqual(len(data.entries), MAX_ENTRIES)
        self.assertEqual(data.oldest[1], "问题0")
        self.assertEqual(data.latest[1], "问题19")
        # 被省略的是中间那段：总数 - 最早一条 - 展示的最近几条
        self.assertEqual(data.omitted, 20 - 1 - (MAX_ENTRIES - 1))
        bodies = [body for _stamp, body in data.entries]
        self.assertEqual(bodies, ["问题0", "问题15", "问题16", "问题17", "问题18", "问题19"])

    def test_time_column_drops_the_date_for_todays_prompts(self) -> None:
        """横向寸土寸金：当天只给 HH:MM，更早只给 MM-DD，两者都恰好 5 格宽。"""
        from pickup.ui.session_hud import _short_time

        now = time.mktime((2026, 7, 31, 16, 30, 0, 0, 0, -1))
        today = time.mktime((2026, 7, 31, 9, 5, 0, 0, 0, -1))
        earlier = time.mktime((2026, 7, 28, 9, 5, 0, 0, 0, -1))
        self.assertEqual(_short_time(today, now), "09:05")
        self.assertEqual(_short_time(earlier, now), "07-28")
        for stamp in (_short_time(today, now), _short_time(earlier, now)):
            self.assertEqual(pickup._text_width(stamp), 5)

    def test_expanded_shows_na_when_timestamp_missing(self) -> None:
        """Cursor 等没有逐条时间的历史：展开态用 N/A 占位，列宽与真实时间对齐。"""
        from pickup.ui.session_hud import _MISSING_TIME, SessionHud, summarize_user_messages

        hud = SessionHud()
        hud.update_data(
            summarize_user_messages([pickup.ConversationMessage("user", "无时间提问")]),
            expanded=True,
        )
        body = hud.lines(40)[1].plain
        self.assertEqual(pickup._text_width(_MISSING_TIME), 5)
        self.assertTrue(body.startswith(_MISSING_TIME), body)
        self.assertEqual(body[7:7 + len("无时间提问")], "无时间提问")

    def test_multiline_prompt_collapsed_to_single_line(self) -> None:
        from pickup.ui.session_hud import summarize_user_messages

        data = summarize_user_messages(
            [pickup.ConversationMessage("user", "第一行\n\n  第二行\t第三行  ")],
        )
        self.assertEqual(data.entries[0][1], "第一行 第二行 第三行")

    def test_no_user_messages_means_no_hud(self) -> None:
        from pickup.ui.session_hud import summarize_user_messages

        data = summarize_user_messages([pickup.ConversationMessage("assistant", "只有回复")])
        self.assertFalse(data)
        self.assertEqual(data.count, 0)

    def test_consecutive_duplicate_prompts_collapse(self) -> None:
        """相邻同一句只留一条；隔了一轮再发同一句是真人重复，要留下。"""
        from pickup.ui.session_hud import summarize_user_messages

        data = summarize_user_messages([
            pickup.ConversationMessage("user", "同一句"),
            pickup.ConversationMessage("user", "同一句\n"),
            pickup.ConversationMessage("assistant", "回"),
            pickup.ConversationMessage("user", "下一句"),
            pickup.ConversationMessage("user", "同一句"),
        ])
        self.assertEqual(data.count, 3)
        self.assertEqual(
            [body for _stamp, body in data.entries],
            ["同一句", "下一句", "同一句"],
        )


class SessionHudRenderTests(unittest.TestCase):
    """小窗两种形态的内容：收起态给两头，展开态补上中间并如实说明省略了多少条。"""

    def _hud(self, count: int, *, expanded: bool):
        from pickup.ui.session_hud import SessionHud, summarize_user_messages

        messages = []
        for i in range(count):
            messages.append(pickup.ConversationMessage("user", f"问题{i}"))
        hud = SessionHud()
        hud.update_data(summarize_user_messages(messages), expanded=expanded)
        return hud

    def test_collapsed_shows_both_ends_oldest_above_latest(self) -> None:
        """最初一条看出会话本来要干嘛，最近一条看出现在做到哪；顺序从上到下由旧到新。"""
        hud = self._hud(4, expanded=False)
        lines = [line.plain for line in hud.lines(40)]
        self.assertEqual(len(lines), 3)
        self.assertIn("4 prompts", lines[0])
        self.assertIn("First", lines[1])
        self.assertIn("问题0", lines[1])
        self.assertIn("Latest", lines[2])
        self.assertIn("问题3", lines[2])

    def test_collapsed_single_prompt_has_no_duplicate_row(self) -> None:
        hud = self._hud(1, expanded=False)
        lines = [line.plain for line in hud.lines(40)]
        self.assertEqual(len(lines), 2)
        self.assertIn("1 prompt", lines[0])
        self.assertIn("问题0", lines[1])

    def test_expanded_is_oldest_to_newest_with_the_middle_reported(self) -> None:
        from pickup.ui.session_hud import MAX_ENTRIES

        hud = self._hud(10, expanded=True)
        lines = [line.plain for line in hud.lines(40)]
        # 标题 + 最早一条 + "中间省略 N 条" + 最近 (MAX_ENTRIES-1) 条 + 收起提示
        self.assertEqual(len(lines), MAX_ENTRIES + 3)
        self.assertIn("Your prompts (10)", lines[0])
        self.assertIn("问题0", lines[1], "最早那条必须排在最上面")
        self.assertIn(f"{10 - MAX_ENTRIES} more in between", lines[2])
        self.assertIn("问题9", lines[-2], "最新那条必须排在最下面")
        self.assertIn("Click to collapse", lines[-1])

    def test_expanded_without_truncation_lists_everything_in_order(self) -> None:
        hud = self._hud(3, expanded=True)
        lines = [line.plain for line in hud.lines(40)]
        self.assertEqual(len(lines), 5)  # 标题 + 3 条 + 收起提示
        self.assertNotIn("in between", " ".join(lines))
        self.assertIn("问题0", lines[1])
        self.assertIn("问题1", lines[2])
        self.assertIn("问题2", lines[3])

    def test_expanded_folds_long_prompt_with_ellipsis(self) -> None:
        """展开态每条最多两行，超出末行加省略号，不再整条换行铺满浮层。"""
        from pickup.ui.session_hud import _MAX_PROMPT_LINES, SessionHud, summarize_user_messages

        body = "长提问" * 40
        hud = SessionHud()
        hud.update_data(
            summarize_user_messages([pickup.ConversationMessage("user", body)]),
            expanded=True,
        )
        lines = hud.lines(30)
        body_lines = lines[1:-1]
        self.assertEqual(len(body_lines), _MAX_PROMPT_LINES)
        self.assertTrue(body_lines[-1].plain.rstrip().endswith("..."))
        self.assertIn("长提问", body_lines[0].plain)
        joined = "".join(line.plain[7:].rstrip() for line in body_lines)
        self.assertLess(len(joined), len(body))
        for line in lines:
            self.assertLessEqual(pickup._text_width(line.plain), 30)

    def test_short_prompt_does_not_grow_or_ellipsis(self) -> None:
        from pickup.ui.session_hud import SessionHud, summarize_user_messages

        hud = SessionHud()
        hud.update_data(
            summarize_user_messages([pickup.ConversationMessage("user", "短提问")]),
            expanded=True,
        )
        body_lines = hud.lines(40)[1:-1]
        self.assertEqual(len(body_lines), 1)
        self.assertIn("短提问", body_lines[0].plain)
        self.assertNotIn("...", body_lines[0].plain)

    def test_expanded_continuation_lines_align_with_the_first_line(self) -> None:
        from pickup.ui.session_hud import SessionHud, summarize_user_messages

        hud = SessionHud()
        hud.update_data(
            summarize_user_messages([
                pickup.ConversationMessage("user", "对齐" * 30, 1_785_000_000.0),
            ]),
            expanded=True,
        )
        body = hud.lines(40)[1:-1]
        self.assertGreater(len(body), 1, "这么长的提问必须换行，不能一行装下")
        head_indent = len(body[0].plain) - len(body[0].plain.lstrip())
        for line in body[1:]:
            self.assertEqual(
                len(line.plain) - len(line.plain.lstrip()),
                len(body[0].plain[:7]),
                "续行必须缩进到与首行正文同一列",
            )
        self.assertEqual(head_indent, 0, "首行以时间列开头，不额外缩进")

    def test_expanded_caps_height_and_scrolls_instead_of_dropping_content(self) -> None:
        from pickup.ui.session_hud import SessionHud, summarize_user_messages

        msgs = [pickup.ConversationMessage("user", f"第{i}条" + "正文" * 20) for i in range(6)]
        hud = SessionHud()
        hud.update_data(summarize_user_messages(msgs), expanded=True)

        capped = hud.lines(40, 10)
        self.assertEqual(len(capped), 10, "超出高度必须封顶，不能盖满整格")
        self.assertGreater(hud._max_scroll, 0)  # noqa: SLF001
        self.assertEqual(hud._scroll, hud._max_scroll, "默认钉底，视野里是最新提问")  # noqa: SLF001
        self.assertIn("Your prompts", capped[0].plain, "页眉必须常驻")
        self.assertIn("Click to collapse", capped[-1].plain, "页脚（唯一的收起出口）必须常驻")
        self.assertIn("scroll for more", capped[-1].plain)
        # 封顶窗口默认贴底：可见正文应包含最新那条，而不是最早那条
        body = " ".join(line.plain for line in capped[1:-1])
        self.assertIn("第5条", body)
        self.assertNotIn("第0条", body)

        bottom = [line.plain for line in capped[1:-1]]
        self.assertTrue(hud._scroll_by(-3))  # noqa: SLF001
        scrolled = [line.plain for line in hud.lines(40, 10)[1:-1]]
        self.assertNotEqual(bottom, scrolled, "上滚必须换到更早的正文")
        self.assertFalse(hud._stick_bottom)  # noqa: SLF001
        # 滚回底部后重新钉底，也不会滚出界
        for _ in range(50):
            hud._scroll_by(3)  # noqa: SLF001
        self.assertEqual(hud._scroll, hud._max_scroll)  # noqa: SLF001
        self.assertTrue(hud._stick_bottom)  # noqa: SLF001

    def test_new_prompts_keep_viewport_pinned_to_latest(self) -> None:
        """新提问追加在末尾时，钉底状态必须跟着贴到最新，不能停在旧位置。"""
        from pickup.ui.session_hud import SessionHud, summarize_user_messages

        short = [pickup.ConversationMessage("user", f"第{i}条" + "正文" * 20) for i in range(3)]
        hud = SessionHud()
        hud.update_data(summarize_user_messages(short), expanded=True)
        hud.lines(40, 10)
        self.assertTrue(hud._stick_bottom)  # noqa: SLF001

        longer = short + [
            pickup.ConversationMessage("user", f"第{i}条" + "正文" * 20) for i in range(3, 6)
        ]
        hud.update_data(summarize_user_messages(longer), expanded=True)
        visible = " ".join(line.plain for line in hud.lines(40, 10)[1:-1])
        self.assertIn("第5条", visible)
        self.assertEqual(hud._scroll, hud._max_scroll)  # noqa: SLF001

    def test_collapsing_resets_scroll(self) -> None:
        from pickup.ui.session_hud import SessionHud, summarize_user_messages

        msgs = [pickup.ConversationMessage("user", f"第{i}条" + "正文" * 20) for i in range(6)]
        data = summarize_user_messages(msgs)
        hud = SessionHud()
        hud.update_data(data, expanded=True)
        hud.lines(40, 10)
        hud._scroll_by(-6)  # noqa: SLF001
        self.assertLess(hud._scroll, hud._max_scroll)  # noqa: SLF001
        self.assertFalse(hud._stick_bottom)  # noqa: SLF001
        hud.update_data(data, expanded=False)
        hud.update_data(data, expanded=True)
        hud.lines(40, 10)
        self.assertTrue(hud._stick_bottom, "重新展开必须重新钉底")  # noqa: SLF001
        self.assertEqual(hud._scroll, hud._max_scroll, "重新展开必须落到最新")  # noqa: SLF001

    def test_hide_clears_data(self) -> None:
        hud = self._hud(3, expanded=False)
        self.assertTrue(hud.data)
        hud.hide()
        self.assertFalse(hud.data)
        self.assertEqual(hud.lines(40), [])

    def test_expanded_zebra_paints_odd_prompt_blocks(self) -> None:
        """提问块按奇偶交替涂条纹；页眉、中间省略行、页脚都不涂。"""
        from pickup.ui.session_hud import SessionHud, summarize_user_messages

        def has_bg(line) -> bool:
            for span in line.spans:
                style = span.style
                if isinstance(style, str) and "on " in style:
                    return True
                if getattr(style, "bgcolor", None) is not None:
                    return True
            return False

        msgs = [pickup.ConversationMessage("user", f"问题{i}") for i in range(8)]
        hud = SessionHud()
        hud.update_data(summarize_user_messages(msgs), expanded=True)
        body = hud._expanded_body(40, "on #334455")  # noqa: SLF001
        plains = [line.plain for line in body]
        omitted_at = next(i for i, text in enumerate(plains) if "in between" in text or "省略" in text)
        self.assertFalse(has_bg(body[omitted_at]), "中间省略行不得涂条纹")
        prompt_rows = [(i, line) for i, line in enumerate(body) if i != omitted_at]
        # entries: 问题0, 问题3..问题7（limit=6 留两头砍中间）→ 奇偶按 entries index
        self.assertIn("问题0", prompt_rows[0][1].plain)
        self.assertFalse(has_bg(prompt_rows[0][1]), "最早一条是偶数块，不涂")
        self.assertTrue(has_bg(prompt_rows[1][1]), "第二条提问是奇数块，要涂")

    def test_hud_stripe_stays_in_pane_blue_family(self) -> None:
        """条纹叠 `$primary`，必须仍是蓝，不能被 `$foreground` 洗成灰。"""
        from textual.color import Color as TextualColor

        from pickup.ui.session_hud import _hud_stripe_color

        gray = TextualColor.parse("#C9D1D9")
        for bg_hex, primary_hex in (("#31475E", "#3B7EB8"), ("#D1E7F7", "#2F6F9F")):
            background = TextualColor.parse(bg_hex)
            primary = TextualColor.parse(primary_hex)
            mixed = _hud_stripe_color(background, primary)
            self.assertGreater(
                mixed.b, mixed.r,
                f"{bg_hex}+{primary_hex} → {mixed.hex} 必须偏蓝",
            )
            gray_mix = background.blend(gray, 0.16)
            self.assertGreater(
                mixed.b - mixed.r,
                gray_mix.b - gray_mix.r,
                f"{mixed.hex} 应比叠灰 {gray_mix.hex} 更蓝",
            )
            self.assertNotEqual(mixed.hex, gray_mix.hex)


class SessionHudPlacementTests(unittest.IsolatedAsyncioTestCase):
    """小窗贴在实时格右上角：只盖一行、不压标题栏，且命中区只有胶囊自己。

    命中区是重点回归：写成「整行宽的容器里右对齐」会让托管画面顶部整条横带都吃掉
    鼠标事件，用户在那一行滚不动、划不了词。
    """

    def _live_sessions(self, count: int = 2):
        return [
            {
                "source": "claude", "id": f"s{i}", "short_id": f"s{i}",
                "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"会话{i}",
                "cwd": "/tmp", "live": True,
                "keepalive_name": f"pickup-claude-s{i}",
            }
            for i in range(count)
        ]

    async def _hosted_app(self, sessions):
        store, registry = _make_store(sessions=sessions)
        # 收起态要同时给出最初和最近，夹具至少得有两条真人提问
        registry.get("claude").load_conversation.return_value = [
            pickup.ConversationMessage("user", "最初的问题"),
            pickup.ConversationMessage("assistant", "测试回复"),
            pickup.ConversationMessage("user", "最近的问题"),
        ]
        app = PickupApp(store, embed_ok=True)
        return store, app

    async def test_collapsed_hud_sits_top_right_and_stays_small(self) -> None:
        from pickup.ui.session_hud import SessionHud

        sessions = self._live_sessions(1)
        store, app = await self._hosted_app(sessions)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            area = app.screen.query_one(SplitPaneArea)
            key = pickup.session_key(sessions[0])
            area.show_hosted_group(
                "/tmp", [(sessions[0], sessions[0]["keepalive_name"], lambda: "")],
                focus_key=key,
            )
            await _wait_until(lambda: len(area._cells()) == 1)  # noqa: SLF001
            app.screen._sync_hud()  # noqa: SLF001
            hud = area._cells()[0].session_hud()  # noqa: SLF001
            self.assertIsInstance(hud, SessionHud)
            await _wait_until(lambda: hud.display and hud.region.height > 0)
            self.assertTrue(hud.expanded, "会话提问小窗启动后应默认展开")
            app.screen.action_toggle_hud()
            await _wait_until(lambda: not hud.expanded and hud.region.height == 3)
            cell = area._cells()[0]  # noqa: SLF001
            header = cell.region.y
            # 收起态固定三行：条数 + 最初 + 最近
            self.assertEqual(hud.region.height, 3, "收起态只能是「条数 + 最初 + 最近」三行")
            self.assertEqual(hud.region.y, header + 1, "不得压住分栏标题栏")
            self.assertEqual(hud.region.right, cell.region.right - 1, "右边留一列")
            self.assertLess(
                hud.region.width, cell.region.width // 2,
                "命中区必须只有小窗本身，不能是整行宽的容器",
            )
            rendered = hud.render().plain
            self.assertIn("最初的问题", rendered)
            self.assertIn("最近的问题", rendered)
            self.assertLess(
                rendered.index("最初的问题"), rendered.index("最近的问题"),
                "从上到下必须是由旧到新",
            )

    async def test_expanded_hud_grows_and_collapses_back(self) -> None:
        sessions = self._live_sessions(1)
        store, app = await self._hosted_app(sessions)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            area = app.screen.query_one(SplitPaneArea)
            key = pickup.session_key(sessions[0])
            area.show_hosted_group(
                "/tmp", [(sessions[0], sessions[0]["keepalive_name"], lambda: "")],
                focus_key=key,
            )
            await _wait_until(lambda: len(area._cells()) == 1)  # noqa: SLF001
            hud = area._cells()[0].session_hud()  # noqa: SLF001
            app.screen._sync_hud()  # noqa: SLF001
            await _wait_until(lambda: hud.display and hud.expanded and hud.region.height > 3)
            self.assertIn("Your prompts", hud.render().plain)
            self.assertEqual(hud.region.right, area._cells()[0].region.right - 1)  # noqa: SLF001

            app.screen.action_toggle_hud()
            await _wait_until(lambda: not hud.expanded and hud.region.height == 3)
            app.screen.action_toggle_hud()
            await _wait_until(lambda: hud.expanded and hud.region.height > 3)

    async def test_box_height_matches_rendered_lines_in_both_states(self) -> None:
        """底色框的高度必须恰好等于正文行数。

        真机现象：展开后浮层底色比文字高出一截。根因是布局阶段（`get_content_height`）
        和渲染阶段各自拿 container 尺寸算了一遍可见高度，两边看到的中间态不一定一样。
        渲染必须按**已经分配给自己的** content 高度开窗，行数与框高才恒等。
        """
        sessions = self._live_sessions(1)
        store, app = await self._hosted_app(sessions)
        registry = store.registry
        registry.get("claude").load_conversation.return_value = [
            pickup.ConversationMessage("user", f"第{i}条提问：" + "很长的正文" * 10)
            for i in range(6)
        ]
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            area = app.screen.query_one(SplitPaneArea)
            key = pickup.session_key(sessions[0])
            area.show_hosted_group(
                "/tmp", [(sessions[0], sessions[0]["keepalive_name"], lambda: "")],
                focus_key=key,
            )
            await _wait_until(lambda: len(area._cells()) == 1)  # noqa: SLF001
            hud = area._cells()[0].session_hud()  # noqa: SLF001
            app.screen._sync_hud()  # noqa: SLF001
            await _wait_until(lambda: hud.display and hud.size.height > 0)

            def _matches() -> bool:
                return hud.size.height == len(hud.render().plain.split("\n"))

            self.assertTrue(hud.expanded, "会话提问小窗启动后应默认展开")
            self.assertTrue(_matches(), "展开态：底色框高度与正文行数不一致")
            app.screen.action_toggle_hud()
            await _wait_until(lambda: not hud.expanded and hud.size.height == 3)
            self.assertTrue(_matches(), "收起态：底色框高度与正文行数不一致")
            # 每行都补齐到同宽，底色才是规整矩形，右侧不会露出锯齿
            widths = {pickup._text_width(line) for line in hud.render().plain.split("\n")}
            self.assertEqual(widths, {hud.size.width})

    async def test_every_live_pane_draws_its_own_hud(self) -> None:
        """多分屏时每个实时托管格都画自己的 Your prompts，不只激活格。"""
        sessions = self._live_sessions(2)
        store, app = await self._hosted_app(sessions)
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause(delay=0.2)
            area = app.screen.query_one(SplitPaneArea)
            key0 = pickup.session_key(sessions[0])
            key1 = pickup.session_key(sessions[1])
            app.screen._apply_layout_change(  # noqa: SLF001
                lambda s: s.set_group("/tmp", [key0, key1], focus_key=key0)
            )
            area.show_hosted_group(
                "/tmp",
                [(s, s["keepalive_name"], lambda: "") for s in sessions],
                focus_key=key0,
            )
            await _wait_until(lambda: len(area._cells()) == 2)  # noqa: SLF001
            app.screen._sync_hud()  # noqa: SLF001
            huds = [cell.session_hud() for cell in area._cells()]  # noqa: SLF001
            await _wait_until(lambda: all(h is not None and h.display for h in huds))
            for hud in huds:
                self.assertTrue(hud.display)
                self.assertTrue(hud.expanded)
                self.assertGreater(hud.data.count, 0)
                # 二分屏格较窄，页眉可能被截成 "Your prompt…"，只断言前缀。
                self.assertIn("Your prompt", hud.render().plain)

            # 焦点切到第二格后，两格小窗都还在
            area._cells()[1].embed_pane().focus()  # noqa: SLF001
            await pilot.pause()
            app.screen._sync_hud()  # noqa: SLF001
            await _wait_until(lambda: all(h.display for h in huds))
            self.assertTrue(huds[0].display)
            self.assertTrue(huds[1].display)

    async def test_static_preview_pane_also_draws_hud(self) -> None:
        """历史消息预览也要画 Your prompts：长对话里靠小窗扫提问脉络。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            area = app.screen.query_one(SplitPaneArea)
            # 夹具默认选中已结束会话，右栏是静态对话预览。
            app.screen._sync_hud()  # noqa: SLF001
            await _wait_until(
                lambda: any(
                    (hud := cell.session_hud()) is not None and hud.display
                    for cell in area._cells()  # noqa: SLF001
                )
            )
            visible = [
                cell.session_hud()
                for cell in area._cells()  # noqa: SLF001
                if cell.session_hud() is not None and cell.session_hud().display
            ]
            self.assertTrue(visible)
            for hud in visible:
                self.assertTrue(hud.expanded)
                self.assertIn("Your prompt", hud.render().plain)


class SessionHudGatingTests(unittest.TestCase):
    """小窗的快捷键归属：右栏实时格持有输入时让路给助手，纯列表模式整体不可用。"""

    def test_toggle_hud_yields_to_the_assistant(self) -> None:
        from pickup.ui.main_screen import MainScreen

        store, _ = _make_store()
        screen = MainScreen(store, embed_ok=True)
        screen._live_embed_focused = lambda: True  # noqa: SLF001
        screen._any_embed_focused = lambda: True  # noqa: SLF001
        self.assertIs(screen.check_action("toggle_hud", ()), False)
        screen._live_embed_focused = lambda: False  # noqa: SLF001
        self.assertTrue(screen.check_action("toggle_hud", ()))

    def test_toggle_hud_disabled_without_embed(self) -> None:
        from pickup.ui.main_screen import MainScreen

        store, _ = _make_store()
        screen = MainScreen(store, embed_ok=False)
        self.assertIs(screen.check_action("toggle_hud", ()), False)
        screen.action_toggle_hud()  # 纯列表模式下调用也不能崩
        self.assertTrue(screen._hud_expanded)  # noqa: SLF001 — 默认展开且无面板时不改状态


class PreviewSustainWarmTests(unittest.TestCase):
    """静态详情预览在会话活跃写入期的续温：缓存随 mtime 失效后，按节流间隔
    后台重新解析，而不是一路停留在「正在读取对话内容…」空态。"""

    def _make_preview(
        self, *, keepalive: str | None = None,
    ) -> tuple[object, object, mock.Mock, dict]:
        import tempfile
        import types

        from pickup.ui.main_screen import MainScreen

        store, _ = _make_store()
        screen = MainScreen(store, embed_ok=True)
        session = store.find_session("claude:s0")
        assert session is not None
        # 单文件运行时的版本签名依赖 path 的 stat，给会话挂一个真实文件：
        # 先让缓存有内容，再把 mtime 推前模拟「会话又写了一条」，制造版本失效。
        path = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        path.close()
        session["path"] = path.name
        try:
            store.get_conversation(session)
            future = os.stat(path.name).st_mtime + 3600
            os.utime(path.name, (future, future))

            spec = types.SimpleNamespace(
                session_key="claude:s0", keepalive_name=keepalive,
            )
            area = mock.Mock()
            area.pane_specs.return_value = [spec]
            screen._preview_warm_at = {}  # noqa: SLF001 — 每例独立节流表
            return store, screen, area, spec
        finally:
            os.unlink(path.name)

    def test_missed_preview_warm_is_throttled(self) -> None:
        from pickup.ui.main_screen import MainScreen

        store, screen, area, spec = self._make_preview()
        with mock.patch.object(MainScreen, "_split_area", return_value=area), mock.patch.object(
            MainScreen, "_warm_conversation"
        ) as warm:
            screen._sustain_preview_warm()  # noqa: SLF001
            screen._sustain_preview_warm()  # noqa: SLF001 — 节流窗口内不重复解析
            self.assertEqual(warm.call_count, 1)
            # 越过节流窗口后再次失效，应继续后台解析。
            screen._preview_warm_at["claude:s0"] -= screen._PREVIEW_WARM_INTERVAL + 1  # noqa: SLF001
            screen._sustain_preview_warm()  # noqa: SLF001
            self.assertEqual(warm.call_count, 2)
            # 记录解析后下一次会话已读到新版本（缓存有效），不再触发。
            session = store.find_session("claude:s0")
            assert session is not None
            store.get_conversation(session)
            screen._sustain_preview_warm()  # noqa: SLF001
            self.assertEqual(warm.call_count, 2)

    def test_keepalive_pane_never_triggers_rewarm(self) -> None:
        from pickup.ui.main_screen import MainScreen

        store, screen, area, spec = self._make_preview(keepalive="pickup-claude-abc")
        with mock.patch.object(MainScreen, "_split_area", return_value=area), mock.patch.object(
            MainScreen, "_warm_conversation"
        ) as warm:
            screen._sustain_preview_warm()  # noqa: SLF001 — 托管格由 embed 画面负责，不续温
            warm.assert_not_called()


class ShellPaneTests(unittest.IsolatedAsyncioTestCase):
    """顶栏「终端」：内嵌自由 shell 分屏与生命周期清理。"""

    async def _open_shell_pane(self, pilot, app, *, keepalive_name: str = "pickup-shell-abc123"):
        area = app.screen.query_one(SplitPaneArea)
        area.current_project = "/tmp"
        app.screen._on_shell_pick()
        await _wait_until(lambda: app.screen._host_pending == 0)
        await _wait_until(
            lambda: any(spec.is_shell for spec in area.pane_specs()),
        )
        return area

    async def test_shell_chip_opens_hosted_pane(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.embed.host_session", return_value="pickup-shell-abc123") as host_mock,
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                chip = app.screen.query_one("#shell-chip")
                from pickup.i18n import t
                self.assertIn(t("shell.chip_label"), chip.render().plain)
                area = await self._open_shell_pane(pilot, app)
                host_mock.assert_called_once()
                runtime_id = host_mock.call_args[0][1]
                self.assertEqual(runtime_id, pickup.models.SHELL_RUNTIME_ID)
                shell_specs = [spec for spec in area.pane_specs() if spec.is_shell]
                self.assertEqual(len(shell_specs), 1)
                self.assertEqual(shell_specs[0].keepalive_name, "pickup-shell-abc123")

    async def test_shell_not_listed_in_sidebar(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.embed.host_session", return_value="pickup-shell-sidebar"),
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await self._open_shell_pane(pilot, app, keepalive_name="pickup-shell-sidebar")
                list_view = app.screen.query_one(SessionListView)
                keys = {pickup.session_key(s) for s in list_view.visible_sessions()}
                self.assertTrue(all(not key.startswith("shell:") for key in keys))

    async def test_close_shell_pane_kills_tmux_session(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.embed.host_session", return_value="pickup-shell-closeme"),
            mock.patch("pickup.embed.is_alive", return_value=True),
            mock.patch("pickup.keepalive.kill", return_value=True) as kill_mock,
            mock.patch("pickup.embed.close_channel") as close_mock,
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                area = await self._open_shell_pane(
                    pilot, app, keepalive_name="pickup-shell-closeme",
                )
                shell_key = next(
                    spec.session_key for spec in area.pane_specs() if spec.is_shell
                )
                area.focus_session_key(shell_key, only_live=False)
                await _wait_until(lambda: area.any_embed_focused())
                before = len(area.pane_specs())
                app.screen.action_close_pane()
                await _wait_until(lambda: len(area.pane_specs()) == before - 1)
                kill_mock.assert_called_once_with("pickup-shell-closeme")
                close_mock.assert_called_once_with("pickup-shell-closeme")

    async def test_close_agent_pane_does_not_kill_tmux(self) -> None:
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.embed.host_session", return_value="pickup-claude-s0"),
            mock.patch("pickup.embed.is_alive", return_value=True),
            mock.patch("pickup.keepalive.kill", return_value=True) as kill_mock,
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("enter")
                await _wait_until(lambda: app.screen._host_pending == 0)
                pane = await _wait_for_embed_session(app.screen, "pickup-claude-s0")
                await _wait_until(lambda: pane.has_focus)
                before = len(app.screen.query_one(SplitPaneArea).pane_specs())
                app.screen.action_close_pane()
                await _wait_until(
                    lambda: len(app.screen.query_one(SplitPaneArea).pane_specs()) == before - 1,
                )
                kill_mock.assert_not_called()

    def test_shell_launch_plan_uses_login_shell(self) -> None:
        from pickup.ui.controllers.host_controller import _shell_launch_plan

        with (
            mock.patch.dict(os.environ, {"SHELL": "/bin/zsh"}, clear=False),
            mock.patch("os.path.isfile", return_value=True),
            mock.patch("os.access", return_value=True),
        ):
            plan = _shell_launch_plan("/tmp/work")
        self.assertEqual(plan.argv, ("/bin/zsh",))
        self.assertEqual(plan.cwd, "/tmp/work")

    async def test_shell_member_in_split_group_not_rendered_in_sidebar(self) -> None:
        """shell pane 进会话组后，侧边栏组卡不得为 shell 成员渲染会话卡。

        组卡成员渲染路径（`_sidebar_rows` 的 all_members → `SessionCard.render_line`
        会查运行时注册表）此前会为 shell 会话抛 LaunchError，导致后台重建列表时
        整屏弹错误面板；shell 成员应被过滤，只留 AI 成员。
        """
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.embed.host_session", return_value="pickup-shell-grp1"),
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                area = await self._open_shell_pane(pilot, app, keepalive_name="pickup-shell-grp1")
                shell_key = next(
                    spec.session_key for spec in area.pane_specs() if spec.is_shell
                )
                # 再开一个 AI 会话格，形成右栏双格 → 会话组持久化会把 shell 也存进组
                await pilot.press("enter")
                await _wait_until(lambda: app.screen._host_pending == 0)
                await _wait_until(lambda: len(area.pane_specs()) == 2)
                group = app.screen._split_store.get_group(shell_key)
                self.assertIsNotNone(group, "shell pane 应被持久化进会话组")
                self.assertIn(shell_key, group.session_keys)

                # 重建列表走组卡成员渲染路径，不得抛 LaunchError
                await app.screen._rebuild_list()
                await pilot.pause(delay=0.2)
                list_view = app.screen.query_one(SessionListView)
                rendered_keys = {
                    pickup.session_key(s)
                    for card in list_view.query(SessionCard)
                    if (s := card.session) is not None
                }
                self.assertTrue(
                    all(not key.startswith("shell:") for key in rendered_keys),
                    f"组卡成员不应包含 shell 会话，实际渲染了：{rendered_keys}",
                )

    async def test_search_renders_shell_hit_without_registry_error(self) -> None:
        """全文搜索命中 shell 会话（按标题「终端」）时，结果行渲染不得查运行时注册表。

        shell 会话没有正文，只能按标题元数据命中；此前 `SearchResultRow.render`
        对 shell 会话调 `registry.get("shell")` 直接抛 LaunchError。
        """
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.embed.host_session", return_value="pickup-shell-srch1"),
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await self._open_shell_pane(pilot, app, keepalive_name="pickup-shell-srch1")
                await pilot.press("ctrl+f")
                await _wait_until(lambda: isinstance(app.screen, FullTextSearchModal))
                modal = app.screen
                await _wait_until(lambda: not modal._indexing)
                from pickup.i18n import t as _t

                modal.query_one("#search-query", TextArea).load_text(
                    _t("shell.pane_title"),
                )
                await pilot.pause()
                await _wait_until(lambda: modal._debounce_timer is None)
                await pilot.pause()

                hits = {m.session["id"] for m in modal._matches}
                shell_hits = {
                    m.session["id"] for m in modal._matches if m.session.get("source") == "shell"
                }
                self.assertTrue(
                    bool(shell_hits),
                    f"应命中 shell 会话，实际命中：{hits}",
                )
                for row in modal.query(SearchResultRow):
                    rendered = row.render().plain
                    self.assertNotIn("LaunchError", rendered)
                await pilot.press("escape")


if __name__ == "__main__":
    unittest.main()
