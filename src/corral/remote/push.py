"""推送：等你回答、以及一轮正常/异常结束时，把人叫回手机。

触发口径：

- **等你回答**：关注状态变成 waiting（原有行为）。
- **一轮结束**：SessKit ``status_tag`` 变为已完成 / 已中断（产品方向 2026-09-15）。
  禁止用进程退出或裸「有新消息」当触发——长任务会刷屏，用户很快关掉通知。

因为通道是端到端加密的，中继读不到内容，所以推送发出去的是一层加密壳：手机上的
通知服务扩展在本地解开，再渲染出真实的标题与正文。中继全程只知道「给哪个设备令牌
发一条多大的推送」。

节流：同一会话同一类提醒两分钟内只推一次。助手在等待与执行之间来回抖动、或
status 短时间抖动时，不加节流会把用户口袋里的手机震到没电。
"""

from __future__ import annotations

import base64
import json
import threading
import time

from sesskit import titles as sesskit_titles

from corral import observe
from corral.remote import crypto
from corral.remote.config import RemoteState

_THROTTLE_SECONDS = 120

_KIND_WAITING = "waiting"
_KIND_COMPLETED = "completed"
_KIND_ABORTED = "aborted"


class PushNotifier:
    """把关注状态 / 会话结束状态翻译成加密推送，交给中继投递。"""

    def __init__(self, state: RemoteState, static_private: bytes, sender=None) -> None:
        self.state = state
        self.static_private = static_private
        self.sender = sender  # 由中继客户端注入：(token, env, payload) -> None
        self._lock = threading.Lock()
        self._last_sent: dict[str, float] = {}

    def set_sender(self, sender) -> None:
        self.sender = sender

    def on_attention_change(self, session: dict, previous: str, current: str) -> None:
        if current != "waiting":
            return
        self._emit(session, kind=_KIND_WAITING)

    def on_status_change(self, session: dict, previous: str, current: str) -> None:
        """SessKit status_tag 跃迁：已完成 / 已中断才推；首扫与同值抖动不推。"""
        if current == sesskit_titles.STATUS_DONE and previous != sesskit_titles.STATUS_DONE:
            self._emit(session, kind=_KIND_COMPLETED)
        elif (
            current == sesskit_titles.STATUS_ABORTED
            and previous != sesskit_titles.STATUS_ABORTED
        ):
            self._emit(session, kind=_KIND_ABORTED)

    def _emit(self, session: dict, *, kind: str) -> None:
        if self.sender is None:
            return
        key = str(session.get("key") or "")
        if not key:
            return
        throttle_key = f"{key}:{kind}"
        now = time.time()
        with self._lock:
            if now - self._last_sent.get(throttle_key, 0.0) < _THROTTLE_SECONDS:
                return
            self._last_sent[throttle_key] = now
        body = self._render(session, kind=kind)
        sent = 0
        for device in self.state.devices:
            if not device.push_token:
                continue
            try:
                sealed = crypto.seal_for_device(
                    self.static_private, bytes.fromhex(device.public_key), body
                )
            except Exception as exc:
                observe.event("remote_push_seal_failed", error=str(exc), kind=kind)
                continue
            self.sender(device.push_token, device.push_env, base64.b64encode(sealed))
            sent += 1
        observe.event("remote_push_sent", session=key, kind=kind, devices=sent)

    def _render(self, session: dict, *, kind: str) -> bytes:
        """通知的真实内容。手机解开后直接照这几个字段渲染，不需要再回来查一次。"""
        last_agent = str(session.get("last_agent") or "").strip()
        if kind == _KIND_WAITING:
            body = last_agent or "助手在等你回答"
        elif kind == _KIND_ABORTED:
            body = last_agent or "Session stopped with an error"
        else:
            body = last_agent or "Session finished"
        payload = {
            "key": session.get("key"),
            "title": session.get("title") or "会话",
            "runtime": session.get("runtime"),
            "cwd": session.get("cwd_display") or "",
            "body": body,
            "kind": kind,
            "ts": time.time(),
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
