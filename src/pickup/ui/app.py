"""PickupApp：Textual 应用外壳，唯一界面入口。

`run_app()` 是唯一对外函数，返回值语义：
`LaunchRequest | NewSessionRequest` 表示需要外层 execvp 全屏接管，
`None` 表示用户只是退出。
"""

from __future__ import annotations

from textual.app import App, ScreenError
from textual.theme import Theme
from textual.timer import Timer

from pickup.ui.main_screen import MainScreen
from pickup.ui.terminal_theme import (
    BACKGROUND_POLL_INTERVAL,
    TerminalBackgroundReport,
    enhance_driver,
    query_runtime_theme,
    supports_runtime_theme_tracking,
)

# 窗口拖动时 SIGWINCH 会连续触发。布局跟随 Textual 自带的 ~120fps 合并即可；
# 整屏全量重绘（用来清掉终端 reflow 残影）必须另行防抖，等尺寸停稳再做一次，
# 否则拖动过程中会疯狂全量刷新、卡顿闪烁。
_RESIZE_FULL_REPAINT_DEBOUNCE = 0.12  # 秒；最后一次尺寸变化后再等这么久才整屏重绘

# 冷静工作台：壳层用深石板分层，主色只做焦点/选中提示，避免大面积高饱和蓝抢戏。
_PICKUP_DARK = Theme(
    name="pickup-dark",
    primary="#3B7EB8",
    secondary="#6B8FA3",
    accent="#5B9FD4",
    foreground="#C9D1D9",
    background="#0D1117",
    surface="#161B22",
    panel="#1C2430",
    warning="#D4A017",
    error="#E5534B",
    success="#3F9A6A",
    dark=True,
    variables={
        # 选中抬一层冷灰蓝，不用高饱和 primary 铺满
        "block-cursor-background": "#243447",
        "block-cursor-blurred-background": "#24344766",
        # 分栏激活顶/底条：$primary-muted 再提亮约 10%
        "pane-active-background": "#31475E",
        # 侧边栏投影分屏组合的四级底色（见 _SIDEBAR_SPLIT_LADDER 的说明）
        "sidebar-split-background": "#2A3F58",
        "sidebar-split-cursor-background": "#35506E",
        "sidebar-split-active-background": "#3E648B",
        "sidebar-split-active-cursor-background": "#4A76A3",
    },
)
_PICKUP_LIGHT = Theme(
    name="pickup-light",
    primary="#2F6F9F",
    secondary="#5A7A8C",
    accent="#3D7EB8",
    foreground="#1F2328",
    background="#F0F3F6",
    surface="#E6EBF0",
    panel="#DCE3EA",
    warning="#B8860B",
    error="#CF222E",
    success="#1A7F4B",
    dark=False,
    variables={
        "block-cursor-background": "#C5D6E8",
        "block-cursor-blurred-background": "#C5D6E880",
        # 分栏激活顶/底条：$primary-muted 再提亮约 10%
        "pane-active-background": "#D1E7F7",
        # 浅色下"更显著"是更深更饱和，梯度方向与深色相反，见 _SIDEBAR_SPLIT_LADDER
        "sidebar-split-background": "#BCD5EE",
        "sidebar-split-cursor-background": "#A8C9E9",
        "sidebar-split-active-background": "#93BCE4",
        "sidebar-split-active-cursor-background": "#7FAEDC",
    },
)


# pickup 自有主题变量的兜底值。**凡是在 widget 的 DEFAULT_CSS 里用到、而 Textual
# 内置主题没有的变量，都必须同时登记在这里**，不能只写在上面两个 Theme 里。
# 原因：widget 的 DEFAULT_CSS 是在该类第一次挂载时并入应用样式表并做变量代换的，
# 那一刻当前主题不一定已经切到 pickup 自有主题（`on_mount` 里注册与切换，和首屏
# 各控件的挂载时机不是同一件事，且随 Textual 版本、终端探测结果、直启/普通启动
# 路径而变）。主题里缺一个变量的后果不是"颜色不对"，而是**整个应用起不来**：
# Textual 直接报 `reference to undefined variable` 并中止（2026-07-31 真机事故：
# 0.24.29 在 macOS Homebrew 安装下一启动就报 `$pane-inactive-background` 未定义）。
# 这里给的是深色兜底值，具体主题里的同名变量会覆盖它。
_THEME_VARIABLE_DEFAULTS = {
    "pane-active-background": "#31475E",
    "sidebar-split-background": "#2A3F58",
    "sidebar-split-cursor-background": "#35506E",
    "sidebar-split-active-background": "#3E648B",
    "sidebar-split-active-cursor-background": "#4A76A3",
}

# 侧边栏投影右栏分屏组合时的四级底色阶梯，从弱到强：
#   组合内 → 组合内且键盘光标停在它上面 → 当前激活格 → 激活格且光标停在它上面
# 必须整体单调（深色越来越亮、浅色越来越深），否则会出现「光标移到激活行上，
# 整行反而变暗」的倒挂——列表自身的选中底色（`block-cursor-background`）比激活
# 格底色弱，光标一旦落到组合行上就会把它压回去，看着像状态丢了。四级都用主题
# 变量而不是写死 hex，深浅主题各给一套。
_SIDEBAR_SPLIT_LADDER = (
    "sidebar-split-background",
    "sidebar-split-cursor-background",
    "sidebar-split-active-background",
    "sidebar-split-active-cursor-background",
)


class PickupApp(App):
    """pickup 的主应用；具体界面全部在 MainScreen 里，这里只挂屏幕。"""

    TITLE = "pickup"
    # 不整体关闭 Textual 内置的鼠标拖拽文本选择：EmbedPane 需要它来实现"划词
    # 选中托管会话画面里的文字，抬起后自动 OSC 52 复制"。真机实测过的崩溃只出在 SessionCard/
    # NewSessionCard 这类会被后台重扫动态增删的列表项 Widget 上（选择过程中
    # 控件被移除，container 解析为 None 后访问 .region 崩溃），已单独在那两个
    # 类和弹窗菜单项上关闭 ALLOW_SELECT；EmbedPane 画面不会被动态增删，不受
    # 这条限制，保留选择能力。
    CSS = """
    #project-search {
        /* 侧边栏项约定：总高度含末行间隔（正文 1 + 间隔 1），间隔算进本项命中区。
           禁止用 ListItem/兄弟节点的 margin 做分隔——点在空隙上不会落到本项。 */
        height: 2;
        margin: 0;
        border: none;
        padding: 0 1 1 1;
        /* 筛选是辅助入口：表面层，不要主色大色块抢层级 */
        color: $text-muted;
        background: $panel;
    }

    #project-search:focus {
        color: $foreground;
        background: $primary-muted;
    }
    """

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """pickup 自有主题变量的兜底值，见 `_THEME_VARIABLE_DEFAULTS` 的说明。"""
        return dict(_THEME_VARIABLE_DEFAULTS)

    def get_driver_class(self):
        """默认 Unix 驱动增加终端主题应答解析，其他驱动保持 Textual 原行为。"""
        return enhance_driver(super().get_driver_class())

    def __init__(self, store, embed_ok: bool, direct=None, osc_report: bytes | None = None) -> None:
        super().__init__()
        self._store = store
        self._embed_ok = embed_ok
        self._direct = direct
        self._osc_report = osc_report
        self._resize_full_repaint_timer: Timer | None = None
        # Textual compositor 在窗口高度骤变时偶发 chops/spans 行数不一致（IndexError）；
        # 连续恢复有上限，避免真故障时死循环刷屏。
        self._compositor_recovery_budget = 0

    def _on_terminal_supports_synchronized_output(self, message) -> None:
        # Textual 默认在终端支持时把每帧包进同步输出（`\e[?2026h…l`，原子提交整帧防撕裂）。
        # 排查"iTerm2 + SSH 下输入法候选词小窗跑到左上角"时怀疑过这层：SSH 有网络
        # 延迟/分片，合成期间 iTerm2 有概率读到同步块里尚未提交的旧光标位置（初始
        # 左上角）。设 PICKUP_DISABLE_SYNC_OUTPUT=1 可保持 _sync_available=False、
        # 不再包同步块，用于验证/规避该现象（代价是大面积重绘可能有轻微闪烁）。
        import os

        if os.environ.get("PICKUP_DISABLE_SYNC_OUTPUT") == "1":
            return
        super()._on_terminal_supports_synchronized_output(message)

    def _check_resize(self) -> None:
        """布局立即跟随；整屏清残影防抖后再做。

        Textual 默认只差分刷新布局变化的区域。iTerm2 等终端会在应用重绘前自行
        reflow 备用屏幕，差分重绘会留下残影/错位。必须整屏全量重绘才能清干净，
        但不能在拖动过程中每次尺寸变化都全量刷——这里只重置防抖计时器，等
        `_RESIZE_FULL_REPAINT_DEBOUNCE` 内不再有新尺寸后再 `_force_full_repaint`。
        """
        super()._check_resize()
        # 每次尺寸变化给一次恢复额度：缩放过程中 compositor IndexError 可自愈。
        self._compositor_recovery_budget = 2
        if self._resize_full_repaint_timer is not None:
            self._resize_full_repaint_timer.stop()
        self._resize_full_repaint_timer = self.set_timer(
            _RESIZE_FULL_REPAINT_DEBOUNCE,
            self._force_full_repaint,
        )

    def _force_full_repaint(self) -> None:
        """尺寸停稳后：把当前屏整屏标脏，触发一次 compositor 全量重绘。"""
        self._resize_full_repaint_timer = None
        try:
            screens = [self.screen, *self._background_screens]
        except ScreenError:
            return
        for screen in screens:
            compositor = getattr(screen, "_compositor", None)
            if compositor is None:
                continue
            region = screen.size.region
            if region:
                compositor._dirty_regions.add(region)
            screen.refresh()

    def _handle_exception(self, error: Exception) -> None:
        """缩放期 compositor 行数竞态：整屏重绘一次自愈，不退出整个界面。

        Textual 默认 `_handle_exception` 一律退出。真机在拖动窗口时偶发
        `ChopsUpdate` 的 `chops[y]` IndexError（spans 仍引用旧高度）。
        自愈失败或其它未捕获异常：落盘后再走默认退出，供事后 diagnose。
        """
        if isinstance(error, IndexError) and self._compositor_recovery_budget > 0:
            self._compositor_recovery_budget -= 1
            try:
                self._force_full_repaint()
                return
            except Exception:
                pass
        try:
            from pickup.observe import log_exception

            log_exception("TUI 未捕获异常", error)
        except Exception:
            pass
        super()._handle_exception(error)

    def on_mount(self) -> None:
        # pickup 自己的界面色也要跟随外层终端的深浅色，不能只管注入托管 pane
        # 那一份——两者是分开的：这里管 pickup 自身列表/页头/弹窗的配色，
        # embed_pane.py 的 report_theme 管托管会话自己的深浅色检测。此前完全
        # 没接这一段，Textual 默认主题在浅色终端下会显得配色不对（真机实测发现）。
        # 深/浅都用 pickup 自有「冷静工作台」主题，不用 Textual 默认高饱和主色。
        import pickup

        self.register_theme(_PICKUP_DARK)
        self.register_theme(_PICKUP_LIGHT)
        is_light = pickup._background_is_light(self._osc_report)
        self.theme = "pickup-light" if is_light is True else "pickup-dark"
        self.push_screen(MainScreen(self._store, self._embed_ok, self._direct, self._osc_report))
        if supports_runtime_theme_tracking(self._driver):
            self.set_interval(BACKGROUND_POLL_INTERVAL, self._query_runtime_theme)

    def _query_runtime_theme(self) -> None:
        query_runtime_theme(self._driver)

    def on_terminal_background_report(self, message: TerminalBackgroundReport) -> None:
        """终端运行中换配色时，同步 pickup 壳层、当前面板和后续新会话。"""
        import pickup

        is_light = message.is_light
        if message.osc_report is not None:
            self._osc_report = pickup.theme._replace_background_report(
                self._osc_report,
                message.osc_report,
            )
            is_light = pickup._background_is_light(message.osc_report)
            try:
                screen = self.screen
            except ScreenError:
                screen = None
            if isinstance(screen, MainScreen):
                screen.update_terminal_background(self._osc_report)
        if is_light is not None:
            target = "pickup-light" if is_light else "pickup-dark"
            if self.theme != target:
                self.theme = target


def run_app(store, embed_ok: bool, direct=None, osc_report: bytes | None = None):
    """启动 Textual 界面并阻塞直至用户退出或选择启动某个会话。

    返回 `LaunchRequest | NewSessionRequest | None`。osc_report 是启动前探测到的
    外层终端 OSC 10/11 应答（见 pickup._probe_osc_colours），用于内嵌面板聚焦托管
    会话时注入真实背景色。
    """
    from pickup import i18n

    i18n.init()  # 按 PICKUP_LANG / 系统 locale 选定界面语言
    app = PickupApp(store, embed_ok, direct, osc_report)
    return app.run()
