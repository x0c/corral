"""主屏：左栏会话列表 + 右栏预览/内嵌终端（corral 唯一界面）。

按键语义（/ 聚焦项目搜索 / a 高级操作 /
q 结束会话 / x 删除会话 / c 关闭面板 / Ctrl+Shift+B 显隐侧栏 / Esc 退出）；选中非进行中会话时右栏直接
展示完整对话预览。键盘焦点跟随明确意图：回车 / 单击会话卡打开、新建或直启托管成功后
输入交给右栏那一格（仅限活着的实时会话），上下浏览不抢焦点；再点当前持有输入的那张
会话卡则把焦点撤回侧边栏，与 `Ctrl+\\` 等价。右栏滚轮/预览翻页与焦点无关，鼠标在右栏
上即可滚动。焦点契约与两条易踩的时序坑见 docs/TERMINAL_UI_KNOWLEDGE_BASE.md §6。
多分屏时聚焦某一格会把侧边栏高亮切到对应会话。新建会话走侧边栏「＋ 新建」或
右栏顶栏加格，不再提供底栏 `n` 快捷键。
侧边栏顶部为搜索框，大小写无关模糊匹配组名、项目名与会话标题。
`Ctrl+Shift+B` 与右栏顶栏左侧开关可显隐侧栏（无右栏时不可用）；该偏好与会话组、置顶一起
存在侧边栏记忆库里（见 `split_layout`）。禁止再加第二套全屏预览或纯列表旧界面。
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections import OrderedDict
from collections.abc import Callable

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input
from textual.worker import get_current_worker

from corral import i18n, ui_prefs
from corral.activity_board import ActivityBoard
from corral.i18n import t
from corral.ui.controllers.attention_reader import AttentionReaderMixin
from corral.ui.controllers.board_controller import BoardControllerMixin
from corral.ui.controllers.host_controller import HostControllerMixin
from corral.ui.controllers.hud_controller import HUD_POLL_INTERVAL, HudControllerMixin
from corral.ui.controllers.layout_controller import LayoutControllerMixin
from corral.ui.controllers.update_controller import UpdateControllerMixin
from corral.ui.dragon_easter_egg import DragonOverlay
from corral.ui.dragon_splash import DragonSplash
from corral.ui.footer import CorralFooter
from corral.ui.modals import (
    COPY_SESSION_CHOICE,
    EXPORT_SESSION_CHOICE,
    RESTART_SESSION_CHOICE,
    ConfirmModal,
    choose_target_runtime,
    new_session_flow,
)
from corral.ui.nav import NavState
from corral.ui.runtime_top_bar import RuntimeTopBar
from corral.ui.session_list import STICKY_IDS, SessionListView
from corral.ui.split_pane_area import SplitPaneArea
from corral.ui.update_toast import UpdateToast

try:
    from textual.screen import Screen
except ImportError:  # pragma: no cover
    from textual import Screen

REFRESH_INTERVAL = 3.0  # 秒，后台重扫会话列表的最短间隔，与旧版 _background_refresh 一致
REFRESH_INTERVAL_MAX = 10.0  # 秒，连续空闲多轮后退避到的最长间隔
_IDLE_ROUNDS_BEFORE_BACKOFF = 3  # 连续几轮扫描都没变化才开始拉长间隔，避免偶发抖动误判空闲
CACHE_POLL_INTERVAL = 0.5  # 秒，标题缓存文件轮询间隔（比会话重扫轻得多，保持高频）
# 秒，侧边栏记忆的跨窗口同步间隔。每次只读一个版本号（单行 SELECT），版本号没变就什么都不做；
# 变了才重新读快照，且只有「看得见的部分」真的变了才重建列表（全量重建是秒级重活）。
LAYOUT_POLL_INTERVAL = 1.0
LIST_PANE_WIDTH = 39  # 分栏时左栏固定宽度，对应旧版 EMBED_LEFT_BAND
# 活跃判定可接受的存活证据陈旧上限（秒）。右栏在显示的会话每轮抓帧都会刷新证据，
# 所以这条路几乎永远命中缓存；只有久未露面的会话才真去 fork 一次 has-session。
# 判定「会话是否已结束」不走这条缓存，见 liveness.is_alive 的 max_age 说明。
_ALIVE_EVIDENCE_TTL = 3.0
# 选择跟随的节流窗口（秒）。单次方向键立即生效（无额外延迟），连按时窗口内只
# 保留最后一次——否则连按 N 下就实打实重建 N 次右栏，每次约 180ms。
_FOLLOW_THROTTLE = 0.12
# 首屏画完到开始预热全文搜索索引的间隔（秒）。见 _schedule_search_index_warm。
_SEARCH_INDEX_WARM_DELAY = 1.5


def is_external_running(session: dict) -> bool:
    """会话在本机跑着，但不在 corral 的托管终端里——通常是用户自己开窗口起的。

    这种会话拿不到实时画面：画面只存在于那个窗口自己的终端连接里，事后没法从
    外部接管过来（macOS 上连 reptyr 那条 ptrace 路子都不存在）。右栏只能给对话
    内容，并如实说明原因；在这里"打开"它只会另起一个恢复进程，必须先问用户。
    """
    return bool(session.get("live")) and not session.get("keepalive_name")


def _status_key(session: dict) -> str:
    """详情头的状态文案键：托管中 / 在别的窗口跑 / 已结束。"""
    if session.get("keepalive_name"):
        return "status.running_hosted"
    if session.get("live"):
        return "status.running_external"
    return "status.ended"


def _attention_key(session: dict) -> str:
    """详情头的可访问关注状态文案键；颜色圆点不是唯一信息来源。"""
    kind = str(session.get("attention_kind") or "none")
    if kind in {"waiting", "working", "unread"}:
        return f"attention.{kind}"
    return "attention.none"


def _filter_looks_like_osc_leak(value: str) -> bool:
    """搜索框内容是否像外层终端 OSC 10/11 应答泄漏（含 ESC 或 rgb: 片段）。

    探测结束后才迟到的应答会被 Textual 当键盘输入灌进有焦点的 Input；
    这种垃圾永远不该成为项目筛选条件。
    """
    if not value:
        return False
    return "\x1b" in value or "rgb:" in value


def _drop_layout_sessions(store, keys: list[str]) -> None:
    """把一批会话从侧边栏记忆里摘掉（成员不足两个时组会自动解散）。

    写成模块级函数而不是内联 lambda：`_apply_layout_change` 收到的这个回调会在
    记忆库事务里对着**最新**快照重放，一次调用摘一批比逐个开事务省得多。
    """
    for key in keys:
        store.remove_session(key)


# 动作名 → 文案 key；实例化时只改 description，不能整表替换（会丢掉 ListView/Screen 继承绑键）
_ACTION_I18N = {
    "search_content": "action.search",
    "toggle_pin": "action.toggle_pin",
    "handoff": "action.advanced",
    "kill_keepalive": "action.kill_session",
    "delete_session": "action.delete_session",
    "close_pane": "action.close_pane",
    "focus_list": "action.focus_list",
    "toggle_sidebar": "action.toggle_sidebar",
    "toggle_hud": "action.toggle_hud",
    "save_screenshot": "action.screenshot",
    "preview_home": "action.preview_home",
    "preview_end": "action.preview_end",
    "preview_page_up": "action.preview_page_up",
    "preview_page_down": "action.preview_page_down",
    "board_prev": "action.board_prev",
    "board_next": "action.board_next",
    "quit_app": "action.quit",
}


# 只在「焦点还在侧边栏」时才成立的动作：右栏实时终端持有输入时，这些键要么
# 本就到不了（EmbedPane 先 stop 掉），要么会把用户想打给助手的内容截胡。
_LIST_ONLY_ACTIONS = frozenset(
    {
        # 会话小窗的展开/收起：右栏实时格持有输入时让路给助手。小窗本来就是"扫一眼"
        # 用的，不值得为它从助手手里抢一个组合键；那种场景下点一下小窗本身即可
        # （点浮层不改焦点，见 `SessionHud`）。
        "toggle_hud",
        "handoff",
        "kill_keepalive",
        "delete_session",
        "close_pane",
        "quit_app",
        "preview_home",
        "preview_end",
        "preview_page_up",
        "preview_page_down",
        "board_prev",
        "board_next",
    }
)


def _main_bindings() -> list[Binding]:
    """按当前语言生成底部快捷键说明。"""
    return [
        # Ctrl+F / Ctrl+P 是壳层全局键。priority 让它们先于当前聚焦控件处理，
        # 运行中助手不能再截走；临时弹窗不继承主屏绑定，仍保持自己的输入语义。
        Binding("ctrl+f", "search_content", t("action.search"), priority=True),
        Binding("ctrl+p", "toggle_pin", t("action.toggle_pin"), priority=True),
        Binding("a", "handoff", t("action.advanced")),
        Binding("q", "kill_keepalive", t("action.kill_session")),
        Binding("x", "delete_session", t("action.delete_session")),
        Binding("c", "close_pane", t("action.close_pane"), show=False),
        # 内嵌终端持有输入时的唯一出口。EmbedPane 自己会先吃掉这个键（实时会话
        # 路径），这里的绑定负责两件事：静态预览格聚焦时也能回列表，以及让
        # Footer 在右栏持有输入时把出口显示出来（见 check_action）。
        Binding("ctrl+backslash", "focus_list", t("action.focus_list")),
        # 与 Ctrl+\ 同级的壳层键：右栏持焦时仍可用，不得进 _LIST_ONLY_ACTIONS。
        # EmbedPane 实时路径会先拦截 ctrl+shift+b，避免键被转发给托管会话。
        # 不用 Ctrl+B：机主在 Claude Code 里用它「把任务转后台」（2026-08-04 冲突实报）。
        Binding("ctrl+shift+b", "toggle_sidebar", t("action.toggle_sidebar")),
        # 会话小窗展开/收起。Footer 已经很挤，这个键不展示；小窗自身可点。
        Binding("ctrl+g", "toggle_hud", t("action.toggle_hud"), show=False),
        Binding("f12", "save_screenshot", t("action.screenshot"), show=False),
        # 右栏静态对话预览滚动（列表聚焦时也生效；优先级高于 ListView 的同名键）
        Binding("home", "preview_home", t("action.preview_home"), show=False, priority=True),
        Binding("end", "preview_end", t("action.preview_end"), show=False, priority=True),
        Binding("pageup", "preview_page_up", t("action.preview_page_up"), show=False, priority=True),
        Binding("pagedown", "preview_page_down", t("action.preview_page_down"), show=False, priority=True),
        Binding("left_square_bracket", "board_prev", t("action.board_prev"), show=False),
        Binding("right_square_bracket", "board_next", t("action.board_next"), show=False),
        Binding("escape", "quit_app", t("action.quit")),
        # 不再单独绑 ctrl+c 退出：Textual 的 Screen 基类自带 ctrl+c -> copy_text。
        # 划词抬起已由 on_text_selected 自动复制；Ctrl+C 仍作手动再复制/无选区时
        # EmbedPane 转发给托管会话中断。子类 BINDINGS 重复 ctrl+c 会盖掉基类复制绑定。
        # Esc 是文档化的主退出键。
    ]


def _localize_binding_descriptions(node) -> None:
    """就地刷新已合并绑键的 description，保留继承来的 up/down/enter 等。"""
    for key, bindings in list(node._bindings.key_to_bindings.items()):
        node._bindings.key_to_bindings[key] = [
            dataclasses.replace(b, description=t(_ACTION_I18N[b.action]))
            if b.action in _ACTION_I18N
            else b
            for b in bindings
        ]


class MainScreen(
    Screen,
    LayoutControllerMixin,
    AttentionReaderMixin,
    BoardControllerMixin,
    HostControllerMixin,
    HudControllerMixin,
    UpdateControllerMixin,
):
    BINDINGS = _main_bindings()

    def __init__(self, store, embed_ok: bool, direct=None, osc_report: bytes | None = None) -> None:
        super().__init__()
        _localize_binding_descriptions(self)
        self.store = store
        self.embed_ok = embed_ok
        self.direct = direct
        self.osc_report = osc_report
        runtime_ids = store.registry.ids
        source = next((rid for rid in runtime_ids if store.sessions[rid]), runtime_ids[0])
        self.nav = NavState(source=source)
        self._host_pending = 0
        self._preview_gen = 0
        # 后台重扫、标题刷新和交互动作都可能要求重建列表；Textual 的异步
        # clear/extend 不能并发，否则会重复挂载同一个「新建会话」条目。
        self._rebuild_lock = asyncio.Lock()
        from corral import split_layout

        # 侧边栏记忆：`_layout_db` 是唯一写入口（事务内重读最新再叠加，多窗口不互相覆盖），
        # `_split_store` 只是给界面渲染用的本地快照，靠 revision 轮询跟上别的窗口。
        self._layout_db = split_layout.default_layout_db()
        self._split_store = self._layout_db.read()
        self._layout_revision = self._split_store.revision
        # 有右栏时才允许藏侧栏；无右栏（纯列表）藏了无处可点回来。
        self.sidebar_visible = (
            True if not embed_ok else ui_prefs.load_sidebar_visible(default=True)
        )
        # 选择跟随的节流状态，见 _schedule_follow_selection
        self._follow_timer = None
        self._follow_last_run = 0.0
        # 静态详情 renderer 的内容签名缓存：同一份内容（会话键+mtime+标题+
        # 关注态都未变）复用同一个闭包，EmbedPane 才能靠 renderer 身份命中
        # 全文排版 LRU，A->B->A 切回与无变化重扫都零重排（见 _detail_renderer_for）。
        self._detail_renderers: OrderedDict[tuple, Callable[[], object]] = OrderedDict()
        # 尾部优先渲染状态：_preview_tail 是当前处于尾部模式的会话键，
        # _preview_full_done 是已升级过全文、下次直接整篇的会话键。
        self._preview_tail: set[str] = set()
        self._preview_full_done: set[str] = set()
        # 关注已读判定：观察右侧可见的红点会话（分屏下所有可见格一起），
        # 内容真实就绪即清；切换、失焦会取消观察。
        self._attention_read_timer = None
        self._attention_read_keys: set[str] = set()
        self._app_focused = True
        self._update_channel: str | None = None
        self._update_latest: str | None = None
        # 全文搜索索引：首屏卡片画完后再延后预热，Ctrl+F 打开弹窗时通常已就绪。
        self._search_index = None
        self._search_warm_scheduled = False
        # 右上角会话小窗：展开状态全局共用一份（切格不该让它一会儿开一会儿关），
        # 最近一次算好的摘要按会话键留着，历史文件正在被写、缓存暂时失效时继续
        # 显示旧摘要，避免小窗一秒一闪。
        # 会话提问概览默认展开，让用户进入会话时直接看到上下文；仍可随时收起。
        # 每个实时托管格各自画一份（不再只画激活格）。
        self._hud_expanded = True
        self._hud_cache: dict[str, object] = {}
        self._hud_warm_at: dict[str, float] = {}
        # 静态详情预览的续温节流表（会话写入期按 _PREVIEW_WARM_INTERVAL 重解析）。
        self._preview_warm_at: dict[str, float] = {}
        self._activity_board = ActivityBoard()
        self._activity_board_active = False
        self._shell_after_board = False

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="list-pane"):
                yield Input(placeholder=t("filter.placeholder"), id="project-search")
                yield SessionListView(
                    self.store,
                    self.nav,
                    group_store=self._split_store,
                    on_layout_change=self._apply_layout_change,
                    id="session-list",
                )
            if self.embed_ok:
                yield SplitPaneArea(
                    self.store,
                    on_runtime_pick=self._on_runtime_pick,
                    on_shell_pick=self._on_shell_pick,
                    on_dragon_click=self._play_dragon,
                    on_pane_close=self._on_pane_close,
                    on_focus_list=self._focus_list,
                    on_pane_focused=self._on_pane_focused,
                    on_pane_restart=self._restart_session_from_pane,
                    on_hud_toggle=self.action_toggle_hud,
                    osc_report=self.osc_report,
                    sidebar_visible=self.sidebar_visible,
                    id="split-pane-area",
                )
        yield UpdateToast(
            on_update=self._on_update_toast_update,
            on_restart=self._on_update_toast_restart,
            on_retry=self._on_update_toast_retry,
            on_dismiss=self._on_update_toast_dismiss,
            id="update-toast",
        )
        yield DragonOverlay(id="dragon-overlay")
        yield CorralFooter()
        # 启动加载占位屏：首扫未完成且没有秒开快照时，整屏铺灰度龙 + 居中
        # Logo，遮住还没内容的骨架 UI；扫描完成由 _on_initial_load_done 摘除。
        # 直启子命令不铺：用户就是冲着新会话来的，尽快进入托管流程。
        if self.direct is None and not self.store.loaded and not self.store.hydrated:
            yield DragonSplash(t("split.empty_hint"), fullscreen=True, id="boot-splash")

    def on_mount(self) -> None:
        self._app_focused = bool(self.app.app_focus)
        self.watch(self.app, "app_focus", self._on_app_focus_changed, init=False)
        if self.embed_ok:
            self._apply_sidebar_visible(persist=False)
        self._update_header()
        # store 可能已经 load() 过（如 _dispatch_direct_launch、测试里的 _make_store
        # 都是同步预加载好再传进来），也可能还没有（main() 现在把 load() 挪到后台
        # 线程异步跑，UI 先渲染骨架）。已加载时直接进入后台重扫循环；未加载时先挂
        # 一个 worker 等它跑完，完成后再启动后台重扫，避免两者并发调用同一个
        # registry.scan_all()（RuntimeRegistry 的廉价预检缓存不是线程安全的）。
        if self.store.loaded:
            self._start_background_refresh()
            # 必须等首帧 refresh 之后再开始倒计时：从 on_mount 起算墙钟，
            # 首屏本身一慢（真机高负载 / Pilot）预热就会撞上出卡片。
            self.call_after_refresh(self._schedule_search_index_warm)
        else:
            self._await_initial_load()
        self.set_interval(CACHE_POLL_INTERVAL, self._poll_cache)
        # 侧边栏记忆是多窗口共享的，别的 corral 窗口改了置顶/分组要能自动跟上。
        self.set_interval(LAYOUT_POLL_INTERVAL, self._poll_layout_state)
        if self.embed_ok:
            self.set_interval(HUD_POLL_INTERVAL, self._sync_hud)
            # 分屏标记以显式调用为主（关格/换焦点/重建列表即时生效），这条定时
            # 同步只做兜底：任何没覆盖到的右栏变动，最迟一秒后也会对齐。
            self.set_interval(HUD_POLL_INTERVAL, self._sync_split_marks)
        self._check_for_update()
        if self.direct is not None:
            # 直启子命令：焦点最终要落在内嵌面板上（用户就是来操作新会话的）。
            # 不要先调 SessionListView.focus()——它走 call_later，会在托管完成后
            # 把焦点抢回列表（真机冒烟回归过）。
            # 托管完成前也绝不能让默认焦点落在搜索框：Textual 会把第一个可聚焦
            # 控件（#project-search）当作焦点，探测结束后才迟到的 OSC 应答会被当
            # 键盘输入灌进筛选框，把侧边栏滤空（`corral cursor` 等直启真机复现）。
            search = self.query_one("#project-search", Input)
            search.can_focus = False
            self._host_direct_launch()
        else:
            self.query_one(SessionListView).focus()
            self.call_after_refresh(self._follow_current_selection)
            # store 已同步加载时（测试 / 直启预扫）可立即恢复；异步首扫路径改到
            # `_rebuild_and_follow` 末尾，避免扫描完成前 prune+save 清空磁盘记忆。
            if self.store.loaded:
                self.call_after_refresh(self._try_restore_startup_layout)
        # 真实终端启动后静默补齐 Cursor 观察配置。Pilot/截图等无终端测试不应
        # 改写运行测试者的用户配置；测试可直接调用该后台 worker 验证接线。
        if not self.app.is_headless:
            self._install_cursor_observer()

    @work(thread=True, exclusive=True, group="cursor-observer-install")
    def _install_cursor_observer(self) -> None:
        """后台幂等安装 Cursor 观察条目；失败不影响首屏与会话操作。"""
        try:
            from corral import cursor_observer

            cursor_observer.install()
        except Exception:
            return

    def _split_area(self) -> SplitPaneArea:
        return self.query_one(SplitPaneArea)

    def update_terminal_background(self, osc_report: bytes) -> None:
        """同步运行中终端的新背景，供现有面板和后续托管会话共同使用。"""
        self.osc_report = osc_report
        if self.embed_ok:
            self._split_area().update_terminal_background(osc_report)

    def _session_is_active(self, session: dict) -> bool:
        """单条扫描快照是否仍活跃（托管 tmux 存活或扫描器报 live）。

        本进程 `store.hosted` 仍登记时优先相信托管身份，避免单次
        `has-session` 超时假阴性把分屏组拆掉再 remount。

        判活顺序必须先走 hosted / live：二者都是内存字段，开屏和方向键跟随
        会对每个组成员调用这里。先问 `is_alive` 会在证据缓存未命中时同步
        fork `has-session`（约 5ms × 格数），而结果就算失败也仍会落到 hosted/live。
        `is_alive(..., max_age=)` 只留给「扫描器还没标 live、本进程也没登记」的
        兜底；抓帧死亡宣告仍走不带缓存的那条路径。
        """
        import corral
        from corral import liveness

        key = corral.session_key(session)
        if self.store.hosted.get(key):
            return True
        if session.get("live"):
            return True
        kname = session.get("keepalive_name")
        if kname and liveness.is_alive(str(kname), max_age=_ALIVE_EVIDENCE_TTL):
            return True
        return False

    def _is_session_active(self, key: str) -> bool:
        from corral import liveness

        session = self.store.find_session(key)
        if session is not None and self._session_is_active(session):
            return True
        hosted = self.store.hosted.get(key)
        if hosted:
            # 本进程仍登记托管：优先相信，不要求当次 is_alive（高负载假阴性）。
            return True
        # 占位→真实或重扫替换 dict 后，分屏格仍绑着 keepalive；以 tmux 为准。
        try:
            area = self._split_area()
        except Exception:
            return False
        checked: set[str] = set()
        for spec in area.pane_specs():
            if spec.session_key != key or not spec.keepalive_name:
                continue
            if spec.keepalive_name in checked:
                continue
            checked.add(spec.keepalive_name)
            if liveness.is_alive(spec.keepalive_name, max_age=_ALIVE_EVIDENCE_TTL):
                return True
        return False

    def _play_dragon(self) -> None:
        try:
            self.query_one("#dragon-overlay", DragonOverlay).play()
        except Exception:
            pass

    def _on_runtime_pick(self, runtime_id: str) -> None:
        import corral
        from corral.split_layout import MAX_PANES

        area = self._split_area()
        if not self._board_skips_split_cap() and not area.can_add_pane():
            self.notify(t("split.full", n=MAX_PANES))
            self.app.bell()
            return
        session_list = self.query_one(SessionListView)
        session = session_list.selected_session()
        cwd = area.current_project or corral.usable_cwd(
            corral._new_session_cwd(self.store, self.nav, session)
        )
        if cwd is None:
            self.notify(t("split.no_project"))
            self.app.bell()
            return
        request = corral.NewSessionRequest(runtime_id, cwd)
        self._embed_open(request, add_pane=True)

    def _on_shell_pick(self) -> None:
        import corral
        from corral.split_layout import MAX_PANES

        leaving_board = self._board_skips_split_cap()
        if leaving_board:
            if self._host_pending > 0:
                self.app.bell()
                return
            self._shell_after_board = True
            self._leave_activity_board_to_first_session()
        area = self._split_area()
        if not leaving_board and not area.can_add_pane():
            self.notify(t("split.full", n=MAX_PANES))
            self.app.bell()
            return
        session_list = self.query_one(SessionListView)
        session = session_list.selected_session()
        cwd = area.current_project or corral.usable_cwd(
            corral._new_session_cwd(self.store, self.nav, session)
        )
        if cwd is None:
            self._shell_after_board = False
            self.notify(t("split.no_project"))
            self.app.bell()
            return
        self._embed_open_shell(cwd)

    def _cleanup_shell_pane(self, session_key: str) -> None:
        """关 shell 分屏或 shell 进程退出：结束 tmux 会话并摘掉占位，避免堆积。"""
        from corral.models import SHELL_RUNTIME_ID, is_shell_session

        if not session_key.startswith(f"{SHELL_RUNTIME_ID}:"):
            return
        session = self.store.find_session(session_key)
        if session is not None and not is_shell_session(session):
            return
        keepalive_name = None
        if session is not None:
            keepalive_name = session.get("keepalive_name")
        if not keepalive_name:
            area = self._split_area()
            for spec in area.pane_specs():
                if spec.session_key == session_key:
                    keepalive_name = spec.keepalive_name
                    break
        if keepalive_name:
            from corral import embed, keepalive

            keepalive.kill(str(keepalive_name))
            embed.close_channel(str(keepalive_name))
        self.store.remove_session(session_key)

    def _on_pane_close(self, session_key: str) -> None:
        self._cleanup_shell_pane(session_key)
        LayoutControllerMixin._on_pane_close(self, session_key)


    # ---- 首屏异步加载：main() 把 store.load() 挪到后台线程异步跑，这里等它跑完
    # 再渲染真实列表（骨架已经在 compose() 时就显示出来了：空列表 + "＋ 新建会话"） ----

    @work(thread=True, group="initial-load")
    def _await_initial_load(self) -> None:
        worker = get_current_worker()
        # 短超时轮询让 Screen 卸载/测试退出时能及时响应 worker.cancel()，不能永久
        # 卡在一次无期限 Event.wait() 里拖住 run_test 或真实应用退出。
        while not worker.is_cancelled:
            if self.store.wait_loaded(timeout=0.1):
                if not worker.is_cancelled:
                    self.app.call_from_thread(self._on_initial_load_done)
                return

    def _on_initial_load_done(self) -> None:
        # 加载占位屏退场：先摘全屏龙屏，再重建列表，保证用户看到的第一帧
        # 正常界面不带占位残留。
        self.call_next(self._rebuild_after_boot_splash)

    async def _rebuild_after_boot_splash(self) -> None:
        splash = self.query("#boot-splash")
        if splash:
            await splash.remove()
        # __init__ 时 store 还没扫完，默认来源只能先假定成 registry 里的第一个；
        # 扫完后如果它其实没有会话而别的运行时有，重新选一次，跟 __init__ 里
        # "挑第一个有会话的运行时"这条默认选择逻辑保持一致。
        if not self.store.sessions.get(self.nav.source):
            alt = next(
                (rid for rid in self.store.registry.ids if self.store.sessions[rid]), None,
            )
            if alt is not None:
                self.nav.source = alt
        await self._rebuild_and_follow()
        self._start_background_refresh()

    async def _rebuild_and_follow(self) -> None:
        await self._rebuild_list()
        self._try_restore_startup_layout()
        self._schedule_search_index_warm()

    # ---- 后台重扫：Textual worker（取代旧版裸 threading.Thread + 0.5s dirty 轮询），
    # 发现变化直接 call_from_thread 触发重建，不再有轮询延迟；连续空闲多轮后自适应
    # 拉长扫描间隔，省磁盘/CPU；任何异常都要捕获且继续循环，不能让后台线程静默死掉 ----

    def _start_background_refresh(self) -> None:
        self._background_refresh_worker()

    @work(thread=True, exclusive=True, group="session-refresh")
    def _background_refresh_worker(self) -> None:
        import corral

        worker = get_current_worker()
        interval = REFRESH_INTERVAL
        idle_rounds = 0
        while not worker.is_cancelled:
            # cancelled_event.wait() 同时承担定时器和取消唤醒；Screen 一退出便立即
            # 返回，不再被 time.sleep(10) 拖住。
            if worker.cancelled_event.wait(interval):
                return
            had_error = self.store.get_load_error() is not None
            try:
                changed = self.store.refresh()
            except Exception as exc:  # 全异常兜底：只捕获 OSError 曾经让这个线程
                # 遇到未预料异常（如扫描器 bug）就静默死掉，此后列表再也不会更新
                # 且没有任何提示；模式与 ui/embed_pane.py 的 _capture_loop 一致，
                # 复用同一个错误日志，写文件留证并继续循环，而不是让线程退出。
                corral._log_embed_error("后台会话重扫线程", exc)
                idle_rounds = 0
                interval = REFRESH_INTERVAL
                if not worker.is_cancelled:
                    self.app.call_from_thread(self._update_header)
                continue
            if worker.is_cancelled:
                return
            recovered = had_error and self.store.get_load_error() is None
            if changed:
                idle_rounds = 0
                interval = REFRESH_INTERVAL
                self.app.call_from_thread(self._rebuild_list)
            else:
                if recovered:
                    self.app.call_from_thread(self._update_header)
                idle_rounds += 1
                if idle_rounds >= _IDLE_ROUNDS_BEFORE_BACKOFF:
                    interval = min(REFRESH_INTERVAL_MAX, interval * 2)

    def _poll_cache(self) -> None:
        """标题缓存文件轮询：比会话重扫轻得多（只 stat 一个文件），保持独立的
        高频轮询；命中变化时复用同一个 store.dirty 事件当"待重建"标志。"""
        self.store.poll_cache_updates()
        if self.store.dirty.is_set():
            self.store.dirty.clear()
            self.call_next(self._rebuild_list)
        self._sustain_preview_warm()

    # 详情预览在会话活跃写入期的续温节流间隔：会话每写一条历史，缓存版本就
    # 作废一次；对正在看的预览会话按此间隔在后台重新解析，让右栏跟着会话走，
    # 而不是一路停留在「正在读取对话内容…」空态。
    _PREVIEW_WARM_INTERVAL = 2.0

    # 长对话尾部优先渲染：消息数超过下限时先只排版最近 _PREVIEW_TAIL_MESSAGES
    # 条（首次上屏从数百毫秒降到百毫秒内），首帧后 _PREVIEW_FULL_UPGRADE_DELAY
    # 秒自动升级为整篇排版。钉底语义保证升级瞬间可见画面不跳（只是上方多出
    # 更早的消息）。已升级过（或全文排版已在缓存里）的会话不再走尾部模式。
    _PREVIEW_TAIL_MIN_MESSAGES = 80
    _PREVIEW_TAIL_MESSAGES = 40
    _PREVIEW_FULL_UPGRADE_DELAY = 0.2

    def _sustain_preview_warm(self) -> None:
        """当前右侧静态详情预览的会话若缓存已失效（会话正在写入），节流地后台
        重新解析；其余场景（托管格、无预览）不动作。"""
        if not self.embed_ok:
            return
        try:
            area = self._split_area()
        except Exception:
            return
        import time as _time

        now = _time.monotonic()
        for spec in area.pane_specs():
            if spec.keepalive_name:
                continue
            session = self.store.find_session(spec.session_key)
            if session is None:
                continue
            if self.store.peek_conversation(session) is not None:
                continue
            # 首次（从未解析过）立即重解析；之后按节流间隔限频。
            if now - self._preview_warm_at.get(spec.session_key, float("-inf")) < self._PREVIEW_WARM_INTERVAL:
                continue
            self._preview_warm_at[spec.session_key] = now
            self._warm_conversation(session, self._preview_gen)

    async def _rebuild_list(self, select_key: str | None = None) -> None:
        async with self._rebuild_lock:
            session_list = self.query_one(SessionListView)
            migrated: dict[str, str] = {}
            if self.embed_ok and self.store.loaded:
                migrated = self._reconcile_split_session_keys()
            if select_key is None:
                selected_key = session_list._displayed_selected_key()
                select_key = migrated.get(selected_key) if selected_key else None
            else:
                select_key = migrated.get(select_key, select_key)
            await session_list.rebuild(select_key=select_key)
            self._update_header()
            self._sync_activity_board_entry()
            if self.embed_ok:
                # 仅刷新当前可见预览格，避免每次重扫都把 Cursor store.db 预览整页重载。
                self._split_area().invalidate_visible_previews()
                self._schedule_follow_selection()
                # 后台重扫带来的新红点（含分屏中非聚焦格）也要立即纳入观察：
                # 用户正看着这些格，画面就绪即清，不能等下一次交互。
                self.call_next(self._begin_attention_read)

    def _update_header(self) -> None:
        """刷新搜索框占位文案与「有筛选」高亮：空查询时展示命中数；出错/无会话时给出原因。"""
        session_list = self.query_one(SessionListView)
        search = self.query_one("#project-search", Input)
        count = len(session_list.visible_sessions())
        load_error = self.store.get_load_error()
        active = bool(self.nav.project_query.strip())
        # 关键字非空就贴 -active：失焦也不退回灰底，用户才看得出列表为什么变少。
        search.set_class(active, "-active")
        # 首屏扫描已经跑完（store.loaded）且全部运行时都没扫到任何会话时，给出
        # 友好提示，而不是让用户面对一个永远空白、原因不明的列表——旧版是在 main()
        # 里同步扫完就直接打印错误退出，扫描挪到后台 worker 后这个判断只能挪到这里，
        # 扫描没跑完之前（store.loaded 为 False）不能误判为"确实没有会话"。
        if load_error:
            search.placeholder = t("filter.load_error", error=load_error)
        elif self.store.loaded and count == 0 and not any(self.store.sessions.values()):
            names = i18n.join_names(
                [runtime.display_name for runtime in self.store.registry]
            )
            search.placeholder = t("filter.no_sessions", names=names)
        elif active:
            search.placeholder = t("filter.placeholder_count_active", count=count)
        else:
            search.placeholder = t("filter.placeholder_count", count=count)

    # ---- 选择跟随：右栏默认展示左栏当前选中项 ----

    def on_list_view_highlighted(self, event) -> None:
        # 高亮一变就立刻停止观察上一条会话，不能等 120ms 的右栏跟随节流结束，
        # 否则快速掠过时旧红点可能在这段空窗里被误清。
        self._cancel_attention_read()
        self._schedule_follow_selection()

    def _schedule_follow_selection(self) -> None:
        """节流版选择跟随：首次立即执行，窗口内的连续高亮只保留最后一次。

        用 leading-edge 而不是纯 debounce：纯 debounce 会给「按一下方向键」也加上
        固定延迟，单步操作反而更迟钝；这里单步零延迟，只有连按才合并。
        """
        import time as _time

        now = _time.monotonic()
        if self._follow_timer is None and now - self._follow_last_run >= _FOLLOW_THROTTLE:
            self._run_follow_selection()
            return
        if self._follow_timer is not None:
            self._follow_timer.stop()
        # 下限不能是 0：Textual 的 Timer 用间隔做除法，interval=0 会在停表时抛
        # ZeroDivisionError 把整个屏幕卸载流程带崩。
        delay = max(0.01, _FOLLOW_THROTTLE - (now - self._follow_last_run))
        self._follow_timer = self.set_timer(delay, self._run_follow_selection)

    def _run_follow_selection(self) -> None:
        import time as _time

        self._follow_timer = None
        self._follow_last_run = _time.monotonic()
        self._follow_current_selection()
        # 跟随可能把右栏从分屏换成单格预览（反之亦然），底色标记跟着走一遍。
        self._sync_split_marks()

    def on_unmount(self) -> None:
        # 待触发的节流定时器不能活过屏幕本身，否则回调会打到已卸载的控件树上。
        if self._follow_timer is not None:
            self._follow_timer.stop()
            self._follow_timer = None
        self._cancel_attention_read()


    def _follow_current_selection(self) -> None:
        if not self.embed_ok:
            return
        session_list = self.query_one(SessionListView)
        if session_list.multi_count() > 0:
            return
        area = self._split_area()
        if session_list.is_activity_board_selected():
            self._show_activity_board(focus_pane=False)
            return
        was_board = getattr(self, "_activity_board_active", False)
        self._leave_activity_board()
        if area.any_embed_focused() and not was_board:
            return
        if session_list.is_new_session_selected():
            # 已在新建提示格时勿重复挂载，否则 remount 会抢走列表焦点
            if area.ordered_session_keys() == ["__hint__"]:
                return
            area.show_new_session_hint()
            return
        group = session_list.selected_group()
        if group is not None:
            focus_key = (
                group.focus_key
                if group.focus_key in group.session_keys
                else group.session_keys[0]
            )
            self._show_session_group(focus_key, include_inactive=True)
            return
        session = session_list.selected_session()
        if session is None:
            return
        import corral

        key = corral.session_key(session)
        if self._split_store.get_group(key) is not None:
            self._show_session_group(key, include_inactive=True)
            return
        # 托管成功后 store 会先写入 keepalive，列表卡片要到下一次异步重建才
        # 换成新 dict。此间若旧高亮事件到达，必须以 store 的最新快照为准；
        # 否则旧卡会被误判成静态会话，把刚挂上的实时终端盖回预览。
        session = self.store.find_session(key) or session
        kname = session.get("keepalive_name")
        if kname or session.get("live"):
            # 当前右侧已是该组合时仍走 show_hosted_group：内部按有序
            # (session_key, keepalive) 身份就地更新，禁止整排 remount。
            from corral import split_layout

            project, keys = split_layout.resolve_active_group(
                self._split_store,
                key,
                is_active=self._is_session_active,
                find_session=self.store.find_session,
            )
            entries = self._build_hosted_entries(keys)
            if not entries:
                return
            target_identity = [
                (corral.session_key(s), kn) for s, kn, _ in entries
            ]
            if (
                area.hosted_identity() == target_identity
                and key in {k for k, _ in target_identity}
            ):
                area.show_hosted_group(project, entries, focus_key=key)
                self._begin_attention_read(key)
                return
            area.show_hosted_group(project, entries, focus_key=key)
            self._persist_split_composition()
            self._begin_attention_read(key)
            return
        # 已在单格预览同一会话：只失效缓存并重新暖加载，避免 remount 抢焦点
        if area.ordered_session_keys() == [key] and not any(
            p.keepalive_name for p in area.pane_specs()
        ):
            self._preview_gen += 1
            self._warm_conversation(session, self._preview_gen)
            area.invalidate_all_details()
            self._begin_attention_read(key)
            return
        self._preview_gen += 1
        self._warm_conversation(session, self._preview_gen)
        area.show_single_preview(session, self._detail_renderer_for(session))
        self._begin_attention_read(key)

    def _detail_header(self, session: dict) -> Text:
        import corral
        from corral.models import is_shell_session

        title = self.store.get_title(session)
        status = t(_status_key(session))
        attention = t(_attention_key(session))
        project = str(
            session.get("cwd") or session.get("cwd_display") or t("project.unknown")
        )
        out = Text(title, style="bold")
        out.append("\n")
        if is_shell_session(session):
            # 终端 pane 不挂任何运行时；详情头直接标「终端」，不查注册表。
            out.append(t("shell.pane_title"), style="dim")
        else:
            runtime = self.store.registry.get(str(session.get("source") or ""))
            out.append(runtime.display_name, style=corral.runtime_label_style(runtime.id))
        out.append(f" · {status}", style="dim")
        out.append(f" · {attention}", style="dim")
        out.append("\n" + project, style="dim")
        # 在别的窗口跑的会话右栏永远只有静态对话，不说明原因就会被当成"会话已断"。
        if is_external_running(session):
            out.append("\n" + t("detail.running_external"), style="#B8860B")
        elif not session.get("keepalive_name") and not session.get("provisional"):
            # 已结束会话默认只给历史预览，重启没有任何可见入口——用户会以为
            # 这条会话再也接不上了。把回车这条路写在头上。
            out.append("\n" + t("detail.restart_hint"), style="dim")
        return out

    # 静态详情 renderer 缓存上限：覆盖最近看过的几个会话，超过就淘汰最旧的。
    # renderer 本体被 EmbedPane 的布局 LRU 键强引用，这里淘汰不会导致 id 复用。
    _DETAIL_RENDERER_CACHE_MAX = 12

    def _detail_signature(self, session: dict) -> tuple:
        """渲染静态详情所需的全部外部输入的轻量签名。

        `_render_detail` 读什么，这里就得囊括什么：详情头（标题/状态/关注态）、
        项目路径、对话正文（按 mtime 失效）。签名变了就是新 renderer、新排版；
        签名没变则复用同一个 renderer，EmbedPane 靠它的身份命中全文排版 LRU。
        """
        import corral

        return (
            corral.session_key(session),
            session.get("mtime"),
            session.get("size_bytes"),
            bool(session.get("live")),
            session.get("keepalive_name"),
            session.get("attention_kind"),
            session.get("attention_updated_at"),
            self.store.get_title(session),
        )

    def _detail_renderer_for(self, session: dict) -> Callable[[], object]:
        """按内容签名取稳定的静态详情 renderer。

        以前每次选择跟随都新建 `lambda s=session: ...`，EmbedPane 的全文排版
        缓存键是 renderer 身份，闭包一换缓存必失效——切回刚看过的会话也要把
        整篇对话重新排版（百消息量级数百毫秒，主线程阻塞）。改成同一份内容
        复用同一闭包后，切回/无变化重扫都能命中已有排版。内容真正变化时
        签名变、新闭包、自然重排，不需要额外的失效路径。
        """
        signature = self._detail_signature(session)
        renderer = self._detail_renderers.get(signature)
        if renderer is None:
            key = signature[0]

            def _render(_key=key):
                # 闭包不捕获 session dict（后台重扫会换对象），渲染时按稳定键
                # 现查 store 最新快照。
                current = self.store.find_session(_key)
                if current is None:
                    return Text(t("detail.loading_preview"), style="dim")
                return self._render_detail(current)

            renderer = _render
            self._detail_renderers[signature] = renderer
            while len(self._detail_renderers) > self._DETAIL_RENDERER_CACHE_MAX:
                self._detail_renderers.popitem(last=False)
        else:
            self._detail_renderers.move_to_end(signature)
        return renderer

    def _render_detail(self, session: dict):
        """右栏静态预览的整篇内容：详情头 + 逐条消息（角色抬头 + Markdown 正文）。

        返回的是 Rich 可渲染对象（`Group`），不是 `Text`——正文要按 Markdown 排版，
        排版结果没法塞回单个 `Text`。`EmbedPane` 那条 Visual→Strip 管线本来就接受
        任意 Rich 可渲染对象，不需要为此特判。
        """
        from rich.console import Group

        import corral

        # 详情 renderer 会被 EmbedPane 缓存并延后调用；后台重扫后闭包捕获的 dict
        # 已不是 Store 当前对象，必须每次按稳定会话键重新解析最新快照。
        session = self.store.find_session(corral.session_key(session)) or session
        head = self._detail_header(session)
        messages = self.store.peek_conversation(session, stale_ok=True)
        if messages is None:
            # 从未加载过时才显示占位（加载在 _warm_conversation 里异步补齐）；
            # 已加载过但刚被会话自身写入作废的，继续渲染旧内容直到后台重新解析
            # 完成（会话活跃期不会再闪回「正在读取对话内容…」空态）。
            return Group(head, Text(t("detail.loading_preview"), style="dim"))
        runtime = self.store.registry.get(str(session.get("source") or ""))
        width = self._preview_width(corral.session_key(session))
        blocks = corral._preview_blocks(
            messages,
            runtime.display_name,
            width,
            assistant_style=corral.runtime_label_style(runtime.id),
        )
        return Group(head, Text(""), *blocks)

    def _preview_width(self, session_key: str) -> int:
        """这条会话的预览该按多少列排版：优先用它自己那一格的宽度。

        Markdown 正文是预排好的（不会再被上层重新折行），拿错宽度就会出现「分隔线
        和格子对不上、正文早折或超宽」。取不到时退回第一格，再退回 40。
        """
        try:
            area = self._split_area()
            width = area.pane_width_for(session_key)
            if width is None:
                cells = area.cells()
                width = (cells[0].embed_pane().size.width if cells else 0) or 40
        except Exception:
            width = 40
        return max(20, width - 2)

    @work(thread=True)
    def _warm_conversation(self, session: dict, gen: int) -> None:
        """后台填对话缓存；只刷新仍在右栏的同一会话。"""
        import corral

        key = corral.session_key(session)
        try:
            self.store.get_conversation(session)
        except Exception:
            return
        if gen != self._preview_gen:
            return
        self.app.call_from_thread(self._refresh_preview_detail, key, gen)

    def _refresh_preview_detail(
        self, key: str | None = None, gen: int | None = None,
    ) -> None:
        """拒绝过期异步结果，避免旧会话的预览刷新当前右栏。"""
        if gen is not None and gen != self._preview_gen:
            return
        if not self.embed_ok:
            return
        area = self._split_area()
        if key is not None and key not in area.ordered_session_keys():
            return
        if area.any_embed_focused():
            return
        area.invalidate_visible_previews()

    # ---- 右上角会话小窗：每个实时托管格各自一份 ----


    # ---- 会话选择/新建 ----

    @work
    async def on_list_view_selected(self, event) -> None:
        session_list = self.query_one(SessionListView)
        # 每次「打开」都要消费掉按下前的持有输入会话，避免上一次点击的旧值留到下一次判定。
        focus_before_click = session_list.take_focus_before_click()
        selected_by_key = session_list.take_selected_by_key()
        multi = session_list.multi_keys()
        if len(multi) >= 2:
            session_list.clear_multi()
            self._open_split_from_selection(multi)
            return
        session_list.clear_multi()
        if session_list.is_new_session_selected():
            self._leave_activity_board()
            await self._start_new_session_flow()
            return
        if session_list.is_activity_board_selected():
            self._show_activity_board(focus_pane=True)
            return
        self._leave_activity_board()
        group = session_list.selected_group()
        if group is not None:
            focus_key = (
                group.focus_key
                if group.focus_key in group.session_keys
                else group.session_keys[0]
            )
            if self.embed_ok:
                # 会话组卡：右栏跟随展示组合，焦点固定留在侧边栏。
                # 点组卡不是「打开某一格输入」——进成员会话卡才把输入交给右栏。
                self._show_session_group(
                    focus_key, focus_pane=False, include_inactive=True
                )
                self._focus_list()
                return
            session = self.store.find_session(focus_key)
        else:
            session = session_list.selected_session()
        if session is None:
            return
        import corral
        session_key = corral.session_key(session)
        if self._click_returns_focus_to_list(focus_before_click, session_key):
            self._focus_list()
            return
        active = self._is_session_active(session_key)
        if is_external_running(session):
            # 外部窗口仍在写同一段历史时，只保留已经显示的静态预览；不能另起恢复
            # 进程，既避免历史竞争，也不以确认弹窗打断用户。
            self._focus_list()
            return
        if self._split_store.get_group(session_key) is not None and active:
            # 还活着的组成员：回车 = 把输入交给它那一格。已结束的成员必须往下走
            # 到启动那一支——否则组里的历史会话点进去永远只有静态预览，再没有任何
            # 重启入口（会话组结束后仍然保留，这条路会一直被撞上）。
            self._show_session_group(
                session_key, focus_pane=True, include_inactive=True
            )
            return
        if not active and not selected_by_key:
            # 进程早就没了的会话：鼠标单击只把历史消息摆出来（高亮跟随已经做完了），
            # 恢复会话必须显式回车。误点一下就真去起一个助手进程、真去烧账号额度，
            # 代价和"看一眼历史"完全不对等（机主 2026-08-05 拍板）。
            self._focus_list()
            return
        request = corral.LaunchRequest(
            session, str(session.get("source") or self.nav.source), self.store.get_title(session)
        )
        await self._open_or_exit(request)

    @work
    async def _restart_session_from_pane(self, session_key: str, dead: bool) -> None:
        """右栏静态预览格 / 已结束格上按回车：就地把这条会话重新拉起来。

        与侧边栏回车走的是同一条启动路径，区别只是触发入口在右栏——已结束会话
        的右栏往往是用户当下唯一在看的地方。
        """
        import corral
        from corral.models import is_shell_session

        session = self.store.find_session(session_key)
        if session is None or session.get("provisional"):
            # 占位卡（接力 / 空白新建还没落盘历史就退出了）没有可恢复的会话，
            # 拿它去生成启动计划只会失败；这条会话卡本身下一轮重扫也会消失。
            self.app.bell()
            return
        if is_shell_session(session):
            self.app.bell()
            return
        if is_external_running(session):
            # 外部窗口仍在写同一段历史时，只保留已经显示的静态预览；不能另起恢复
            # 进程，既避免历史竞争，也不以确认弹窗打断用户（2026-08-08 裁定）。
            self._focus_list()
            return
        if dead:
            # 这一格里的会话刚跑完退出，但 store 里的托管标记要等下一轮重扫才撤。
            # 不先撤掉的话 `_embed_open` 会认定它"已托管"，转身把那格死画面又摆一遍。
            session = self.store.mark_hosted(session_key, None) or session
        request = corral.LaunchRequest(
            session, str(session.get("source") or self.nav.source), self.store.get_title(session)
        )
        await self._open_or_exit(request)

    def _click_returns_focus_to_list(self, focus_before_click, key: str) -> bool:
        """点「当前正持有输入的那张会话卡」= 把焦点撤回侧边栏，与 Ctrl+\\ 等价。

        点开→点同一张卡收回→再点又进去，形成对称的鼠标开关；点的是别的会话卡时
        一律按「打开」处理，把输入交给那一格（判定只比会话键，不看右栏此刻的
        控件绑定，原因见 `session_list._focused_live_session_key()`）。
        """
        if focus_before_click is None or not self.embed_ok:
            return False
        return focus_before_click == key

    async def _start_new_session_flow(self) -> None:
        session_list = self.query_one(SessionListView)
        sessions = session_list.visible_sessions()
        anchor_session = sessions[0] if sessions else None
        request = await new_session_flow(self.app, self.store, self.nav, anchor_session)
        if request is not None:
            await self._open_or_exit(request)

    async def _open_or_exit(self, request, *, add_pane: bool = False) -> None:
        """embed 可用则原地内嵌打开；否则退出应用，交给外层 execvp 全屏接管。

        `add_pane=True` 时在当前右栏旁加一格（跨助手接力默认如此，保留被接力会话）。
        """
        if self.embed_ok:
            self._embed_open(request, add_pane=add_pane)
        else:
            self.app.exit(result=request)

    def _prepare_handoff_split(self, session: dict) -> None:
        """跨助手接力前：确保被接力会话已在右栏，目标才能并排加一格。

        选中跟随通常已经摆好；这里兜底「右栏空着 / 还在别组」时先把源会话单独摆上。
        """
        import corral

        key = corral.session_key(session)
        area = self._split_area()
        if key in area.ordered_session_keys():
            return
        name = session.get("keepalive_name") if self._session_is_active(session) else None
        if name:
            project = corral._normalize_cwd(session.get("cwd"))
            area.show_hosted_group(
                project,
                [(session, str(name), self._detail_renderer_for(session))],
                focus_key=key,
            )
        else:
            area.show_single_preview(
                session, self._detail_renderer_for(session)
            )

    async def _confirm_external_resume(self, request) -> bool:
        """会话正在别的窗口跑时，"打开"实际是另起一个恢复进程——先问过用户。

        原来这一步是静默的：点进去右栏冒出一个刚从历史恢复的新进程，原窗口那个
        还在跑，观感就是"会话已中断"，而且两个进程写同一份历史有互相覆盖的风险。
        """
        import corral

        if not isinstance(request, corral.LaunchRequest):
            return True
        # 接力新建 / 复制会话只读原历史或另建目标，不拦。
        if (
            request.force_new
            or request.copy_session
            or request.session.get("source") != request.target_runtime_id
        ):
            return True
        session = self.store.find_session(corral.session_key(request.session)) or request.session
        if not is_external_running(session):
            return True
        title = self.store.get_title(session)
        return bool(
            await self.app.push_screen_wait(
                ConfirmModal(t("confirm.resume_external_running", title=title), confirm_key="r")
            )
        )


    def _restore_direct_search_focus(self) -> None:
        """直启托管结束（成功或失败）后恢复搜索框可聚焦，并清掉 OSC 泄漏垃圾。"""
        if self.direct is None:
            return
        search = self.query_one("#project-search", Input)
        search.can_focus = True
        if _filter_looks_like_osc_leak(search.value):
            search.value = ""
            self.nav.project_query = ""
            self._update_header()

    def _focus_list(self) -> None:
        # 用户主动回列表：撤销右栏还没兑现的自动聚焦意图，别让它随后把焦点抢回去。
        # 侧栏已藏时先展开，否则 SessionListView 不可见、焦点落空。
        if self.embed_ok and not self.sidebar_visible:
            self.sidebar_visible = True
            self._apply_sidebar_visible(persist=True)
        try:
            self._split_area().clear_focus_intent()
        except Exception:
            pass
        # 用 Screen.set_focus 同步生效，不要用 Widget.focus()——后者走 call_later，
        # 生效顺序与调用顺序会**反过来**：本方法先被调用、把「回列表」排进队列，
        # 随后用户点进某个内嵌格（或代码直接 EmbedPane.focus()）也排进队列，队列
        # 依次兑现时较早排队的「回列表」反而落在后面，把焦点从格子上抢走。真机
        # 表现是点进内嵌会话、键盘却还在侧边栏。同步设置后，谁后请求谁生效。
        # （main_screen.on_mount 里「不要先调 SessionListView.focus()」那条注释
        # 说的是同一个坑，当时是绕开、没有根治。）
        self.set_focus(self.query_one(SessionListView).focus_target())

    def on_descendant_focus(self, event) -> None:
        self._sync_input_mask()

    def on_descendant_blur(self, event) -> None:
        self._sync_input_mask()

    def _sync_input_mask(self) -> None:
        """焦点在侧边栏时把右栏实时画面压暗。

        Textual 派发 Focus/Blur 时 `has_focus` 往往还没翻转（EmbedPane 的光标
        同步踩过同一个坑），必须等这一轮刷新完再读，否则压暗状态整体慢一拍。
        """
        if not self.embed_ok:
            return
        self.call_after_refresh(self._sync_input_mask_now)

    def _sync_input_mask_now(self) -> None:
        try:
            self._split_area().sync_input_mask()
        except Exception:  # noqa: BLE001 分栏重建中间态查不到，下一轮焦点事件会再同步
            return

    def _can_autofocus(self) -> bool:
        """自动把输入交给右栏的前置条件。

        托管是后台 worker 完成的，回调到达时用户可能已经打开了高级操作弹窗或
        正在筛选框里打字——这两种情况下抢焦点等于把用户正在输入的内容打断。
        """
        if not self.embed_ok:
            return False
        if self.app.screen is not self:
            return False
        try:
            if self.query_one("#project-search", Input).has_focus:
                return False
        except Exception:
            pass
        return True

    def _live_embed_focused(self) -> bool:
        """右栏是否有「活着的实时终端」正持有输入（此时按键都发给托管会话）。"""
        if not self.embed_ok:
            return False
        try:
            return self._split_area().live_embed_focused()
        except Exception:
            return False

    def _any_embed_focused(self) -> bool:
        if not self.embed_ok:
            return False
        try:
            return self._split_area().any_embed_focused()
        except Exception:
            return False

    def get_widget_and_offset_at(self, x: int, y: int):
        """命中表里可能还留着刚被移出 DOM 的控件，这种命中一律当作没命中。

        合成器的命中表按帧更新，全量重建列表 / 换格的那一两帧里，它给出的控件
        `parent` 已经是 `None`。Textual 8.2.8 的鼠标按下分支拿到这种控件后会直接
        取 `parent.region`（`screen.py` 的文本选择状态初始化），抛出未捕获的
        `AttributeError: 'NoneType' object has no attribute 'region'`，整个 TUI
        当场退出——真机两次都发生在**启动首屏重建期间点鼠标**（2026-08-03、
        2026-08-05）。上游同类问题（Textualize/textual#5629）至今未修，8.2.8 已是
        最新版，只能在这里兜。

        只有允许文本选择的控件才会走进那个分支（右栏内嵌终端刻意保留划词复制），
        但这里对所有命中一视同仁：拿一个已脱离 DOM 的控件去派发任何鼠标事件都是
        错的。回归：`test_mouse_down_on_detached_widget_does_not_crash`。
        """
        widget, offset = super().get_widget_and_offset_at(x, y)
        if widget is not None and widget.parent is None:
            return None, None
        return widget, offset

    def check_action(self, action: str, parameters) -> bool | None:
        """按当前焦点裁剪可用动作：右栏持有输入时，列表侧快捷键必须整体让路。

        两个作用同时生效：Footer 不再展示此刻按了没用（还会被当字符打给助手）的
        键；`run_action` 也会因此不派发，让 Home/End/翻页这些**优先级绑定**穿透到
        内嵌会话——否则用户在助手里翻历史会被右栏预览滚动截胡。
        """
        if action == "focus_list":
            return self._any_embed_focused()
        if action == "toggle_sidebar":
            # 无右栏时藏侧栏没有工作区可扩大，顶栏开关也不存在。
            return self.embed_ok
        if action == "toggle_hud" and not self.embed_ok:
            # 纯列表模式没有右栏，也就没有小窗可展开。
            return False
        if action in ("board_prev", "board_next"):
            if not self._activity_board_active:
                return False
            snap = self.query_one(SessionListView).board_snapshot
            if snap is None or snap.page_count <= 1:
                return False
        if action in _LIST_ONLY_ACTIONS and self._live_embed_focused():
            return False
        return True

    def _on_pane_focused(self, session_key: str) -> None:
        """右栏某格拿到焦点后，侧边栏高亮切到同一会话（不改右栏布局）。"""
        list_view = self.query_one(SessionListView)
        list_view.clear_multi()
        if getattr(self, "_activity_board_active", False):
            self._activity_board.set_typing_key(session_key)
            self.call_next(self._begin_attention_read, session_key)
            return
        if not list_view.select_session_key(session_key):
            return
        self._persist_split_focus()
        # 选择事件与焦点事件可能同帧到达；放到下一轮，确保高亮变更的取消逻辑
        # 先执行，再以实际持有焦点的可见格重新开始观察。
        self.call_next(self._begin_attention_read, session_key)

    # ---- 动作 ----

    def action_focus_list(self) -> None:
        self._focus_list()

    def action_toggle_sidebar(self) -> None:
        if not self.embed_ok:
            return
        self.sidebar_visible = not self.sidebar_visible
        self._apply_sidebar_visible(persist=True)

    def _apply_sidebar_visible(self, *, persist: bool) -> None:
        """按 sidebar_visible 显隐左栏，并同步顶栏开关字形。"""
        list_pane = self.query_one("#list-pane")
        if self.sidebar_visible:
            list_pane.display = True
            list_pane.styles.width = LIST_PANE_WIDTH
        else:
            if self._focus_is_in_list_pane():
                self._focus_away_from_hidden_sidebar()
            list_pane.display = False
        try:
            self.query_one("#runtime-top-bar", RuntimeTopBar).set_sidebar_visible(
                self.sidebar_visible
            )
        except Exception:
            pass
        if persist:
            ui_prefs.save_sidebar_visible(self.sidebar_visible)

    def _focus_is_in_list_pane(self) -> bool:
        focused = self.app.focused
        if focused is None:
            return False
        if getattr(focused, "id", None) in ("session-list", "project-search"):
            return True
        widget = focused
        while widget is not None:
            if getattr(widget, "id", None) == "list-pane":
                return True
            widget = widget.parent
        return False

    def _focus_away_from_hidden_sidebar(self) -> None:
        """侧栏即将藏起时，把焦点挪出不可见控件。"""
        try:
            area = self._split_area()
        except Exception:
            return
        for cell in area.cells():
            pane = cell.embed_pane()
            if pane is not None:
                self.set_focus(pane)
                return
        # 尚无分屏格：清空焦点，避免留在即将 display:none 的列表上。
        self.set_focus(None)

    def action_focus_search(self) -> None:
        if not self.sidebar_visible:
            return
        self.query_one("#project-search", Input).focus()

    # ---- 全文搜索：侧边栏筛选框只收窄当前列表，这条路才是搜对话正文 ----

    def search_index(self):
        """惰性创建全文搜索索引；正文解析走 store 的对话缓存，不重复读盘。"""
        if self._search_index is None:
            from corral.search import ConversationIndex

            self._search_index = ConversationIndex()
        return self._search_index

    def _schedule_search_index_warm(self) -> None:
        """把索引预热排到首屏画完之后，不要和首帧抢 CPU。

        预热跑在后台线程，但 Python 有 GIL：解析正文期间实测会让界面每帧多滞后
        4~5ms（p95 9~14ms），首屏出卡片因此慢了 110~165ms——而首屏目标本来就只有
        1 秒。倒计时必须从「卡片已经画出来」算起，不能从 on_mount 起算墙钟。
        """
        if self._search_warm_scheduled:
            return
        self._search_warm_scheduled = True
        self.set_timer(_SEARCH_INDEX_WARM_DELAY, self._warm_search_index)

    @work(thread=True, group="search-index")
    def _warm_search_index(self) -> None:
        """在后台把对话正文读进索引。

        放后台线程是硬要求：首次要解析没缓存过的会话（本机实测约 1 秒），第二次
        起命中 SQLite 派生缓存只剩几十毫秒。失败不影响主流程——弹窗打开时发现
        索引没就绪会自己再建一次，那条路带进度显示。
        """
        import corral

        try:
            self.search_index().refresh(self.store)
        except Exception as exc:
            corral._log_embed_error("全文搜索索引预热", exc)

    # @work 是硬要求，不是可选优化：`push_screen_wait` 只能在 worker 里调用
    # （Textual 会直接抛 NoActiveWorker），与 action_handoff 同一个模式。
    @work
    async def action_search_content(self) -> None:
        from corral.ui.search_modal import FullTextSearchModal

        # 侧边栏当前的筛选词大概率就是用户想搜的东西，带进弹窗省得重敲一遍。
        initial = self.nav.project_query.strip()
        key = await self.app.push_screen_wait(
            FullTextSearchModal(self.store, self.search_index(), initial)
        )
        if key:
            await self._reveal_session(key)

    def action_toggle_pin(self) -> None:
        """Ctrl+P 全局置顶：右栏持焦时钉当前这一格（或其会话组），否则钉侧栏选中项。"""
        list_view = self.query_one(SessionListView)
        if self._any_embed_focused():
            try:
                key = self._split_area().focus_key
            except Exception:
                key = None
            if key:
                list_view.toggle_pin_key(key)
                return
        list_view.action_toggle_pin()

    async def _reveal_session(self, key: str) -> None:
        """把搜索结果选中的会话定位到侧边栏。

        选中的会话可能正被筛选词挡在列表外——那就先把筛选清掉，否则用户会看到
        「搜到了却跳不过去」。清空输入框本身也会触发一次重建（不带 select_key），
        它走的是"保持当前选中"分支，不会把这里定位好的选中项挤掉。
        """
        import corral

        session_list = self.query_one(SessionListView)
        visible = {corral.session_key(s) for s in session_list.visible_sessions()}
        if key not in visible:
            self.nav.project_query = ""
            self.query_one("#project-search", Input).value = ""
            session_list.clear_multi()
        await self._rebuild_list(key)
        self._update_header()
        if self.sidebar_visible:
            session_list.focus()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "project-search":
            return
        # 兜底：任何路径漏进搜索框的 OSC 应答都直接丢掉，避免把会话列表滤空。
        if _filter_looks_like_osc_leak(event.value):
            if event.input.value:
                event.input.value = ""
            self.nav.project_query = ""
            list_view = self.query_one(SessionListView)
            list_view.clear_multi()
            await list_view.rebuild(keep_selection=True)
            self._update_header()
            return
        self.nav.project_query = event.value
        list_view = self.query_one(SessionListView)
        list_view.clear_multi()
        await list_view.rebuild(keep_selection=True)
        self._update_header()
        self._schedule_follow_selection()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "project-search":
            return
        # Enter：把焦点交回列表，方便继续用 j/k / 回车操作会话
        list_view = self.query_one(SessionListView)
        list_view.focus()
        if list_view.index is None:
            list_view.index = len(STICKY_IDS) if list_view.visible_sessions() else 0

    def on_text_selected(self, event: events.TextSelected) -> None:
        """划词抬起：有选区则经 OSC 52 自动复制（无需再按 Ctrl+C / ⌘C）。

        Textual 在每次 MouseUp 都会发 TextSelected（含空点选）；无选区时跳过。
        """
        selected = self.get_selected_text()
        if selected:
            self.app.copy_to_clipboard(selected)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self._forward_sticky_sidebar_wheel(event, 3):
            event.stop()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self._forward_sticky_sidebar_wheel(event, -3):
            event.stop()

    def _forward_sticky_sidebar_wheel(self, event, delta: int) -> bool:
        """筛选框在列表外；指针在固定头上滚轮仍带动会话列表，顶部不动。"""
        node = getattr(event, "control", None) or getattr(event, "widget", None)
        while node is not None:
            nid = getattr(node, "id", None)
            if nid == "sidebar-scroll":
                return False
            if nid in ("project-search", "sidebar-sticky"):
                try:
                    self.query_one(SessionListView).scroll_unpinned(delta)
                except Exception:
                    return False
                return True
            node = getattr(node, "parent", None)
        return False

    def on_key(self, event) -> None:
        search = self.query_one("#project-search", Input)
        list_view = self.query_one(SessionListView)
        if search.has_focus:
            # 搜索框内 Down：跳到列表；不在这里绑 /，避免吞掉用户想输入的斜杠
            if event.key == "down":
                event.stop()
                list_view.focus()
                if list_view.index is None:
                    list_view.index = (
                        len(STICKY_IDS) if list_view.visible_sessions() else 0
                    )
            return
        # 列表聚焦时 / 打开搜索（不用 Screen Binding，否则搜索框里按 / 会被截走）
        if event.key == "slash" and list_view.has_focus:
            event.stop()
            search.focus()

    @work
    async def action_handoff(self) -> None:
        session_list = self.query_one(SessionListView)
        session = session_list.selected_session()
        if session is None:
            self.app.bell()
            return
        source = str(session.get("source") or self.nav.source)
        from corral.models import is_shell_session
        from corral.runtime.base import LaunchError

        restart_available = bool(
            session.get("keepalive_name")
            and not is_shell_session(session)
            and not session.get("provisional")
        )
        target = await choose_target_runtime(
            self.app, self.store, source, restart_available=restart_available,
        )
        if target is None:
            return
        import corral

        if target == RESTART_SESSION_CHOICE:
            await self._restart_hosted_session(session)
            return

        if target == EXPORT_SESSION_CHOICE:
            from corral.agent_api import ApiError, export_share_to_cache

            try:
                path = export_share_to_cache(session, self.store.registry)
            except Exception as exc:
                error = exc.message if isinstance(exc, ApiError) else str(exc)
                self.notify(t("modal.export_session_failed", error=error))
                self.app.bell()
                return
            self.app.copy_to_clipboard(path)
            self.notify(t("modal.export_session_copied", path=path))
            return
        if target == COPY_SESSION_CHOICE:
            try:
                request = self.store.registry.prepare_copy_request(
                    session, self.store.get_title(session),
                )
            except LaunchError as exc:
                self.notify(t("modal.copy_session_failed", error=str(exc)))
                self.app.bell()
                return
            if self.embed_ok:
                self._prepare_handoff_split(session)
            await self._open_or_exit(request, add_pane=self.embed_ok)
            return
        # 高级操作一律「读历史后新建」——含同助手（原会话卡住时另起）；
        # 原生恢复留给侧边栏回车。新建默认旁挂被接力会话。
        request = corral.LaunchRequest(
            session, target, self.store.get_title(session), force_new=True,
        )
        if self.embed_ok:
            self._prepare_handoff_split(session)
        await self._open_or_exit(request, add_pane=self.embed_ok)

    async def _restart_hosted_session(self, session: dict) -> None:
        """高级操作「重启会话」：结束托管进程后按原会话原地恢复（上下文保留）。

        面向「进程还活着但已卡住/跑飞」的场景：一步完成结束 + 恢复，替代先 q 再
        回车的两步操作。与右栏已结束格上回车的 `_restart_session_from_pane` 走同一条
        启动路径；区别只是这里要先亲手杀掉还活着的托管进程。分屏格不摘除，
        重新托管后经 `_show_session_group` 原位换回实时画面，不拆用户的分屏组合。
        """
        import corral
        from corral import embed, keepalive
        from corral.models import is_shell_session

        keepalive_name = session.get("keepalive_name")
        if (
            not keepalive_name
            or is_shell_session(session)
            or session.get("provisional")
        ):
            # 弹窗置灰拦不住程序化调用；这里再守一道，不能对没托管的会话硬杀。
            self.app.bell()
            return
        title = self.store.get_title(session)
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(t("confirm.restart_session", title=title), confirm_key="r")
        )
        if not confirmed:
            return
        key = corral.session_key(session)
        keepalive.kill(str(keepalive_name))
        embed.close_channel(str(keepalive_name))
        current = self.store.mark_hosted(key, None) or session
        # mark 未命中时落到原 session，里面还带着旧 keepalive 名；不搞掉的话
        # `_embed_open` 会误判「已托管」而只聚焦旧格，重启实际没发生。
        current.pop("keepalive_name", None)
        request = corral.LaunchRequest(
            current, str(current.get("source") or self.nav.source), title,
        )
        await self._open_or_exit(request)

    @work
    async def action_kill_keepalive(self) -> None:
        import corral
        from corral import keepalive

        session_list = self.query_one(SessionListView)
        session = session_list.selected_session()
        keepalive_name = session.get("keepalive_name") if session else None
        if not keepalive_name:
            self.app.bell()
            return
        title = self.store.get_title(session)
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(t("confirm.kill_session", title=title))
        )
        if not confirmed:
            return
        keepalive.kill(keepalive_name)
        key = corral.session_key(session)
        self.store.mark_hosted(key, None)
        if self.embed_ok:
            self._split_area().remove_by_keepalive(keepalive_name)
        await self._rebuild_list()

    @work
    async def action_delete_session(self) -> None:
        """x：彻底删除选中会话的本地历史，不可恢复；运行中/托管会话先结束再删。

        光标停在会话组标题上时删的是整组（组卡本身不对应任何一条会话，只删"选中的
        那一条"在这里没有意义），交给 `_delete_session_group` 处理。

        二次确认按 x（而不是复用 q），与结束会话共用同一套 ConfirmModal 交互形态，
        只是把确认键换成触发本动作的键，避免用户记混"删除按 x 确认却按了 q"。
        """
        import asyncio
        import sqlite3

        import corral
        from corral import keepalive
        from corral.runtime import LaunchError

        session_list = self.query_one(SessionListView)
        group = session_list.selected_group()
        if group is not None:
            await self._delete_session_group(group)
            return
        session = session_list.selected_session()
        if session is None:
            self.app.bell()
            return
        key = corral.session_key(session)
        keepalive_name = session.get("keepalive_name")
        title = self.store.get_title(session)
        message_key = (
            "confirm.delete_running_session"
            if keepalive_name
            else "confirm.delete_session"
        )
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(t(message_key, title=title), confirm_key="x")
        )
        if not confirmed:
            return
        # 乐观 UI：确认的那一刻就摘卡，结束进程和磁盘抹除全部推到后台线程。
        # 这两步都可能是秒级的（tmux kill 走子进程且带超时、OpenCode 要写全局共享
        # SQLite 可能等锁、Cursor 要 rmtree 整个会话目录），放在摘卡之前会让侧边栏
        # 干等；tombstone 负责挡住后台重扫的回灌。
        if keepalive_name:
            # 先撤托管标记再摘卡：反过来会往 `_force_ended` 里塞一个已不存在的键。
            self.store.mark_hosted(key, None)
            if self.embed_ok:
                self._split_area().remove_by_keepalive(keepalive_name)
        self.store.mark_deleted(key)
        await self._rebuild_list()
        runtime = self.store.registry.get(str(session.get("source") or ""))

        def purge() -> None:
            # 顺序不能反：进程还活着时先抹历史，运行时可能立刻又写回一份。
            if keepalive_name:
                keepalive.kill(keepalive_name)
            runtime.delete_session(session)

        try:
            await asyncio.to_thread(purge)
        except (LaunchError, OSError, sqlite3.Error) as exc:
            self.store.abort_delete(key)
            try:
                self.store.refresh()
            except Exception:
                pass
            await self._rebuild_list()
            self.notify(t("notify.delete_failed", error=exc))
            self.app.bell()
            return
        self._apply_layout_change(lambda store: store.remove_session(key))
        await self._rebuild_list()

    async def _delete_session_group(self, group) -> None:
        """x 落在会话组标题上：把整组会话的本地历史一次删干净。

        与删单条同构（乐观摘卡 → 后台结束进程 + 抹磁盘 → 失败回滚），只是把每一步
        铺到全部成员上。**逐条容错**：某个运行时抹历史失败不该连累同组其他会话，失败
        的那几条解除 tombstone 让下一轮扫描把卡片捞回来，成功的照常消失。
        """
        import asyncio
        import sqlite3

        from corral import keepalive
        from corral.runtime import LaunchError

        # keepalive 名必须在动手前就抄下来：`mark_hosted(key, None)` 会把它从会话
        # 字典里摘掉，等进了后台 purge 再读就永远是空的，托管进程会被漏杀。
        members = [
            (key, session, session.get("keepalive_name"))
            for key in group.session_keys
            if (session := self.store.find_session(key)) is not None
        ]
        if not members:
            # 成员都已不在扫描快照里（可能被别的窗口删了）：没有历史可抹，只解散组。
            self._apply_layout_change(
                lambda store, keys=list(group.session_keys): _drop_layout_sessions(
                    store, keys
                )
            )
            await self._rebuild_list()
            return
        running = sum(1 for *_, keepalive_name in members if keepalive_name)
        message = (
            t(
                "confirm.delete_running_group",
                name=group.name,
                count=len(members),
                running=running,
            )
            if running
            else t("confirm.delete_group", name=group.name, count=len(members))
        )
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(message, confirm_key="x")
        )
        if not confirmed:
            return
        for key, _session, keepalive_name in members:
            if keepalive_name:
                # 先撤托管标记再摘格，顺序与删单条一致。
                self.store.mark_hosted(key, None)
                if self.embed_ok:
                    self._split_area().remove_by_keepalive(keepalive_name)
            self.store.mark_deleted(key)
        await self._rebuild_list()

        def purge() -> list[tuple[str, Exception]]:
            from corral.models import is_shell_session

            failures: list[tuple[str, Exception]] = []
            for key, session, keepalive_name in members:
                if is_shell_session(session):
                    # 终端 pane 没有历史可抹，托管标记与分屏格已在上面的循环里撤掉。
                    continue
                runtime = self.store.registry.get(str(session.get("source") or ""))
                try:
                    # 顺序不能反：进程还活着时先抹历史，运行时可能立刻又写回一份。
                    if keepalive_name:
                        keepalive.kill(keepalive_name)
                    runtime.delete_session(session)
                except (LaunchError, OSError, sqlite3.Error) as exc:
                    failures.append((key, exc))
            return failures

        failures = await asyncio.to_thread(purge)
        failed_keys = {key for key, _ in failures}
        for key in failed_keys:
            self.store.abort_delete(key)
        if failed_keys:
            try:
                self.store.refresh()
            except Exception:  # noqa: BLE001 刷新失败不该盖掉上面的删除结果
                pass
        purged = [key for key, *_ in members if key not in failed_keys]
        if purged:
            self._apply_layout_change(
                lambda store, keys=purged: _drop_layout_sessions(store, keys)
            )
        await self._rebuild_list()
        if failures:
            self.notify(t("notify.delete_failed", error=failures[0][1]))
            self.app.bell()

    def action_close_pane(self) -> None:
        if not self.embed_ok:
            return
        self._split_area().close_focused_pane()
        self._persist_split_composition()

    def action_preview_home(self) -> None:
        if self.embed_ok:
            self._split_area().scroll_preview_home()

    def action_preview_end(self) -> None:
        if self.embed_ok:
            self._split_area().scroll_preview_end()

    def action_preview_page_up(self) -> None:
        if self.embed_ok:
            self._split_area().scroll_preview_page(-1)

    def action_preview_page_down(self) -> None:
        if self.embed_ok:
            self._split_area().scroll_preview_page(1)

    def action_save_screenshot(self) -> None:
        """F12：导出当前 TUI 到 ~/.cache/corral/screenshots/（用户主动触发）。"""
        from corral import observe

        try:
            path = observe.save_tui_screenshot(self.app)
        except Exception as exc:  # noqa: BLE001
            import corral
            corral._log_embed_error("TUI 截图", exc)
            self.app.bell()
            return
        self.notify(t("notify.screenshot", path=path), title="corral", timeout=4)

    # ---- 客户端自动更新：右下角浮层 ----
    # 每次打开 corral 都后台查一次最新版本；源码/开发安装（无法一键升级）时
    # 直接跳过，不弹窗打扰。检查/升级全程跑在 worker 线程，任何异常都不能
    # 拖垮 UI 或阻塞首屏——updater 模块本身已把网络/子进程异常全部吞掉。


    def action_quit_app(self) -> None:
        # 搜索框聚焦时 Esc 先清空查询，再交回列表；列表上 Esc 才真正退出
        search = self.query_one("#project-search", Input)
        list_view = self.query_one(SessionListView)
        if search.has_focus:
            if search.value:
                search.value = ""
                return
            list_view.focus()
            return
        if list_view.has_focus and list_view.multi_count() > 0:
            list_view.clear_multi()
            return
        self.app.exit(result=None)
