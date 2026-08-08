# 手机端远程接力（pickup remote）

改、评审或排查「手机连开发机看会话 / 输入 / 推送 / 配对 / 局域网直连」前必读。

配套客户端：`../ios/`（见 `../ios/AGENTS.md`）。零知识中继：`../relay/`。

## 产品边界

- 开发机跑 `pickup remote` 常驻服务；手机只连这台服务，不直接扫各助手历史文件。
- 中继只做路由与代发推送，**看不到**会话明文；推送正文在手机本地用设备私钥解开。
- 手机与桌面共享同一个保活窗格时，**手机端禁止发 `screen.resize`**——否则会把电脑正在看的窗口挤窄。
- 可选依赖：`pip install 'pickup[remote]'`（`cryptography` / `websockets` / `segno`），不进主依赖。

## 命令入口

| 命令 | 作用 |
|---|---|
| `pickup remote` / `pickup remote serve` | 启动常驻服务（局域网 WebSocket + 可选连中继） |
| `pickup remote pair` | 打开配对窗口，展示二维码 / `pickup://pair?...` |
| `pickup remote status` | 查看服务与已配对设备 |

入口挂在 `bootstrap.py` 的 `remote` 分支，不进 TUI、不碰 Agent 只读接口。

## 协议分层

1. **中继层（明文）**：`[1 字节类型][16 字节通道][载荷]`。中继只看类型与通道做转发。
2. **应用层（密文）**：载荷解密后是 JSON：`req` / `res` / `evt`。方法名在 `remote/protocol.py`（`M_*`）与 iOS `WireProtocol` 对齐。

常用方法前缀：

- 只读：`sessions.*` / `session.messages` / `session.prompts` / `projects.list` / `runtimes.list` / `search`
- 订阅：`sessions.watch`、`session.watch`、`screen.watch`（事件通道 `sessions` / `session:<key>` / `screen:<key>`）
- 输入：`input.text` / `input.keys` / `input.image`
- 配对与推送：`pair`、`push.register`

画面帧字段见 `remote/screen.py` 的 `to_dict()`：`cols/rows/full/lines/cursor/history/status`。`status` 取画面最后一行有内容的文本，供手机对话页做实时状态条（历史文件可能长时间不落盘）。

## 加密与身份

- 设备与开发机各有一把长期 X25519；配对后手机把开发机公钥存钥匙串，开发机记设备公钥。
- 会话通道用握手派生的双向密钥；推送另有密封盒，中继字段：
  - `pickup`：base64 密文
  - `host_id`：明文开发机标识（**仅**用于手机取公钥；不含会话内容）
- 推送 APNs 需 `mutable-content`，以便通知服务扩展改写标题正文。

## 连接策略

手机侧默认：**局域网提示地址优先**，失败再回落中继。开发机 `pair` 载荷里的 `l`（local hints）与 `r`（relay）都要填对，否则手机只能走一侧。

## 踩坑

| 现象 | 原因 / 处理 |
|---|---|
| 手机一开终端，电脑窗口变窄 | 某处发了 `screen.resize`；手机端必须删掉这条调用 |
| 推送仍是占位文案 | NSE 解不开：缺 `host_id`、钥匙串 access group 两边不一致、或主 App 未把开发机公钥写入共享组 |
| Swift 里写了 `$(AppIdentifierPrefix)...` 却永远对不上钥匙串 | 宏只在 entitlements 展开；源码必须写死 `TEAMID.com.x0c.pickup` |
| 只装主包没装 `[remote]` | `pickup remote` 导入失败；提示用户装可选依赖 |
| 事件只到一个界面 | 客户端事件流做成了单消费者；必须按通道多播 |

## 验证

```bash
# 协议与加密单测（含在全量 ci-test）
env -u TEXTUAL_DISABLE_KITTY_KEY python3 scripts/ci-test.py

# 本机冒烟（另开终端）
pickup remote pair
# 手机扫码后：列表是否刷新、输入是否进会话、推送标题是否解密
```
