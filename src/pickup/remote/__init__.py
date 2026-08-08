"""pickup 手机端接力：开发机上的常驻服务。

这一层负责把 pickup 已有的能力（会话扫描、关注状态、保活层、内嵌画面抓取、
启动与接力）通过一条加密通道交给手机客户端，**不改变 `agent_api` 的只读契约**：
所有带副作用的能力（送按键、新建、结束、删除）都收在本包里，`agent_api` 仍然
只负责把数据交出来。

模块划分：

- ``config``    本机身份、已配对设备、中继地址等持久化状态
- ``crypto``    X25519 + HKDF-SHA256 + ChaCha20-Poly1305 的端到端加密通道
- ``protocol``  应用层消息格式与方法名常量
- ``richmsg``   富消息解析：在现有对话读取之外保留工具调用摘要
- ``screen``    终端画面网格的序列化与行级差分
- ``sessions``  会话视图：包一层 SessionStore，提供订阅与查询
- ``service``   方法路由与订阅管理，传输无关
- ``transport`` 中继出站长连接与局域网直连服务
- ``pairing``   配对码、二维码
- ``cli``       ``pickup remote`` 子命令

除 ``cli`` 外的模块不打印任何东西，日志统一走 ``observe.event``。
"""

from __future__ import annotations

REMOTE_PROTOCOL_VERSION = 1

__all__ = ["REMOTE_PROTOCOL_VERSION"]
