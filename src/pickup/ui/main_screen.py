"""主屏：左栏会话列表 + 右栏预览/内嵌终端（pickup 唯一界面）。

按键语义（/ 聚焦项目搜索 / a 高级操作 /
q 结束会话 / x 删除会话 / c 关闭面板 / Ctrl+B 显隐侧栏 / Esc 退出）；选中非进行中会话时右栏直接
展示完整对话预览。键盘焦点跟随明确意图：回车 / 单击会话卡打开、新建或直启托管成功后
输入交给右栏那一格（仅限活着的实时会话），上下浏览不抢焦点；再点当前持有输入的那张
会话卡则把焦点撤回侧边栏，与 `Ctrl+\\` 等价。右栏滚轮/预览翻页与焦点无关，鼠标在右栏
上即可滚动。焦点契约与两条易踩的时序坑见 docs/TERMINAL_UI_KNOWLEDGE_BASE.md §6。
多分屏时聚焦某一格会把侧边栏高亮切到对应会话。新建会话走侧边栏「＋ 新建」或
右栏顶栏加格，不再提供底栏 `n` 快捷键。
侧边栏顶部为搜索框，大小写无关模糊匹配组名、项目名与会话标题。
`Ctrl+B` 与右栏顶栏左侧开关可显隐侧栏（无右栏时不可用）；偏好写入
`~/.cache/pickup/ui-prefs.json`。禁止再加第二套全屏预览或纯列表旧界面。
"""

from __future__ import annotations

import asyncio
import os
import time

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Input
from textual.worker import get_current_worker

import dataclasses

from pickup import i18n, updater, ui_prefs
from pickup.i18n import t
from pickup.ui.split_pane_area import SplitPaneArea
from pickup.ui.modals import ConfirmModal, choose_target_runtime, new_session_flow
from pickup.ui.nav import NavState
from pickup.ui.session_hud import summarize_user_messages
from pickup.ui.session_list import SessionListView
from pickup.ui.update_toast import UpdateToast
from pickup.ui.runtime_top_bar import RuntimeTopBar

try:
    from textual.screen import Screen
except ImportError:  # pragma: no cover
    from textual import Screen

REFRESH_INTERVAL = 3.0  # 秒，后台重扫会话列表的最短间隔，与旧版 _background_refresh 一致
REFRESH_INTERVAL_MAX = 10.0  # 秒，连续空闲多轮后退避到的最长间隔
_IDLE_ROUNDS_BEFORE_BACKOFF = 3  # 连续几轮扫描都没变化才开始拉长间隔，避免偶发抖动误判空闲
CACHE_POLL_INTERVAL = 0.5  # 秒，标题缓存文件轮询间隔（比会话重扫轻得多，保持高频）

# 秒，右上角会话小窗的同步间隔。每次只做一次 stat + 内存缓存命中判断（`peek_conversation`），
# 真正解析历史另有节流（HUD_WARM_INTERVAL），不会因为助手在狂写历史就每秒重解析一遍。
HUD_POLL_INTERVAL = 1.0
HUD_WARM_INTERVAL = 3.0  # 秒，同一会话两次重新解析对话之间的最小间隔
LIST_PANE_WIDTH = 39  # 分栏时左栏固定宽度，对应旧版 EMBED_LEFT_BAND
# 活跃判定可接受的存活证据陈旧上限（秒）。右栏在显示的会话每轮抓帧都会刷新证据，
# 所以这条路几乎永远命中缓存；只有久未露面的会话才真去 fork 一次 has-session。
# 判定「会话是否已结束」不走这条缓存，见 embed.is_alive 的 max_age 说明。
_ALIVE_EVIDENCE_TTL = 3.0
# 选择跟随的节流窗口（秒）。单次方向键立即生效（无额外延迟），连按时窗口内只
# 保留最后一次——否则连按 N 下就实打实重建 N 次右栏，每次约 180ms。
_FOLLOW_THROTTLE = 0.12
# 红点只有在右侧内容真实可见并连续稳定一段时间后才算已读；等待首帧或对话缓存
# 时做轻量轮询，不能把「选中过」误当成「看过了」。
_ATTENTION_READ_DELAY = 0.5
_ATTENTION_READY_POLL = 0.1
# 首屏画完到开始预热全文搜索索引的间隔（秒）。见 _schedule_search_index_warm。
_SEARCH_INDEX_WARM_DELAY = 1.5


def is_external_running(session: dict) -> bool:
    """会话在本机跑着，但不在 pickup 的托管终端里——通常是用户自己开窗口起的。

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


# 动作名 → 文案 key；实例化时只改 description，不能整表替换（会丢掉 ListView/Screen 继承绑键）
_ACTION_I18N = {
    "search_content": "action.search",
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
    "quit_app": "action.quit",
}


# 只在「焦点还在侧边栏」时才成立的动作：右栏实时终端持有输入时，这些键要么
# 本就到不了（EmbedPane 先 stop 掉），要么会把用户想打给助手的内容截胡。
_LIST_ONLY_ACTIONS = frozenset(
    {
        # 全文搜索是列表侧的检索动作，不是壳层开关：Ctrl+F 在助手里是常用键
        # （readline 前移光标、翻页搜索），右栏持有输入时必须原样转发给会话，
        # 想搜就先 Ctrl+\ 回列表。这一点与 Ctrl+B 显隐侧栏刻意不同。
        "search_content",
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
    }
)


def _main_bindings() -> list[Binding]:
    """按当前语言生成底部快捷键说明。"""
    return [
        Binding("ctrl+f", "search_content", t("action.search")),
        Binding("a", "handoff", t("action.advanced")),
        Binding("q", "kill_keepalive", t("action.kill_session")),
        Binding("x", "delete_session", t("action.delete_session")),
        Binding("c", "close_pane", t("action.close_pane"), show=False),
        # 内嵌终端持有输入时的唯一出口。EmbedPane 自己会先吃掉这个键（实时会话
        # 路径），这里的绑定负责两件事：静态预览格聚焦时也能回列表，以及让
        # Footer 在右栏持有输入时把出口显示出来（见 check_action）。
        Binding("ctrl+backslash", "focus_list", t("action.focus_list")),
        # 与 Ctrl+\ 同级的壳层键：右栏持焦时仍可用，不得进 _LIST_ONLY_ACTIONS。
        # EmbedPane 实时路径会先拦截 ctrl+b，避免键被转发给托管会话。
        Binding("ctrl+b", "toggle_sidebar", t("action.toggle_sidebar")),
        # 会话小窗展开/收起。Footer 已经很挤，这个键不展示；小窗自身可点。
        Binding("ctrl+g", "toggle_hud", t("action.toggle_hud"), show=False),
        Binding("f12", "save_screenshot", t("action.screenshot"), show=False),
        # 右栏静态对话预览滚动（列表聚焦时也生效；优先级高于 ListView 的同名键）
        Binding("home", "preview_home", t("action.preview_home"), show=False, priority=True),
        Binding("end", "preview_end", t("action.preview_end"), show=False, priority=True),
        Binding("pageup", "preview_page_up", t("action.preview_page_up"), show=False, priority=True),
        Binding("pagedown", "preview_page_down", t("action.preview_page_down"), show=False, priority=True),
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


class MainScreen(Screen):
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
        from pickup import split_layout

        self._split_store = split_layout.load_layout()
        # 有右栏时才允许藏侧栏；无右栏（纯列表）藏了无处可点回来。
        self.sidebar_visible = (
            True if not embed_ok else ui_prefs.load_sidebar_visible(default=True)
        )
        # 选择跟随的节流状态，见 _schedule_follow_selection
        self._follow_timer = None
        self._follow_last_run = 0.0
        # 稳定查看判定：只跟踪当前主选择的红点；切换、失焦或内容未就绪都会
        # 取消连续计时，重新看到后必须再完整停留 0.5 秒。
        self._attention_read_timer = None
        self._attention_read_key: str | None = None
        self._attention_read_token: str | None = None
        self._attention_visible_since: float | None = None
        self._app_focused = True
        self._update_channel: str | None = None
        self._update_latest: str | None = None
        # 全文搜索索引：首屏扫描完成后在后台预热，Ctrl+F 打开弹窗时通常已就绪。
        self._search_index = None
        # 右上角会话小窗：展开状态全局共用一份（切格不该让它一会儿开一会儿关），
        # 最近一次算好的摘要按会话键留着，历史文件正在被写、缓存暂时失效时继续
        # 显示旧摘要，避免小窗一秒一闪。
        # 会话提问概览默认展开，让用户进入会话时直接看到上下文；仍可随时收起。
        self._hud_expanded = True
        self._hud_cache: dict[str, object] = {}
        self._hud_warm_at = 0.0
        self._hud_warm_key: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="list-pane"):
                yield Input(placeholder=t("filter.placeholder"), id="project-search")
                yield SessionListView(
                    self.store,
                    self.nav,
                    group_store=self._split_store,
                    on_group_changed=self._save_sidebar_state,
                    id="session-list",
                )
            if self.embed_ok:
                yield SplitPaneArea(
                    self.store,
                    on_runtime_pick=self._on_runtime_pick,
                    on_pane_close=self._on_pane_close,
                    on_focus_list=self._focus_list,
                    on_pane_focused=self._on_pane_focused,
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
        yield Footer()

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
            self._schedule_search_index_warm()
        else:
            self._await_initial_load()
        self.set_interval(CACHE_POLL_INTERVAL, self._poll_cache)
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
            # 键盘输入灌进筛选框，把侧边栏滤空（`pickup cursor` 等直启真机复现）。
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
            from pickup import cursor_observer

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
        """
        import pickup
        from pickup import embed

        kname = session.get("keepalive_name")
        if kname and embed.is_alive(str(kname), max_age=_ALIVE_EVIDENCE_TTL):
            return True
        key = pickup.session_key(session)
        hosted = self.store.hosted.get(key)
        if hosted:
            return True
        return bool(session.get("live"))

    def _is_session_active(self, key: str) -> bool:
        import pickup
        from pickup import embed

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
            if embed.is_alive(spec.keepalive_name, max_age=_ALIVE_EVIDENCE_TTL):
                return True
        return False

    def _reconcile_split_session_keys(self) -> dict[str, str]:
        """占位卡转正后 session_key 会变；按 keepalive 对齐分屏并返回键迁移。"""
        import pickup

        key_by_keepalive: dict[str, str] = {}
        for bucket in self.store.sessions.values():
            for session in bucket:
                kname = session.get("keepalive_name")
                if kname:
                    key_by_keepalive[str(kname)] = pickup.session_key(session)
        for key, kname in self.store.hosted.items():
            if kname:
                key_by_keepalive.setdefault(str(kname), key)
        area = self._split_area()
        migrated: dict[str, str] = {}
        for spec in area.pane_specs():
            kname = spec.keepalive_name
            if not kname:
                continue
            new_key = key_by_keepalive.get(kname)
            if new_key and new_key != spec.session_key:
                migrated[spec.session_key] = new_key
                self._split_store.migrate_session_key(spec.session_key, new_key)
        area.reconcile_session_keys(key_by_keepalive)
        return migrated

    def _sync_split_marks(self) -> None:
        """把右栏当前分屏组合与激活格投影到组标题和激活子会话底色。

        右栏格数、格内绑定的会话、激活格都可能变；这里统一取一次现状交给列表，
        列表内部会跟上次比对，没变就不动 DOM。
        """
        if not self.embed_ok:
            return
        try:
            area = self._split_area()
            session_list = self.query_one(SessionListView)
        except Exception:  # noqa: BLE001 分栏/列表重建中间态查不到，下一轮兜底同步会补上
            return
        session_list.set_split_marks(area.ordered_session_keys(), area.focus_key)

    def _save_split_layout(self) -> None:
        from pickup import split_layout

        if not self.embed_ok:
            return
        # 右栏格数/绑定/焦点变了才会走到这里，顺手把侧边栏的当前组与激活会话
        # 底色对齐。已结束成员仍属于会话组，因此持久化不再按活跃状态裁剪。
        self._sync_split_marks()
        area = self._split_area()
        keys = [
            k for k in area.ordered_session_keys()
            if not k.startswith("__")
        ]
        if len(keys) >= 2:
            focus = area.focus_key if area.focus_key in keys else keys[0]
            self._split_store.set_group(area.current_project, keys, focus_key=focus)
        split_layout.save_layout(self._split_store)

    def _save_sidebar_state(self) -> None:
        """保存会话组折叠与置顶状态；不改动右栏当前布局。"""
        from pickup import split_layout

        split_layout.save_layout(self._split_store)

    def _on_pane_close(self, session_key: str) -> None:
        from pickup import split_layout

        self._split_store.remove_session(session_key)
        split_layout.save_layout(self._split_store)
        self._sync_split_marks()
        self.call_next(self._rebuild_sidebar_projection)
        # 焦点由 SplitPaneArea 收尾：还有剩余实时格就接着用，最后一格被关掉才
        # 回列表。这里再调一次 _focus_list() 会把焦点提前抢走，让接力落空。

    async def _rebuild_sidebar_projection(self) -> None:
        """只重建会话组树，不触发右栏跟随，避免关格时重挂仍存活的同伴格。"""
        session_list = self.query_one(SessionListView)
        await session_list.rebuild()
        self._update_header()
        self._sync_split_marks()

    def _on_runtime_pick(self, runtime_id: str) -> None:
        import pickup

        area = self._split_area()
        if not area.can_add_pane():
            self.notify(t("split.full"))
            self.app.bell()
            return
        session_list = self.query_one(SessionListView)
        session = session_list.selected_session()
        cwd = area.current_project or pickup.usable_cwd(
            pickup._new_session_cwd(self.store, self.nav, session)
        )
        if cwd is None:
            self.notify(t("split.no_project"))
            self.app.bell()
            return
        request = pickup.NewSessionRequest(runtime_id, cwd)
        self._embed_open(request, add_pane=True)

    def _try_restore_startup_layout(self) -> None:
        """启动时从持久会话组中恢复仍活跃/托管的成员。"""
        if not self.embed_ok or self.direct is not None:
            return
        # 扫描未完成时 _is_session_active 全假；此时 prune+save 会把磁盘上的
        # 分屏记忆整份清空，且后续首屏也不会再恢复（真机：重启后组合丢失）。
        if not self.store.loaded:
            return
        self._reconcile_split_session_keys()
        focus = self._split_store.last_focus_key
        if focus and self._is_session_active(focus):
            self._show_session_group(focus)
            return
        project = self._split_store.last_project
        if not project:
            return
        for group in self._split_store.groups.values():
            if group.project_cwd != project:
                continue
            alive = [k for k in group.session_keys if self._is_session_active(k)]
            if alive:
                self._show_session_group(alive[0])
                return

    def _show_session_group(
        self,
        focus_key: str,
        *,
        focus_pane: bool = False,
        include_inactive: bool = False,
    ) -> None:
        if not self.embed_ok:
            return
        from pickup import split_layout

        group = self._split_store.get_group(focus_key)
        if include_inactive and group is not None:
            project = group.project_cwd
            keys = [
                key
                for key in group.session_keys
                if self.store.find_session(key) is not None
            ]
        else:
            project, keys = split_layout.resolve_active_group(
                self._split_store,
                focus_key,
                is_active=self._is_session_active,
                find_session=self.store.find_session,
            )
        entries = self._build_hosted_entries(keys)
        if not entries:
            return
        self._split_area().show_hosted_group(
            project, entries, focus_key=focus_key, focus_pane=focus_pane and self._can_autofocus(),
        )
        self._save_split_layout()
        self._begin_attention_read(focus_key)

    def _build_hosted_entries(
        self, keys: list[str],
    ) -> list[tuple[dict, str | None, object]]:
        entries: list[tuple[dict, str | None, object]] = []
        for key in keys:
            session = self.store.find_session(key)
            if session is None:
                continue
            kname = session.get("keepalive_name")
            if kname:
                entries.append(
                    (session, str(kname), lambda s=session: self._render_detail(s)),
                )
                continue
            entries.append((session, None, lambda s=session: self._render_detail(s)))
            if session.get("live"):
                # 在别的窗口跑的会话没有实时画面，右栏就是这份对话；助手还在写
                # 历史文件，mtime 一变 peek_conversation 就失效（正文会空掉），
                # 必须每轮重扫后台补读一次，"下方对话持续更新"才成立。
                self._warm_conversation(session, self._preview_gen)
        return entries

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
        # __init__ 时 store 还没扫完，默认来源只能先假定成 registry 里的第一个；
        # 扫完后如果它其实没有会话而别的运行时有，重新选一次，跟 __init__ 里
        # "挑第一个有会话的运行时"这条默认选择逻辑保持一致。
        if not self.store.sessions.get(self.nav.source):
            alt = next(
                (rid for rid in self.store.registry.ids if self.store.sessions[rid]), None,
            )
            if alt is not None:
                self.nav.source = alt
        self.call_next(self._rebuild_and_follow)
        self._start_background_refresh()
        self._schedule_search_index_warm()

    async def _rebuild_and_follow(self) -> None:
        await self._rebuild_list()
        self._try_restore_startup_layout()

    # ---- 后台重扫：Textual worker（取代旧版裸 threading.Thread + 0.5s dirty 轮询），
    # 发现变化直接 call_from_thread 触发重建，不再有轮询延迟；连续空闲多轮后自适应
    # 拉长扫描间隔，省磁盘/CPU；任何异常都要捕获且继续循环，不能让后台线程静默死掉 ----

    def _start_background_refresh(self) -> None:
        self._background_refresh_worker()

    @work(thread=True, exclusive=True, group="session-refresh")
    def _background_refresh_worker(self) -> None:
        import pickup

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
                pickup._log_embed_error("后台会话重扫线程", exc)
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

    async def _rebuild_list(self, select_key: str | None = None) -> None:
        async with self._rebuild_lock:
            session_list = self.query_one(SessionListView)
            migrated: dict[str, str] = {}
            if self.embed_ok and self.store.loaded:
                migrated = self._reconcile_split_session_keys()
                self._save_sidebar_state()
            if select_key is None:
                selected_key = session_list._displayed_selected_key()
                select_key = migrated.get(selected_key) if selected_key else None
            else:
                select_key = migrated.get(select_key, select_key)
            await session_list.rebuild(select_key=select_key)
            self._update_header()
            if self.embed_ok:
                # 仅刷新当前可见预览格，避免每次重扫都把 Cursor store.db 预览整页重载。
                self._split_area().invalidate_visible_previews()
                self._schedule_follow_selection()

    def _update_header(self) -> None:
        """刷新搜索框占位文案：空查询时展示命中数；出错/无会话时给出原因。"""
        session_list = self.query_one(SessionListView)
        search = self.query_one("#project-search", Input)
        count = len(session_list.visible_sessions())
        load_error = self.store.get_load_error()
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
        elif self.nav.project_query.strip():
            search.placeholder = t("filter.placeholder_count_active", count=count)
        else:
            search.placeholder = t("filter.placeholder_count", count=count)

    # ---- 选择跟随：右栏默认展示左栏当前选中项 ----

    def on_list_view_highlighted(self, event) -> None:
        # 高亮一变就立刻终止上一条会话的稳定查看计时，不能等 120ms 的右栏跟随
        # 节流结束，否则快速掠过时旧红点可能在这段空窗里被误清。
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

    def _on_app_focus_changed(self, focused: bool) -> None:
        """终端应用失焦即作废连续查看；重新聚焦后从零开始计算。"""
        self._app_focused = bool(focused)
        self._cancel_attention_read()
        if focused:
            self.call_next(self._begin_selected_attention_read)

    def _cancel_attention_read(self) -> None:
        timer = self._attention_read_timer
        self._attention_read_timer = None
        if timer is not None:
            timer.stop()
        self._attention_read_key = None
        self._attention_read_token = None
        self._attention_visible_since = None

    def _begin_selected_attention_read(self) -> None:
        if not self.embed_ok:
            return
        key = self.query_one(SessionListView)._displayed_selected_key()
        if key is None:
            return
        self._begin_attention_read(key)

    def _begin_attention_read(self, key: str) -> None:
        """开始观察一条红点会话；此时不等于已读，先等右侧内容真实就绪。"""
        self._cancel_attention_read()
        if not self.embed_ok or not self._app_focused:
            return
        session = self.store.find_session(key)
        if session is None or session.get("attention_kind") != "unread":
            return
        self._attention_read_key = key
        self._attention_read_token = session.get("attention_token")
        self._attention_read_timer = self.set_timer(
            _ATTENTION_READY_POLL, self._check_attention_read,
        )

    def _attention_view_ready(self, key: str) -> bool:
        """目标会话是否仍被选中，且右侧已画出可读的预览或真实终端首帧。"""
        if not self.embed_ok or not self._app_focused:
            return False
        if self.query_one(SessionListView)._displayed_selected_key() != key:
            return False
        session = self.store.find_session(key)
        if session is None or session.get("attention_kind") != "unread":
            return False
        area = self._split_area()
        for cell in area.cells():
            if cell.spec.session_key != key:
                continue
            pane = cell.embed_pane()
            if pane is None or pane.size.width <= 0 or pane.size.height <= 0:
                return False
            keepalive_name = cell.spec.keepalive_name
            if keepalive_name:
                # 仅挂上控件或正在显示静态回退都不算；首帧成功写入网格后，用户
                # 才真正看到了这个托管终端。
                return (
                    pane.session_name == keepalive_name
                    and not pane.dead
                    and pane._grid is not None  # noqa: SLF001
                )
            # 静态详情只有对话缓存已成功填充后才算就绪；加载异常会一直保持 None，
            # 因而不会因为右栏只出现标题或空白回退而误清红点。
            return pane.session_name is None and self.store.peek_conversation(session) is not None
        return False

    def _check_attention_read(self) -> None:
        """轮询首帧/预览就绪，并在连续可见 0.5 秒后清除红点。"""
        import time as _time

        self._attention_read_timer = None
        key = self._attention_read_key
        if key is None:
            return
        session = self.store.find_session(key)
        if session is None or session.get("attention_kind") != "unread":
            self._cancel_attention_read()
            return
        token = session.get("attention_token")
        if token != self._attention_read_token:
            # 正在看的 0.5 秒内又到了一条新结果：旧计时不能顺手把新结果也标成
            # 已读，必须从新内容真实可见的时刻重新完整计算。
            self._attention_read_token = token
            self._attention_visible_since = None
        if not self._attention_view_ready(key):
            self._attention_visible_since = None
            if self._app_focused:
                self._attention_read_timer = self.set_timer(
                    _ATTENTION_READY_POLL, self._check_attention_read,
                )
            return
        now = _time.monotonic()
        if self._attention_visible_since is None:
            self._attention_visible_since = now
            self._attention_read_timer = self.set_timer(
                _ATTENTION_READ_DELAY, self._check_attention_read,
            )
            return
        remaining = _ATTENTION_READ_DELAY - (now - self._attention_visible_since)
        if remaining > 0:
            self._attention_read_timer = self.set_timer(
                remaining, self._check_attention_read,
            )
            return

        self._attention_read_key = None
        self._attention_read_token = None
        self._attention_visible_since = None
        state = self.store.mark_session_read(key)
        if state.kind == "none":
            # mark_session_read 已原地更新会话快照；重建只会刷新发生变化的卡片，
            # 同时让详情头的可访问文字与红点一起消失。
            self.call_next(self._rebuild_list, key)

    def _follow_current_selection(self) -> None:
        if not self.embed_ok:
            return
        session_list = self.query_one(SessionListView)
        if session_list.multi_count() > 0:
            return
        area = self._split_area()
        if area.any_embed_focused():
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
        import pickup

        key = pickup.session_key(session)
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
            from pickup import split_layout

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
                (pickup.session_key(s), kn) for s, kn, _ in entries
            ]
            if (
                area.hosted_identity() == target_identity
                and key in {k for k, _ in target_identity}
            ):
                area.show_hosted_group(project, entries, focus_key=key)
                self._begin_attention_read(key)
                return
            area.show_hosted_group(project, entries, focus_key=key)
            self._save_split_layout()
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
        area.show_single_preview(session, lambda s=session: self._render_detail(s))
        self._begin_attention_read(key)

    def _detail_header(self, session: dict) -> Text:
        import pickup

        title = self.store.get_title(session)
        runtime = self.store.registry.get(str(session.get("source") or ""))
        status = t(_status_key(session))
        attention = t(_attention_key(session))
        project = str(
            session.get("cwd") or session.get("cwd_display") or t("project.unknown")
        )
        out = Text(title, style="bold")
        out.append("\n")
        out.append(runtime.display_name, style=pickup.runtime_label_style(runtime.id))
        out.append(f" · {status}", style="dim")
        out.append(f" · {attention}", style="dim")
        out.append("\n" + project, style="dim")
        # 在别的窗口跑的会话右栏永远只有静态对话，不说明原因就会被当成"会话已断"。
        if is_external_running(session):
            out.append("\n" + t("detail.running_external"), style="#B8860B")
        return out

    def _render_detail(self, session: dict) -> Text:
        import pickup

        # 详情 renderer 会被 EmbedPane 缓存并延后调用；后台重扫后闭包捕获的 dict
        # 已不是 Store 当前对象，必须每次按稳定会话键重新解析最新快照。
        session = self.store.find_session(pickup.session_key(session)) or session
        out = self._detail_header(session)
        messages = self.store.peek_conversation(session)
        if messages is None:
            return out
        runtime = self.store.registry.get(str(session.get("source") or ""))
        runtime_name = runtime.display_name
        runtime_style = pickup.runtime_label_style(runtime.id)
        try:
            area = self._split_area()
            cells = area.cells()
            if cells:
                width = max(20, (cells[0].embed_pane().size.width or 40) - 2)
            else:
                width = 40
        except Exception:
            width = 40
        lines = pickup._preview_lines(messages, runtime_name, width)
        out.append("\n")
        for i, (kind, line, suffix) in enumerate(lines):
            out.append("\n")
            # 角色与正文同色：user 用 cyan，assistant 用该 runtime 品牌色，整段（含续行）一致。
            if kind == "assistant":
                style = runtime_style
            else:
                style = {"user": "bold cyan", "dim": "dim"}.get(kind, "")
            out.append(line, style=style)
            if suffix:
                out.append(suffix, style="dim")
        return out

    @work(thread=True)
    def _warm_conversation(self, session: dict, gen: int) -> None:
        """后台填对话缓存；仅当仍是当前选中世代时刷新右栏。"""
        try:
            self.store.get_conversation(session)
        except Exception:
            return
        if gen != self._preview_gen:
            return
        self.app.call_from_thread(self._refresh_preview_detail)

    def _refresh_preview_detail(self) -> None:
        if not self.embed_ok:
            return
        area = self._split_area()
        if area.any_embed_focused():
            return
        area.invalidate_all_details()

    # ---- 右上角会话小窗：只画在激活格、只对实时托管画面画 ----

    def _hud_target(self) -> tuple[str | None, dict | None]:
        """返回该画小窗的 (会话键, 会话)；不该画时返回 (None, None)。

        条件有两条：这一格是当前激活格，且它是**实时托管画面**。已结束会话的
        右栏本来就是完整对话，浮层只会挡住它自己的正文。
        """
        if not self.embed_ok:
            return None, None
        try:
            area = self._split_area()
        except Exception:
            # 内嵌不可用时右栏根本不在 DOM 里（纯列表模式），不能裸 query_one。
            return None, None
        key = area.focus_key
        if not key:
            return None, None
        spec = next((s for s in area.pane_specs() if s.session_key == key), None)
        if spec is None or not spec.keepalive_name:
            return None, None
        # 占位卡（直启/空白新建后尚未写出真实历史）在快照里找不到，先不画。
        return key, self.store.find_session(key)

    def _sync_hud(self) -> None:
        """把小窗刷成当前激活格的最新摘要。主线程调用，只做 stat + 内存缓存判定。"""
        if not self.embed_ok:
            return
        try:
            area = self._split_area()
        except Exception:
            return
        key, session = self._hud_target()
        if key is None or session is None:
            area.sync_hud(None, None, expanded=False)
            return
        messages = self.store.peek_conversation(session)
        if messages is None:
            # 助手正在写历史，内存缓存已按 mtime 失效：继续显示上一次的摘要，
            # 同时按节流去后台重解析，避免小窗每秒空一下再闪回来。
            data = self._hud_cache.get(key)
            self._schedule_hud_warm(session, key)
        else:
            data = summarize_user_messages(messages)
            self._hud_cache[key] = data
        area.sync_hud(key, data or None, expanded=self._hud_expanded)

    def _schedule_hud_warm(self, session: dict, key: str) -> None:
        now = time.monotonic()
        if key == self._hud_warm_key and now - self._hud_warm_at < HUD_WARM_INTERVAL:
            return
        self._hud_warm_key = key
        self._hud_warm_at = now
        self._warm_hud(session, key)

    @work(thread=True, exclusive=True, group="hud-warm")
    def _warm_hud(self, session: dict, key: str) -> None:
        """后台解析对话（超大会话可到 200ms 量级），完成后回主线程刷小窗。"""
        try:
            self.store.get_conversation(session)
        except Exception:
            return
        self.app.call_from_thread(self._sync_hud)

    def action_toggle_hud(self) -> None:
        """展开/收起会话小窗；展开状态所有格共用一份。"""
        if not self.embed_ok:
            return
        self._hud_expanded = not self._hud_expanded
        self._sync_hud()

    def _open_split_from_selection(self, keys: list[str]) -> None:
        """按侧边栏多选组合开分屏（活跃会话内嵌，已结束会话预览）。"""
        if not self.embed_ok or len(keys) < 2:
            return
        import pickup
        from pickup import split_layout
        from pickup.split_layout import MAX_PANES

        keys = keys[:MAX_PANES]
        focus_key = keys[0]
        focus_session = self.store.find_session(focus_key)
        project = pickup._normalize_cwd(focus_session.get("cwd")) if focus_session else ""
        entries = self._build_hosted_entries(keys)
        if len(entries) < 2:
            self.app.bell()
            return
        area = self._split_area()
        area.show_hosted_group(
            project, entries, focus_key=focus_key, focus_pane=self._can_autofocus(),
        )
        self._begin_attention_read(focus_key)
        self._sync_split_marks()
        self._split_store.set_group(project, keys, focus_key=focus_key)
        split_layout.save_layout(self._split_store)
        self.call_next(self._rebuild_list, focus_key)
        self._preview_gen += 1
        for key in keys:
            session = self.store.find_session(key)
            if session is not None:
                self._warm_conversation(session, self._preview_gen)

    # ---- 会话选择/新建 ----

    @work
    async def on_list_view_selected(self, event) -> None:
        session_list = self.query_one(SessionListView)
        # 每次「打开」都要消费掉按下前的持有输入会话，避免上一次点击的旧值留到下一次判定。
        focus_before_click = session_list.take_focus_before_click()
        multi = session_list.multi_keys()
        if len(multi) >= 2:
            session_list.clear_multi()
            self._open_split_from_selection(multi)
            return
        session_list.clear_multi()
        if session_list.is_new_session_selected():
            await self._start_new_session_flow()
            return
        group = session_list.selected_group()
        if group is not None:
            focus_key = (
                group.focus_key
                if group.focus_key in group.session_keys
                else group.session_keys[0]
            )
            if self.embed_ok:
                self._show_session_group(
                    focus_key, focus_pane=True, include_inactive=True
                )
                return
            session = self.store.find_session(focus_key)
        else:
            session = session_list.selected_session()
        if session is None:
            return
        import pickup
        session_key = pickup.session_key(session)
        if self._click_returns_focus_to_list(focus_before_click, session_key):
            self._focus_list()
            return
        if self._split_store.get_group(session_key) is not None:
            self._show_session_group(
                session_key, focus_pane=True, include_inactive=True
            )
            return
        request = pickup.LaunchRequest(
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

    async def _open_or_exit(self, request) -> None:
        """embed 可用则原地内嵌打开；否则退出应用，交给外层 execvp 全屏接管。"""
        if not await self._confirm_external_resume(request):
            return
        if self.embed_ok:
            self._embed_open(request)
        else:
            self.app.exit(result=request)

    async def _confirm_external_resume(self, request) -> bool:
        """会话正在别的窗口跑时，"打开"实际是另起一个恢复进程——先问过用户。

        原来这一步是静默的：点进去右栏冒出一个刚从历史恢复的新进程，原窗口那个
        还在跑，观感就是"会话已中断"，而且两个进程写同一份历史有互相覆盖的风险。
        """
        import pickup

        if not isinstance(request, pickup.LaunchRequest):
            return True
        # 跨运行时接力只读原历史、另建目标会话，没有互相覆盖的问题，不拦。
        if request.session.get("source") != request.target_runtime_id:
            return True
        session = self.store.find_session(pickup.session_key(request.session)) or request.session
        if not is_external_running(session):
            return True
        title = self.store.get_title(session)
        return bool(
            await self.app.push_screen_wait(
                ConfirmModal(t("confirm.resume_external_running", title=title), confirm_key="r")
            )
        )

    def _embed_open(self, request, *, add_pane: bool = False) -> None:
        """准备启动计划（不涉及阻塞 I/O）后，把 `embed.host_session` 这个真正阻塞的
        tmux 子进程调用甩给后台 worker（见 `_host_and_focus`），不在 Textual 事件
        循环所在线程上跑——tmux 卡顿（系统负载高/磁盘慢）时 `_CREATE_TIMEOUT` 上限
        有 5s，同步跑会把整个 UI 冻住那么久。"""
        from pickup import keepalive
        import pickup
        from pickup.split_layout import MAX_PANES

        same_runtime = isinstance(request, pickup.LaunchRequest) and (
            request.session.get("source") == request.target_runtime_id
        )
        area = self._split_area()
        if isinstance(request, pickup.LaunchRequest):
            key = pickup.session_key(request.session)
            current = self.store.find_session(key) or request.session
            request = pickup.LaunchRequest(current, request.target_runtime_id, request.title)
            existing = request.session.get("keepalive_name") if same_runtime else None
            if existing:
                # 回车打开已托管会话 = 明确意图，直接把输入交给右栏那一格。
                if add_pane:
                    area.add_hosted_pane(
                        current, str(existing),
                        lambda s=current: self._render_detail(s),
                        focus=True,
                        focus_pane=self._can_autofocus(),
                    )
                else:
                    self._show_session_group(key, focus_pane=True)
                return
            if self._host_pending > 0 and not add_pane:
                self.app.bell()
                return
            if add_pane and (area.pane_count() + self._host_pending) >= MAX_PANES:
                self.notify(t("split.full"))
                self.app.bell()
                return
            plan = self.store.registry.build_launch_plan(request)
            ident = request.session["id"] if same_runtime else keepalive.new_session_ident()
        else:
            if not add_pane and area.pane_count() > 0 and not area.can_add_pane():
                self.notify(t("split.full"))
                self.app.bell()
                return
            if self._host_pending > 0 and not add_pane:
                self.app.bell()
                return
            if add_pane and (area.pane_count() + self._host_pending) >= MAX_PANES:
                self.notify(t("split.full"))
                self.app.bell()
                return
            plan = self.store.registry.build_new_session_plan(request)
            ident = keepalive.new_session_ident()

        width, height = area.host_pane_size()
        self._host_pending += 1
        self._host_and_focus(
            request, plan, ident, same_runtime, width, height, add_pane=add_pane,
        )

    @work(thread=True, group="host")
    def _host_and_focus(
        self, request, plan, ident, same_runtime, width, height, *, add_pane: bool = False,
    ) -> None:
        from pickup import embed
        from pickup import observe
        import pickup
        import time

        t0 = time.perf_counter()
        runtime = request.target_runtime_id
        try:
            name = embed.host_session(
                plan, request.target_runtime_id, ident, width, height, osc_report=self.osc_report,
            )
        except Exception as exc:
            observe.event(
                "host_session",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                runtime=runtime,
                ok=False,
            )
            pickup._log_embed_error("内嵌会话启动线程", exc)
            self.app.call_from_thread(self._on_host_failed)
            return
        observe.event(
            "host_session",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            runtime=runtime,
            ok=True,
        )
        self.app.call_from_thread(
            self._on_embed_hosted, request, name, same_runtime, add_pane,
        )

    def _on_host_failed(self) -> None:
        """host worker 失败收尾：释放托管计数并给用户终端响铃。"""
        self._host_pending = max(0, self._host_pending - 1)
        self._restore_direct_search_focus()
        self.app.bell()

    def _restore_direct_search_focus(self) -> None:
        """直启托管结束（成功或失败）后恢复搜索框可聚焦，并清掉 OSC 泄漏垃圾。"""
        if self.direct is None:
            return
        search = self.query_one("#project-search", Input)
        search.can_focus = True
        if _filter_looks_like_osc_leak(search.value):
            search.value = ""
            self.nav.project_query = ""

    def _on_embed_hosted(
        self, request, name: str, same_runtime: bool, add_pane: bool = False,
    ) -> None:
        """`_host_and_focus` worker 成功后的收尾：只在主线程操作 Textual/store 状态。

        `request` 可能是 `LaunchRequest`（恢复/接力）或 `NewSessionRequest`（空白新建）。
        后者没有关联会话，不能读 `.session`——空白新建路径曾经因此闪退。

        跨运行时接力 / 空白新建时目标助手可能尚未落盘历史（例如 Cursor 卡在
        Workspace Trust），扫描器看不到条目；必须立刻插入托管占位卡并选中它，
        否则左栏空白、随后的 `_rebuild_list` 还会按仍选中的源会话把右栏盖回去。
        """
        import pickup

        self._host_pending = max(0, self._host_pending - 1)
        area = self._split_area()
        fallback = None
        select_key = None
        if isinstance(request, pickup.LaunchRequest):
            current = request.session
            if same_runtime:
                key = pickup.session_key(request.session)
                marked = self.store.mark_hosted(key, name)
                if marked is None:
                    request.session["keepalive_name"] = name
                current = marked or request.session
            else:
                source_name = self.store.registry.get(
                    str(request.session.get("source") or "")
                ).display_name
                title = request.title or f"接力自 {source_name}"
                current = self.store.register_hosted_session(
                    runtime_id=request.target_runtime_id,
                    keepalive_name=name,
                    title=title,
                    cwd=str(request.session.get("cwd") or "") or None,
                )
                select_key = pickup.session_key(current)
            fallback = lambda s=current: self._render_detail(s)
        else:
            runtime = self.store.registry.get(request.target_runtime_id)
            current = self.store.register_hosted_session(
                runtime_id=request.target_runtime_id,
                keepalive_name=name,
                title=f"新{runtime.display_name}会话",
                cwd=request.cwd,
            )
            select_key = pickup.session_key(current)
            fallback = lambda s=current: self._render_detail(s)
        # 新建 / 接力托管成功同样是明确意图：用户就是来跟这个新会话说话的。
        autofocus = self._can_autofocus()
        if add_pane:
            area.add_hosted_pane(
                current, name, fallback, focus=True, focus_pane=autofocus,
            )
        else:
            import pickup as pickup_mod

            key = pickup.session_key(current)
            project = pickup_mod._normalize_cwd(current.get("cwd"))
            area.show_hosted_group(
                project,
                [(current, name, fallback)],
                focus_key=key,
                focus_pane=autofocus,
            )
        self._save_split_layout()
        self._begin_attention_read(pickup.session_key(current))
        self.call_next(self._rebuild_list, select_key)

    def _host_direct_launch(self) -> None:
        if self._host_pending >= 3:
            self.app.bell()
            return
        direct = self.direct
        area = self._split_area()
        width, height = area.host_pane_size()
        self._host_pending += 1
        self._host_direct_worker(direct, width, height)

    @work(thread=True, group="host")
    def _host_direct_worker(self, direct, width: int, height: int) -> None:
        from pickup import embed
        from pickup import observe
        import pickup
        import time

        t0 = time.perf_counter()
        runtime = direct.runtime_id
        try:
            name = embed.host_session(
                direct.plan, direct.runtime_id, direct.ident, width, height, osc_report=self.osc_report,
            )
        except Exception as exc:
            observe.event(
                "host_session",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                runtime=runtime,
                ok=False,
            )
            pickup._log_embed_error("直启会话启动线程", exc)
            self.app.call_from_thread(self._on_host_failed)
            return
        observe.event(
            "host_session",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            runtime=runtime,
            ok=True,
        )
        self.app.call_from_thread(self._on_direct_hosted, name)

    def _on_direct_hosted(self, name: str) -> None:
        self._host_pending = max(0, self._host_pending - 1)
        self._restore_direct_search_focus()
        area = self._split_area()
        direct = self.direct
        runtime = self.store.registry.get(direct.runtime_id)
        cwd = direct.plan.cwd or os.getcwd()
        session = self.store.register_hosted_session(
            runtime_id=direct.runtime_id,
            keepalive_name=name,
            title=f"新{runtime.display_name}会话",
            cwd=cwd,
            ident=direct.ident,
        )
        from pickup import _normalize_cwd, session_key

        key = session_key(session)
        area.show_hosted_group(
            _normalize_cwd(cwd),
            [(session, name, None)],
            focus_key=key,
        )
        self._save_split_layout()
        self.call_next(self._rebuild_list, key)
        cells = area.cells()
        if cells:
            pane = cells[0].embed_pane()
            pane.focus_session(name)
            self.set_focus(pane)

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
        self.set_focus(self.query_one(SessionListView))

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
        if action in _LIST_ONLY_ACTIONS and self._live_embed_focused():
            return False
        return True

    def _on_pane_focused(self, session_key: str) -> None:
        """右栏某格拿到焦点后，侧边栏高亮切到同一会话（不改右栏布局）。"""
        list_view = self.query_one(SessionListView)
        list_view.clear_multi()
        if not list_view.select_session_key(session_key):
            return
        self._save_split_layout()
        # 选择事件与焦点事件可能同帧到达；放到下一轮，确保高亮变更的取消逻辑
        # 先执行，再以实际持有焦点的可见格重新开始完整 0.5 秒计时。
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
            from pickup.search import ConversationIndex

            self._search_index = ConversationIndex()
        return self._search_index

    def _schedule_search_index_warm(self) -> None:
        """把索引预热排到首屏画完之后，不要和首帧抢 CPU。

        预热跑在后台线程，但 Python 有 GIL：解析正文期间实测会让界面每帧多滞后
        4~5ms（p95 9~14ms），首屏出卡片因此慢了 110~165ms——而首屏目标本来就只有
        1 秒。延后一小会儿再开始，用户完全无感，首屏回归也回到噪声水平。
        """
        self.set_timer(_SEARCH_INDEX_WARM_DELAY, self._warm_search_index)

    @work(thread=True, group="search-index")
    def _warm_search_index(self) -> None:
        """在后台把对话正文读进索引。

        放后台线程是硬要求：首次要解析没缓存过的会话（本机实测约 1 秒），第二次
        起命中 SQLite 派生缓存只剩几十毫秒。失败不影响主流程——弹窗打开时发现
        索引没就绪会自己再建一次，那条路带进度显示。
        """
        import pickup

        try:
            self.search_index().refresh(self.store)
        except Exception as exc:
            pickup._log_embed_error("全文搜索索引预热", exc)

    # @work 是硬要求，不是可选优化：`push_screen_wait` 只能在 worker 里调用
    # （Textual 会直接抛 NoActiveWorker），与 action_handoff 同一个模式。
    @work
    async def action_search_content(self) -> None:
        from pickup.ui.search_modal import FullTextSearchModal

        # 侧边栏当前的筛选词大概率就是用户想搜的东西，带进弹窗省得重敲一遍。
        initial = self.nav.project_query.strip()
        key = await self.app.push_screen_wait(
            FullTextSearchModal(self.store, self.search_index(), initial)
        )
        if key:
            await self._reveal_session(key)

    async def _reveal_session(self, key: str) -> None:
        """把搜索结果选中的会话定位到侧边栏。

        选中的会话可能正被筛选词挡在列表外——那就先把筛选清掉，否则用户会看到
        「搜到了却跳不过去」。清空输入框本身也会触发一次重建（不带 select_key），
        它走的是"保持当前选中"分支，不会把这里定位好的选中项挤掉。
        """
        import pickup

        session_list = self.query_one(SessionListView)
        visible = {pickup.session_key(s) for s in session_list.visible_sessions()}
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
            list_view.index = 1 if list_view.visible_sessions() else 0

    def on_text_selected(self, event: events.TextSelected) -> None:
        """划词抬起：有选区则经 OSC 52 自动复制（无需再按 Ctrl+C / ⌘C）。

        Textual 在每次 MouseUp 都会发 TextSelected（含空点选）；无选区时跳过。
        """
        selected = self.get_selected_text()
        if selected:
            self.app.copy_to_clipboard(selected)

    def on_key(self, event) -> None:
        search = self.query_one("#project-search", Input)
        list_view = self.query_one(SessionListView)
        if search.has_focus:
            # 搜索框内 Down：跳到列表；不在这里绑 /，避免吞掉用户想输入的斜杠
            if event.key == "down":
                event.stop()
                list_view.focus()
                if list_view.index is None:
                    list_view.index = 1 if list_view.visible_sessions() else 0
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
        target = await choose_target_runtime(
            self.app, self.store, str(session.get("source") or self.nav.source)
        )
        if target is None:
            return
        import pickup
        request = pickup.LaunchRequest(session, target, self.store.get_title(session))
        await self._open_or_exit(request)

    @work
    async def action_kill_keepalive(self) -> None:
        from pickup import keepalive
        import pickup

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
        key = pickup.session_key(session)
        self.store.mark_hosted(key, None)
        if self.embed_ok:
            self._split_area().remove_by_keepalive(keepalive_name)
        await self._rebuild_list()

    @work
    async def action_delete_session(self) -> None:
        """x：彻底删除选中会话的本地历史，不可恢复；运行中/托管会话先结束再删。

        二次确认按 x（而不是复用 q），与结束会话共用同一套 ConfirmModal 交互形态，
        只是把确认键换成触发本动作的键，避免用户记混"删除按 x 确认却按了 q"。
        """
        import asyncio
        import sqlite3
        import pickup
        from pickup import keepalive
        from pickup.runtime import LaunchError

        session_list = self.query_one(SessionListView)
        session = session_list.selected_session()
        if session is None:
            self.app.bell()
            return
        key = pickup.session_key(session)
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
        if keepalive_name:
            keepalive.kill(keepalive_name)
            self.store.mark_hosted(key, None)
            if self.embed_ok:
                self._split_area().remove_by_keepalive(keepalive_name)
        # 乐观 UI：确认后立刻从内存与侧边栏摘除；磁盘 delete 可能较慢（如 Cursor
        # 整目录 rmtree），期间后台 refresh 仍可能扫到路径——tombstone 挡回灌。
        self.store.mark_pending_delete(key)
        await self._rebuild_list()
        runtime = self.store.registry.get(str(session.get("source") or ""))
        try:
            await asyncio.to_thread(runtime.delete_session, session)
        except (LaunchError, OSError, sqlite3.Error) as exc:
            self.store.abort_pending_delete(key)
            try:
                self.store.refresh()
            except Exception:
                pass
            await self._rebuild_list()
            self.notify(t("notify.delete_failed", error=exc))
            self.app.bell()
            return
        self.store.finish_pending_delete(key)
        from pickup import split_layout

        self._split_store.remove_session(key)
        split_layout.save_layout(self._split_store)
        await self._rebuild_list()

    def action_close_pane(self) -> None:
        if not self.embed_ok:
            return
        self._split_area().close_focused_pane()
        self._save_split_layout()

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
        """F12：导出当前 TUI 到 ~/.cache/pickup/screenshots/（用户主动触发）。"""
        from pickup import observe

        try:
            path = observe.save_tui_screenshot(self.app)
        except Exception as exc:  # noqa: BLE001
            import pickup
            pickup._log_embed_error("TUI 截图", exc)
            self.app.bell()
            return
        self.notify(t("notify.screenshot", path=path), title="pickup", timeout=4)

    # ---- 客户端自动更新：右下角浮层 ----
    # 每次打开 pickup 都后台查一次最新版本；源码/开发安装（无法一键升级）时
    # 直接跳过，不弹窗打扰。检查/升级全程跑在 worker 线程，任何异常都不能
    # 拖垮 UI 或阻塞首屏——updater 模块本身已把网络/子进程异常全部吞掉。

    @work(thread=True, group="update-check")
    def _check_for_update(self) -> None:
        channel = updater.detect_channel()
        if not updater.is_updatable(channel):
            return
        latest = updater.fetch_latest()
        if latest is None or not updater.should_prompt(latest):
            return
        self._update_channel = channel
        self._update_latest = latest
        worker = get_current_worker()
        if not worker.is_cancelled:
            self.app.call_from_thread(lambda: self.query_one(UpdateToast).show_available(latest))

    def _on_update_toast_update(self) -> None:
        toast = self.query_one(UpdateToast)
        toast.show_updating()
        self._run_update_worker()

    @work(thread=True, group="update-apply")
    def _run_update_worker(self) -> None:
        from pickup import observe

        latest = self._update_latest
        ok, output = updater.run_update(latest, self._update_channel)
        observe.event("self_update", ok=ok, latest=latest, channel=self._update_channel)
        if not ok:
            observe.debug("self_update_output", output=output)
        worker = get_current_worker()
        if worker.is_cancelled:
            return
        toast = self.query_one(UpdateToast)
        if ok:
            self.app.call_from_thread(lambda: toast.show_done(latest))
        else:
            self.app.call_from_thread(lambda: toast.show_failed(output))

    def _on_update_toast_restart(self) -> None:
        # 交给 cli.main()：用新装好的磁盘代码 re-exec 一个全新 pickup 进程。
        self.app.exit(result=updater.RestartRequest())

    def _on_update_toast_retry(self) -> None:
        self._on_update_toast_update()

    def _on_update_toast_dismiss(self, version: str) -> None:
        updater.mark_dismissed(version)
        self.query_one(UpdateToast).hide()

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
