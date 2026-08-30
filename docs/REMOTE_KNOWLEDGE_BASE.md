# 手机端远程接力（corral remote）

改、评审或排查「手机连开发机看会话 / 输入 / 推送 / 配对 / 局域网直连 / **换网不可用与中继**」前必读。

配套客户端：`../ios/`（见 `../ios/AGENTS.md`）。零知识中继：`../relay/`（开源自建看其 README；个人公网实例运维见 agentsync 基础设施知识库 `corral-relay.caozc.top` 节）。

## 产品边界

- 开发机服务由 `corral remote start` 启动后，必须立即在启动它的终端输出可供手机客户端扫描的配对二维码与手动配对码；不要求再执行第二条配对命令，也不以打开图形窗口作为前提。二维码沿用当前一次性、十分钟有效的配对凭据与 v2 载荷（含中继地址），不得为方便展示而弱化配对或换网可达性。
- 服务已经运行时再次执行 `corral remote start`，必须不重启服务、直接刷新并输出新的二维码；禁止只返回「已在运行」让用户另找配对命令。
- 服务**未**运行时，`corral remote start` **就是**常驻进程：打完二维码后占住当前终端，直到被停掉。不得把「命令一直不结束」当成卡死去杀；需要本回合继续干活时放到后台，再用 `corral remote status` 确认 running。`--quiet` 同样占进程。只有已在运行的第二次 `start` 才会打完二维码就退出。
- 手机与开发机的远程接力须按端到端链路持续优化：没有会话或画面变化时，不得反复编码、复制或广播同一份数据；有变化时仍须及时送达所有已订阅页面。性能改动不得把事件改回单消费者，也不得恢复手机改变开发机窗口尺寸的能力。
- 手机端远程数据必须遵守明确的数据边界：一次只传当前会话和当前页面需要的字段，不把无关会话、无关参与者、内部事件或无界历史塞进移动端首包；历史、增量和画面必须分别定义上限、游标/序号与重同步方式。
- 远程协议必须把「快照、增量、确认、重连」作为一套契约设计：每个订阅都能说明数据范围、版本、序号与是否完整；断线后按游标补增量，游标失效才请求受限快照，不能靠客户端猜测或把整份历史反复重传。
- 加密前允许对可压缩的结构化载荷做无损压缩：超过约 1 KB 才压缩，载荷头带标记与未压缩长度上限；密文、中继和解密边界不变，不能为了压缩泄露会话明文。开发机与手机必须使用同一套 raw DEFLATE（RFC 1951）。Apple Compression 的 `COMPRESSION_ZLIB` 名称容易误导——它实际输出的是裸 deflate，不是 zlib 包装；不要改成 `zlib.compress()` 的 RFC 1950 包装去“对齐名字”。
- 会话消息的语义只允许用户与助手进入聊天时间线；工具调用、服务事件、连接状态和未知角色必须有独立的数据类别，不能被当作普通聊天气泡，也不能因客户端静默丢弃而让助手正文消失。

- 开发机跑 `corral remote` 常驻服务；手机只连这台服务，不直接扫各助手历史文件。
- 中继只做路由与代发推送，**看不到**会话明文；推送正文在手机本地用设备私钥解开。
- 手机与桌面共享同一个保活窗格时，**手机端禁止发 `screen.resize`**——否则会把电脑正在看的窗口挤窄。服务端即使收到也会以 `usage_error` 拒绝，**不会**改桌面窗口尺寸（不挂真实 resize 实现）。
- 远程能力的组件（`cryptography` / `websockets` / `segno`）不进主安装包：首次执行会启动服务或配对的命令时，必须自动、幂等地补齐到 **当前 `corral` 命令实际使用的安装副本**。不得误装到系统 Python 后仍报缺依赖；只有网络或软件源不可用时才报清晰失败原因与可重试提示。只读状态查询不得为检查而改动安装环境。

## 命令入口

| 命令 | 作用 |
|---|---|
| `corral login` / `logout` / `whoami` | 公共中继 GitHub 设备码登录（自建单租户不需要） |
| `corral remote start` | 启动常驻服务（局域网 WebSocket + 可选连中继），并立即输出配对二维码。未运行时本命令即守护进程、不自行退出；已在运行则只刷新二维码后返回 |
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
| `hello` | 含 `paired` / `runtimes`（未配对为空）以及 **`relay_url` / `relay_enabled` / `local_enabled`**（未配对也返回；关中继时 `relay_url` 为空串）。`capabilities` **增加** `"planes": ["control", "data"]`（旧客户端忽略未知字段）。请求带 `"want_data_plane": true` 时额外给一次性 `"data_bind"`；不带该字段的旧客户端不发 token，仍单连接。数据面第二条 WebSocket 独立握手后 `hello`：`{"plane":"data","bind":"<token>","name":...}`。令牌绑定设备公钥、TTL ≤ 120 秒、一次性；校验失败只关数据通道，不得踢控制面。 |
| `sessions.list` / `sessions.watch` | `{"sessions":[...],"version":"<窗指纹>","unchanged":false,"has_more":bool,"total":int}`。请求可带 `since_version`；版本相同则 `unchanged=true` 且**不带** `sessions`。旧手机忽略多余字段仍读 `sessions`。**禁止**把未变回包当成空表覆盖。列表窗口与截断规则见 `docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md` §4.5 |

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

信任假设与硬约束（2026-08-08 审查后落地；**2026-08-30 对「手机走公网连开发机」整条线复审**，结论见本节后半）：

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

### 2026-08-30：公网连接线复审与落地

审查范围：扫码配对、局域网直连、公网中继、推送密文、手机抢答选路。对照同类零知识中继（中继只转发密文、身份钉在扫码时的开发机公钥上）。**结论：会话正文在公网路上是加密的，中继即使被攻破也读不到对话；真正的风险在「可用性」和「根凭据怎么保管」，不是「中继偷看」。**

仍然成立、禁止为「看起来更安全」拆掉的：

- 手机用扫码得到的开发机长期公钥做握手，不信任中继宣布的身份。只重放公钥不能完成授权，必须等第一条可解密密文。
- 配对码约 80 bit、十分钟、空码拒绝、恒定时间比对；陌生设备只在配对窗口内能完成握手。
- 私钥落盘 0600；手机钥匙串 `AfterFirstUnlockThisDeviceOnly`，锁屏后推送扩展仍能解密封壳，但不进 iCloud。
- 手机系统传输安全只放行局域网明文，公网默认必须加密；开发机侧明文中继要显式 `--insecure-relay`。
- 浏览器跨站 WebSocket（带 Origin）局域网与中继都拒。
- 解绑以磁盘为准并踢连接；只读配对、破坏性操作二次确认、新建会话目录白名单仍有效。

已落地（2026-08-30 当晚；不要退回去）。个人公网中继已于当日 18:33 换上含第 1、3 条的二进制（覆盖安装与「误把 Mac 程序拷上去服务起不来」见基础设施知识库 `corral-relay.caozc.top` 节）：

1. **单租户中继禁止离线换注册钥匙抢走坑位。** 第一次见到某路由标识仍按信任首次使用登记；之后换钥匙必须同时带旧钥匙签名的 `X-Corral-Prev-Auth`（与当前断言同一路由标识、同一时间戳、同一 nonce）。电脑执行 `rotate-key` 会把旧钥匙暂存在 `host.key.prev`，下次连上中继后删掉。丢了注册钥匙：操作者自己上中继删那条登记，不能靠「离线换一把」自动收回。多租户仍走账号登记，不走这条。
2. **配对码必须原子作废。** 读码与删窗口在同一把锁里；同时扫同一张码只允许一台成功。
3. **中继限流看真实来源。** 来自环回/私网反代时读 `X-Forwarded-For` 第一段，其它连接只用对端地址（防伪造）。另加按开发机维度的建连预算，避免反代后「整台共用一桶」被一个人打满。开发机自己的通道上限仍在。
4. **扫码必须明示「配成功等于把这台电脑交给这部手机」。** 电脑端打印二维码时、手机扫码页都要出现这句。只读配对维持原有只读说明。

仍不修、也不要当成漏洞去改的：

- 中继对手机接入仍只先验路由标识（未配对读不到、也控制不了）。大规模对公众开放前再加配对时签发的短时票据；见中继 README「当前尚未覆盖的边界」。
- 局域网口听在全部网卡：给 Tailscale / 同网直连用。有公网地址或端口转发时这个口会暴露到互联网；未配对仍要猜配对码。
- 每次 `corral remote start`（含已在运行时刷新二维码）都会重新打开十分钟配对窗口。终端回滚、SSH 录像、把配对链接发到聊天里，等于把根凭据交出去。
- 中继能看见谁在连谁、每帧多大、何时连——零知识中继的固有元数据。
- 未做中继证书钉扎：证书体系被劫持时对方最多变成另一台中继。
- 已配对手机能向助手粘贴任意文本 = 完整执行权。丢失手机用 `corral remote unpair`。

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
| 换网后对话像重新加载整段历史、重连后聊天闪空 | 旧手机重连会再要一整段尾部窗口。新契约：`session.watch` 带已应用到的序号和历史代次；开发机只补缺口（`resume=replay`，空包不是清空），对不上才给尾部（`resume=tail`）。终端画面仍只留最新帧。契约见 `docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md` §4.3。自 CLI **0.24.150** / iOS **1.0.11** 起（本机与 suzhou 常驻远程已于 2026-08-30 17:05 左右换成该版）；旧客户端不带序号仍走整段尾部。真机换网体感仍须在手机上点一次确认 |
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
| `corral remote start` 一直不结束 / 超时被标失败 / 随后通道没了 | 服务本来没在跑时，该命令前台就是守护进程，不会自行退出。超时杀掉它等于把刚拉起来的通道掐掉。放到后台再 `corral remote status` 看是否 running；不要把 `start; sleep; status` 串在同一条前台命令里等 start 返回。已在运行时再执行 start 才会打完二维码就退出。`--quiet` 同样占进程 |
| 执行 `corral remote start` 提示缺 `cryptography` / `websockets` / `segno`，或只显示手动配对码没有终端二维码 | 当前实际运行的 Corral 安装副本没有远程组件；启动或配对命令必须自动补齐。pipx 是隔离环境且默认不含 pip，必须走 `pipx inject corral …`，不能把包装到系统 Python；若自动补齐失败，才提示检查网络或软件源后重试 |
| 单租户中继上执行 `corral login` | 登录并不适用；客户端必须立即说明该中继无需账号并继续可用，不得向不存在的设备码入口发请求后抛 404。主域名若返回 404，说明公共多租户尚未部署；若要启用，必须先在服务器配置数据库、会话密钥和 GitHub OAuth 应用，禁止把单租户实例伪装成已隔离的公共服务 |
| 守护进程还是旧名 `pickup`（改名前起的），想换新名重启 | `corral remote stop` 停旧进程后 `corral remote start` 即可，`identity.key` 与手机配对都在 `~/.local/state/corral/remote/`，不会丢。**relay_url 保持在别名 `wss://pickup-relay.caozc.top`**：`is_public_relay()` 只认主域名，改成主域名反而会被要求先 `corral login`（多租户账号路径）；别名与主域名是同一服务（2026-08-25 实操验证） |
| 新版 CLI 守护进程连中继报 `HTTP 404`（events.log `remote_relay_disconnected`） | 首尔中继二进制落后（只有 `/v1`，新版 CLI 走 `/v2/host`）。按 agentsync 基础设施知识库 `corral-relay.caozc.top` 节升级中继，升级后 v1/v2 并存；注意实际单元名是 `pickup-relay.service`，不是文档早年写的 `corral-relay.service`（2026-08-25 已升级） |
| 手机 App 突然连不上、守护进程状态一切正常 | 手机 App 已升 v2 协议（`/v2/device`，路由 id 由主机 X25519 公钥派生），而守护进程还是旧版只登记 v1（旧路由 id 是十六进制老格式）。把守护进程升到当前版本即恢复；配对按设备公钥绑定，手机无需重扫。端到端验证用 `corral remote pair --readonly --json` 拿 `--code` 交给 `relay/scripts/device_probe.py`（探针钥匙若重新生成过，旧配对作废须重新配对） |
| 空状态目录里 `corral remote` 测试或首次启动卡住 | `load_state` 持锁时会再进 `load_or_create_identity` / `host_key`；`config._lock` 必须是 `RLock`，改回普通 `Lock` 会在没有 `identity.key` 时死锁 |
| 事件只到一个界面 | 客户端事件流做成了单消费者；必须按通道多播 |
| 手机会话列表或打开历史极慢 / 转圈后「开发机响应超时」 | 不是单纯中继慢。2026-08-29 经公网中继、按手机同款请求实测：`sessions.watch` 整表 535 条约 13.5s（已接近手机 20s 超时），随后打开约 87MB 历史的 `session.watch` 在 20s 内无回包，中继因心跳未应答被掐断。根因是常驻进程把扫描/解析与心跳放在同一把解释器锁上，且把几百条闲置会话整表塞进首包。开发机必须：列表首包只带当前页（等待/置顶优先、闲置截断）、解析大文件时让出锁、心跳超时宽于一次冷解析、回包失败要回错误而不是默默断连接。详见 `docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md` §4.5。截断之后若**每次进列表仍先转圈**，是手机没落下该机上次窗口：必须先画出快照，刷新带版本号，未变不重传、也不得把未变回包当成空表。**禁止**只加大手机超时、只靠压缩、用滚动分页冒充首屏优化、或用 `sessions.list --limit 5` / 本机 unittest 冒充已验收 |
| 打开大历史第一次仍像卡死 / 详情把那条设备通道堵住 | 缓存未命中时禁止从 JSONL 文件头读到尾。第一次打开只从末尾向前取完整行，解析足够填满当前消息窗口的记录；工具配对不完整允许再向前一块。向前翻页从窗口左缘再补一块，不要为翻一页读完整文件。左侧还有未读字节时 `has_more` 必须为真。Cursor 按 rowid 取尾部，不要扫全表。文件变长仍从上次偏移增量读。解析/IO 失败降级为未命中或本轮无新消息，不得炸通道。改了读取语义必须抬规范化缓存版本并重启常驻服务。权威设计见 `docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md` §4.2 |
| 打开大历史时输入/心跳被堵住 | 历史页和终端帧必须走第二条数据面连接；控制面继续承载输入、短 RPC、列表事件、对话实时事件和心跳。旧手机不声明 `want_data_plane` 时仍单连接。数据面队列满只丢过时帧或拒绝新的历史页，禁止因此踢掉控制面。错误的 data_bind 只关数据通道。 |
| Cursor 用户气泡里出现整段系统上下文 | 远程富消息必须走与本地扫描器相同的 `user_query` 提取；不能在手机端用固定字符串过滤。未重启常驻服务时仍会发出旧解析结果 |
| Pi（或任意新助手）会话在手机上是空聊天，电脑预览却有对话 | 远程富消息有独立解析表，不会回落到桌面扫描器。Pi 曾完全未登记，打开详情只能拿到空窗口。补登记后必须抬高规范化缓存版本并重启常驻远程服务，否则会继续命中「空结果」缓存 |
| Codex 详情第一句是系统说明 / 打开像空白 | 首轮 `response_item` 常把 `# AGENTS.md instructions`、环境块写成 user；桌面扫描器会丢掉，旧远程解析会整段当人话。中断标记 `<turn_aborted>`、`<subagent_notification>`、`<user_action>` 同理。手机时间线若再抄桌面小窗黑名单，还会把「对本仓库做 code review」这种真人可见提问滤成空白。服务端丢掉高置信系统包装，真人提问必须留下 |
| Claude 详情多出一条「到点了」系统通知 | 到点任务通知挂在 user 轮次下，桌面预览按 `origin.kind` 丢掉，远程必须同样丢掉，不能当成用户气泡 |
| 电脑预览正常、手机某个助手仍是旧内容或空聊天 | **本机和开发机是两套常驻进程**。源码改了不等于手机已换新解析。2026-08-29 真机：开发机远程进程从 8 月 25 日起一直没重启，Pi/Codex 修复写进源码后手机仍走旧进程。修完必须重启**用户正在连的那台**的 `corral remote`，并抬高规范化缓存版本 |
| 只抽了一条 Codex 就说「详情修好了」 | 六个助手历史格式不同，问题不会碰巧相同。验收必须每个助手各打开一条有最后一句的真实会话：首句不能是系统说明，列表有最后一句则详情不能空。`phone_remote_acceptance.py` 按助手抽样，禁止只验体积最大的那一条 |
| 同一连接第二次 `session.watch` 历史为空 | 连接级订阅已存在时 `_subscribe` 返回 false，旧实现直接回空列表；应走 `conversation_page`（与 `screen.watch`→`resync_screen` 同理），且不增加中枢订阅计数 |
| 聊天状态条与终端页叠订后第二帧空白 | 同连接重复 `screen.watch` 必须 `resync_screen`，不能只加订阅 |
| 手机上两台开发机点进去会话一模一样 | 不是身份撞车、也不是两台电脑共用历史。手机切机时先改「当前选中」再拿选中项判断要不要换连接，会继续拉上一台的列表。必须按「真正连着哪一台」决定重连。排查入口：`ios/docs/troubleshooting/2026-08-29-two-hosts-same-sessions.md` |

## 验证

宣称「手机列表/详情已可用」时，必须同时给出：常驻服务启动时间、手机或探针走的是中继还是直连、以及下面这条**与手机同款**的路径。只跑编译、只跑 unittest、只跑 `sessions.list --limit 5` 都不算完成。

```bash
# 协议与加密单测（含在全量 ci-test）
env -u TEXTUAL_DISABLE_KITTY_KEY python3 scripts/ci-test.py

# 本机冒烟（另开终端）
corral remote status   # 须见公网中继在线，勿长期 --no-relay
corral remote pair --readonly --json   # 二维码须为 v=2 且含 r= 中继；仅有 l= 则换网必挂
# 公共中继先 corral login
# 按手机真实请求走公网中继：整表订阅 + 每个助手抽一条详情 + 20s 超时 + 空闲后再心跳
python3 scripts/phone_remote_acceptance.py \
  --relay wss://pickup-relay.caozc.top \
  --key <hello/pair 输出的公钥> \
  --code <只读配对码>
# 必须打用户正在连的那台开发机（本机和开发机公钥不同）。只抽一条 Codex 不算过。
# 2026-08-30 17:11 本机 `dev`（0.24.150 常驻已重启）经 `wss://pickup-relay.caozc.top`：
# 整表首包 0.52s / 80 条；Cursor、Codex、Pi、OpenCode、Claude 详情均在 2s 内有正文；
# 双连接竞速与空闲 25s 心跳通过。首包窗口里没有 Kimi 样本，不能据此说 Kimi 详情已验。
# 可选：叠加蜂窝近似（额外往返 + 带宽上限）
#   --rtt-ms 80 --bytes-per-sec 50000
```

`relay/scripts/device_probe.py` 默认只拉 5 条摘要、不打开详情，只能证明「中继握手通了」，不能证明列表和详情能在手机超时前回来。

换网验收（对标 shell-gate，缺一不可）：

1. 开发机中继在线；手机用蜂窝或不在开发机局域网仍能连上并列出会话。
2. 同局域网时优先直连（可加速），失败须自动回落中继，**不要**要求用户再扫一张「外网码」。
3. 无手机时用 `cli/scripts/phone_remote_acceptance.py` 经当前配置的中继跑完整表订阅，并**每个助手各打开一条详情**；`device_probe.py` 只作握手对照。
