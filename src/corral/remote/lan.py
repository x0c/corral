"""局域网地址与端口：配对二维码、hello 与状态输出共用的唯一真源。

只回答两件事：这台机器在局域网上的真实地址有哪些、直连服务实际用哪个端口。
不碰监听 socket、不做 mDNS 广播（见 `mdns.py`），无第三方依赖。

为什么独立成模块：旧实现（`transport/local.py`）把 UDP 探针结果不加过滤就写进
配对二维码；在有 VPN/透明代理的机器上探针会返回脏地址（如 OpenClash fake-ip
段 198.18.0.0/15），手机拿到连不上的地址只能回退中继。这里用标准库
`ipaddress` 做分类过滤，真局域网优先，其它单播地址保留在后。
"""

from __future__ import annotations

import socket
from ipaddress import ip_address, ip_network
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from corral.remote.config import RemoteState

#: LocalServer 未显式指定端口时的监听端口（`remote on --port` 可覆盖）。
DEFAULT_LOCAL_PORT = 8737

#: 配对二维码 / hello 里最多带几条局域网提示。
MAX_LOCAL_HINTS = 3

_RFC1918 = (
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
)
#: 透明代理 fake-ip 常用段（RFC 2544 基准测试段，常被本机代理占用）：绝不发给手机。
_FAKE_IP = ip_network("198.18.0.0/15")


def effective_local_port(state: RemoteState) -> int:
    """直连实际端口：`remote.json` 只在 `on --port` 时写，0 表示走默认。"""
    try:
        return int(getattr(state, "local_port", 0) or DEFAULT_LOCAL_PORT)
    except (TypeError, ValueError):
        return DEFAULT_LOCAL_PORT


def _rank(address: object) -> tuple[int, str] | None:
    """地址分类：家用 Wi-Fi 段优先，再其它 RFC1918，再叠加组网/公网；脏地址丢掉。

    同属 RFC1918 时把 ``192.168/16``、``172.16/12`` 排在 ``10/8`` 前面——后者常被
    ZeroTier / 虚拟网卡占用；手机同 Wi-Fi 抢答应先试真正的局域网地址。
    """
    try:
        ip = ip_address(str(address).strip())
    except ValueError:
        return None
    if ip.version != 4:
        return None
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return None
    if ip in _FAKE_IP:
        return None
    if ip in ip_network("192.168.0.0/16"):
        return (0, str(ip))
    if ip in ip_network("172.16.0.0/12"):
        return (1, str(ip))
    if ip in ip_network("10.0.0.0/8"):
        return (2, str(ip))
    # Tailscale / 其它私有组网与公网地址落在这里：能用，排后面。
    return (3, str(ip))


def lan_addresses() -> list[str]:
    """列出这台开发机在局域网上的地址，真局域网在前，去重并排好序。

    刻意不做完整的网卡枚举：先用一次到外网地址的 UDP「连接」（不产生任何流量）
    问操作系统「出去的话会走哪个地址」，再用 hostname 解析补充；两路候选都经过
    `_rank` 过滤，VPN/代理的脏地址进不了结果。
    """
    candidates: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1 文档保留地址，不会真的发包
        candidates.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    if hostname:
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
        except OSError:
            infos = []
        for info in infos:
            try:
                candidates.append(info[4][0])
            except (IndexError, TypeError):
                continue
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for raw in candidates:
        item = _rank(raw)
        if item is None or item[1] in seen:
            continue
        seen.add(item[1])
        ranked.append(item)
    ranked.sort()
    return [text for _, text in ranked]


def local_hints(port: int) -> list[str]:
    """`"ip:port"` 提示列表（配对 `l=` / hello `local_hints` 共用），最多三条。"""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return []
    if port <= 0:
        return []
    return [f"{address}:{port}" for address in lan_addresses()[:MAX_LOCAL_HINTS]]
