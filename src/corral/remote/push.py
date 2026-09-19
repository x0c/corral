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

去重：同一会话同一轮结束只推一次。去重键是
``(session_key, completion_id, kind)``——``completion_id`` 来自 SessKit
（同一轮重扫不变，新一轮必变，重启可重算）。内存 120 秒节流只防抖动，
不同轮次不受牵连；已发集合落盘，重启不重推。
"""

from __future__ import annotations

import base64
import json
import threading
import time

from sesskit import titles as sesskit_titles

from corral import observe
from corral.remote import crypto
from corral.remote.config import RemoteState, remote_dir

_THROTTLE_SECONDS = 120
_SENT_FILENAME = "push-sent.json"
_SENT_LIMIT = 500

_KIND_WAITING = "waiting"
_KIND_COMPLETED = "completed"
_KIND_ABORTED = "aborted"


class PushNotifier:
    """把关注状态 / 会话结束状态翻译成加密推送，交给中继投递。"""

    def __init__(
        self,
        state: RemoteState,
        static_private: bytes,
        sender=None,
        *,
        sent_path=None,
    ) -> None:
        self.state = state
        self.static_private = static_private
        self.sender = sender  # 由中继客户端注入：(token, env, payload) -> None
        self._lock = threading.Lock()
        self._last_sent: dict[str, float] = {}
        # 已发集合：(session_key, completion_id, kind) -> 发送时间。落盘，重启不重推。
        # sent_path 仅测试注入；生产默认走 remote_dir()/push-sent.json。
        self._sent_path_override = sent_path
        self._sent_rounds: dict[str, float] = self._load_sent()

    def set_sender(self, sender) -> None:
        self.sender = sender

    def on_attention_change(self, session: dict, previous: str, current: str) -> None:
        if current != "waiting":
            return
        self._emit(session, kind=_KIND_WAITING)

    def on_status_change(self, session: dict, previous: str, current: str) -> None:
        """SessKit status_tag 跃迁：已完成 / 已中断才推；首扫与同值抖动不推。

        已完成但缺 ``completion_id``（老 SessKit / Cursor 弱证据）时默认不推——
        宁可漏推一次，也不把「刚开了个头」当成干完了。
        """
        # 同标签但 completion_id 变了 = 新一轮结束（SessionHub 在 DONE→DONE
        # 新一轮时也会调 hook，prev 传回当前标签）。_emit 内按轮去重+节流，
        # 同一轮重复调用不会重发。缺 id 的 DONE 保守静默。
        if current == sesskit_titles.STATUS_DONE and (
            previous != sesskit_titles.STATUS_DONE or previous == current
        ):
            if not str(session.get("completion_id") or ""):
                observe.event(
                    "remote_push_skipped_no_completion_id",
                    session=str(session.get("key") or ""),
                    kind=_KIND_COMPLETED,
                )
                return
            self._emit(session, kind=_KIND_COMPLETED)
        elif current == sesskit_titles.STATUS_ABORTED and (
            previous != sesskit_titles.STATUS_ABORTED or previous == current
        ):
            self._emit(session, kind=_KIND_ABORTED)

    @staticmethod
    def _round_key(session: dict, kind: str) -> str:
        key = str(session.get("key") or "")
        completion = str(session.get("completion_id") or "")
        return f"{key}\0{completion or session.get('status', '')}\0{kind}"

    def _already_sent(self, round_key: str) -> bool:
        with self._lock:
            return round_key in self._sent_rounds

    def _mark_sent(self, round_key: str, now: float) -> None:
        with self._lock:
            self._sent_rounds[round_key] = now
            # 有界 LRU：只留最近 N 轮，旧轮自然淘汰（新一轮 id 必变，不会误删）。
            if len(self._sent_rounds) > _SENT_LIMIT:
                for old in sorted(self._sent_rounds, key=self._sent_rounds.get)[: len(self._sent_rounds) - _SENT_LIMIT]:
                    del self._sent_rounds[old]
            snapshot = dict(self._sent_rounds)
        self._save_sent(snapshot)

    def _sent_path(self):
        if self._sent_path_override is not None:
            return self._sent_path_override
        return remote_dir() / _SENT_FILENAME

    def _load_sent(self) -> dict[str, float]:
        try:
            raw = json.loads(self._sent_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, float] = {}
        for key, value in raw.items():
            try:
                out[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        # 启动时只留最近 N 轮，防止旧文件无限膨胀。
        if len(out) > _SENT_LIMIT:
            ordered = sorted(out, key=out.get)[- _SENT_LIMIT:]
            out = {key: out[key] for key in ordered}
        return out

    def _save_sent(self, snapshot: dict[str, float]) -> None:
        try:
            path = self._sent_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            observe.event("remote_push_sent_save_failed", error=str(exc))

    def _emit(self, session: dict, *, kind: str) -> None:
        if self.sender is None:
            return
        key = str(session.get("key") or "")
        if not key:
            return
        if not self._device_wants(session, kind):
            return
        round_key = self._round_key(session, kind)
        if self._already_sent(round_key):
            return
        throttle_key = f"{key}:{kind}"
        now = time.time()
        with self._lock:
            if now - self._last_sent.get(throttle_key, 0.0) < _THROTTLE_SECONDS:
                # 节流只跳过本次发送，不记已发：同一轮下次扫描仍可推。
                return
            self._last_sent[throttle_key] = now
        body = self._render(session, kind=kind)
        sent = 0
        for device in self.state.devices:
            if not device.push_token:
                continue
            if not self._device_allows(device, kind):
                continue
            try:
                sealed = crypto.seal_for_device(
                    self.static_private, bytes.fromhex(device.public_key), body
                )
            except Exception as exc:
                observe.event("remote_push_seal_failed", error=str(exc), kind=kind)
                continue
            try:
                self.sender(device.push_token, device.push_env, base64.b64encode(sealed))
            except Exception as exc:
                # 发送失败不记已发：同一轮下次扫描仍可重试；轮次已变则自然过期。
                observe.event("remote_push_send_failed", error=str(exc), kind=kind, session=key)
                return
            sent += 1
        self._mark_sent(round_key, now)
        observe.event("remote_push_sent", session=key, kind=kind, devices=sent)

    def _device_wants(self, session: dict, kind: str) -> bool:
        """任一有 token 且允许该类的设备存在才发；否则连已发都不记。"""
        for device in self.state.devices:
            if device.push_token and self._device_allows(device, kind):
                return True
        return False

    @staticmethod
    def _device_allows(device, kind: str) -> bool:
        if kind == _KIND_COMPLETED:
            return getattr(device, "notify_completed", True) is not False
        if kind == _KIND_ABORTED:
            return getattr(device, "notify_aborted", True) is not False
        return True

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
