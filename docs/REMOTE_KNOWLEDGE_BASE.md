# 手机端远程接力（pickup remote）

改、评审或排查「手机连开发机看会话 / 输入 / 推送 / 配对 / 局域网直连」前必读。

配套客户端：`../ios/`（见 `../ios/AGENTS.md`）。零知识中继：`../relay/`。

## 产品边界

- 开发机跑 `pickup remote` 常驻服务；手机只连这台服务，不直接扫各助手历史文件。
- 中继只做路由与代发推送，**看不到**会话明文；推送正文在手机本地用设备私钥解开。
- 手机与桌面共享同一个保活窗格时，**手机端禁止发 `screen.resize`**——否则会把电脑正在看的窗口挤窄。服务端即使收到也会以 `usage_error` 拒绝，**不会**改桌面窗口尺寸（不挂真实 resize 实现）。
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
- 会话动作：`session.new` / `session.stop` / `session.delete` / `session.markRead` …
- 配对与推送：`pair`、`push.register`

成功返回形状（手机解码依赖这些字段，缺了会空白或静默失败）：

| 方法 | 成功 `d` |
|---|---|
| `input.text` / `input.keys` / `session.stop` / `session.delete` | `{"ok": true}` |
| `input.image` | `{"path": "<开发机落盘绝对路径>"}`（JPEG/PNG 等可识别字节；空/坏 base64 → `usage_error`） |
| `session.new` / `resume` / `handoff` | `{"session": <SessionSummary>}`（含 `key` 等列表字段） |
| `session.markRead` | `{"attention": "none\|unread\|working\|waiting"}` |
| `projects.list` | `{"projects":[{"path","name","cwd","label","count","mtime"}, …]}`（`path`/`name` 给 iOS 新建页；`cwd`/`label` 与桌面项目列表同义） |
| `runtimes.list` | `{"runtimes":[{"id","name","available"}, …]}` |
| `hello` | 含 `paired` / `runtimes`（未配对为空）以及 **`relay_url` / `relay_enabled` / `local_enabled`**（未配对也返回；关中继时 `relay_url` 为空串） |

画面帧字段见 `remote/screen.py` 的 `to_dict()`：`cols/rows/full/lines/cursor/history/status`。`status` 取画面最后一行有内容的文本，供手机对话页做实时状态条（历史文件可能长时间不落盘）。

## 加密与身份

- 设备与开发机各有一把长期 X25519；配对后手机把开发机公钥存钥匙串，开发机记设备公钥。
- 会话通道用握手派生的双向密钥；推送另有密封盒，中继字段：
  - `pickup`：base64 密文
  - `host_id`：明文开发机标识（**仅**用于手机取公钥；不含会话内容）
- 推送 APNs 需 `mutable-content`，以便通知服务扩展改写标题正文。

## 连接策略

产品目标：手机扫码配对一次后，**任意能上网的网络**都应能连开发机（对标 shell-gate）；局域网只是更快路径，不能当唯一通路。

手机侧默认：**局域网提示地址优先**，失败再回落中继。关掉中继等于手机只能同网使用，**禁止当默认**（仅本机调试可显式 `--no-relay`）。

开发机 `pair` 载荷里的 `l`（local hints）与 `r`（relay）都要填对；若旧配对二维码没有 `r=`，手机可在后续 `hello` 里读到 `relay_url` / `relay_enabled` / `local_enabled`（未配对也返回）并写回本地 Host 记录，无需重新扫码。公网默认中继为 `wss://pickup-relay.caozc.top`。

## 踩坑

| 现象 | 原因 / 处理 |
|---|---|
| 扫码后换网不可用 | 开发机关了中继（`--no-relay`），或历史占位中继域名不可达；确认 `pickup remote status` 显示公网中继在线。手机侧：新客户端对无 `r=` 的旧配对会回落内置默认中继；也可靠 `hello.relay_url` 写回 Host |
| 手机一开终端，电脑窗口变窄 | 某处发了 `screen.resize`；手机端必须删掉这条调用；服务端应拒绝而非执行 |
| 新建会话页项目列表空白 | `projects.list` 缺 `path`/`name`（旧版只有 `cwd`/`label`）；两端需同时认两套字段 |
| 发送失败但输入框已清空 | 客户端在 `try?` 后无条件清空草稿；应仅在成功时清空并展示服务端错误文案 |
| 置顶接口永远回未置顶 / 组内会话点置顶无效 | `session.pin` 必须读 `pinned_session_keys`（不是已废弃的 `pinned_sessions`）；组成员不能单独置顶，应改切 `pinned_group_ids`（与桌面侧栏一致）。列表载荷里组字段用 `group.id`（值取自 `SplitGroup.group_id`） |
| 手机删掉组内一条后，另一条仍挂着幽灵分组 | `session.delete` 成功后必须 `layout_db.remove_session`，不足两成员时解散组 |
| 会话列表整页空白 | 手机 `SessionSummary` 对 `id`/`short_id` 按 String、数值按 Double 解码；`session_payload` 必须先做类型收口，任一字段类型不符会让整份 `sessions.list` 解码失败（客户端 `catch` 后静默空白） |
| 推送仍是占位文案 | NSE 解不开：缺 `host_id`、钥匙串 access group 两边不一致、或主 App 未把开发机公钥写入共享组 |
| Swift 里写了 `$(AppIdentifierPrefix)...` 却永远对不上钥匙串 | 宏只在 entitlements 展开；源码必须写死 `TEAMID.com.x0c.pickup` |
| 只装主包没装 `[remote]` | `pickup remote` 导入失败；提示用户装可选依赖 |
| 事件只到一个界面 | 客户端事件流做成了单消费者；必须按通道多播 |
| 同一连接第二次 `session.watch` 历史为空 | 连接级订阅已存在时 `_subscribe` 返回 false，旧实现直接回空列表；应走 `conversation_snapshot`（与 `screen.watch`→`resync_screen` 同理），且不增加中枢订阅计数 |
| 聊天状态条与终端页叠订后第二帧空白 | 同连接重复 `screen.watch` 必须 `resync_screen`，不能只加订阅 |

## 验证

```bash
# 协议与加密单测（含在全量 ci-test）
env -u TEXTUAL_DISABLE_KITTY_KEY python3 scripts/ci-test.py

# 本机冒烟（另开终端）
pickup remote pair
# 手机扫码后：列表是否刷新、输入是否进会话、推送标题是否解密
```
