"""推送：助手卡住等回答时，把人叫回手机。

只在关注状态从别的状态变成「等你回答」时触发。刻意不推「有新消息」——桌面端
实测一轮长任务能产生几十次状态波动，全推出去就是刷屏，用户很快会关掉通知，
那才是真正的功能失效。

因为通道是端到端加密的，中继读不到内容，所以推送发出去的是一层加密壳：手机上的
通知服务扩展在本地解开，再渲染出真实的标题与正文。中继全程只知道「给哪个设备令牌
发一条多大的推送」。

节流：同一条会话两分钟内只推一次。助手在等待与执行之间来回抖动是常见现象，
不加节流会把用户口袋里的手机震到没电。
"""

from __future__ import annotations

import base64
import json
import threading
import time

from corral import observe
from corral.remote import crypto
from corral.remote.config import RemoteState

_THROTTLE_SECONDS = 120


class PushNotifier:
    """把关注状态变化翻译成加密推送，交给中继投递。"""

    def __init__(self, state: RemoteState, static_private: bytes, sender=None) -> None:
        self.state = state
        self.static_private = static_private
        self.sender = sender  # 由中继客户端注入：(token, env, payload) -> None
        self._lock = threading.Lock()
        self._last_sent: dict[str, float] = {}

    def set_sender(self, sender) -> None:
        self.sender = sender

    def on_attention_change(self, session: dict, previous: str, current: str) -> None:
        if current != "waiting" or self.sender is None:
            return
        key = str(session.get("key") or "")
        now = time.time()
        with self._lock:
            if now - self._last_sent.get(key, 0.0) < _THROTTLE_SECONDS:
                return
            self._last_sent[key] = now
        body = self._render(session)
        for device in self.state.devices:
            if not device.push_token:
                continue
            try:
                sealed = crypto.seal_for_device(
                    self.static_private, bytes.fromhex(device.public_key), body
                )
            except Exception as exc:
                observe.event("remote_push_seal_failed", error=str(exc))
                continue
            self.sender(device.push_token, device.push_env, base64.b64encode(sealed))
        observe.event("remote_push_sent", session=key)

    def _render(self, session: dict) -> bytes:
        """通知的真实内容。手机解开后直接照这几个字段渲染，不需要再回来查一次。"""
        payload = {
            "key": session.get("key"),
            "title": session.get("title") or "会话",
            "runtime": session.get("runtime"),
            "cwd": session.get("cwd_display") or "",
            "body": session.get("last_agent") or "助手在等你回答",
            "ts": time.time(),
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
