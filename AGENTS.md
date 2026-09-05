<!-- managed:inherited-agents:start -->
<!-- source: /Users/geraltgraham/Codes/Corral/AGENTS.md -->
# Corral

终端会话接力 CLI，支持跨 Claude Code / Codex / OpenCode / Kimi Code / Cursor / Pi 会话恢复与接力。

通用工程规范：[Python 规范](../_standards/python.md)

## 文档导航

> 以下文档在涉及对应领域的开发、评审或排查时先读取。领域知识库与验证细则见组件内说明。

- [cli/AGENTS.md](cli/AGENTS.md)：改、评审或发布 Corral CLI 工具前必读（含领域知识库、截图验收，以及**排查「GitHub 持续发单测失败邮件 / 流水线作业排队十几小时 / macOS 作业挂死」「敲原命令没进托管」「启动 Pi 出现 No project session found with id」「新开 Pi 会话切走后消失 / 标题和 prompt 历史挂到另一空 Pi 分屏 / `/resume` 看不到其它会话」「看不到历史 Pi 会话 / 只能看到最近的 Pi / 旧 Pi 会话搬家之后去哪了 / 搬家挡发版」「钉过的 Pi 不在 pinned / 不筛项目名就不在置顶区、一筛又出现」「取消置顶 Pi 分组无效 / 右下弹出 Pinned 提示但分组还在置顶区」「刚在分屏里开的两个会话自己拆开 / 变成两张独立卡」「分屏里刚开的 Cursor 短暂重复 / 组内组外各一份闪一下又没了」「分屏两格画面一模一样 / 两个会话内容相同」「分屏两格但每格画面只占一半 / 像被压成 1/4 / 分屏里会话只占约 1/3 / 右侧大块空白」「从分屏 x 掉会话却切到被关掉的那一格 / 点 ✕ 后画面变成刚关掉的会话」「活跃会话看不到翻页 / Mac 没有 PageUp PageDown」「Cursor 画面疯狂抽动 / 宽度抖动 / 有的会话抖有的不抖」「本机与开发机版本对不上」「助手还在跑、侧栏却显示已结束」「Pi 进程还在却显示 Enter restart / 按回车进去还在跑」「Cursor 会话不见了 / 刚开的会话从列表消失」「Cursor 子代理还在跑、主会话却显示已结束」「还能执行 pickup / 敲 corral command not found / 新名无法启动」「Ctrl+R 后卡住 / 强停打出 Python 堆栈 / 退出后鼠标一点就出现 `^[[<`」「英文会话却出中文标题 / 标题跟界面语言走」「自身 CPU 占用过高 / 风扇狂转 / 两个窗口特别吃 CPU」「Cursor 进程过多 / 活动监视器一堆 agent」「ci-test 跑很久像卡住 / 发版检查跑三遍 / 不要每次都跑这么重」**的入口）。**有人提议做 Windows / WSL 兼容时进 `cli/docs/design/WINDOWS_COMPATIBILITY_DESIGN.md`（2026-08-27 已裁定不做）。**用 Corral 导出的会话数据写周报 / 日报 / 工作总结，或排查「导出内容不够写总结」时，从这里进 `cli/docs/SKILL.md` 的「拿会话数据做总结 / 周报时的边界」节。** Remote：`ssh://git@10.10.10.2:2222/Max/corral.git`
- [ios/AGENTS.md](ios/AGENTS.md)：改、评审、构建或真机验收手机客户端前必读（含签名推送、钥匙串共享、禁止 resize、模拟器/真机脚本）。**用户可见改动完成后必须立刻装到 iPhone Max**，不读会只改源码让真机继续跑旧过滤/旧界面。界面视觉、两端状态色、**会话展示字号**与**助手官方标识（禁止自绘）**见 [ios/docs/UI_DESIGN_KNOWLEDGE_BASE.md](ios/docs/UI_DESIGN_KNOWLEDGE_BASE.md)。**排查「两台开发机点进去会话一模一样 / 打开会话后闪退」进 iOS 故障排查索引。** Remote：`ssh://git@10.10.10.2:2222/Max/corral-ios.git`
- [cli/docs/REMOTE_KNOWLEDGE_BASE.md](cli/docs/REMOTE_KNOWLEDGE_BASE.md)：改、评审或排查手机 ↔ 开发机远程接力协议、配对、推送密文、画面差分、**审查手机互联网连接 / 中继 / 配对安全线、换网连不上 / 中继开关与默认地址、任意网络可达策略、`corral remote start` 一直不退出、会话列表或打开历史极慢 / 进列表仍先转圈 / 转圈后开发机响应超时、打开大历史第一次仍像卡死 / 详情把通道堵住、Cursor 用户气泡出现整段系统上下文、Codex 详情第一句是系统说明、Pi 会话在手机上是空聊天、两台开发机点进去会话一模一样、发了消息对话不更新 / 看不到助手回复 / 新开会话发了在吗 / 刚开的会话只有自己那句 / 终端里有字聊天没有 / This session is no longer in the list / 对话在刷但选择题还卡在底上 / 两道题合成一排选项**前必读（验收必须走中继上的整表订阅+**每个助手一条详情**，禁止用 5 条摘要、单条 Codex 或本机 unittest 冒充）。个人中继部署拓扑见 agentsync 基础设施知识库 `corral-relay.caozc.top` 节（**覆盖安装后服务起不来 / 刚换程序公网通道掉线**也进该节）。**审查「中继会不会偷看 / 扫码等不等于把电脑交出去 / 合盖后别人占了公网通道」进该文「安全边界」。**
- [cli/docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md](cli/docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md)：规划、设计、评审或排查手机会话列表/历史加载慢、**进列表仍先转圈、不要堆滚动分页**、**进详情后返回没反应**、实时数据被大历史拖住、**打开大历史第一次解析整份 JSONL**、Cursor 上下文泄漏、Codex 消息缺失、**Pi 手机聊天空白**、**发了消息对话不更新 / 会话已不在列表里 / 新开会话发了在吗 / 刚开的会话只有自己那句 / 终端里有字聊天没有**、**对话在刷但选择题还卡在底上 / 两道题合成一排选项**、直连/中继切换与断线恢复前必读。不读会把压缩或超时当成完整方案，漏掉缓存分页、尾部偏移读取、控制/数据隔离、序号恢复和真实设备验收。
- [cli/docs/design/PI_SESSION_IDENTITY_EXTENSION_DESIGN.md](cli/docs/design/PI_SESSION_IDENTITY_EXTENSION_DESIGN.md)：设计、开发、评审或排查 Pi / Codex 托管会话身份、pane 错绑、claim 协议、插件自动安装、旧隔离目录迁移前**必读**。不读会继续沿用已废弃的每会话小房间、让子代理抢主画面、破坏 Pi 原生恢复列表，或在身份不确定时误绑会话。

## 组件一览

| 目录 | 技术栈 | 状态 |
|---|---|---|
| `cli/` | Python | 活跃 |
| `ios/` | SwiftUI | 活跃 |
| `relay/` | Go | 活跃（零知识中继 + APNs） Remote：`ssh://git@10.10.10.2:2222/Max/corral-relay.git` |

## 领域地图（doc-init）

<!-- 覆盖度复核基线：2026-08-30 · 源码指纹 扫描 533 文件 / Python 152 · Swift 51 · Go 21 / 2 子模块 · 基线版本 0.24.156 -->

| 领域 | 入口锚点 |
|------|---------|
| 终端界面 | cli/src/corral/ui/ · cli/src/corral/activity_board.py · cli/src/corral/cli.py · cli/src/corral/display.py · cli/src/corral/textutil.py · cli/src/corral/theme.py · cli/src/corral/store.py · cli/src/corral/i18n.py · cli/src/corral/split_layout.py · cli/src/corral/ui_prefs.py |
| 会话关注状态 | cli/src/corral/attention.py · cli/src/corral/attention_signals.py · cli/src/corral/cursor_observer.py · cli/src/corral/store.py · cli/src/corral/ui/ |
| 会话全文搜索 | cli/src/corral/search.py · cli/src/corral/ui/search_modal.py |
| 内嵌实时终端 | cli/src/corral/embed.py · cli/src/corral/ui/embed_pane.py |
| 会话扫描与对话内容 | cli/src/corral/scan/ · cli/src/corral/scan/pi.py · cli/src/corral/transcript.py · cli/src/corral/models.py · cli/src/corral/runtime/ |
| 跨助手接力与启动 | cli/src/corral/runtime/ · cli/src/corral/runtime/pi.py · cli/src/corral/models.py |
| 新助手接入 | cli/src/corral/runtime/ · cli/src/corral/scan/ · cli/src/corral/runtime/pi.py · cli/src/corral/scan/pi.py |
| 托管会话身份 | cli/src/corral/pi_identity.py · cli/src/corral/pi_migration.py · cli/src/corral/codex_identity.py · cli/src/corral/pi_extension/ · cli/docs/design/PI_SESSION_IDENTITY_EXTENSION_DESIGN.md · cli/tests/test_pi_identity.py · cli/tests/test_pi_migration.py |
| 性能、派生缓存与原生加速 | cli/src/corral/cache.py · cli/src/corral/cache_cli.py · cli/src/corral/native.py · cli/src/corral/schedprio.py · cli/src/corral/bootstrap.py · cli/rust/lib.rs · cli/Cargo.toml · cli/scripts/benchmark.py |
| 可观测与诊断 | cli/src/corral/observe.py · cli/src/corral/agent_api.py |
| 会话保活 | cli/src/corral/keepalive.py · cli/src/corral/liveness.py · cli/src/corral/legacy_names.py |
| 直启子命令 | cli/src/corral/cli.py · cli/src/corral/projects.py |
| 命令拦截（shim） | cli/src/corral/shim.py · cli/src/corral/bootstrap.py · cli/src/corral/runtime/registry.py |
| 标题补全 | cli/src/corral/titles.py · cli/src/corral/titlegen.py |
| Agent 只读查询 | cli/src/corral/agent_api.py |
| 手机远程接力（开发机侧） | cli/src/corral/remote/ · cli/docs/REMOTE_KNOWLEDGE_BASE.md · cli/src/corral/bootstrap.py（任意网络 = 中继默认开；勿长期 `--no-relay`） · cli/tests/test_remote_service.py |
| 手机客户端 | ios/ · ios/AGENTS.md · ios/Corral/Design/ · ios/docs/UI_DESIGN_KNOWLEDGE_BASE.md（局域网优先、失败回落中继；换网冒烟） |
| 零知识中继与 APNs | relay/ · relay/docs/PROTOCOL_V2.md · 个人公网实例见 agentsync 基础设施知识库 `corral-relay.caozc.top` |
| Windows / WSL 兼容（已裁定不做） | cli/docs/design/WINDOWS_COMPATIBILITY_DESIGN.md |
| 开源发布与一键安装 | cli/install.sh · cli/.github/workflows/ · cli/scripts/publish-release.sh |
| CI 流水线 | cli/.github/workflows/test.yml · cli/scripts/ci-test.py · cli/.githooks/pre-push · cli/scripts/install-git-hooks.sh |
| 客户端自动更新 | cli/src/corral/updater.py · cli/src/corral/ui/update_toast.py |
| 隐私与本地数据边界 | cli/PRIVACY.md |

## 待补充知识库（doc-init backlog）

（当前无待补充项；开源自建中继看 `relay/README.md`；本机公网中继运维看 agentsync 基础设施知识库 `corral-relay.caozc.top` 节。）

覆盖度扫描必须跳过手机端 Xcode 构建缓存（`ios/.derivedData*`）；不跳过会把文件数顶到上限、把 Swift 入口扫没。

<!-- managed:inherited-agents:end -->

# corral 项目规范

## 文档导航

> 以下文档在涉及对应领域的开发、评审或排查时先读取。

- `README.md`：使用、修改、评审或扩展会话扫描、会话关注圆点、Cursor 状态观察、终端界面、标题生成、运行时适配和跨运行时接力
- `docs/TERMINAL_UI_KNOWLEDGE_BASE.md`：开发、评审、优化或排查终端界面、侧边栏会话关注圆点/已读判定、筛选/会话全文搜索弹窗（`Ctrl+F`）/新建会话、对话预览（含默认钉底滚动）、右侧多分屏顶栏、**会话标题语言 vs 界面语言（标题不是 chrome，不跟 locale）**、**中国龙横飞彩蛋（`#dragon-chip`、快照合成、CJK 定格画面、动画时长）**、分屏格数上限（`split_layout.MAX_PANES`，改这个数前必读）、分屏组合记忆、高级操作弹窗、Footer 按键、多语言文案、运行中系统/终端深浅色跟随、截图验收；**设计或修改「键盘输入归属谁」相关行为（自动聚焦、鼠标点击语义、回列表出口、输入蒙版、快捷键随焦点裁剪）前必读 §6 焦点契约**；排查 SSH 下 TUI 颜色失真 / 真彩降级时也读；**排查「已安装助手在新建 / 接力选择器中显示未安装」也读**；**排查「钉过的会话不在 pinned / 不筛项目名就不在置顶区、一筛又出现」也读**（进组不得毁掉独立 pin；筛选可见成员 < 2 则解散为独立会话）；**排查「取消置顶分组无效 / 右下弹出 Pinned / 已置顶提示但分组还钉着」也读**（显式 unpin 整组必须清成员独立 pin，否则 promote 立刻钉回去）；**排查「Pi 进程还在却显示 Enter restart / 按回车进去还在跑」也读**（格子走静态预览是没贴上 `keepalive_name`，回车会 attach 回原进程；根因在扫描/存活判定，不要改重启文案）；**排查「刚在分屏里开的两个会话自己拆开 / 变成两张独立卡」「分屏里刚开的 Cursor 短暂重复 / 组内组外各一份闪一下又没了」也读**（占位卡转正后组记忆必须跟上，即使右栏已切走）；**排查「从分屏 x 掉会话却切到被关掉的那一格 / 点 ✕ 后画面变成刚关掉的会话」也读**（关格不得让被关会话抢到焦点再被选择跟随打开）；**排查「两格分屏但每格画面只占一半 / 像被压成 1/4 / 分屏里会话只占约 1/3 / 右侧大块空白」也读**（较窄观看方含活跃会话看板、控制通道默认 80 列不得压窄共享画面）；**排查「活跃会话看不到翻页 / 没有分页按钮 / Mac 没有 PageUp PageDown / 不知道怎么翻页」也读**（三行卡：第二行可点上一页/下一页并循环，底栏露出 `[` `]`，禁止靠 Page Up/Down）；**排查「Your prompts / 本会话提问中间省略 / 中间提问看不见 / 小窗只能看两头不能滚看全部」也读**（展开态保留全部提问，高度封顶靠滚动，禁止砍中间）；**排查「Ctrl+R 后卡住 / 强停打出 Python 堆栈 / 退出后鼠标一点就出现 `^[[<`」也读**（Warp 拦 Ctrl+R；强停必须吞中断并同步关掉鼠标跟踪，禁止把 Ctrl+R 绑成全文搜索）
- `docs/EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md`：内嵌实时终端、右栏托管画面（最多四格；调整格数上限时必读，含通道池与最小托管宽度的连带约束）、控制通道池、抓帧与按键转发、焦点边界/结束会话、连接中卡死；排查或修改**内嵌助手深浅色主题识别错误**（外层终端背景色探测与注入）也从这里进；**排查「分屏两格画面一模一样 / 两个会话内容相同」也读**（同一 `keepalive_name` 开了两格内嵌终端）；**排查「分屏两格但每格画面只占一半 / 像被压成 1/4 / 分屏里会话只占约 1/3 / 右侧大块空白」也读**（另一扇窗口的活跃会话或更多格、或控制通道默认 80 列；较窄观看方不许压窄，不是单窗口把宽度除了两次）；**排查「分屏里 Claude 只占约 1/3 / 右侧大块空白」也读**（同一条：`window-size manual` + 最宽观看方说了算）；**排查「Cursor 画面疯狂抽动 / 宽度抖动 / 有的会话抖有的不抖」也读**（先看是不是两扇窗口或控制通道默认 80 列在改同一条会话的宽度；较窄观看方不许压窄。Cursor 长对话整屏重画是另一条、已验证不要给抓帧加中间态过滤）
- `docs/SESSION_SCANNING_KNOWLEDGE_BASE.md`：开发、评审、优化或排查会话扫描、关注状态证据、Cursor 状态观察、对话预览数据、判活、扫描性能和各助手历史格式；**排查「助手还在跑、侧栏却显示已结束 / 点进去变成历史预览」、尤其 OpenCode 带初始提问或 Pi 接力提问被误判成非交互时也读**（`--prompt` 后的说明词不当命令行，见该文 §6）；**排查「Pi 进程还在却显示 Enter restart / 按回车进去还在跑」也读**（无 pid 仍要按托管名贴 `keepalive_name`，claim 对不上 header 时用 sessionFile；不要拆重启提示或用 cwd 猜身份）；**排查「Cursor 会话不见了 / 刚开的会话从列表消失」「分屏里刚开的 Cursor 短暂重复 / 闪一下双份」也读**（只扫 CLI 正式历史，对话备份不进列表；误删正式目录后侧栏立刻没了；偶发自愈双份先看界面占位转正，长期双份才查判活）；**排查「Cursor 子代理还在跑、主会话却显示已结束」也读**（子代理不得进列表，但 live 必须记到父会话）；**排查「新开 Pi 会话切走后消失 / 标题和 Your prompts 挂到另一个空 Pi 分屏 / Pi 原生 `/resume` 看不到其它会话 / subagent 抢走主 pane」必读身份设计；扫描知识库 §2.2.1 只讲列表怎么消费 claim，禁止在扫描里继续修补已废弃的每会话小房间**；排查「只能看到最近的 Pi / 钉过的旧 Pi 从列表消失」也读（旧隔离目录占满 `limit`、置顶/组成员须经 `keep_ids` 豁免，以及 Pi v1 无 `parentId`）；**排查「分屏两格画面一模一样」也读**（`annotate` 把同一 pane 贴给两条会话）
- `docs/design/PI_SESSION_IDENTITY_EXTENSION_DESIGN.md`：设计、开发、评审或排查 Pi 会话身份插件、pane 会话错绑、插件自动安装与协议升级、协作式所有权锁、旧 `corral-*`/`pickup-*` 隔离历史迁移前**必读**。不读会继续沿用已废弃的 `--session-dir` 小房间、让 subagent 抢主 pane、破坏 Pi 原生 `/resume`，或在身份不确定时误绑会话。
- `docs/design/WINDOWS_COMPATIBILITY_DESIGN.md`：有人提议做 Windows / WSL 兼容、或想改 `install.sh`/CI 加 win 矩阵前**必读**——**2026-08-27 已裁定不做**（含原生与专项 WSL 产品化）；重开须机主显式推翻该裁定
- `docs/PERFORMANCE_KNOWLEDGE_BASE.md`：改、评审、优化或排查启动、扫描、预览、终端渲染、**手机远程画面无变化时的重复解析 / 编码 / 推送**、**手机会话历史打开极慢 / 同一会话被完整解析多遍 / 打开大历史第一次解析整份 JSONL**、侧边栏列表重建 / 分屏加格卡顿、派生缓存、原生加速、性能基准与预编译包；**排查「电脑忙时界面卡、自身占用却不高」「自身 CPU 占用过高 / 风扇狂转 / 两个窗口特别吃 CPU / corral 为什么吃 40% 核 / 网页终端跟着卡」「Cursor 进程过多 / 活动监视器一堆 agent / cursor-agent」、系统高负载调度优先级，或对照同类会话管理 / 内嵌终端 TUI 的踩坑地图时也读**（2026-08-31 起：v0.24.153 之后先看 `rchar` 与主线程，不要只怪全量重扫）
- `docs/CROSS_RUNTIME_HANDOFF_KNOWLEDGE_BASE.md`：跨助手接力、高级操作、原生恢复、空白新建、启动计划与接力提示词；**改接力说明，排查「接力时提示没有历史记录位置后终端界面退出」，或排查「刚派生的 OpenCode/Pi 会话被标成已结束」时也读**（提问会进 `--prompt` / 位置参数，不要为了判活去改说明词）
- `docs/NEW_RUNTIME_ONBOARDING_KNOWLEDGE_BASE.md`：新增、修改、评审或排查一种 AI 助手（含 Pi）的扫描、预览、恢复、接力、空白新建、命令托管、标题生成、**手机远程对话空白**或**命令别名导致的安装状态误判**前必读，避免出现半接入状态；**给新助手设计「带初始提问启动」时也读**（先分清交互窗口还是打印模式，提问正文不能当命令行扫）
- `docs/OBSERVABILITY_KNOWLEDGE_BASE.md`：改、评审或排查事件日志、诊断、F12 截图观测、界面异常前必读；**排查历史事件“明明发生过但日志没有”也读**（当前 256KB 整文件截断会永久丢掉前一段，不能据此断言事件未发生）
- `docs/MAINTAINER_GUIDE.md`：维护、评审或排查标题生成、**标题语言（跟用户提问主语言，不跟界面语言、不默认中文；排查「英文会话却出中文标题 / 标题跟系统语言走」）**、会话关注状态与 Cursor 观察器、会话保活（含**排查「Cursor 进程过多 / 活动监视器一堆 agent」**）、直启、Agent 只读接口、**启动 Pi 每次都打出「Warning: No project session found with id …」（进「Pi 扫描与启动」节；无害，禁止为消警告拆掉 `--session-id`）**、**排查「看不到历史 Pi 会话 / 只能看到最近的 Pi / 钉过的旧 Pi 从列表消失 / 旧会话搬家挡发版 / 搬家之后会话去哪了」也进该节**、开源发布与分发渠道（含**排查「发了新版本但用户升不了级 / `brew upgrade` 拉不到新版 / 发布卡在 CI 排队」**、要不要上 PyPI）、**CI 工作流（改 `.github/workflows/` / `scripts/ci-test.py` / 推送门禁与 `install-git-hooks.sh`、排查「GitHub 天天发单测失败邮件 / 作业排队十几小时 / macOS 作业挂死 / 本机漏跑 ruff / 多 Agent 脏树挡发版 / 推 tag 后要用 ls-remote 核对远端 / ci-test 跑很久像卡住 / 发版检查跑三遍 / 不要每次都跑这么重」前必读「CI 工作流」节）**、客户端自动更新及上述领域的维护级细节与历史踩坑（含 pipx/安装副本与源码分叉、SSH `COLORTERM` 真彩降级、内嵌 pane 背景色注入与助手深浅色主题的历次真机排查记录）；**排查「还能执行 pickup / 敲 corral command not found / 新名无法启动 / No module named pickup.bootstrap」进「内嵌面板」节改名后未重装入口那条**（禁止加回 `pickup` console script，跑 `scripts/dev-install.sh`）
- `docs/REMOTE_KNOWLEDGE_BASE.md`：改、评审或排查 `corral remote`、手机配对、**审查手机互联网连接 / 中继 / 配对安全线**、**服务启动即在终端输出可扫码二维码**、**`corral remote start` 一直不退出 / 超时杀掉后通道没了**、推送密文、画面差分、禁止手机 resize、可选依赖 `[remote]`、**换网不可用 / 中继默认与 `--no-relay` 禁区、任意网络可达、守护进程还叫旧名 pickup / 连中继 404 / 手机 App 突然连不上 / 会话列表或打开历史极慢 / 进列表仍先转圈 / 转圈后开发机响应超时 / 打开大历史第一次仍像卡死 / 详情把通道堵住 / Cursor 用户气泡出现整段系统上下文 / Codex 详情第一句是系统说明 / Pi 会话在手机上是空聊天 / 两台开发机点进去会话一模一样 / 发了消息对话不更新 / 看不到助手回复 / 新开会话发了在吗 / 刚开的会话只有自己那句 / 终端里有字聊天没有 / This session is no longer in the list / 对话在刷但选择题还卡在底上 / 两道题合成一排选项 / 换网后对话整段重拉 / 重连后聊天闪空**前必读（验收必须走中继上的整表订阅+**每个助手一条详情**，禁止用 5 条摘要、单条 Codex 或本机 unittest 冒充）；客户端工程见 `../ios/AGENTS.md`；个人中继部署见 agentsync 基础设施知识库 `corral-relay.caozc.top`。**审查「中继会不会偷看 / 扫码等不等于把电脑交出去 / 合盖后别人占了公网通道」进该文「安全边界」。**
- `docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md`：规划、设计、评审或排查手机会话列表/历史加载慢、**进列表仍先转圈、不要堆滚动分页**、**进详情后返回没反应**、实时数据被大历史拖住、**打开大历史第一次解析整份 JSONL**、Cursor 上下文泄漏、Codex 消息缺失、**Pi 手机聊天空白**、**发了消息对话不更新 / 会话已不在列表里 / 新开会话发了在吗 / 刚开的会话只有自己那句 / 终端里有字聊天没有**、直连/中继切换与断线恢复、**换网后对话像冷启动 / 按序号补缺口**前必读。不读会把压缩或超时当成完整方案，漏掉缓存分页、尾部偏移读取、控制/数据隔离、序号恢复和真实设备验收
- `docs/SKILL.md`：修改、评审 `agent_api.py` 面向 Agent 的子命令、字段或退出码语义（含 `diagnose`）；这是 Agent 侧唯一的使用文档，改命令行为必须同步这里。**用 `show`/`export` 的会话数据做周报、日报、工作总结、活动统计，或排查「导出的内容不够写总结 / 看不出到底改了什么」时，必读「拿会话数据做总结 / 周报时的边界」节**——那 5 条（对话不含工具调用与改码证据、标题只能当索引、`last_agent` 常为空、user 侧混着系统注入文本、没有成果字段）是不会改的产品边界，得在调用方侧校正
- `PRIVACY.md`：修改、评审或排查历史文件读取、会话关注状态库、Cursor 用户级观察配置、缓存写入、标题生成、跨运行时接力和开源隐私边界
- `CONTRIBUTING.md`：修改开源贡献流程、验证命令、设计边界或 PR 要求

## 架构约束

- `corral.cli` / `store` / `display` / `theme` 只负责入口、会话展示状态与用户选择，不得直接拼接某个运行时的启动参数。
- **入口分层与包顶层的兼容导出（改错了不报错，只会静默变慢或让老调用方失效）**：真正的命令入口是 `bootstrap.py`（`[project.scripts]` 指向它），它按子命令惰性分发，**只有进交互界面才 import Textual 与扫描器**——往 `bootstrap.py` 顶部加任何重量级 import，或把快速子命令（`--version`、`cache`、Agent 只读查询、`update`）改成经 `cli.py` 走一圈，都不会报错，只会让每次敲命令白付几百毫秒导入成本（实测 Textual 导入约 198ms），细则见 `docs/PERFORMANCE_KNOWLEDGE_BASE.md`「性能架构」。同理，`src/corral/__init__.py` 必须保持零重依赖：它只有 `importlib`/`os`/`sys`，历史扁平模块时代的符号（如 `RUNTIME_LABEL_STYLES`、`SessionStore`、`_filter_sessions_by_query`）靠 `_SYMBOL_EXPORTS` + `__getattr__` 惰性重导出。**移动或重命名这些符号时必须同步这张映射表**，否则 `corral.X` 形式的老调用方会在运行期才抛 `AttributeError`；也不要为了省事把它改成顶层 `from … import …`，那会让包顶层重新拖进整棵依赖树。`TEXTUAL_DISABLE_KITTY_KEY` 的 `setdefault` 必须留在包顶层（早于任何 `import textual`），原因与真实事故见 `docs/MAINTAINER_GUIDE.md`「CI 工作流」节。
- **派生缓存只做加速，任何异常都必须降级为「未命中」**（`cache.py`）：数据库损坏、锁竞争、只读文件系统都不得阻断原始历史读取，`CORRAL_CACHE=0` 要能完全绕开。一轮扫描内的元数据快照由 `begin_scan()` / `end_scan()` 圈定，两个并发扫描入口（`runtime/registry.py` 的 `scan_all`、`agent_api.py` 的 `_scan_runtimes`）都必须成对调用且 `end_scan()` 放在 `finally` 里；**快照严禁跨扫描长期持有**（同进程后续扫描会看不到本轮新写入的会话），payload 解码必须保持惰性。这几条写反了都不报错，只会表现成「列表少了会话」或「优化白做」，细则与实测数据见 `docs/PERFORMANCE_KNOWLEDGE_BASE.md`「派生缓存边界」。
- 运行时私有行为必须收敛在 `runtime/` 对应适配器中；新增运行时只实现扫描、对话预览、原生恢复、历史格式提示、接力新会话（读取其他运行时历史）和空白新会话（不关联任何历史，仅指定工作目录）两种启动能力，并在默认注册表注册一次。
- 跨运行时接力统一走“源适配器导出 `Handoff` → 目标适配器生成 `LaunchPlan`”，禁止增加 Claude→Gemini、Codex→Gemini 等两两转换分支。
- 同运行时使用原生恢复；跨运行时必须新建目标会话、让目标 Agent 按需读取原始 JSONL，不能改写或伪造原会话。
- 标题生成是独立服务，不属于任何运行时适配器。生成后端统一走 `titlegen.py` 的 `TitleGenerator` 抽象，覆盖与默认运行时注册表一致的助手（claude / codex / opencode / kimi / cursor）：本机装了哪个就可以用哪个生成标题，首选失败按注册顺序自动切换。`titles.py` 不得直接拼接任何 CLI 命令；`titlegen.py` 与 `runtime/` 互不 import——运行时适配器管「怎么恢复/接力会话」，标题生成器管「怎么无头问一次模型」，两者后端恰好重名但职责不同，不要合并。标题和界面状态使用“运行时 + 会话 ID”作为唯一键，新增运行时不得退回纯会话 ID。新增运行时必须同时在 `titlegen._GENERATORS` 加对应生成器，且若该 CLI 会把生成调用落盘成会话历史，对应扫描器必须加 `titles.PROMPT_MARKER` 前缀过滤。**生成标题的语言跟该会话用户提问的主语言，不跟界面语言，也不默认中文**（2026-08-30 裁定；细则见 `docs/MAINTAINER_GUIDE.md`「标题与排序」）。禁止把标题当 `i18n.t()` 文案，也禁止写「英文会话也可以用中文」。`PROMPT_MARKER` 是噪音过滤用的固定原文，不得翻译。
- 会话预览：选中非进行中会话时，右栏直接展示完整对话（**默认钉在最新消息**，上滚看更早；用户离开底部后列表刷新不得强行钉回）；已托管会话右栏展示内嵌实时终端。**在别的终端窗口里跑、没被 corral 托管的会话（`live` 且无 `keepalive_name`）拿不到实时画面**——右栏走完整对话那一路并在详情头写明原因，打开它必须先确认（那是对同一份历史另起恢复进程，不是接管），细则见 `docs/EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md` §1。唯一界面是左栏会话列表 + 右栏（可最多四格均分内嵌终端），禁止再加回全屏预览或纯列表第二套入口。右侧顶栏可点选已安装助手在当前项目下加格；分屏会话会形成持久会话组，结束后仍保留，运行成员才参与启动恢复；组名、成员、折叠、置顶与侧栏显隐见 `split_layout.py`（`~/.cache/corral/sidebar-layout.sqlite3`）。**这份记忆多窗口共享：所有写入必须经 `SidebarLayoutDB` 在事务里重读最新再叠加，界面只持有只读快照，禁止改快照后整份覆盖写**（那正是多开窗口互相抹掉置顶与分组的老缺陷）。细则与 `_detail_stick_bottom` 见 `docs/TERMINAL_UI_KNOWLEDGE_BASE.md` / `docs/MAINTAINER_GUIDE.md`。
- **外部运行会话（2026-08-08 裁定）**：上条会话预览规则中“打开它必须先确认”的旧表述已废止。外部运行会话只能保持静态预览，**不得弹确认框，也不得针对同一份历史另起恢复进程**；等待原窗口结束后才可正常恢复。
- **侧边栏末行间隔、会话组与关注圆点（硬约定）**：凡往左栏加控件（搜索框、新建项、未来任何块），**最后一行必须是间隔空行**，画在该控件自身高度内并算进命中区与选中高亮；禁止用 `margin`、兄弟空隙或 `ListItem` padding 做分隔（点在空隙上不会落到本项）。会话卡例外：固定三行正文、高度 3，不再另加末行空行；标题统一使用基础标题样式，不因运行中整行变绿；**首行整体 bold（与下面两行拉开层级），其中项目名比标题淡一档（`dim`）、标题本身不得 dim**——项目名是定位用的前缀，同亮度会和标题抢视线；淡化只用 `dim` 这类相对语汇，不要写死具体颜色（深浅色主题都要成立），窄栏截断时别把 `dim` 涂进标题；**首行最左是关注圆点**（等待回答黄 > 执行中绿 > 未读新结果红 > 无），独立会话卡圆点后接空格分隔的「项目 标题」（**不带冒号**），**组内子项不写项目名前缀**（项目已在组卡第二行）；**无圆点时不留占位空格**，标题直接顶到最左并吃满整行宽度（截断宽度按有无圆点取 `width - 2` 或 `width`）；第二行运行时靠右、第三行时间靠右。**第三行时间按新鲜度分四档亮度**（半小时内 / 三小时内 / 一天内 / 更早），最新一档与标题同色（着重显示），越旧越暗；档位色一律用 `$foreground` + 透明度经组件样式解析，禁止写死颜色或退回单级 `dim`，也禁止让时间行带上自己的背景色（会盖掉整行的选中/分屏底色）。圆点不得参与排序、筛选或计数。圆点字符 `●` 的 East Asian Width 是 Ambiguous：Rich 按 1 格算，把它放进首行文本流时必须让宽度预算与 Rich 一致，不要按「CJK 字体看起来占 2 格」去补偿；出图时 `docs/screenshots/capture.py` 只给「内容恰为该字形」的独立 `<text>` 换成非 CJK 等宽族来修观感。当前基准：搜索框高 2、新建项高 2、活动看板高 3（首行名称、第二行上一页/下一页、第三行留白）、会话卡与会话组卡高 3；组卡第一行只保留展开/收起三角、可选置顶标记和水果组名，**不得画关注圆点**；组卡第二行项目名与 `Group …` 同列左对齐；第三行留白（不写时间，避免与成员卡重复）；成员用贴左缘的半角框线 `├─ `/`└─ `（续行同列 `│`，无前导空格、不用 dim；禁止混全角竖线以免三行卡之间断线）且不在顶层重复。分栏时左栏固定宽 39（`ui/main_screen.py` 的 `LIST_PANE_WIDTH`），内层 `#sidebar-sticky` / `#sidebar-scroll` 的垂直/水平 `scrollbar-size` 均为 0（滚动条不占列宽，键盘与滚轮滚动照常）。**筛选框、＋新建和活动看板固定不滚**（`#project-search` 在列表外，`＋ 新建` 与活动看板在 `#sidebar-sticky`）；置顶块、Pinned 线与未置顶 Today / older 都在 `#sidebar-scroll` 里一起滚——置顶只改变排序（钉在列表最上），不冻在视口里，钉再多也不会裁切或挤掉未置顶；指针在固定头上滚轮仍带动会话列表、顶部不动。**改左栏宽度必须同步改 `selftest.sh` 的 IME 光标锚定断言**——那里把面板起点硬编码成第 40 列（`expected_x=$((40 + inner_x))`，即 39 宽 + 1 列空隙），只改宽度会让端到端冒烟直接判失败。**右栏分屏（≥2 格）时，侧边栏给当前会话组整组铺底（Group 行 + 全部成员），激活会话再重一档**；光标停在组卡上时整组贴 `-group-selected`（成员与组卡同档高光），激活成员再叠 `-split-active`。底色标在 `ListItem` 上，组标题 / 组标题且光标在其上 / 激活格 / 激活格且光标在其上四级必须单调递进。置顶用 `p` / `Ctrl+P`：独立会话可单独置顶，会话组只能整体置顶（组内成员改为整组置顶）；`Ctrl+P` 是与 `Ctrl+F` 同级的全局键，右栏实时格持焦时仍可用；已关闭 Textual 命令面板，不要再展示 `^p palette`；未置顶区跟 SessionStore 稳定顺序走（进入后已有项不因 mtime 更新而飘；新建会话仍插最前），只有置顶块固定在最上；**置顶与未置顶都非空时中间插一行居中 `Pinned`/`置顶` 的 `$primary` 蓝横线；未置顶按滚动 24 小时切 today/older 两桶（桶内不重排），两侧都有时插 `Today`/`今天` 线；高 1、disabled、键盘跳过；禁止 Older/其他标签**。细则见 `docs/TERMINAL_UI_KNOWLEDGE_BASE.md` / `docs/MAINTAINER_GUIDE.md`「界面」节。
- `agent_api.py`（`corral list`/`search`/`show`/`export`/`share`/`context`/`describe`）是只读数据接口，禁止新增任何执行/拉起副作用命令——corral 只负责把会话数据交出来，怎么用是调用方的事。暴露更多可见性字段（如运行中会话的 `live`/`pid`）不违反这条约束，只要新字段本身来自扫描/只读探测、不触发任何拉起或写操作；真正"接管/下发指令给运行中会话"的能力不属于 corral，留给调用方基于这些数据自行实现。命令参数与 `corral describe` 的输出必须共用同一份 `COMMANDS` 定义，不能各写一份导致漂移。新增或修改子命令时同步 `docs/SKILL.md`。
- Agent 接口里 `list`/`search` 的 `--limit` 固定表示每个运行时的扫描深度，`--top` 才表示最终返回条数；`--compact` 必须同时做到紧凑 JSON 和精简默认字段。改这三个参数或 `show --out` 大结果落盘行为时，同步 `corral describe`、`docs/SKILL.md` 和 `docs/MAINTAINER_GUIDE.md`。
- 会话保活（`keepalive.py`）是运行时无关的启动包装层，只在 `registry` 生成 `LaunchPlan` 之后、`execute_launch` 之前介入，禁止塞进 `runtime/` 某个具体适配器，也禁止让适配器感知 tmux 的存在。改保活匹配/回收逻辑前先读 `docs/MAINTAINER_GUIDE.md`「会话保活」节。`corral claude`/`corral codex` 直启子命令默认带 `_DirectLaunch` 进 TUI、经 `embed.host_session` 托管（与界面内「新建会话」同一路径），托管成功后必须立即登记侧边栏占位卡，禁止等待运行时写出首条历史；扫描器随后发现真实历史、占位卡转正时，侧边栏选中态与右栏分屏键必须一起迁移，不能退回「＋ 新建会话」空态；仅非真实终端 / `--no-keepalive` / 内嵌不可用时退回 `keepalive.enabled`/`wrap_plan` + `execute_launch` 旧路径（保活的第三个调用点，与 TUI 的 `_launch()` 复用同一套开关语义）。
- 内嵌面板（`embed.py`）是与 `keepalive.py` 平级的运行时无关层：不 attach，用 `capture-pane` 拿画面、经常驻 `tmux -C attach` 控制通道（`ControlChannel`）送按键与修改类命令（通道死亡自动回退外部 fork），把托管在保活 socket（`corral-*`/`sc-*` 命名空间）里的会话渲染进 TUI 右半屏。控制通道按 tmux 会话名维护通道池，多分屏可同时存活；`close_channel(name)` 只关指定格，省略 name 时关闭全部。适配器不感知本模块；`ui.main_screen.MainScreen` / `ui.split_pane_area.SplitPaneArea` / `ui.embed_pane.EmbedPane` 是主要调用方。tmux 是软件级硬依赖（TUI 与直启启动时检查，缺失即报错退出；agent_api 只读子命令不受影响）。环境变量新名为 `CORRAL_*`（`CORRAL_KEEPALIVE`、`CORRAL_KEEPALIVE_IDLE_HOURS`、`CORRAL_TITLE_GENERATOR`、`CORRAL_TITLE_MODEL`、`CORRAL_RUNTIME`、`CORRAL_SESSION_ID`），旧名 `SC_*` 一律保留兜底读取/注入，不得删除兼容路径。
- 运行时跳过权限审批的危险启动参数（如 Claude 的 `--dangerously-skip-permissions`、Codex 的 `--dangerously-bypass-approvals-and-sandbox`）必须声明为对应适配器的 `auto_approve_args` 类属性，不得在 `build_resume_plan`/`build_new_plan`/直启透传等多处各写一份字面量字符串；入口层和 `registry.build_passthrough_plan` 只负责按需拼接这个属性，不感知具体参数内容。
- **助手模型与推理强度完全归用户配置所有**：corral 的恢复、接力、空白新建、直启、命令拦截和后台标题生成都不得内置或注入模型/推理强度，必须继承对应助手自身的全局默认。唯一例外是用户明确给直启传入的参数，或用户明确设置仅用于标题生成的 `CORRAL_TITLE_MODEL`（兼容旧名 `SC_TITLE_MODEL`）；不得为了省额度或“质量更好”私自选模型。
- **「默认跳过全部权限问询」是本项目的既定产品默认，不是待讨论选项**（机主 2026-08-01 明确拍板）。凡是 corral 拉起运行时的路径——原生恢复、跨运行时接力、空白新建、直启透传，以及未来的命令拦截/shim 入口——都必须自动垫上该运行时的放行参数，让用户拿到的是开箱免打断的体验。新增运行时时，找出并验证它的放行参数属于接入工作的必做项，不是可选增强；找不到就在维护指南里如实记录能力差距，而不是默默留空。放行参数在某些运行时里只属于部分子命令、或对位置敏感（OpenCode 的 `--auto` 两者都占），这类规则写进适配器的 `compose_passthrough_argv`，不要塞进注册表。**禁止把它改成默认关闭、需显式开启，也不要再以「不安全」为由向机主重复征询确认**——理由是当前各家模型自身的谨慎度已足以覆盖日常风险，机主已知悉并接受。唯一允许不加的情形是运行时自身硬性拒绝该参数（如 Claude 在 root/sudo 下带 `--dangerously-skip-permissions` 会直接退出；OpenCode 的 `stats`/`export`/`auth` 等子命令不认 `--auto`），这类情形按"加了就起不来"的事实判断，与安全权衡无关。**运行时旧版本不支持放行参数不属于此列**：按"该升级那个助手"处理，不为旧版保留降级分支（机主 2026-08-04 拍板）。

## 发版要求

**功能/修复改完后必须发布新版本**（补丁位递增），不要只提交代码就结束。同步 bump `pyproject.toml` / `Cargo.toml` / `Cargo.lock` / `src/corral/__init__.py`，提交 `release: vX.Y.Z …`，打 annotated tag，推送 `github` 与 `origin`，**再跑 `bash scripts/publish-release.sh`**（建 Release、本机构建并上传安装包、更新 Homebrew 配方，一步到位；脚本自带收尾核对输出）。纯文档/规则整理且无产品行为变化时可不发版；有疑义时默认发版。

**不要把「推了 tag」当成发布完成。** GitHub Actions 的免费并发额度经常让整批任务排队几十分钟（真实发生过 45 分钟仍未开始），期间用户 `brew upgrade` 拿到的还是几个版本前的配方、一键安装脚本找不到预编译包。`scripts/publish-release.sh` 就是为此存在的：它在本机做完 CI 那两件真正决定「用户能不能升级」的事，CI 退化成补齐本机出不了的那部分平台包。细则与历史见 `docs/MAINTAINER_GUIDE.md`「开源发布」。

**发版前必须判定工作区里其他 Agent 的改动是否已完工，未完工则不得打 tag。** 本仓库长期有多个 Agent 并行改动，全局规范要求发版时「不挑拣、不等对方、一并纳入」——但那条的前提是那些改动**本身是完好的**。半成品跟着 tag 发出去，用户升级后就会撞上缺陷。判定手法（2026-07-31 实测有效）：

1. 跑之前先给工作区所有改动文件（含未跟踪文件）算一个哈希快照，跑完再算一次；**两次不一致说明有 Agent 正在编辑，此刻的任何提交都可能捕获到写了一半的文件**。
2. 用 `env -u TEXTUAL_DISABLE_KITTY_KEY python scripts/ci-test.py` 跑全量，失败用例按归属分类：**未跟踪的新模块 + 它自带的新用例成片失败 = 对方的新功能还没做完**，这是硬阻断，不要发版、也不要替对方修。
3. 真实案例：本次修 CI 时工作区并存着另一个 Agent 正在开发的会话概览新功能（新模块尚未纳入版本管理、自带用例 5 个全挂），同时 tmux 集成用例因两边同时跑真实 tmux 而大面积 `new-session` 失败——后者属于负载干扰、单独重跑即恢复，前者属于真未完工。两类要分清，不要笼统判成「测试挂了不能发」。

阻断时的正确做法：把自己的改动留在工作区不提交，向机主说明「谁的什么功能没完工、卡在哪」，由机主决定是单独发自己的修复、还是等对方收尾后合并发布。

## 验证要求

**首屏（进程启动到 TUI 首次渲染完成）延迟目标 ≤1s；这条红线已随界面层改用 Textual 放宽为非阻断项（用户已同意），但改动扫描/标题/界面代码后仍必须实测并如实汇报耗时，不能不测。** 改动扫描（`scan/claude.py`/`scan/codex.py`/`runtime/`）、标题或界面相关代码后，除下面的编译/单测外，必须额外跑一次真实计时并汇报数值：

```bash
python3 -c "
import time
from corral.runtime import default_registry
r = default_registry()
t = time.perf_counter()
r.scan_all(50)
print(f'{(time.perf_counter()-t)*1000:.0f}ms')
"
```

`test_session_scanning.py` 的 `StartupLatencyTests` 会在有真实会话数据时对同一调用做 <1s 断言（`python3 -m unittest -v` 已包含），但真实计时仍要单独跑一次确认，不能只信任测试里的一次采样。**不达标不再是提交阻断条件（硬性红线已放宽），但必须如实汇报实测耗时**；根因排查思路和已修过的坑见 `docs/MAINTAINER_GUIDE.md`「扫描性能」节。

改动代码、界面或运行时适配器后至少执行：

```bash
python3 -m compileall -q src/corral tests
env -u TEXTUAL_DISABLE_KITTY_KEY python3 scripts/ci-test.py
```

`scripts/ci-test.py` 会**先跑与 CI 相同的 `ruff check`**（固定 `ruff==0.16.1`），再跑全量单测（另加挂死打栈与已知偶发自动重跑一次）。本机只跑 `unittest discover` 会漏掉 lint——2026-08-07 起连续多个版本就因一处 import 排序在 CI Lint 步全矩阵报红、天天发失败邮件，单测根本没跑到。细则见 `docs/MAINTAINER_GUIDE.md`「CI 工作流」节。**复现 CI 环境时必须 `env -u TEXTUAL_DISABLE_KITTY_KEY`**——开发机 shell 里通常已导出该变量，会掩盖掉真实失败。

**推送 / 发版门禁（防再狂发失败邮件）：** 克隆后先跑一次 `bash scripts/install-git-hooks.sh`（写入 `.git/hooks`，不改 git config）。之后：

- 日常 `git push`：自动只跑 `python3 scripts/ci-test.py --lint-only`（几秒）；不过则推送被拦。
- 提交说明以 `release:` 开头，或推送 `v*` 标签：需要完整检查。**完整套件每个版本只跑一次**：本机刚跑过且产品代码未改时，推送门禁和收尾脚本认戳跳过整套、只再拦 ruff。戳失效（改过 `src/` / `tests/` / `scripts/` 等）才再跑全量。不要指望每次手设 `CORRAL_SKIP_*`。
- `scripts/publish-release.sh` 同样认戳；应急才用 `CORRAL_SKIP_CI_GATE=1` / `CORRAL_SKIP_PUSH_GATE=1`。

全量单测约 560 项、**耗时 10 分钟量级**（含真实 tmux 与 Textual 集成用例），别按"几十秒跑完"预期设超时。**排查「ci-test 跑很久 / 每次都要等很久 / 发版检查跑三遍 / 不要每次都跑这么重 / 是不是卡住了」：** 单次完整检查大约十分钟，不是故障；发版慢是因为同一套曾被连跑最多三遍（发版前、推送门禁、收尾脚本），已改为认戳跳过重复。时间几乎都花在界面自动化和真实终端集成上，中途会刷「某任务执行超过 0.1 秒」一类提示，那是界面框架在抱怨慢，不是挂死。还在往下出新的通过行 = 正常；连续许多分钟没有任何新结果、或跑到约 25 分钟被打出全部线程栈才是挂死（那条已修过，不要和「十分钟量级」搞混）。不要为了「跑快点」去拆掉界面/终端集成或改成只跑改过的那几个文件充当发版门禁。机器负载高时，涉及真实 tmux 回显和 Textual Pilot 等待的用例（`ControlChannelIntegrationTests`、`MainScreenEmbedFlowTests` 等）会因 4s 级等待超时而假失败：**先把失败用例单独重跑一遍确认，再判定是否真回归**，不要直接当成自己改坏了去查。

涉及界面时还要运行一次真实终端冒烟。标题后台生成会调用本机 agent CLI、消耗对应账号额度；只验证界面时，在临时目录把 `claude`、`codex` 指向本机 `true`，放到 `PATH` 最前面，再启动 `python3 -m corral --limit 5`（或已安装的 `corral --limit 5`），确认：

- 底部 Textual `Footer` 显示 `a Advanced`（中文环境下为 `a 高级操作`；`ui/main_screen.py` 的 `MainScreen.BINDINGS`，不再是 curses 手绘的底部帮助行）。
- **按键无响应时先查焦点**：启动时若选中的是别处托管的实时会话，右栏实时格会持有输入焦点，Footer 变成「`Ctrl+\` 返回列表」——此时 `a`/`q` 等会被转发进助手的真实输入行（可能污染正在跑的会话，务必避免盲发鼠标序列）。先按 `Ctrl+\` 把焦点切回侧栏再操作（真机冒烟踩坑：2026-08-17）。
- 高级操作弹窗（`ui/modals.py` 的 `choose_target_runtime`）第一项是导出会话、第二项是复制会话、第三项是重启会话（结束卡住的托管进程后按原会话原地恢复，上下文保留；仅对 corral 正托管且非占位的会话可用，其余置灰），其后动态列出注册表中的运行时。
- 默认选中第一个已安装的其他运行时。
- `Esc` 先关闭弹窗，再退出主界面。
- 选中已结束会话时右栏是完整对话预览（消息之间是角色色的分隔横线；`● 你` / `◆ 运行时` 抬头独占一行并带时间；正文按 Markdown 排版、顶格另起一行且不着色），不再出现「最近提问 / 最近回复」摘要块。

**界面改动后的截图验收（必要步骤，不能只靠单测文字断言）：** Agent / 维护者必须自己进 TUI 出图并肉眼看图，确认布局与文案没有明显回归。标准做法（Textual Pilot → SVG → PNG，与当初 README 截图同一路径）：

```bash
cd cli
pip install cairosvg   # 首次；ImageMagick convert 渲 Rich SVG 常出空白图，不要当主路径
python3 docs/screenshots/capture.py   # → docs/screenshots/list.png
```

然后用读图工具打开 `docs/screenshots/list.png`（以及必要时其它新截图）检查：左栏搜索框与卡片、右栏完整对话、Footer、有无截断错乱、错误文案（如残留「最近提问」、空白右栏、运行时名缺失、标题整行转圈）；图中应有 runtime 真彩（如 Claude `#D97757`），且无 Rich 假 macOS 标题栏/三色点。**若整图灰阶**：先查环境是否带了 `NO_COLOR`——Textual 会启用 Monochrome；`capture.py` 已在创建 App 前清除该变量，不要绕过脚本另跑导出。配色也可用真机 TUI 或 `SessionCard.render_line` 的 segment style 交叉验收。中文若成豆腐块，多半是截图环境缺 CJK 字体——本机（`root@10.10.10.2` / suzhou）需有 `fonts-noto-cjk`（`Noto Sans Mono CJK SC`）；`capture.py` 已按该字体族改写 SVG。**侧边栏会话组名前的水果 emoji 同理**：cairosvg 不做逐字形字体回退，`capture.py` 已把 emoji 单独拆成一个 `<text>` 换成 `Noto Color Emoji`/`Apple Color Emoji` 字体族（与圆点 `●` 换族同一套机制，见该文件 `_EMOJI_FONT`），本机缺彩色 emoji 字体时同样会变方框；suzhou 可 `apt-get download fonts-noto-color-emoji` 解包后放进 `~/.local/share/fonts`（容器 `sudo` 因 `no-new-privileges` 不可用，这条路径不需要 root）。属出图环境问题，不要当成产品回归。README 若仍引用旧「全屏预览」图，界面语义变了必须同步换图与说明。截图使用虚构演示数据，禁止把真实用户会话内容写进仓库。

**改动 `keepalive`、入口层保活接线、`embed`/`ui/embed_pane` 内嵌面板、或 `corral claude`/`corral codex` 直启子命令时，除单测外必须额外跑一次真实 tmux 冒烟**：内嵌面板与界面交互（控制通道、滚轮转发、copy-mode、光标、主题注入、「连接中…」回归；界面层已从 curses 换成 Textual，鼠标拖拽选词这版暂未实现，见 `docs/MAINTAINER_GUIDE.md`「内嵌面板」节）的统一入口是仓库根的端到端脚本——直接跑 `bash selftest.sh`（外层 TUI 跑在独立 tmux socket + 隔离 fake HOME；**但托管侧用的就是真实保活 socket `corral-keepalive`**——除固定的 `corral-claude-aaaa1111/bbbb2222` 外，直启与 cursor 两段还会以随机 ident 创建真实命名的会话，正常退出由 trap 清掉，**脚本中途崩溃则会残留**。收工前对照 `tmux -L corral-keepalive list-sessions` 检查：pane 启动命令指向 `/tmp/corral-selftest.*/fakebin/` 的才是本次残留的假夹具、可以清，其余一律不动），全部断言全绿才算过。用 `python3 -c "from corral import keepalive; from corral.models import LaunchPlan; print(keepalive.wrap_plan(LaunchPlan(('sleep','300'),None),'claude','smoketest'))"` 拿到真实 argv 后执行（加 `-d` 变成后台创建，不实际 attach），确认 `tmux -L corral-keepalive list-sessions` 能看到会话、`keepalive.annotate()` 能靠 pid 匹配上、`keepalive.reap_idle(now=<未来时间戳>)` 能正确回收、正常退出（跑一个立即结束的命令如 `true`）后会话不留残留；测试用的 socket 用完后确认没有残留 `tmux -L corral-keepalive` 进程（`ps aux | grep "[t]mux -L corral-keepalive"` 应为空）。改完配置内容（`keepalive` 里的 `_TMUX_CONFIG` 常量）后，额外跑一次 `pip install --target <临时目录> .` 确认真实安装产物里 `src/corral` 包完整。直启子命令额外验证：把 `claude`/`codex` 指向本机 `true`（或一个会 sleep 的 fake 脚本）放到 `PATH` 最前面，跑 `corral --no-keepalive claude <参数>` 确认参数原样透传且垫上了危险参数、用户已带危险参数时不重复；默认路径（真实终端内跑 `corral claude`）确认进入 TUI 侧边栏模式、新会话包进 `tmux -L corral-keepalive` 并显示在右栏；非真实终端（管道）则确认退回 `tmux -L corral-keepalive` 包装后的 execvp 全屏接管。**本机若已有其他真实保活会话在跑（`tmux -L corral-keepalive list-sessions` 能看到非本次测试创建的 `corral-*`/`sc-*` 会话），冒烟测试一律只操作自己新建的会话名，不得 `kill-session` 或以其他方式影响已存在的会话**——那些通常是该机器上真实在跑的 Agent 会话。

**涉及会话扫描、标题或会话预览（`load_conversation`）时，改完必须至少随机抽查 5 条真实会话验证，不能只靠手写的单测小样例过关。** 优先用真实终端打开预览页肉眼检查内容，或写一次性脚本批量跑 `load_conversation`/`scan_sessions` 扫描本机全部真实会话文件、断言没有异常（如空文本、字面量 `"None"`、角色标错、时间戳缺失或非单调）。本机 Claude/Codex 历史里曾各自藏着单测样例覆盖不到的真实格式坑（`stop_reason` 与文本内容无关、`origin.kind` 区分真人和系统事件、`payload` 字段值可能是 JSON `null` 而不是缺失），这类坑只有跑真实数据才会暴露，见「Claude 扫描」节的具体记录。

**标题生成改动的自测硬要求：完成安装后必须直接运行真实 `corral --generate-titles`，同时记录缓存条目数和待补会话数。** 若命令因已有后台补全进程持锁而立即返回，必须检查该进程及其 5 路生成子进程、持续观察缓存增长，不能把立即返回误判为未执行或完成；补全结束后再扫描确认只剩没有可提炼任务信息的会话，且这类会话不会继续排队。不得只验证 `corral list`、源码函数或单测。

## 本机入口

产品代码在 `src/corral/`（标准 src-layout）。不要再直接跑已删除的根目录 `corral.py`。

**改名后 PATH 里还是 `pickup`、没有 `corral`：** 不是兼容别名。`[project.scripts]` 只注册 `corral`；旧 `~/.local/bin/pickup` 是改名前 pip/pipx 留下的入口，装新包名不会自动替换它。旧脚本在源码搬走后会变成 `No module named 'pickup.bootstrap'`。不要加回 `pickup` console script。在当前 `cli/` 跑下面的 `dev-install.sh`，再视情况 `python3 -m pip uninstall pickup`。细则见 `docs/MAINTAINER_GUIDE.md` 内嵌面板节 2026-08-23 条。

**开发机一次性装好（推荐，彻底避免 pipx 旧副本）：**

```bash
cd cli
bash scripts/dev-install.sh
# 把本仓库 editable 装进「corral 命令实际用的解释器」（含 pipx venv）
corral --version   # 应看到 package_file 落在本仓库 …/cli/src/corral/
corral --limit 5
```

之后改 `src/` 立刻生效，无需反复 `force-reinstall`；**仍须重启**已打开的 TUI。

备选（无 pipx / 只想装到当前 python3）：

```bash
cd cli
python3 -m pip install --user --force-reinstall --no-deps -e .
corral --limit 5
# 等价：python3 -m corral --limit 5
```

**验收必须核对「`corral` 命令实际加载的包」，不能只信系统 `python3 -c "import corral"`。** 本机常见：`~/.local/bin/corral` shebang 指向 **pipx venv**，而 Cursor / 普通 `python3` 可能 import 到仓库源码——单测已绿、敲 `corral` 仍是旧包。核对：

```bash
corral --version                 # 或 corral diagnose → data.package_file / stale_source_warning
command -v corral
# pipx 的入口常是 /bin/sh 包装器，不能只看第一行；以 corral --version 的 python/package_file 为准
```

在仓库目录内启动 TUI 若加载了别处的副本，stderr 会打 `[corral] …改源码不会生效` 告警。期望 `package_file` 落在本仓库 `cli/src/corral/`（editable）或你有意使用的 site-packages。样式自检：`corral diagnose` 的 `runtime_label_style_claude` 应为 `bold #D97757`。

## 领域地图（doc-init）

<!-- 覆盖度复核基线：2026-08-01 · 源码指纹 扫描 140 文件 / Python 83 · Rust 1 / 1 子模块 · 基线版本 0.24.33 -->

| 领域 | 入口锚点 |
|------|---------|
| 终端界面 | src/corral/ui/ · src/corral/activity_board.py · src/corral/cli.py · src/corral/display.py · src/corral/theme.py · src/corral/store.py · src/corral/i18n.py · src/corral/split_layout.py · src/corral/ui_prefs.py |
| 会话关注状态 | src/corral/attention.py · src/corral/attention_signals.py · src/corral/cursor_observer.py · src/corral/store.py · src/corral/ui/ |
| 会话全文搜索 | src/corral/search.py · src/corral/ui/search_modal.py |
| 内嵌实时终端 | src/corral/embed.py · src/corral/ui/embed_pane.py |
| 会话扫描与对话内容 | src/corral/scan/ · src/corral/scan/pi.py · src/corral/transcript.py · src/corral/models.py · src/corral/runtime/ |
| 跨助手接力与启动 | src/corral/runtime/ · src/corral/runtime/pi.py · src/corral/models.py |
| 新助手接入 | src/corral/runtime/ · src/corral/scan/ · src/corral/runtime/pi.py · src/corral/scan/pi.py |
| 性能、派生缓存与原生加速 | src/corral/cache.py · src/corral/cache_cli.py · src/corral/native.py · src/corral/schedprio.py · src/corral/bootstrap.py · rust/lib.rs · Cargo.toml · scripts/benchmark.py |
| 可观测与诊断 | src/corral/observe.py · src/corral/agent_api.py |
| 会话保活 | src/corral/keepalive.py |
| 直启子命令 | src/corral/cli.py · src/corral/projects.py |
| 命令拦截（shim） | src/corral/shim.py · src/corral/bootstrap.py · src/corral/runtime/registry.py |
| 标题补全 | src/corral/titles.py · src/corral/titlegen.py |
| Agent 只读查询 | src/corral/agent_api.py |
| 开源发布与一键安装 | install.sh · .github/workflows/ · scripts/publish-release.sh |
| CI 流水线 | .github/workflows/test.yml · scripts/ci-test.py · .githooks/pre-push · scripts/install-git-hooks.sh |
| 客户端自动更新 | src/corral/updater.py · src/corral/ui/update_toast.py |
| 隐私与本地数据边界 | PRIVACY.md |

## 待补充知识库（doc-init backlog）

（当前无待补充项；会话保活、标题补全、Agent 只读查询、直启、开源发布、客户端自动更新仍以维护指南 / SKILL 为主，需要独立知识库时再登记。）
