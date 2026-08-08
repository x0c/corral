"""常驻服务的组装与主循环。

把会话中枢、方法路由、两种连接方式和推送串起来，跑一个 asyncio 事件循环。
业务侧仍然是线程模型（沿用 pickup 已有的 `SessionStore`），事件循环只负责网络。
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from pickup import observe
from pickup.remote import config as remote_config
from pickup.remote.push import PushNotifier
from pickup.remote.service import RemoteService
from pickup.remote.sessions import SessionHub
from pickup.remote.transport.local import LocalServer
from pickup.remote.transport.relay import RelayClient


class RemoteDaemon:
    def __init__(self, state: remote_config.RemoteState) -> None:
        self.state = state
        self.static_private = remote_config.load_or_create_identity()
        self.hub = SessionHub()
        self.service = RemoteService(self.hub)
        self.relay = RelayClient(self.service, state, self.static_private) if state.relay_enabled else None
        self.local = LocalServer(self.service, state, self.static_private) if state.local_enabled else None
        self.push = PushNotifier(state, self.static_private)
        if self.relay is not None:
            self.push.set_sender(self.relay.send_push)
        self.hub.set_attention_hook(self.push.on_attention_change)

    async def run(self) -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, stop.set)

        # 首轮扫描是纯磁盘活儿，别把事件循环堵在这儿——中继连接可以并行建起来。
        await asyncio.to_thread(self.hub.start)
        observe.event("remote_started", host=self.state.host_name)
        remote_config.write_pid()

        tasks = []
        if self.relay is not None:
            tasks.append(asyncio.create_task(self.relay.run(stop)))
        if self.local is not None:
            tasks.append(asyncio.create_task(self.local.run(stop)))
        if not tasks:
            raise RuntimeError("中继和局域网直连都被关掉了，服务没有任何入口")
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(self.hub.stop)
            remote_config.clear_pid()
            observe.event("remote_stopped")

    @property
    def local_port(self) -> int:
        return self.local.port if self.local is not None else 0
