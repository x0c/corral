"""配对：在开发机终端里打一个二维码，手机扫一下就完成绑定并交换公钥。

二维码里装的是一条 ``corral://pair`` 链接，包含中继地址、开发机公钥、
一次性配对码，以及局域网地址（手机若在同一个网内会优先直连，省掉中继一跳）。
路由标识由公钥派生，所以二维码不再带 ``h``。

**配对码只能用一次、十分钟过期**：它是唯一能让陌生设备被开发机接受的凭据，
长期挂在那里等于给自己留一个后门。二维码本身不含任何私钥。
"""

from __future__ import annotations

import base64
import json
import urllib.parse

from corral.i18n import t
from corral.remote.config import RemoteState


def build_payload(state: RemoteState, code: str, public_key: bytes, local_port: int = 0) -> str:
    """生成配对链接。手机端扫到后直接按这条链接建立连接。"""
    from corral.remote.transport.local import lan_addresses

    params = {
        "v": "2",
        "n": state.host_name,
        "k": base64.urlsafe_b64encode(public_key).decode().rstrip("="),
        "c": code,
    }
    if state.relay_enabled and state.relay_url:
        params["r"] = state.relay_url
    if state.local_enabled and local_port:
        addresses = lan_addresses()
        if addresses:
            params["l"] = ",".join(f"{a}:{local_port}" for a in addresses[:3])
    return "corral://pair?" + urllib.parse.urlencode(params)


def as_json(state: RemoteState, code: str, public_key: bytes, local_port: int = 0) -> str:
    """同样的配对信息，但给脚本和自动化用（``--json`` 输出）。"""
    return json.dumps(
        {
            "url": build_payload(state, code, public_key, local_port),
            "host_id": state.host_id,
            "host_name": state.host_name,
            "code": code,
            "public_key": public_key.hex(),
            "relay_url": state.relay_url if state.relay_enabled else "",
            "local_port": local_port if state.local_enabled else 0,
        },
        ensure_ascii=False,
        indent=2,
    )


def render_qr(text: str) -> str | None:
    """把链接渲染成终端能看的二维码；没装二维码库时返回 None，由调用方退化成文本。"""
    try:
        import segno
    except ImportError:
        return None
    import io

    buffer = io.StringIO()
    segno.make(text, error="l").terminal(out=buffer, border=1)
    return buffer.getvalue()


def render_fallback(text: str, code: str) -> str:
    """没有二维码库时的替代方案：手机端也支持手动输入配对码。"""
    return t("remote.pair.fallback", url=text, code=code)
