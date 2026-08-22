"""连接方式：经公网中继的出站长连接，以及跳过中继的局域网直连。

两种方式共用同一套帧格式与握手（见 `HostChannel`），也共用同一个 `RemoteService`
实例，因此不会出现「中继能用、直连不能用」这类功能漂移。

**为什么开发机是主动往外连**：要求用户在自己家里开端口映射、配公网 IP 或者搭
VPN，对一个开源工具来说等于劝退。业界的同类做法（Codex 的远程会话、Happy、
Kraki）一致选择「开发机主动向外建长连接 + 中继零知识转发 + 手机扫码配对」，
corral 照这条走。shell-gate 那种「网关主动连开发机」的拓扑依赖两台自有服务器
之间的内网，只适合自己用，不适合交给别人。
"""

from __future__ import annotations

from corral.remote.transport.channel import HostChannel

__all__ = ["HostChannel"]
