"""会话中枢：把 pickup 已有的会话能力包成一套供手机调用的接口。

这一层是 `pickup remote` 里唯一持有 `SessionStore`、tmux 托管层和布局库的地方，
上面的协议层只管路由。所有带副作用的动作（送输入、新建、结束、删除）都收在这里，
`agent_api` 保持一行不动的只读契约。

线程模型：一个后台线程按固定周期重扫磁盘（与桌面端 TUI 同一套 `SessionStore`），
一个后台线程按需抓取被订阅会话的终端画面。两者都只往队列里塞事件，网络侧在自己的
事件循环里取走，彼此不阻塞。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from pickup import embed, keepalive, titles
from pickup.models import LaunchRequest, NewSessionRequest, session_key
from pickup.remote import richmsg
from pickup.remote.screen import ScreenEncoder
from pickup.runtime import LaunchError
from pickup.split_layout import default_layout_db, group_emoji
from pickup.store import SessionStore

_SCAN_LIMIT = 200
_REFRESH_INTERVAL = 3.0
_SCREEN_INTERVAL = 0.2       # 有人在看终端视图时的抓帧周期
_CONVERSATION_INTERVAL = 1.0  # 实时会话的富消息轮询周期
_HOST_WIDTH = 120            # 新建托管会话的默认窗口宽度（按桌面常见宽度，手机横向平移）
_HOST_HEIGHT = 40

_ATTENTION_LABELS = {"none": "none", "unread": "unread", "working": "working", "waiting": "waiting"}


class ActionError(RuntimeError):
    """动作无法执行；message 直接给用户看，必须是中文人话。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _ScreenWatch:
    key: str
    encoder: ScreenEncoder
    scroll_offset: int = 0
    watchers: int = 0
    cols: int = 0
    rows: int = 0
    size_checked_at: float = 0.0


@dataclass
class _ConversationWatch:
    key: str
    reader: richmsg.RichReader
    watchers: int = 0


class SessionHub:
    """开发机上所有会话相关能力的唯一入口。

    ``on_event`` 会在后台线程里被调用，参数是 (通道名, 数据)。实现方必须自己
    把它转投到网络侧的事件循环，不要在里面做阻塞 I/O。
    """

    def __init__(self, on_event=None, *, scan_limit: int = _SCAN_LIMIT) -> None:
        self.store = SessionStore(limit=scan_limit)
        self.registry = self.store.registry
        self.layout_db = default_layout_db()
        self._on_event = on_event or (lambda channel, data: None)
        self._lock = threading.Lock()
        self._screens: dict[str, _ScreenWatch] = {}
        self._conversations: dict[str, _ConversationWatch] = {}
        self._sessions_watchers = 0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_attention: dict[str, str] = {}
        self._attention_hook = None  # 由推送层注入：(session, 旧状态, 新状态)

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        self.store.load()
        self._snapshot_attention()
        for target in (self._refresh_loop, self._screen_loop, self._conversation_loop):
            thread = threading.Thread(target=target, daemon=True, name=f"remote-{target.__name__}")
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=1.0)
        embed.close_channel()

    def set_attention_hook(self, hook) -> None:
        """注册关注状态变化回调，供推送层订阅。"""
        self._attention_hook = hook

    # -- 后台循环 ---------------------------------------------------------

    def _refresh_loop(self) -> None:
        while not self._stop.wait(_REFRESH_INTERVAL):
            try:
                changed = self.store.refresh()
            except Exception:
                continue
            self._detect_attention_changes()
            if changed and self._sessions_watchers:
                self._on_event("sessions", {"sessions": self.list_sessions()})

    def _screen_loop(self) -> None:
        while not self._stop.wait(_SCREEN_INTERVAL):
            with self._lock:
                watches = [w for w in self._screens.values() if w.watchers > 0]
            for watch in watches:
                try:
                    self._pump_screen(watch)
                except Exception:
                    continue

    def _conversation_loop(self) -> None:
        while not self._stop.wait(_CONVERSATION_INTERVAL):
            with self._lock:
                watches = [w for w in self._conversations.values() if w.watchers > 0]
            for watch in watches:
                try:
                    new_messages = watch.reader.poll()
                except Exception:
                    continue
                if new_messages:
                    self._on_event(
                        f"session:{watch.key}",
                        {"messages": [m.to_dict() for m in new_messages]},
                    )

    # -- 会话查询 ---------------------------------------------------------

    def _layout(self):
        try:
            return self.layout_db.read()
        except Exception:
            return None

    @staticmethod
    def _wire_str(value: object) -> str | None:
        """手机端把若干字段按 String 解码；类型不符会让整份列表解码失败而空白。"""
        if value is None:
            return None
        text = str(value)
        return text

    @staticmethod
    def _wire_float(value: object, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def session_payload(self, session: dict, layout=None) -> dict:
        key = session_key(session)
        title = self.store.get_title(session)
        attention = str(session.get("attention_kind") or "none")
        runtime = self._wire_str(session.get("source"))
        payload = {
            "key": key,
            "runtime": runtime,
            "id": self._wire_str(session.get("id")),
            "short_id": self._wire_str(session.get("short_id")),
            "title": str(title or ""),
            "cwd": str(session.get("cwd") or ""),
            "cwd_display": str(session.get("cwd_display") or ""),
            "mtime": self._wire_float(session.get("mtime")),
            "time": str(session.get("display_time") or ""),
            "size_kb": round(self._wire_float(session.get("size_kb")), 1),
            "status": str(session.get("status_tag") or ""),
            "live": bool(session.get("live")),
            "hosted": bool(session.get("keepalive_name")),
            "attention": _ATTENTION_LABELS.get(attention, "none"),
            "last_user": str(session.get("last_user_msg") or "")[:160],
            "last_agent": str(session.get("last_agent_msg") or "")[:160],
            "rich": richmsg.supports_tool_calls(str(session.get("source") or "")),
            # 布局读失败时也给出稳定布尔，避免手机端 optional 与「未置顶」语义漂移
            "pinned": False,
        }
        if layout is not None:
            group = layout.get_group(key)
            if group is not None:
                pinned_groups = getattr(layout, "pinned_group_ids", {}) or {}
                payload["group"] = {
                    "id": str(group.group_id),
                    "name": str(group.name or ""),
                    "emoji": group_emoji(group.name),
                    "pinned": group.group_id in pinned_groups,
                }
            pinned_sessions = getattr(layout, "pinned_session_keys", {}) or {}
            payload["pinned"] = key in pinned_sessions
        return payload

    def list_sessions(self, query: str = "", limit: int = 0) -> list[dict]:
        layout = self._layout()
        sessions = self.store.all_sessions()
        payloads = [self.session_payload(s, layout) for s in sessions]
        if query:
            needle = query.strip().lower()
            payloads = [
                p
                for p in payloads
                if needle in (p["title"] or "").lower()
                or needle in (p["cwd_display"] or "").lower()
                or needle in (p["last_user"] or "").lower()
                or needle in (p["last_agent"] or "").lower()
                or needle in str((p.get("group") or {}).get("name") or "").lower()
            ]
        if limit > 0:
            payloads = payloads[:limit]
        return payloads

    def require_session(self, key: str) -> dict:
        session = self.store.find_session(key)
        if session is None:
            raise ActionError("not_found", "这条会话已经不在列表里了")
        return session

    def session_detail(self, key: str) -> dict:
        session = self.require_session(key)
        payload = self.session_payload(session, self._layout())
        runtime = self._runtime_of(session)
        payload["runtime_name"] = getattr(runtime, "display_name", "")
        payload["resumable"] = self._resumable(runtime, session)
        payload["history_path"] = session.get("path") or ""
        return payload

    def messages(self, key: str, limit: int = 400) -> list[dict]:
        """整轮读出富消息。列表很长时只保留最近的一段——手机上没人会往上翻两千条。"""
        session = self.require_session(key)
        reader = richmsg.RichReader(session)
        items = reader.read_all()
        if limit > 0 and len(items) > limit:
            items = items[-limit:]
        return [m.to_dict() for m in items]

    def prompts(self, key: str) -> list[dict]:
        """当前仍待回答的提问型工具调用（含可点选项列表）。"""
        session = self.require_session(key)
        return richmsg.pending_prompts(session)

    def projects(self) -> list[dict]:
        # path/name 与 iOS NewSessionSheet 对齐；cwd/label 保留给桌面侧同一套项目列表语义。
        return [
            {
                "cwd": entry.get("cwd_key") or "",
                "path": entry.get("cwd_key") or "",
                "label": entry.get("label") or "",
                "name": entry.get("label") or "",
                "count": entry.get("count") or 0,
                "mtime": entry.get("latest_mtime") or 0.0,
            }
            for entry in self.store.projects()
        ]

    def runtimes(self) -> list[dict]:
        result = []
        for runtime_id in self.registry.ids:
            runtime = self.registry.get(runtime_id)
            result.append({
                "id": runtime_id,
                "name": runtime.display_name,
                "available": bool(runtime.is_available()),
            })
        return result

    def _runtime_of(self, session: dict):
        try:
            return self.registry.get(str(session.get("source") or ""))
        except Exception as exc:
            raise ActionError("unavailable", "这条会话对应的助手当前不可用") from exc

    @staticmethod
    def _resumable(runtime, session: dict) -> bool:
        try:
            runtime.build_resume_plan(session)
        except Exception:
            return False
        return True

    # -- 订阅 -------------------------------------------------------------

    def watch_sessions(self) -> None:
        with self._lock:
            self._sessions_watchers += 1

    def unwatch_sessions(self) -> None:
        with self._lock:
            self._sessions_watchers = max(0, self._sessions_watchers - 1)

    def watch_conversation(self, key: str) -> list[dict]:
        """订阅一条会话的实时聊天流，同时把已有历史一次性返回。

        首包历史用独立 reader 读拍：共享 reader 只负责增量 poll。
        否则第二台手机（或重连后的第二路订阅）会因为 watchers>1 拿到空列表，
        而若对共享 reader 再 read_all 又会把增量游标打乱、把旧消息当新消息重推。
        """
        session = self.require_session(key)
        with self._lock:
            watch = self._conversations.get(key)
            created = watch is None
            if watch is None:
                watch = _ConversationWatch(key, richmsg.RichReader(session))
                self._conversations[key] = watch
            watch.watchers += 1
        if created:
            # 推进共享游标到末尾，后续 poll 只推增量
            watch.reader.read_all()
        snapshot = richmsg.RichReader(session).read_all()
        return [m.to_dict() for m in snapshot]

    def unwatch_conversation(self, key: str) -> None:
        with self._lock:
            watch = self._conversations.get(key)
            if watch is None:
                return
            watch.watchers -= 1
            if watch.watchers <= 0:
                self._conversations.pop(key, None)

    def watch_screen(self, key: str) -> dict | None:
        """订阅终端画面。返回首帧（整屏）；会话没有托管在 tmux 里则返回 None。"""
        session = self.require_session(key)
        if not session.get("keepalive_name"):
            raise ActionError("unavailable", "这条会话没有在后台运行，看不到实时画面")
        with self._lock:
            watch = self._screens.get(key)
            if watch is None:
                watch = _ScreenWatch(key, ScreenEncoder())
                self._screens[key] = watch
            watch.watchers += 1
            watch.encoder.reset()
        return self._capture_frame(watch)

    def unwatch_screen(self, key: str) -> None:
        with self._lock:
            watch = self._screens.get(key)
            if watch is None:
                return
            watch.watchers -= 1
            if watch.watchers <= 0:
                self._screens.pop(key, None)

    def resync_screen(self, key: str) -> dict | None:
        """已在订阅中的连接再要一帧整屏（不增加引用计数）。

        聊天页为状态条、终端页为画面会各调一次 screen.watch；协议层对同一连接
        只记一次订阅，第二次必须仍能拿到 full 基准帧，否则手机只能拿着空网格
        硬套增量，画面会错乱。
        """
        with self._lock:
            watch = self._screens.get(key)
            if watch is None or watch.watchers <= 0:
                raise ActionError("usage_error", "还没有在看这条会话的画面")
            watch.encoder.reset()
        return self._capture_frame(watch)

    def scroll_screen(self, key: str, offset: int) -> dict | None:
        with self._lock:
            watch = self._screens.get(key)
            if watch is None:
                raise ActionError("usage_error", "还没有在看这条会话的画面")
            watch.scroll_offset = max(0, int(offset))
            watch.encoder.reset()
        return self._capture_frame(watch)

    def _keepalive_name(self, key: str) -> str:
        session = self.require_session(key)
        name = str(session.get("keepalive_name") or "")
        if not name:
            raise ActionError("unavailable", "这条会话没有在后台运行")
        return name

    def _capture_frame(self, watch: _ScreenWatch) -> dict | None:
        session = self.store.find_session(watch.key)
        if session is None:
            return None
        name = str(session.get("keepalive_name") or "")
        if not name:
            return None
        state = embed.pane_state(name)
        if state is None:
            return None
        cursor_x, cursor_y, cursor_visible, _mouse_any, _mouse_sgr, history_size = state
        # pane 尺寸不必每帧都问：桌面端改窗口大小是低频动作，两秒查一次足够，
        # 却能把抓帧路径上的 tmux 往返从两次减到一次。
        now = time.monotonic()
        if watch.cols <= 0 or now - watch.size_checked_at > 2.0:
            size = embed.pane_size(name)
            if size is not None:
                watch.cols, watch.rows = size
            watch.size_checked_at = now
        if watch.cols <= 0:
            return None
        # 抓一屏画面。刻意不调用 resize：手机端订阅不该改变会话窗口尺寸，
        # 否则电脑上正在看同一个会话的人会被手机挤窄（这条是设计约束，别顺手改）。
        text = embed.capture(name, watch.scroll_offset, watch.rows)
        if text is None:
            return None
        grid = embed.parse_screen(text, watch.cols, watch.rows)
        frame = watch.encoder.encode(
            grid,
            cursor=(cursor_x, cursor_y, cursor_visible),
            history_size=history_size,
            history_offset=watch.scroll_offset,
        )
        return frame.to_dict() if frame is not None else None

    def _pump_screen(self, watch: _ScreenWatch) -> None:
        frame = self._capture_frame(watch)
        if frame is not None:
            self._on_event(f"screen:{watch.key}", frame)

    # -- 输入 -------------------------------------------------------------

    def send_text(self, key: str, text: str, submit: bool = True) -> None:
        """把一段文本送进会话。

        走 tmux 粘贴缓冲而不是逐字符发送：这条路径对中文输入法、多行文本和
        括号粘贴语义都是安全的，是桌面端已经验证过的写法。回车单独补一次，
        因为部分助手会把粘贴内容里的换行当成软换行而不是提交。
        """
        name = self._keepalive_name(key)
        if text:
            embed.paste(name, text)
        if submit:
            time.sleep(0.05)  # 给目标程序一点时间收完粘贴，避免回车抢在正文前面
            embed.send_key(name, "Enter")

    def send_keys(self, key: str, keys: list[str]) -> None:
        name = self._keepalive_name(key)
        cleaned = [str(k) for k in keys if str(k).strip()]
        if not cleaned:
            raise ActionError("usage_error", "没有要发送的按键")
        embed.send_key(name, *cleaned)

    def send_image(self, key: str, image_bytes: bytes) -> str:
        """把图片落到会话工作目录并把路径交给助手，复用桌面端已有的落盘+粘贴路径协议。"""
        if not image_bytes:
            raise ActionError("usage_error", "没有图片数据")
        name = self._keepalive_name(key)
        path = embed.save_image_and_paste_path(name, image_bytes)
        if not path:
            raise ActionError("unavailable", "图片没能保存到开发机上")
        return path

    # -- 会话动作 ---------------------------------------------------------

    def mark_read(self, key: str) -> str:
        state = self.store.mark_session_read(key)
        return _ATTENTION_LABELS.get(state.kind, "none")

    def toggle_pin(self, key: str) -> bool:
        """切换置顶；组成员不能单独置顶，改为切换整组置顶（与桌面侧栏一致）。

        返回值必须读 ``pinned_session_keys`` / ``pinned_group_ids``，不要再用已废弃的
        ``pinned_sessions``——那个属性不存在时 ``getattr`` 会落到空集合，接口永远回 false。
        """
        snapshot = self.layout_db.read()
        group = snapshot.get_group(key)
        if group is not None:
            layout = self.layout_db.toggle_group_pin(group.group_id)
            return group.group_id in (getattr(layout, "pinned_group_ids", {}) or {})
        layout = self.layout_db.toggle_session_pin(key)
        return key in (getattr(layout, "pinned_session_keys", {}) or {})

    def stop_session(self, key: str) -> None:
        session = self.require_session(key)
        name = str(session.get("keepalive_name") or "")
        if not name:
            raise ActionError("unavailable", "这条会话没有在后台运行")
        keepalive.kill(name)
        self.store.mark_hosted(key, None)

    def delete_session(self, key: str) -> None:
        session = self.require_session(key)
        runtime = self._runtime_of(session)
        self.store.mark_deleted(key)
        try:
            runtime.delete_session(session)
        except Exception as exc:
            self.store.abort_delete(key)
            raise ActionError("unavailable", f"删除失败：{exc}") from exc
        # 删除成功后同步清掉侧栏置顶/分组记忆，避免剩余成员仍挂着「幽灵组」
        try:
            self.layout_db.remove_session(key)
        except Exception:
            pass

    def _host(self, plan, runtime_id: str, title: str, cwd: str | None) -> dict:
        ident = keepalive.new_session_ident()
        width, height = embed.normalize_host_size(_HOST_WIDTH, _HOST_HEIGHT)
        try:
            name = embed.host_session(plan, runtime_id, ident, width, height)
        except embed.EmbedError as exc:
            raise ActionError("unavailable", f"启动失败：{exc}") from exc
        session = self.store.register_hosted_session(
            runtime_id=runtime_id,
            keepalive_name=name,
            title=title,
            cwd=cwd,
            ident=ident,
        )
        return self.session_payload(session, self._layout())

    def new_session(self, runtime_id: str, cwd: str | None) -> dict:
        runtime_id = str(runtime_id or "").strip()
        if not runtime_id:
            raise ActionError("usage_error", "请选择助手")
        if cwd is not None:
            cwd = str(cwd).strip() or None
        if not embed.available():
            raise ActionError("unavailable", "开发机上没有装 tmux，无法从手机启动会话")
        try:
            runtime = self.registry.get(runtime_id)
            plan = runtime.build_new_session_plan(cwd)
        except (KeyError, LaunchError) as exc:
            raise ActionError("usage_error", f"无法启动：{exc}") from exc
        request = NewSessionRequest(target_runtime_id=runtime_id, cwd=cwd or "")
        title = f"{runtime.display_name} · 新会话"
        del request  # 结构体只用于表达意图，实际启动只需要计划本身
        return self._host(plan, runtime_id, title, cwd)

    def resume_session(self, key: str) -> dict:
        """原生恢复：用助手自己的恢复命令重开这条会话，历史完整延续。"""
        if not embed.available():
            raise ActionError("unavailable", "开发机上没有装 tmux，无法从手机恢复会话")
        session = self.require_session(key)
        if session.get("keepalive_name"):
            return self.session_payload(session, self._layout())
        runtime = self._runtime_of(session)
        try:
            plan = runtime.build_resume_plan(session)
        except LaunchError as exc:
            raise ActionError("unavailable", f"这条会话无法原生恢复：{exc}") from exc
        ident = keepalive.new_session_ident()
        width, height = embed.normalize_host_size(_HOST_WIDTH, _HOST_HEIGHT)
        try:
            name = embed.host_session(plan, runtime.id, ident, width, height)
        except embed.EmbedError as exc:
            raise ActionError("unavailable", f"恢复失败：{exc}") from exc
        self.store.mark_hosted(key, name)
        refreshed = self.store.find_session(key) or session
        return self.session_payload(refreshed, self._layout())

    def handoff_session(self, key: str, target_runtime_id: str) -> dict:
        """跨助手接力：把原会话导出成提示词，在目标助手里新开一局。"""
        if not embed.available():
            raise ActionError("unavailable", "开发机上没有装 tmux，无法从手机接力")
        session = self.require_session(key)
        source = self._runtime_of(session)
        try:
            target = self.registry.get(target_runtime_id)
        except KeyError as exc:
            raise ActionError("usage_error", "选中的助手不存在") from exc
        title = self.store.get_title(session)
        request = LaunchRequest(session=session, target_runtime_id=target_runtime_id, title=title)
        try:
            handoff = source.export_handoff(request.session, request.title)
            plan = target.build_new_plan(handoff)
        except LaunchError as exc:
            raise ActionError("unavailable", f"接力失败：{exc}") from exc
        return self._host(plan, target_runtime_id, f"接力 · {title}", session.get("cwd"))

    # -- 关注状态变化 -----------------------------------------------------

    def _snapshot_attention(self) -> None:
        self._last_attention = {
            session_key(s): str(s.get("attention_kind") or "none") for s in self.store.all_sessions()
        }

    def _detect_attention_changes(self) -> None:
        """找出「刚刚从执行中变成等你回答」的会话，交给推送层。

        只报这一种跃迁：用户被叫回手机的唯一理由就是助手卡住等答复。把「有新消息」
        也做成推送会在长任务里刷屏，实测桌面端一轮任务能产生几十次状态波动。
        """
        hook = self._attention_hook
        layout = self._layout() if hook else None
        for session in self.store.all_sessions():
            key = session_key(session)
            current = str(session.get("attention_kind") or "none")
            previous = self._last_attention.get(key)
            self._last_attention[key] = current
            if hook is None or previous is None or current == previous:
                continue
            if current == "waiting":
                try:
                    hook(self.session_payload(session, layout), previous, current)
                except Exception:
                    continue

    # -- 杂项 -------------------------------------------------------------

    def title_cache_size(self) -> int:
        try:
            return len(titles.load_cache())
        except Exception:
            return 0
