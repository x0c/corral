"""方法路由与订阅管理：把手机发来的消息翻译成对会话中枢的调用。

这一层**与传输方式无关**——中继长连接和局域网直连共用同一个实例，各自把已经
解密好的消息喂进来。这样做的直接好处是：加一种连接方式不需要复制一遍全部业务
逻辑，也不会出现「中继能用、直连不能用」的功能漂移。

订阅的记账放在这里而不是会话中枢里：中枢只知道「有几个人在看这条会话」，
不知道这些人分别连在哪条线上；断线时由这一层负责把该连接的订阅全部退掉，
避免没人看了还在后台抓帧。
"""

from __future__ import annotations

import threading
import time

from pickup import __version__, observe
from pickup.remote import config as remote_config
from pickup.remote import crypto, protocol
from pickup.remote.sessions import ActionError, SessionHub

_PAIRING_TTL = 10 * 60  # 配对码有效期：够扫码，又不至于长期挂着一个可用凭据


class Connection:
    """一条已完成加密握手的连接。``send`` 必须是线程安全的。"""

    def __init__(self, device_public_key: str, send, *, address: str = "") -> None:
        self.device_public_key = device_public_key
        self.send = send
        self.address = address
        self.paired = False
        self.device_name = ""
        self.channels: set[str] = set()
        self.closed = False


class RemoteService:
    def __init__(self, hub: SessionHub | None = None) -> None:
        self.hub = hub or SessionHub(on_event=self._dispatch_event)
        if hub is not None:
            hub._on_event = self._dispatch_event
        self.state = remote_config.load_state()
        self._lock = threading.Lock()
        self._connections: set[Connection] = set()
        self._subscribers: dict[str, set[Connection]] = {}

    # -- 配对 -------------------------------------------------------------

    def begin_pairing(self, ttl: float = _PAIRING_TTL) -> str:
        code = crypto.new_pairing_code()
        remote_config.write_pairing(code, ttl)
        return code

    def pairing_open(self) -> bool:
        return remote_config.read_pairing() is not None

    def is_known_device(self, device_public_key: str) -> bool:
        return remote_config.find_device(self.state, device_public_key) is not None

    def accepts(self, device_public_key: str) -> bool:
        """握手阶段的准入判断：已配对设备随时可进，陌生设备只在配对窗口内放行。"""
        return self.is_known_device(device_public_key) or self.pairing_open()

    # -- 连接生命周期 -----------------------------------------------------

    def attach(self, connection: Connection) -> None:
        connection.paired = self.is_known_device(connection.device_public_key)
        with self._lock:
            self._connections.add(connection)
        if connection.paired:
            remote_config.touch_device(self.state, connection.device_public_key)

    def detach(self, connection: Connection) -> None:
        connection.closed = True
        with self._lock:
            self._connections.discard(connection)
            channels = list(connection.channels)
            for channel in channels:
                subscribers = self._subscribers.get(channel)
                if subscribers is not None:
                    subscribers.discard(connection)
                    if not subscribers:
                        self._subscribers.pop(channel, None)
            connection.channels.clear()
        for channel in channels:
            self._release_channel(channel)

    def _subscribe(self, connection: Connection, channel: str) -> bool:
        with self._lock:
            if channel in connection.channels:
                return False
            connection.channels.add(channel)
            self._subscribers.setdefault(channel, set()).add(connection)
        return True

    def _unsubscribe(self, connection: Connection, channel: str) -> bool:
        with self._lock:
            if channel not in connection.channels:
                return False
            connection.channels.discard(channel)
            subscribers = self._subscribers.get(channel)
            if subscribers is not None:
                subscribers.discard(connection)
                if not subscribers:
                    self._subscribers.pop(channel, None)
        return True

    def _release_channel(self, channel: str) -> None:
        """连接断开时把中枢侧的订阅计数减回去，别让后台白抓帧。"""
        if channel == protocol.CH_SESSIONS:
            self.hub.unwatch_sessions()
        elif channel.startswith("screen:"):
            self.hub.unwatch_screen(channel[len("screen:") :])
        elif channel.startswith("session:"):
            self.hub.unwatch_conversation(channel[len("session:") :])

    def _dispatch_event(self, channel: str, data) -> None:
        with self._lock:
            targets = list(self._subscribers.get(channel, ()))
        if not targets:
            return
        message = protocol.event(channel, data)
        for connection in targets:
            if connection.closed:
                continue
            try:
                connection.send(message)
            except Exception:
                continue

    # -- 消息处理 ---------------------------------------------------------

    def handle(self, connection: Connection, message: dict) -> None:
        if message.get("t") != "req":
            return
        req_id = int(message.get("id") or 0)
        method = str(message.get("m") or "")
        params = message.get("p") if isinstance(message.get("p"), dict) else {}
        try:
            data = self._invoke(connection, method, params)
        except ActionError as exc:
            connection.send(protocol.error(req_id, exc.code, exc.message))
            return
        except NotImplementedError:
            connection.send(protocol.error(req_id, protocol.E_USAGE, f"不支持的操作：{method}"))
            return
        except Exception as exc:
            observe.event("remote_method_failed", method=method, error=str(exc))
            connection.send(protocol.error(req_id, protocol.E_INTERNAL, "开发机上出了点问题，请稍后再试"))
            return
        connection.send(protocol.response(req_id, data))

    def _invoke(self, connection: Connection, method: str, params: dict):
        if method == protocol.M_PAIR:
            return self._pair(connection, params)
        if method == protocol.M_HELLO:
            return self._hello(connection, params)
        if not connection.paired:
            raise ActionError(protocol.E_UNAUTHORIZED, "这台设备还没有和开发机配对")
        handler = _HANDLERS.get(method)
        if handler is None:
            raise NotImplementedError(method)
        return handler(self, connection, params)

    # -- 具体方法 ---------------------------------------------------------

    def _pair(self, connection: Connection, params: dict):
        pairing = remote_config.read_pairing()
        if pairing is None:
            raise ActionError(protocol.E_UNAUTHORIZED, "配对码已经失效，请在开发机上重新生成")
        if not crypto.codes_equal(str(params.get("code") or ""), pairing[0]):
            raise ActionError(protocol.E_UNAUTHORIZED, "配对码不对")
        remote_config.clear_pairing()  # 一次性凭据，用掉立刻作废
        device = remote_config.PairedDevice(
            id=crypto.random_id(8),
            name=str(params.get("name") or "iPhone")[:60],
            public_key=connection.device_public_key,
            paired_at=time.time(),
            last_seen_at=time.time(),
            platform=str(params.get("platform") or "ios")[:20],
        )
        remote_config.add_device(self.state, device)
        connection.paired = True
        connection.device_name = device.name
        observe.event("remote_paired", device=device.name)
        return {"device_id": device.id, "host_name": self.state.host_name}

    def _hello(self, connection: Connection, params: dict):
        connection.device_name = str(params.get("name") or "")[:60]
        return {
            "protocol": protocol_version(),
            "pickup_version": __version__,
            "host_id": self.state.host_id,
            "host_name": self.state.host_name,
            "paired": connection.paired,
            "pairing_open": self.pairing_open(),
            "runtimes": self.hub.runtimes() if connection.paired else [],
        }

    def _sessions_list(self, connection: Connection, params: dict):
        return {
            "sessions": self.hub.list_sessions(
                query=str(params.get("q") or ""), limit=_int_param(params, "limit", 0)
            )
        }

    def _sessions_watch(self, connection: Connection, params: dict):
        if self._subscribe(connection, protocol.CH_SESSIONS):
            self.hub.watch_sessions()
        return {"sessions": self.hub.list_sessions()}

    def _sessions_unwatch(self, connection: Connection, params: dict):
        if self._unsubscribe(connection, protocol.CH_SESSIONS):
            self.hub.unwatch_sessions()
        return {"ok": True}

    def _session_get(self, connection: Connection, params: dict):
        return self.hub.session_detail(_key(params))

    def _session_messages(self, connection: Connection, params: dict):
        return {"messages": self.hub.messages(_key(params), _int_param(params, "limit", 400))}

    def _session_prompts(self, connection: Connection, params: dict):
        return {"prompts": self.hub.prompts(_key(params))}

    def _session_watch(self, connection: Connection, params: dict):
        key = _key(params)
        channel = protocol.session_channel(key)
        if self._subscribe(connection, channel):
            history = self.hub.watch_conversation(key)
        else:
            # 同连接重复 watch（对话页重进 / 状态条与聊天叠订）：订阅只记一次，
            # 但仍要返回全文快照，否则第二次只能拿到空列表。
            history = self.hub.conversation_snapshot(key)
        return {"messages": history}

    def _session_unwatch(self, connection: Connection, params: dict):
        key = _key(params)
        if self._unsubscribe(connection, protocol.session_channel(key)):
            self.hub.unwatch_conversation(key)
        return {"ok": True}

    def _screen_watch(self, connection: Connection, params: dict):
        key = _key(params)
        channel = protocol.screen_channel(key)
        if self._subscribe(connection, channel):
            frame = self.hub.watch_screen(key)
        else:
            # 同一连接重复 watch（聊天状态条 + 终端页）：不再加订阅计数，但要整帧。
            frame = self.hub.resync_screen(key)
        return {"frame": frame}

    def _screen_unwatch(self, connection: Connection, params: dict):
        key = _key(params)
        if self._unsubscribe(connection, protocol.screen_channel(key)):
            self.hub.unwatch_screen(key)
        return {"ok": True}

    def _screen_scroll(self, connection: Connection, params: dict):
        return {"frame": self.hub.scroll_screen(_key(params), _int_param(params, "offset", 0))}

    def _input_text(self, connection: Connection, params: dict):
        text = str(params.get("text") or "")
        submit = bool(params.get("submit", True))
        if not text and not submit:
            raise ActionError(protocol.E_USAGE, "没有要发送的内容")
        self.hub.send_text(_key(params), text, submit)
        return {"ok": True}

    def _input_keys(self, connection: Connection, params: dict):
        keys = params.get("keys")
        if not isinstance(keys, list):
            raise ActionError(protocol.E_USAGE, "没有要发送的按键")
        cleaned = [str(k) for k in keys if str(k).strip()]
        if not cleaned:
            raise ActionError(protocol.E_USAGE, "没有要发送的按键")
        self.hub.send_keys(_key(params), cleaned)
        return {"ok": True}

    def _input_image(self, connection: Connection, params: dict):
        import base64
        import binascii

        encoded = str(params.get("data") or "").strip()
        if not encoded:
            raise ActionError(protocol.E_USAGE, "没有图片数据")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ActionError(protocol.E_USAGE, "图片数据不完整") from exc
        if not raw:
            raise ActionError(protocol.E_USAGE, "没有图片数据")
        return {"path": self.hub.send_image(_key(params), raw)}

    def _screen_resize(self, connection: Connection, params: dict):
        # 手机与桌面共享同一保活窗格：即使误发也不改桌面窗口尺寸。
        raise ActionError(protocol.E_USAGE, "手机端不允许调整终端窗口大小")

    def _session_mark_read(self, connection: Connection, params: dict):
        return {"attention": self.hub.mark_read(_key(params))}

    def _session_pin(self, connection: Connection, params: dict):
        return {"pinned": self.hub.toggle_pin(_key(params))}

    def _session_stop(self, connection: Connection, params: dict):
        self.hub.stop_session(_key(params))
        return {"ok": True}

    def _session_delete(self, connection: Connection, params: dict):
        self.hub.delete_session(_key(params))
        return {"ok": True}

    def _session_new(self, connection: Connection, params: dict):
        runtime = str(params.get("runtime") or "").strip()
        if not runtime:
            raise ActionError(protocol.E_USAGE, "请选择助手")
        cwd = params.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ActionError(protocol.E_USAGE, "项目路径格式不对")
        return {"session": self.hub.new_session(runtime, cwd)}

    def _session_resume(self, connection: Connection, params: dict):
        return {"session": self.hub.resume_session(_key(params))}

    def _session_handoff(self, connection: Connection, params: dict):
        return {"session": self.hub.handoff_session(_key(params), str(params.get("runtime") or ""))}

    def _projects_list(self, connection: Connection, params: dict):
        return {"projects": self.hub.projects()}

    def _runtimes_list(self, connection: Connection, params: dict):
        return {"runtimes": self.hub.runtimes()}

    def _search(self, connection: Connection, params: dict):
        return {"sessions": self.hub.list_sessions(query=str(params.get("q") or ""), limit=100)}

    def _push_register(self, connection: Connection, params: dict):
        device = remote_config.touch_device(
            self.state,
            connection.device_public_key,
            push_token=str(params.get("token") or ""),
            push_env=str(params.get("env") or ""),
        )
        if device is None:
            raise ActionError(protocol.E_UNAUTHORIZED, "这台设备还没有和开发机配对")
        return {"ok": True}


def _key(params: dict) -> str:
    key = str(params.get("key") or "").strip()
    if not key:
        raise ActionError(protocol.E_USAGE, "缺少会话标识")
    return key


def _int_param(params: dict, name: str, default: int) -> int:
    """把请求参数收成非负整数；坏值按 usage_error，避免 int() 把整条请求打成 500。"""
    raw = params.get(name, default)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ActionError(protocol.E_USAGE, f"参数 {name} 必须是整数") from exc
    return max(0, value)


def protocol_version() -> int:
    from pickup.remote import REMOTE_PROTOCOL_VERSION

    return REMOTE_PROTOCOL_VERSION


_HANDLERS = {
    protocol.M_SESSIONS_LIST: RemoteService._sessions_list,
    protocol.M_SESSIONS_WATCH: RemoteService._sessions_watch,
    protocol.M_SESSIONS_UNWATCH: RemoteService._sessions_unwatch,
    protocol.M_SESSION_GET: RemoteService._session_get,
    protocol.M_SESSION_MESSAGES: RemoteService._session_messages,
    protocol.M_SESSION_PROMPTS: RemoteService._session_prompts,
    protocol.M_SESSION_WATCH: RemoteService._session_watch,
    protocol.M_SESSION_UNWATCH: RemoteService._session_unwatch,
    protocol.M_SESSION_MARK_READ: RemoteService._session_mark_read,
    protocol.M_SCREEN_WATCH: RemoteService._screen_watch,
    protocol.M_SCREEN_UNWATCH: RemoteService._screen_unwatch,
    protocol.M_SCREEN_SCROLL: RemoteService._screen_scroll,
    protocol.M_SCREEN_RESIZE: RemoteService._screen_resize,
    protocol.M_INPUT_TEXT: RemoteService._input_text,
    protocol.M_INPUT_KEYS: RemoteService._input_keys,
    protocol.M_INPUT_IMAGE: RemoteService._input_image,
    protocol.M_SESSION_NEW: RemoteService._session_new,
    protocol.M_SESSION_RESUME: RemoteService._session_resume,
    protocol.M_SESSION_HANDOFF: RemoteService._session_handoff,
    protocol.M_SESSION_STOP: RemoteService._session_stop,
    protocol.M_SESSION_DELETE: RemoteService._session_delete,
    protocol.M_SESSION_PIN: RemoteService._session_pin,
    protocol.M_PROJECTS_LIST: RemoteService._projects_list,
    protocol.M_RUNTIMES_LIST: RemoteService._runtimes_list,
    protocol.M_SEARCH: RemoteService._search,
    protocol.M_PUSH_REGISTER: RemoteService._push_register,
}
