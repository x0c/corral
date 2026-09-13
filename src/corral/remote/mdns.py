"""mDNS 广播：让同网手机发现开发机，无需扫码也能看到局域网候选。

与 `lan.py` 解耦：本模块不碰 socket、不决定端口，只通过 `port_provider`
（`() -> int` 回调，由 daemon 传 `lambda: self.local.port`）读取 LocalServer
已经监听的实际端口。地址同样从 `lan.py` 取（只取第一条，没有则不广播）。

`zeroconf` 是可选依赖（`pyproject [remote]` extra 的注释里说明）：没装时
`available()` 为 False，`start()` 静默跳过并记一条 `remote_mdns_unavailable`
事件，**绝不抛异常阻断 daemon**。
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable

from corral import observe
from corral.remote.lan import lan_addresses

SERVICE_TYPE = "_corral._tcp.local."
SERVICE_SUFFIX = ".local."
_TXT_VERSION = b"v=2"
_MAX_PORT_WAIT_SEC = 5.0
_PORT_POLL_SEC = 0.2


def available() -> bool:
    """是否装了 zeroconf（mDNS 广播能力开关）。"""
    try:
        import zeroconf  # noqa: F401
    except ImportError:
        return False
    return True


def service_name(host_id: str) -> str:
    """同网多开发机不冲突：实例名带 host_id 前 8 字符。"""
    short = str(host_id or "").strip()[:8] or "dev"
    return f"Corral-{short}"


class MdnsAdvertiser:
    """mDNS 服务广播。`start()`/`stop()` 幂等，任何失败只记事件不抛异常。"""

    def __init__(
        self,
        host_id: str,
        host_name: str,
        port_provider: Callable[[], int],
    ) -> None:
        self.host_id = str(host_id or "")
        self.host_name = str(host_name or "") or "dev"
        self.port_provider = port_provider
        self._lock = threading.Lock()
        self._zc = None
        self._info = None
        self.advertising = False

    def _read_port(self) -> int:
        """LocalServer 还在 listen 时端口可能是 0：等一小会重读，仍无则放弃。"""
        deadline = time.monotonic() + _MAX_PORT_WAIT_SEC
        while True:
            try:
                port = int(self.port_provider() or 0)
            except (TypeError, ValueError):
                port = 0
            if port > 0:
                return port
            if time.monotonic() >= deadline:
                return 0
            time.sleep(_PORT_POLL_SEC)

    def start(self) -> bool:
        """注册 mDNS 服务。成功返回 True；缺依赖/无地址/无端口时静默跳过。"""
        with self._lock:
            if self.advertising:
                return True
            try:
                from zeroconf import ServiceInfo, Zeroconf
            except ImportError:
                observe.event("remote_mdns_unavailable")
                return False
            try:
                port = self._read_port()
                addresses = lan_addresses()
                if port <= 0 or not addresses:
                    observe.event("remote_mdns_skipped")
                    return False
                packed = socket.inet_aton(addresses[0])
                name = f"{service_name(self.host_id)}.{SERVICE_TYPE}"
                server = f"{self.host_name}{SERVICE_SUFFIX}"
                self._info = ServiceInfo(
                    SERVICE_TYPE,
                    name,
                    addresses=[packed],
                    port=port,
                    properties={b"hid": self.host_id.encode("utf-8"), b"v": _TXT_VERSION},
                    server=server,
                )
                self._zc = Zeroconf()
                self._zc.register_service(self._info)
                self.advertising = True
                observe.event("remote_mdns_started", port=port)
                return True
            except Exception as exc:
                observe.event("remote_mdns_failed", error=str(exc))
                self._close_locked()
                return False

    def stop(self) -> None:
        with self._lock:
            if not self.advertising and self._zc is None:
                return
            try:
                if self._zc is not None and self._info is not None:
                    try:
                        self._zc.unregister_service(self._info)
                    except Exception:
                        pass
            finally:
                self._close_locked()
                self.advertising = False
                observe.event("remote_mdns_stopped")

    def _close_locked(self) -> None:
        if self._zc is not None:
            try:
                self._zc.close()
            except Exception:
                pass
        self._zc = None
        self._info = None


__all__ = ["SERVICE_TYPE", "MdnsAdvertiser", "available", "service_name"]
