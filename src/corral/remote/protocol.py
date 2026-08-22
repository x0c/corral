"""手机端与开发机之间的应用层协议。

分两层：

**中继层（明文，中继看得见）** —— 只有路由所需的最小信息：谁发给谁、多大。
每个 WebSocket 二进制帧的结构是 ``[1 字节版本=2][1 字节类型][16 字节会话通道 ID][载荷]``。
中继据此把帧转给对端，不解析载荷。子协议为 ``corral.v2``。

**应用层（密文，只有两端看得见）** —— 载荷解密后是一段 UTF-8 JSON，形如：

    {"t": "req", "id": 7, "m": "session.get", "p": {...}}
    {"t": "res", "id": 7, "ok": true, "d": {...}}
    {"t": "res", "id": 7, "ok": false, "e": {"code": "not_found", "message": "..."}}
    {"t": "evt", "c": "screen:claude:abc", "d": {...}}

请求-响应用 ``id`` 关联；订阅推送用 ``c``（channel）标识，客户端按需退订。
``id`` 由客户端分配，服务端原样回传，不做全局唯一性要求。
"""

from __future__ import annotations

import json
from typing import Any

# --- 中继层帧类型 ---------------------------------------------------------

FRAME_DATA = 0x10          # 承载加密载荷
FRAME_DEVICE_OPEN = 0x01   # 中继 → 主机：某设备接入
FRAME_DEVICE_CLOSE = 0x02  # 中继 → 主机：某设备断开
FRAME_HELLO = 0x03         # 握手材料（明文，见 crypto 模块的说明）
FRAME_PING = 0x04
FRAME_PONG = 0x05
FRAME_PUSH = 0x06          # 主机 → 中继：请中继代发一条推送（内容已加密）
FRAME_REGISTERED = 0x07    # 中继 → 主机：注册成功，可以开始接客

ZERO_CHANNEL = b"\x00" * 16

CHANNEL_ID_LEN = 16
FRAME_VERSION = 2
SUBPROTOCOL = "corral.v2"
_MAX_FRAME_BYTES = 8 * 1024 * 1024


class ProtocolError(RuntimeError):
    pass


def encode_frame(frame_type: int, channel_id: bytes, payload: bytes) -> bytes:
    if len(channel_id) != CHANNEL_ID_LEN:
        raise ProtocolError("通道标识长度不合法")
    return bytes([FRAME_VERSION, frame_type]) + channel_id + payload


def decode_frame(raw: bytes) -> tuple[int, bytes, bytes]:
    if len(raw) < 2 + CHANNEL_ID_LEN:
        raise ProtocolError("帧过短")
    if len(raw) > _MAX_FRAME_BYTES:
        raise ProtocolError("帧过大")
    if raw[0] != FRAME_VERSION:
        raise ProtocolError("帧版本不是 v2")
    return raw[1], raw[2 : 2 + CHANNEL_ID_LEN], raw[2 + CHANNEL_ID_LEN :]


# --- 应用层消息 -----------------------------------------------------------

def dumps(message: dict) -> bytes:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def loads(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("消息不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("消息顶层必须是对象")
    return value


def request(req_id: int, method: str, params: dict | None = None) -> dict:
    return {"t": "req", "id": req_id, "m": method, "p": params or {}}


def response(req_id: int, data: Any) -> dict:
    return {"t": "res", "id": req_id, "ok": True, "d": data}


def error(req_id: int, code: str, message: str, hint: str = "") -> dict:
    payload: dict = {"code": code, "message": message}
    if hint:
        payload["hint"] = hint
    return {"t": "res", "id": req_id, "ok": False, "e": payload}


def event(channel: str, data: Any) -> dict:
    return {"t": "evt", "c": channel, "d": data}


# --- 方法名 ---------------------------------------------------------------
#
# 命名规则：``名词.动词``。带副作用的方法一律放在 `input.` / `session.` 下，
# 只读查询放在 `sessions.` / `projects.` / `search` 下，便于日后按前缀做权限分级。

M_HELLO = "hello"                    # 客户端自报家门 + 取服务端能力
M_PAIR = "pair"                      # 用一次性配对码完成配对
M_PUSH_REGISTER = "push.register"    # 上报推送令牌

M_SESSIONS_LIST = "sessions.list"
M_SESSIONS_WATCH = "sessions.watch"
M_SESSIONS_UNWATCH = "sessions.unwatch"
M_SESSION_GET = "session.get"
M_SESSION_MESSAGES = "session.messages"
M_SESSION_PROMPTS = "session.prompts"
M_SESSION_WATCH = "session.watch"
M_SESSION_UNWATCH = "session.unwatch"
M_SESSION_MARK_READ = "session.markRead"

M_SCREEN_WATCH = "screen.watch"
M_SCREEN_UNWATCH = "screen.unwatch"
M_SCREEN_SCROLL = "screen.scroll"
M_SCREEN_RESIZE = "screen.resize"

M_INPUT_TEXT = "input.text"
M_INPUT_KEYS = "input.keys"
M_INPUT_IMAGE = "input.image"

M_SESSION_NEW = "session.new"
M_SESSION_RESUME = "session.resume"
M_SESSION_HANDOFF = "session.handoff"
M_SESSION_STOP = "session.stop"
M_SESSION_DELETE = "session.delete"
M_SESSION_PIN = "session.pin"

M_PROJECTS_LIST = "projects.list"
M_SEARCH = "search"
M_RUNTIMES_LIST = "runtimes.list"

# 订阅通道名前缀
CH_SESSIONS = "sessions"


def screen_channel(session_key: str) -> str:
    return f"screen:{session_key}"


def session_channel(session_key: str) -> str:
    return f"session:{session_key}"


# --- 错误码 ---------------------------------------------------------------
#
# 与 agent_api 的错误码保持同一套词汇，手机端和 Agent 侧的处理逻辑可以复用。

E_USAGE = "usage_error"
E_NOT_FOUND = "not_found"
E_UNAUTHORIZED = "unauthorized"
E_UNAVAILABLE = "unavailable"
E_INTERNAL = "internal_error"
E_RATE_LIMITED = "rate_limited"
