"""局域网直连：完全跳过中继的逃生口。

给两类人用：同一个 Wi-Fi 下就在旁边的手机，以及已经有 Tailscale / ZeroTier 之类
私有网络、根本不需要第三方中继的用户。协议与走中继时**完全一致**（同一套帧、
同一套端到端加密），区别只是这一条连接是手机主动连进来的。

内容加密照旧：即使有人在同一个 Wi-Fi 上，没有配对过就拿不到会话密钥。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

from pickup import observe
from pickup.remote import protocol
from pickup.remote.config import RemoteState
from pickup.remote.crypto import random_id
from pickup.remote.service import RemoteService
from pickup.remote.transport.channel import HostChannel
from pickup.remote.transport.relay import _websockets

_DEFAULT_PORT = 8737


class LocalServer:
    """在局域网上监听的直连服务。每条 WebSocket 连接对应一台手机。"""

    def __init__(self, service: RemoteService, state: RemoteState, static_private: bytes) -> None:
        self.service = service
        self.state = state
        self.static_private = static_private
        self.port = 0
        self._server = None

    async def run(self, stop: asyncio.Event) -> None:
        websockets = _websockets()
        port = self.state.local_port or _DEFAULT_PORT
        try:
            self._server = await websockets.serve(
                self._handle, "0.0.0.0", port, max_size=8 * 1024 * 1024, ping_interval=20
            )
        except OSError as exc:
            observe.event("remote_local_listen_failed", port=port, error=str(exc))
            return
        self.port = next(
            (s.getsockname()[1] for s in self._server.sockets or [] if s.getsockname()), port
        )
        observe.event("remote_local_listening", port=self.port)
        try:
            await stop.wait()
        finally:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    async def _handle(self, socket_conn) -> None:
        loop = asyncio.get_running_loop()
        channel_id = bytes.fromhex(random_id(protocol.CHANNEL_ID_LEN))
        address = ""
        with contextlib.suppress(Exception):
            address = str(socket_conn.remote_address[0])
        channel = HostChannel(
            self.service,
            self.static_private,
            channel_id,
            lambda frame_type, payload: asyncio.run_coroutine_threadsafe(
                _send(socket_conn, protocol.encode_frame(frame_type, channel_id, payload)), loop
            ),
            address=address,
        )
        # 直连时也先发一次通道分配，让手机端的收包逻辑与走中继时完全一致——
        # 客户端不必为两种连接方式各写一套。
        await _send(socket_conn, protocol.encode_frame(protocol.FRAME_DEVICE_OPEN, channel_id, b""))
        try:
            async for raw in socket_conn:
                if isinstance(raw, str):
                    continue
                try:
                    frame_type, _cid, payload = protocol.decode_frame(raw)
                except protocol.ProtocolError:
                    break
                if frame_type in (protocol.FRAME_HELLO, protocol.FRAME_DATA):
                    channel.submit(frame_type, payload)
        except Exception:
            pass
        finally:
            channel.close()


async def _send(socket_conn, frame: bytes) -> None:
    try:
        await socket_conn.send(frame)
    except Exception:
        pass


def lan_addresses() -> list[str]:
    """列出这台开发机在局域网上的地址，写进配对二维码供手机直连。

    刻意不做完整的网卡枚举：用一次到外网地址的 UDP「连接」（不产生任何流量）
    问操作系统「出去的话会走哪个地址」，这是跨平台且不依赖第三方库的可靠办法。
    """
    addresses: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # 文档保留地址，不会真的发包
        addresses.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in addresses and not address.startswith("127."):
                addresses.append(address)
    except OSError:
        pass
    return addresses
