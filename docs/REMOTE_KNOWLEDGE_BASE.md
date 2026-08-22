# 手机端远程接力（corral remote）

改、评审或排查「手机连开发机看会话 / 输入 / 推送 / 配对 / 局域网直连 / **换网不可用与中继**」前必读。

配套客户端：`../ios/`（见 `../ios/AGENTS.md`）。零知识中继：`../relay/`（开源自建看其 README；个人公网实例运维见 agentsync 基础设施知识库 `corral-relay.caozc.top` 节）。

## 产品边界

- 开发机跑 `corral remote` 常驻服务；手机只连这台服务，不直接扫各助手历史文件。
- 中继只做路由与代发推送，**看不到**会话明文；推送正文在手机本地用设备私钥解开。
- 手机与桌面共享同一个保活窗格时，**手机端禁止发 `screen.resize`**——否则会把电脑正在看的窗口挤窄。服务端即使收到也会以 `usage_error` 拒绝，**不会**改桌面窗口尺寸（不挂真实 resize 实现）。
- 可选依赖：`pip install 'corral[remote]'`（`cryptography` / `websockets` / `segno`），不进主依赖。

## 命令入口

| 命令 | 作用 |
|---|---|
| `corral login` / `logout` / `whoami` | 公共中继 GitHub 设备码登录（自建单租户不需要） |
| `corral remote start` | 启动常驻服务（局域网 WebSocket + 可选连中继） |
| `corral remote pair` | 打开配对窗口，展示二维码 / `corral://pair?v=2...` |
| `corral remote status` | 查看服务、账号与已配对设备 |
| `corral remote rotate-key` | 轮换 Ed25519 注册密钥；路由标识不变，手机不必重扫 |

入口挂在 `bootstrap.py` 的 `remote` 分支，不进 TUI、不碰 Agent 只读接口。

## 协议分层

产品名是 **Corral**。v2 的协议字符串、子协议（`corral.v2`）、请求头（`X-Corral-*`）、HKDF info（`corral/remote/v2 …`）、配对 scheme（`corral://pair`）一律用 `corral`，禁止再引入 `pickup`。旧环境变量、旧推送字段、`/v1` 的 `X-Pickup-*` 只留在存量兼容路径。

1. **中继层（明文）**：`[1 字节版本=2][1 字节类型][16 字节通道][载荷]`，子协议 `corral.v2`。路径 `/v2/host`、`/v2/device?host=<routing_id>`。中继只看版本、类型与通道做转发。`routing_id` 由开发机 X25519 公钥派生。开发机用 Ed25519 签名断言（`X-Corral-Auth`）鉴权，不再用 Bearer token。细则见 [relay/docs/PROTOCOL_V2.md](../../relay/docs/PROTOCOL_V2.md)。
2. **应用层（密文）**：载荷解密后是 JSON：`req` / `res` / `evt`。方法名在 `remote/protocol.py`（`M_*`）与 iOS `WireProtocol` 对齐。HKDF info 为 `corral/remote/v2 …`。

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
  - `corral`：base64 密文
  - `host_id`：明文开发机标识（**仅**用于手机取公钥；不含会话内容）
- 推送 APNs 需 `mutable-content`，以便通知服务扩展改写标题正文。
- **密钥确认**：握手 HELLO 之后，开发机必须等到对端发出第一条可解密密文才 `attach` 并写盘。仅重放公钥不能完成授权。
- 身份与状态落在状态目录（`CORRAL_STATE_DIR` / `XDG_STATE_HOME` / `~/.local/state/corral/remote`），不再放缓存目录；旧缓存路径会一次性迁过去。

## 安全边界

信任假设与硬约束（2026-08-08 审查后落地）：

- **已配对手机 = 开发机上的完整代码执行权**（可向默认跳过权限审批的助手会话粘贴任意文本）。配对码是根凭据：一次性、十分钟过期、恒定时间比对。
- `corral remote unpair` 以磁盘为准；常驻服务会重读清单并在约两秒内踢掉已解绑连接。`touch_device` 不得用进程内陈旧快照整份覆盖把解绑写回去。
- 常驻服务必须**自持**上次加载状态文件的变更令牌再决定要不要重读——模块级全局令牌会被同进程的写盘更新，会导致「刚解绑却仍以为清单没变」。令牌取自纳秒 mtime；写盘后若令牌未前进（远程盘 / virtiofs 同刻写入常见）必须强制 `utime` 推进一步，否则同秒 unpair 会被漏掉。
- `corral remote status` 通过状态目录里的运行快照展示当前在线设备与最近远程操作（服务退出时清除）。
- `input.keys` 只接受 tmux 键名白名单；控制通道参数禁止换行 / 危险控制字节。
- 开发机侧通道数与建通道速率自设上限，不依赖中继记账。
- 局域网与中继均拒绝带 `Origin` 的浏览器跨站 WebSocket。
- 中继地址默认强制 `wss://`；明文 `ws://` 仅 `--insecure-relay`。
- `session.delete` / `session.stop` 必须带 `confirm: true`（手机端确认框之后再发）。
- `corral remote pair --readonly`：只能看会话 / 画面 / 搜索，不能输入或改会话。
- 新建会话的工作目录限定在已知项目或 `cwd_whitelist`。
- 限流错误码 `rate_limited`：配对尝试、输入、新建会话、推送登记、建通道。
- `corral remote rotate-key` 轮换中继 Ed25519 注册密钥；公开路由标识由 X25519 公钥派生，手机不必重扫。

## 连接策略

产品目标：手机扫码配对一次后，**任意能上网的网络**都应能连开发机（对标 shell-gate）；局域网只是更快路径，不能当唯一通路。

手机侧硬约束（**禁止退回串行死等**）：

1. **并发抢答**：全部局域网提示地址与中继候选同时发起，先握手成功者胜出并取消其余。
2. **每路独立超时**：局域网约 2 秒、中继约 8 秒；任何一路不得把整个连接卡住到系统默认的几十秒。
3. **重连必须重新选路**：按「这台开发机」重新抢答，禁止复用上次成功的具体地址（否则出门后会死磕家里局域网）。
4. **换网立刻重选**：Wi-Fi ↔ 蜂窝切换时强制重新抢答，不必等用户切前台。

关掉中继等于手机只能同网使用，**禁止当默认**（仅本机调试可显式 `--no-relay`）。设置里关掉「局域网优先」时只走中继。

开发机 `pair` 载荷里的 `l`（local hints）与 `r`（relay）都要填对；若旧配对二维码没有 `r=`，手机可在后续 `hello` 里读到 `relay_url` / `relay_enabled` / `local_enabled`（未配对也返回）并写回本地 Host 记录，无需重新扫码。公网默认中继为 `wss://corral-relay.caozc.top`。

`corral remote status` 必须能区分「配置了中继地址」与「中继长连接真的在线」——看运行快照里的 `relay_online` / 人读输出的「中继：在线/离线」，不要只看 URL。

## 踩坑

| 现象 | 原因 / 处理 |
|---|---|
| 扫码后换网不可用 | 开发机关了中继（`--no-relay`），或历史占位中继域名不可达；确认 `corral remote status` 显示**中继：在线**（不只是有 URL）。手机侧：新客户端对无 `r=` 的旧配对会回落内置默认中继；也可靠 `hello.relay_url` 写回 Host |
| 蜂窝下要等很久才连上 | 旧客户端串行先试局域网、无超时；不可达局域网会卡到系统默认约 60 秒。必须并发抢答 + 局域网 2 秒超时。重连若仍复用旧局域网地址也会同样慢 |
| 本机以为中继「TLS/证书坏了」其实域名根本不存在 | 旧默认 `relay.corral.sh` 是 **NXDOMAIN**。家里 OpenClash fake-ip 会给未解析域名塞 `198.18.x`，本地 `dig`/`curl` 看起来像「连上了再 TLS 挂」。判真伪：用 DoH（或非本机网络）查权威解析；正确默认是 `wss://corral-relay.caozc.top` |
| 手机一开终端，电脑窗口变窄 | 某处发了 `screen.resize`；手机端必须删掉这条调用；服务端应拒绝而非执行 |
| 新建会话页项目列表空白 | `projects.list` 缺 `path`/`name`（旧版只有 `cwd`/`label`）；两端需同时认两套字段 |
| 发送失败但输入框已清空 | 客户端在 `try?` 后无条件清空草稿；应仅在成功时清空并展示服务端错误文案 |
| 置顶接口永远回未置顶 / 组内会话点置顶无效 | `session.pin` 必须读 `pinned_session_keys`（不是已废弃的 `pinned_sessions`）；组成员不能单独置顶，应改切 `pinned_group_ids`（与桌面侧栏一致）。列表载荷里组字段用 `group.id`（值取自 `SplitGroup.group_id`） |
| 手机删掉组内一条后，另一条仍挂着幽灵分组 | `session.delete` 成功后必须 `layout_db.remove_session`，不足两成员时解散组 |
| 会话列表整页空白 | 手机 `SessionSummary` 对 `id`/`short_id` 按 String、数值按 Double 解码；`session_payload` 必须先做类型收口，任一字段类型不符会让整份 `sessions.list` 解码失败（客户端 `catch` 后静默空白） |
| 推送仍是占位文案 | NSE 解不开：缺 `host_id`、钥匙串 access group 两边不一致、或主 App 未把开发机公钥写入共享组 |
| Swift 里写了 `$(AppIdentifierPrefix)...` 却永远对不上钥匙串 | 宏只在 entitlements 展开；源码必须写死 `TEAMID.com.x0c.corral` |
| 只装主包没装 `[remote]` | `corral remote` 导入失败；提示用户装可选依赖 |
| 空状态目录里 `corral remote` 测试或首次启动卡住 | `load_state` 持锁时会再进 `load_or_create_identity` / `host_key`；`config._lock` 必须是 `RLock`，改回普通 `Lock` 会在没有 `identity.key` 时死锁 |
| 事件只到一个界面 | 客户端事件流做成了单消费者；必须按通道多播 |
| 同一连接第二次 `session.watch` 历史为空 | 连接级订阅已存在时 `_subscribe` 返回 false，旧实现直接回空列表；应走 `conversation_snapshot`（与 `screen.watch`→`resync_screen` 同理），且不增加中枢订阅计数 |
| 聊天状态条与终端页叠订后第二帧空白 | 同连接重复 `screen.watch` 必须 `resync_screen`，不能只加订阅 |

## 验证

```bash
# 协议与加密单测（含在全量 ci-test）
env -u TEXTUAL_DISABLE_KITTY_KEY python3 scripts/ci-test.py

# 本机冒烟（另开终端）
corral remote status   # 须见公网中继在线，勿长期 --no-relay
corral remote pair     # 二维码须为 v=2 且含 r= 中继；仅有 l= 则换网必挂
# 公共中继先 corral login
# 手机扫码后：列表是否刷新、输入是否进会话、推送标题是否解密
```

换网验收（对标 shell-gate，缺一不可）：

1. 开发机中继在线；手机用蜂窝或不在开发机局域网仍能连上并列出会话。
2. 同局域网时优先直连（可加速），失败须自动回落中继，**不要**要求用户再扫一张「外网码」。
3. 无手机时可用中继侧探测脚本（`relay/scripts/device_probe.py`）经 `wss://corral-relay.caozc.top` 列会话，证明开发机已挂上中继而非只开了局域网口。
