<!-- managed:inherited-agents:start -->
<!-- source: /Users/geraltgraham/Codes/pickup/AGENTS.md -->
# pickup

终端会话接力 CLI，支持跨 Claude Code / Codex / OpenCode / Kimi Code / Cursor / Pi 会话恢复与接力。

通用工程规范：[Python 规范](../_standards/python.md)

## 文档导航

> 以下文档在涉及对应领域的开发、评审或排查时先读取。领域知识库与验证细则见组件内说明。

- [cli/AGENTS.md](cli/AGENTS.md)：改、评审或发布 pickup CLI 工具前必读（含领域知识库、截图验收，以及**排查「GitHub 持续发单测失败邮件 / 流水线作业排队十几小时 / macOS 作业挂死」「敲原命令没进托管」「启动 Pi 出现 No project session found with id」「看不到历史 Pi 会话 / 只能看到最近的 Pi」「分屏两格画面一模一样 / 两个会话内容相同」「本机与开发机版本对不上」「助手还在跑、侧栏却显示已结束」**的入口）。**用 pickup 导出的会话数据写周报 / 日报 / 工作总结，或排查「导出内容不够写总结」时，从这里进 `cli/docs/SKILL.md` 的「拿会话数据做总结 / 周报时的边界」节。** Remote：`ssh://git@10.10.10.2:2222/Max/pickup.git`
- [ios/AGENTS.md](ios/AGENTS.md)：改、评审、构建或真机验收手机客户端前必读（含签名推送、钥匙串共享、禁止 resize、模拟器/真机脚本）。界面视觉与两端状态色见 [ios/docs/UI_DESIGN_KNOWLEDGE_BASE.md](ios/docs/UI_DESIGN_KNOWLEDGE_BASE.md)。
- [cli/docs/REMOTE_KNOWLEDGE_BASE.md](cli/docs/REMOTE_KNOWLEDGE_BASE.md)：改、评审或排查手机 ↔ 开发机远程接力协议、配对、推送密文、画面差分、**换网连不上 / 中继开关与默认地址、任意网络可达策略**前必读。个人中继部署拓扑见 agentsync 基础设施知识库 `pickup-relay.caozc.top` 节。

## 组件一览

| 目录 | 技术栈 | 状态 |
|---|---|---|
| `cli/` | Python | 活跃 |
| `ios/` | SwiftUI | 活跃 |
| `relay/` | Go | 活跃（零知识中继 + APNs） |

## 领域地图（doc-init）

<!-- 覆盖度复核基线：2026-08-09 · 含 remote / ios / relay · 基线版本 0.24.88 -->

| 领域 | 入口锚点 |
|------|---------|
| 终端界面 | cli/src/pickup/ui/ · cli/src/pickup/activity_board.py · cli/src/pickup/cli.py · cli/src/pickup/display.py · cli/src/pickup/textutil.py · cli/src/pickup/theme.py · cli/src/pickup/store.py · cli/src/pickup/i18n.py · cli/src/pickup/split_layout.py · cli/src/pickup/ui_prefs.py |
| 会话关注状态 | cli/src/pickup/attention.py · cli/src/pickup/attention_signals.py · cli/src/pickup/cursor_observer.py · cli/src/pickup/store.py · cli/src/pickup/ui/ |
| 会话全文搜索 | cli/src/pickup/search.py · cli/src/pickup/ui/search_modal.py |
| 内嵌实时终端 | cli/src/pickup/embed.py · cli/src/pickup/ui/embed_pane.py |
| 会话扫描与对话内容 | cli/src/pickup/scan/ · cli/src/pickup/scan/pi.py · cli/src/pickup/transcript.py · cli/src/pickup/models.py · cli/src/pickup/runtime/ |
| 跨助手接力与启动 | cli/src/pickup/runtime/ · cli/src/pickup/runtime/pi.py · cli/src/pickup/models.py |
| 新助手接入 | cli/src/pickup/runtime/ · cli/src/pickup/scan/ · cli/src/pickup/runtime/pi.py · cli/src/pickup/scan/pi.py |
| 性能、派生缓存与原生加速 | cli/src/pickup/cache.py · cli/src/pickup/cache_cli.py · cli/src/pickup/native.py · cli/src/pickup/schedprio.py · cli/src/pickup/bootstrap.py · cli/rust/lib.rs · cli/Cargo.toml · cli/scripts/benchmark.py |
| 可观测与诊断 | cli/src/pickup/observe.py · cli/src/pickup/agent_api.py |
| 会话保活 | cli/src/pickup/keepalive.py |
| 直启子命令 | cli/src/pickup/cli.py · cli/src/pickup/projects.py |
| 命令拦截（shim） | cli/src/pickup/shim.py · cli/src/pickup/bootstrap.py · cli/src/pickup/runtime/registry.py |
| 标题补全 | cli/src/pickup/titles.py · cli/src/pickup/titlegen.py |
| Agent 只读查询 | cli/src/pickup/agent_api.py |
| 手机远程接力（开发机侧） | cli/src/pickup/remote/ · cli/docs/REMOTE_KNOWLEDGE_BASE.md · cli/src/pickup/bootstrap.py（任意网络 = 中继默认开；勿长期 `--no-relay`） |
| 手机客户端 | ios/ · ios/AGENTS.md · ios/Pickup/Design/ · ios/docs/UI_DESIGN_KNOWLEDGE_BASE.md（局域网优先、失败回落中继；换网冒烟） |
| 零知识中继与 APNs | relay/ · 个人公网实例见 agentsync 基础设施知识库 `pickup-relay.caozc.top` |
| 开源发布与一键安装 | cli/install.sh · cli/.github/workflows/ · cli/scripts/publish-release.sh |
| CI 流水线 | cli/.github/workflows/test.yml · cli/scripts/ci-test.py · cli/.githooks/pre-push · cli/scripts/install-git-hooks.sh |
| 客户端自动更新 | cli/src/pickup/updater.py · cli/src/pickup/ui/update_toast.py |
| 隐私与本地数据边界 | cli/PRIVACY.md |

## 待补充知识库（doc-init backlog）

（当前无待补充项；开源自建中继看 `relay/README.md`；本机公网中继运维看 agentsync 基础设施知识库 `pickup-relay.caozc.top` 节。）

<!-- managed:inherited-agents:end -->

# pickup 项目规范

## 文档导航

> 以下文档在涉及对应领域的开发、评审或排查时先读取。

- `README.md`：使用、修改、评审或扩展会话扫描、会话关注圆点、Cursor 状态观察、终端界面、标题生成、运行时适配和跨运行时接力
- `docs/TERMINAL_UI_KNOWLEDGE_BASE.md`：开发、评审、优化或排查终端界面、侧边栏会话关注圆点/已读判定、筛选/会话全文搜索弹窗（`Ctrl+F`）/新建会话、对话预览（含默认钉底滚动）、右侧多分屏顶栏、**中国龙横飞彩蛋（`#dragon-chip`、快照合成、CJK 定格画面、动画时长）**、分屏格数上限（`split_layout.MAX_PANES`，改这个数前必读）、分屏组合记忆、高级操作弹窗、Footer 按键、多语言文案、运行中系统/终端深浅色跟随、截图验收；**设计或修改「键盘输入归属谁」相关行为（自动聚焦、鼠标点击语义、回列表出口、输入蒙版、快捷键随焦点裁剪）前必读 §6 焦点契约**；排查 SSH 下 TUI 颜色失真 / 真彩降级时也读
- `docs/EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md`：内嵌实时终端、右栏托管画面（最多四格；调整格数上限时必读，含通道池与最小托管宽度的连带约束）、控制通道池、抓帧与按键转发、焦点边界/结束会话、连接中卡死；排查或修改**内嵌助手深浅色主题识别错误**（外层终端背景色探测与注入）也从这里进；**排查「分屏两格画面一模一样 / 两个会话内容相同」也读**（同一 `keepalive_name` 开了两格内嵌终端）
- `docs/SESSION_SCANNING_KNOWLEDGE_BASE.md`：开发、评审、优化或排查会话扫描、关注状态证据、Cursor 状态观察、对话预览数据、判活、扫描性能和各助手历史格式；**排查「助手还在跑、侧栏却显示已结束 / 点进去变成历史预览」、尤其 OpenCode 带初始提问或 Pi 接力提问被误判成非交互时也读**（`--prompt` 后的说明词不当命令行，见该文 §6）；**排查「看不到历史 Pi 会话 / 只能看到最近的 Pi」也读**（`--session-dir` 隔离目录占满 `limit`、以及 Pi v1 无 `parentId`）；**排查「分屏两格画面一模一样」也读**（`annotate` 把同一 pane 贴给两条会话）
- `docs/PERFORMANCE_KNOWLEDGE_BASE.md`：改、评审、优化或排查启动、扫描、预览、终端渲染、**侧边栏列表重建 / 分屏加格卡顿**、派生缓存、原生加速、性能基准与预编译包；**排查「电脑忙时界面卡、自身占用却不高」、系统高负载调度优先级，或对照同类会话管理 / 内嵌终端 TUI 的踩坑地图时也读**
- `docs/CROSS_RUNTIME_HANDOFF_KNOWLEDGE_BASE.md`：跨助手接力、高级操作、原生恢复、空白新建、启动计划与接力提示词；**改接力说明或排查「刚派生的 OpenCode/Pi 会话被标成已结束」时也读**（提问会进 `--prompt` / 位置参数，不要为了判活去改说明词）
- `docs/NEW_RUNTIME_ONBOARDING_KNOWLEDGE_BASE.md`：新增、修改、评审或排查一种 AI 助手（含 Pi）的扫描、预览、恢复、接力、空白新建、命令托管或标题生成前必读，避免出现半接入状态；**给新助手设计「带初始提问启动」时也读**（先分清交互窗口还是打印模式，提问正文不能当命令行扫）
- `docs/OBSERVABILITY_KNOWLEDGE_BASE.md`：事件日志、诊断、F12 截图观测、界面异常排查
- `docs/MAINTAINER_GUIDE.md`：维护、评审或排查标题生成、会话关注状态与 Cursor 观察器、会话保活、直启、Agent 只读接口、**启动 Pi 每次都打出「Warning: No project session found with id …」（进「Pi 扫描与启动」节；无害，禁止为消警告拆掉 `--session-id`）**、**排查「看不到历史 Pi 会话 / 只能看到最近的 Pi」也进该节**、开源发布与分发渠道（含**排查「发了新版本但用户升不了级 / `brew upgrade` 拉不到新版 / 发布卡在 CI 排队」**、要不要上 PyPI）、**CI 工作流（改 `.github/workflows/` / `scripts/ci-test.py` / 推送门禁与 `install-git-hooks.sh`、排查「GitHub 天天发单测失败邮件 / 作业排队十几小时 / macOS 作业挂死 / 本机漏跑 ruff / 多 Agent 脏树挡发版 / 推 tag 后要用 ls-remote 核对远端」前必读「CI 工作流」节）**、客户端自动更新及上述领域的维护级细节与历史踩坑（含 pipx/安装副本与源码分叉、SSH `COLORTERM` 真彩降级、内嵌 pane 背景色注入与助手深浅色主题的历次真机排查记录）
- `docs/REMOTE_KNOWLEDGE_BASE.md`：改、评审或排查 `pickup remote`、手机配对、推送密文、画面差分、禁止手机 resize、可选依赖 `[remote]`、**换网不可用 / 中继默认与 `--no-relay` 禁区、任意网络可达**前必读；客户端工程见 `../ios/AGENTS.md`；个人中继部署见 agentsync 基础设施知识库 `pickup-relay.caozc.top`
- `docs/SKILL.md`：修改、评审 `agent_api.py` 面向 Agent 的子命令、字段或退出码语义（含 `diagnose`）；这是 Agent 侧唯一的使用文档，改命令行为必须同步这里。**用 `show`/`export` 的会话数据做周报、日报、工作总结、活动统计，或排查「导出的内容不够写总结 / 看不出到底改了什么」时，必读「拿会话数据做总结 / 周报时的边界」节**——那 5 条（对话不含工具调用与改码证据、标题只能当索引、`last_agent` 常为空、user 侧混着系统注入文本、没有成果字段）是不会改的产品边界，得在调用方侧校正
- `PRIVACY.md`：修改、评审或排查历史文件读取、会话关注状态库、Cursor 用户级观察配置、缓存写入、标题生成、跨运行时接力和开源隐私边界
- `CONTRIBUTING.md`：修改开源贡献流程、验证命令、设计边界或 PR 要求

## 架构约束

- `pickup.cli` / `store` / `display` / `theme` 只负责入口、会话展示状态与用户选择，不得直接拼接某个运行时的启动参数。
- **入口分层与包顶层的兼容导出（改错了不报错，只会静默变慢或让老调用方失效）**：真正的命令入口是 `bootstrap.py`（`[project.scripts]` 指向它），它按子命令惰性分发，**只有进交互界面才 import Textual 与扫描器**——往 `bootstrap.py` 顶部加任何重量级 import，或把快速子命令（`--version`、`cache`、Agent 只读查询、`update`）改成经 `cli.py` 走一圈，都不会报错，只会让每次敲命令白付几百毫秒导入成本（实测 Textual 导入约 198ms），细则见 `docs/PERFORMANCE_KNOWLEDGE_BASE.md`「性能架构」。同理，`src/pickup/__init__.py` 必须保持零重依赖：它只有 `importlib`/`os`/`sys`，历史扁平模块时代的符号（如 `RUNTIME_LABEL_STYLES`、`SessionStore`、`_filter_sessions_by_query`）靠 `_SYMBOL_EXPORTS` + `__getattr__` 惰性重导出。**移动或重命名这些符号时必须同步这张映射表**，否则 `pickup.X` 形式的老调用方会在运行期才抛 `AttributeError`；也不要为了省事把它改成顶层 `from … import …`，那会让包顶层重新拖进整棵依赖树。`TEXTUAL_DISABLE_KITTY_KEY` 的 `setdefault` 必须留在包顶层（早于任何 `import textual`），原因与真实事故见 `docs/MAINTAINER_GUIDE.md`「CI 工作流」节。
- **派生缓存只做加速，任何异常都必须降级为「未命中」**（`cache.py`）：数据库损坏、锁竞争、只读文件系统都不得阻断原始历史读取，`PICKUP_CACHE=0` 要能完全绕开。一轮扫描内的元数据快照由 `begin_scan()` / `end_scan()` 圈定，两个并发扫描入口（`runtime/registry.py` 的 `scan_all`、`agent_api.py` 的 `_scan_runtimes`）都必须成对调用且 `end_scan()` 放在 `finally` 里；**快照严禁跨扫描长期持有**（同进程后续扫描会看不到本轮新写入的会话），payload 解码必须保持惰性。这几条写反了都不报错，只会表现成「列表少了会话」或「优化白做」，细则与实测数据见 `docs/PERFORMANCE_KNOWLEDGE_BASE.md`「派生缓存边界」。
- 运行时私有行为必须收敛在 `runtime/` 对应适配器中；新增运行时只实现扫描、对话预览、原生恢复、历史格式提示、接力新会话（读取其他运行时历史）和空白新会话（不关联任何历史，仅指定工作目录）两种启动能力，并在默认注册表注册一次。
- 跨运行时接力统一走“源适配器导出 `Handoff` → 目标适配器生成 `LaunchPlan`”，禁止增加 Claude→Gemini、Codex→Gemini 等两两转换分支。
- 同运行时使用原生恢复；跨运行时必须新建目标会话、让目标 Agent 按需读取原始 JSONL，不能改写或伪造原会话。
- 标题生成是独立服务，不属于任何运行时适配器。生成后端统一走 `titlegen.py` 的 `TitleGenerator` 抽象，覆盖与默认运行时注册表一致的助手（claude / codex / opencode / kimi / cursor）：本机装了哪个就可以用哪个生成标题，首选失败按注册顺序自动切换。`titles.py` 不得直接拼接任何 CLI 命令；`titlegen.py` 与 `runtime/` 互不 import——运行时适配器管「怎么恢复/接力会话」，标题生成器管「怎么无头问一次模型」，两者后端恰好重名但职责不同，不要合并。标题和界面状态使用“运行时 + 会话 ID”作为唯一键，新增运行时不得退回纯会话 ID。新增运行时必须同时在 `titlegen._GENERATORS` 加对应生成器，且若该 CLI 会把生成调用落盘成会话历史，对应扫描器必须加 `titles.PROMPT_MARKER` 前缀过滤。
- 会话预览：选中非进行中会话时，右栏直接展示完整对话（**默认钉在最新消息**，上滚看更早；用户离开底部后列表刷新不得强行钉回）；已托管会话右栏展示内嵌实时终端。**在别的终端窗口里跑、没被 pickup 托管的会话（`live` 且无 `keepalive_name`）拿不到实时画面**——右栏走完整对话那一路并在详情头写明原因，打开它必须先确认（那是对同一份历史另起恢复进程，不是接管），细则见 `docs/EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md` §1。唯一界面是左栏会话列表 + 右栏（可最多四格均分内嵌终端），禁止再加回全屏预览或纯列表第二套入口。右侧顶栏可点选已安装助手在当前项目下加格；分屏会话会形成持久会话组，结束后仍保留，运行成员才参与启动恢复；组名、成员、折叠、置顶与侧栏显隐见 `split_layout.py`（`~/.cache/pickup/sidebar-layout.sqlite3`）。**这份记忆多窗口共享：所有写入必须经 `SidebarLayoutDB` 在事务里重读最新再叠加，界面只持有只读快照，禁止改快照后整份覆盖写**（那正是多开窗口互相抹掉置顶与分组的老缺陷）。细则与 `_detail_stick_bottom` 见 `docs/TERMINAL_UI_KNOWLEDGE_BASE.md` / `docs/MAINTAINER_GUIDE.md`。
- **外部运行会话（2026-08-08 裁定）**：上条会话预览规则中“打开它必须先确认”的旧表述已废止。外部运行会话只能保持静态预览，**不得弹确认框，也不得针对同一份历史另起恢复进程**；等待原窗口结束后才可正常恢复。
- **侧边栏末行间隔、会话组与关注圆点（硬约定）**：凡往左栏加控件（搜索框、新建项、未来任何块），**最后一行必须是间隔空行**，画在该控件自身高度内并算进命中区与选中高亮；禁止用 `margin`、兄弟空隙或 `ListItem` padding 做分隔（点在空隙上不会落到本项）。会话卡例外：固定三行正文、高度 3，不再另加末行空行；标题统一使用基础标题样式，不因运行中整行变绿；**首行整体 bold（与下面两行拉开层级），其中项目名比标题淡一档（`dim`）、标题本身不得 dim**——项目名是定位用的前缀，同亮度会和标题抢视线；淡化只用 `dim` 这类相对语汇，不要写死具体颜色（深浅色主题都要成立），窄栏截断时别把 `dim` 涂进标题；**首行最左是关注圆点**（等待回答黄 > 执行中绿 > 未读新结果红 > 无），独立会话卡圆点后接空格分隔的「项目 标题」（**不带冒号**），**组内子项不写项目名前缀**（项目已在组卡第二行）；**无圆点时不留占位空格**，标题直接顶到最左并吃满整行宽度（截断宽度按有无圆点取 `width - 2` 或 `width`）；第二行运行时靠右、第三行时间靠右。**第三行时间按新鲜度分四档亮度**（半小时内 / 三小时内 / 一天内 / 更早），最新一档与标题同色（着重显示），越旧越暗；档位色一律用 `$foreground` + 透明度经组件样式解析，禁止写死颜色或退回单级 `dim`，也禁止让时间行带上自己的背景色（会盖掉整行的选中/分屏底色）。圆点不得参与排序、筛选或计数。圆点字符 `●` 的 East Asian Width 是 Ambiguous：Rich 按 1 格算，把它放进首行文本流时必须让宽度预算与 Rich 一致，不要按「CJK 字体看起来占 2 格」去补偿；出图时 `docs/screenshots/capture.py` 只给「内容恰为该字形」的独立 `<text>` 换成非 CJK 等宽族来修观感。当前基准：搜索框高 2、新建项高 2、活动看板高 2、会话卡与会话组卡高 3；组卡第一行只保留展开/收起三角、可选置顶标记和水果组名，**不得画关注圆点**；组卡第二行项目名与 `Group …` 同列左对齐；第三行留白（不写时间，避免与成员卡重复）；成员用贴左缘的半角框线 `├─ `/`└─ `（续行同列 `│`，无前导空格、不用 dim；禁止混全角竖线以免三行卡之间断线）且不在顶层重复。分栏时左栏固定宽 39（`ui/main_screen.py` 的 `LIST_PANE_WIDTH`），内层 `#sidebar-sticky` / `#sidebar-scroll` 的垂直/水平 `scrollbar-size` 均为 0（滚动条不占列宽，键盘与滚轮滚动照常）。**筛选框、＋新建和活动看板固定不滚**（`#project-search` 在列表外，`＋ 新建` 与活动看板在 `#sidebar-sticky`）；置顶块、Pinned 线与未置顶 Today / older 都在 `#sidebar-scroll` 里一起滚——置顶只改变排序（钉在列表最上），不冻在视口里，钉再多也不会裁切或挤掉未置顶；指针在固定头上滚轮仍带动会话列表、顶部不动。**改左栏宽度必须同步改 `selftest.sh` 的 IME 光标锚定断言**——那里把面板起点硬编码成第 40 列（`expected_x=$((40 + inner_x))`，即 39 宽 + 1 列空隙），只改宽度会让端到端冒烟直接判失败。**右栏分屏（≥2 格）时，侧边栏给当前会话组整组铺底（Group 行 + 全部成员），激活会话再重一档**；光标停在组卡上时整组贴 `-group-selected`（成员与组卡同档高光），激活成员再叠 `-split-active`。底色标在 `ListItem` 上，组标题 / 组标题且光标在其上 / 激活格 / 激活格且光标在其上四级必须单调递进。置顶用 `p` / `Ctrl+P`：独立会话可单独置顶，会话组只能整体置顶（组内成员改为整组置顶）；`Ctrl+P` 是与 `Ctrl+F` 同级的全局键，右栏实时格持焦时仍可用；已关闭 Textual 命令面板，不要再展示 `^p palette`；未置顶区跟 SessionStore 稳定顺序走（进入后已有项不因 mtime 更新而飘；新建会话仍插最前），只有置顶块固定在最上；**置顶与未置顶都非空时中间插一行居中 `Pinned`/`置顶` 的 `$primary` 蓝横线；未置顶按滚动 24 小时切 today/older 两桶（桶内不重排），两侧都有时插 `Today`/`今天` 线；高 1、disabled、键盘跳过；禁止 Older/其他标签**。细则见 `docs/TERMINAL_UI_KNOWLEDGE_BASE.md` / `docs/MAINTAINER_GUIDE.md`「界面」节。
- `agent_api.py`（`pickup list`/`search`/`show`/`export`/`share`/`context`/`describe`）是只读数据接口，禁止新增任何执行/拉起副作用命令——pickup 只负责把会话数据交出来，怎么用是调用方的事。暴露更多可见性字段（如运行中会话的 `live`/`pid`）不违反这条约束，只要新字段本身来自扫描/只读探测、不触发任何拉起或写操作；真正"接管/下发指令给运行中会话"的能力不属于 pickup，留给调用方基于这些数据自行实现。命令参数与 `pickup describe` 的输出必须共用同一份 `COMMANDS` 定义，不能各写一份导致漂移。新增或修改子命令时同步 `docs/SKILL.md`。
- Agent 接口里 `list`/`search` 的 `--limit` 固定表示每个运行时的扫描深度，`--top` 才表示最终返回条数；`--compact` 必须同时做到紧凑 JSON 和精简默认字段。改这三个参数或 `show --out` 大结果落盘行为时，同步 `pickup describe`、`docs/SKILL.md` 和 `docs/MAINTAINER_GUIDE.md`。
- 会话保活（`keepalive.py`）是运行时无关的启动包装层，只在 `registry` 生成 `LaunchPlan` 之后、`execute_launch` 之前介入，禁止塞进 `runtime/` 某个具体适配器，也禁止让适配器感知 tmux 的存在。改保活匹配/回收逻辑前先读 `docs/MAINTAINER_GUIDE.md`「会话保活」节。`pickup claude`/`pickup codex` 直启子命令默认带 `_DirectLaunch` 进 TUI、经 `embed.host_session` 托管（与界面内「新建会话」同一路径），托管成功后必须立即登记侧边栏占位卡，禁止等待运行时写出首条历史；扫描器随后发现真实历史、占位卡转正时，侧边栏选中态与右栏分屏键必须一起迁移，不能退回「＋ 新建会话」空态；仅非真实终端 / `--no-keepalive` / 内嵌不可用时退回 `keepalive.enabled`/`wrap_plan` + `execute_launch` 旧路径（保活的第三个调用点，与 TUI 的 `_launch()` 复用同一套开关语义）。
- 内嵌面板（`embed.py`）是与 `keepalive.py` 平级的运行时无关层：不 attach，用 `capture-pane` 拿画面、经常驻 `tmux -C attach` 控制通道（`ControlChannel`）送按键与修改类命令（通道死亡自动回退外部 fork），把托管在保活 socket（`pickup-*`/`sc-*` 命名空间）里的会话渲染进 TUI 右半屏。控制通道按 tmux 会话名维护通道池，多分屏可同时存活；`close_channel(name)` 只关指定格，省略 name 时关闭全部。适配器不感知本模块；`ui.main_screen.MainScreen` / `ui.split_pane_area.SplitPaneArea` / `ui.embed_pane.EmbedPane` 是主要调用方。tmux 是软件级硬依赖（TUI 与直启启动时检查，缺失即报错退出；agent_api 只读子命令不受影响）。环境变量新名为 `PICKUP_*`（`PICKUP_KEEPALIVE`、`PICKUP_KEEPALIVE_IDLE_HOURS`、`PICKUP_TITLE_GENERATOR`、`PICKUP_TITLE_MODEL`、`PICKUP_RUNTIME`、`PICKUP_SESSION_ID`），旧名 `SC_*` 一律保留兜底读取/注入，不得删除兼容路径。
- 运行时跳过权限审批的危险启动参数（如 Claude 的 `--dangerously-skip-permissions`、Codex 的 `--dangerously-bypass-approvals-and-sandbox`）必须声明为对应适配器的 `auto_approve_args` 类属性，不得在 `build_resume_plan`/`build_new_plan`/直启透传等多处各写一份字面量字符串；入口层和 `registry.build_passthrough_plan` 只负责按需拼接这个属性，不感知具体参数内容。
- **助手模型与推理强度完全归用户配置所有**：pickup 的恢复、接力、空白新建、直启、命令拦截和后台标题生成都不得内置或注入模型/推理强度，必须继承对应助手自身的全局默认。唯一例外是用户明确给直启传入的参数，或用户明确设置仅用于标题生成的 `PICKUP_TITLE_MODEL`（兼容旧名 `SC_TITLE_MODEL`）；不得为了省额度或“质量更好”私自选模型。
- **「默认跳过全部权限问询」是本项目的既定产品默认，不是待讨论选项**（机主 2026-08-01 明确拍板）。凡是 pickup 拉起运行时的路径——原生恢复、跨运行时接力、空白新建、直启透传，以及未来的命令拦截/shim 入口——都必须自动垫上该运行时的放行参数，让用户拿到的是开箱免打断的体验。新增运行时时，找出并验证它的放行参数属于接入工作的必做项，不是可选增强；找不到就在维护指南里如实记录能力差距，而不是默默留空。放行参数在某些运行时里只属于部分子命令、或对位置敏感（OpenCode 的 `--auto` 两者都占），这类规则写进适配器的 `compose_passthrough_argv`，不要塞进注册表。**禁止把它改成默认关闭、需显式开启，也不要再以「不安全」为由向机主重复征询确认**——理由是当前各家模型自身的谨慎度已足以覆盖日常风险，机主已知悉并接受。唯一允许不加的情形是运行时自身硬性拒绝该参数（如 Claude 在 root/sudo 下带 `--dangerously-skip-permissions` 会直接退出；OpenCode 的 `stats`/`export`/`auth` 等子命令不认 `--auto`），这类情形按"加了就起不来"的事实判断，与安全权衡无关。**运行时旧版本不支持放行参数不属于此列**：按"该升级那个助手"处理，不为旧版保留降级分支（机主 2026-08-04 拍板）。

## 发版要求

**功能/修复改完后必须发布新版本**（补丁位递增），不要只提交代码就结束。同步 bump `pyproject.toml` / `Cargo.toml` / `Cargo.lock` / `src/pickup/__init__.py`，提交 `release: vX.Y.Z …`，打 annotated tag，推送 `github` 与 `origin`，**再跑 `bash scripts/publish-release.sh`**（建 Release、本机构建并上传安装包、更新 Homebrew 配方，一步到位；脚本自带收尾核对输出）。纯文档/规则整理且无产品行为变化时可不发版；有疑义时默认发版。

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
from pickup.runtime import default_registry
r = default_registry()
t = time.perf_counter()
r.scan_all(50)
print(f'{(time.perf_counter()-t)*1000:.0f}ms')
"
```

`test_session_scanning.py` 的 `StartupLatencyTests` 会在有真实会话数据时对同一调用做 <1s 断言（`python3 -m unittest -v` 已包含），但真实计时仍要单独跑一次确认，不能只信任测试里的一次采样。**不达标不再是提交阻断条件（硬性红线已放宽），但必须如实汇报实测耗时**；根因排查思路和已修过的坑见 `docs/MAINTAINER_GUIDE.md`「扫描性能」节。

改动代码、界面或运行时适配器后至少执行：

```bash
python3 -m compileall -q src/pickup tests
env -u TEXTUAL_DISABLE_KITTY_KEY python3 scripts/ci-test.py
```

`scripts/ci-test.py` 会**先跑与 CI 相同的 `ruff check`**（固定 `ruff==0.16.1`），再跑全量单测（另加挂死打栈与已知偶发自动重跑一次）。本机只跑 `unittest discover` 会漏掉 lint——2026-08-07 起连续多个版本就因一处 import 排序在 CI Lint 步全矩阵报红、天天发失败邮件，单测根本没跑到。细则见 `docs/MAINTAINER_GUIDE.md`「CI 工作流」节。**复现 CI 环境时必须 `env -u TEXTUAL_DISABLE_KITTY_KEY`**——开发机 shell 里通常已导出该变量，会掩盖掉真实失败。

**推送 / 发版门禁（防再狂发失败邮件）：** 克隆后先跑一次 `bash scripts/install-git-hooks.sh`（写入 `.git/hooks`，不改 git config）。之后：

- 日常 `git push`：自动只跑 `python3 scripts/ci-test.py --lint-only`（几秒）；不过则推送被拦。
- 提交说明以 `release:` 开头，或推送 `v*` 标签：自动跑**完整** `ci-test.py`（与线上同源）；不过则推送被拦。
- `scripts/publish-release.sh` 开头再强制跑一遍完整检查（防 `--no-verify` 绕过）；应急才用 `PICKUP_SKIP_CI_GATE=1` / `PICKUP_SKIP_PUSH_GATE=1`。

全量单测约 560 项、**耗时 10 分钟量级**（含真实 tmux 与 Textual 集成用例），别按"几十秒跑完"预期设超时。机器负载高时，涉及真实 tmux 回显和 Textual Pilot 等待的用例（`ControlChannelIntegrationTests`、`MainScreenEmbedFlowTests` 等）会因 4s 级等待超时而假失败：**先把失败用例单独重跑一遍确认，再判定是否真回归**，不要直接当成自己改坏了去查。

涉及界面时还要运行一次真实终端冒烟。标题后台生成会调用本机 agent CLI、消耗对应账号额度；只验证界面时，在临时目录把 `claude`、`codex` 指向本机 `true`，放到 `PATH` 最前面，再启动 `python3 -m pickup --limit 5`（或已安装的 `pickup --limit 5`），确认：

- 底部 Textual `Footer` 显示 `a Advanced`（中文环境下为 `a 高级操作`；`ui/main_screen.py` 的 `MainScreen.BINDINGS`，不再是 curses 手绘的底部帮助行）。
- **按键无响应时先查焦点**：启动时若选中的是别处托管的实时会话，右栏实时格会持有输入焦点，Footer 变成「`Ctrl+\` 返回列表」——此时 `a`/`q` 等会被转发进助手的真实输入行（可能污染正在跑的会话，务必避免盲发鼠标序列）。先按 `Ctrl+\` 把焦点切回侧栏再操作（真机冒烟踩坑：2026-08-17）。
- 高级操作弹窗（`ui/modals.py` 的 `choose_target_runtime`）第一项是导出会话、第二项是复制会话、第三项是重启会话（结束卡住的托管进程后按原会话原地恢复，上下文保留；仅对 pickup 正托管且非占位的会话可用，其余置灰），其后动态列出注册表中的运行时。
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

**改动 `keepalive`、入口层保活接线、`embed`/`ui/embed_pane` 内嵌面板、或 `pickup claude`/`pickup codex` 直启子命令时，除单测外必须额外跑一次真实 tmux 冒烟**：内嵌面板与界面交互（控制通道、滚轮转发、copy-mode、光标、主题注入、「连接中…」回归；界面层已从 curses 换成 Textual，鼠标拖拽选词这版暂未实现，见 `docs/MAINTAINER_GUIDE.md`「内嵌面板」节）的统一入口是仓库根的端到端脚本——直接跑 `bash selftest.sh`（外层 TUI 跑在独立 tmux socket + 隔离 fake HOME；**但托管侧用的就是真实保活 socket `pickup-keepalive`**——除固定的 `pickup-claude-aaaa1111/bbbb2222` 外，直启与 cursor 两段还会以随机 ident 创建真实命名的会话，正常退出由 trap 清掉，**脚本中途崩溃则会残留**。收工前对照 `tmux -L pickup-keepalive list-sessions` 检查：pane 启动命令指向 `/tmp/pickup-selftest.*/fakebin/` 的才是本次残留的假夹具、可以清，其余一律不动），全部断言全绿才算过。用 `python3 -c "from pickup import keepalive; from pickup.models import LaunchPlan; print(keepalive.wrap_plan(LaunchPlan(('sleep','300'),None),'claude','smoketest'))"` 拿到真实 argv 后执行（加 `-d` 变成后台创建，不实际 attach），确认 `tmux -L pickup-keepalive list-sessions` 能看到会话、`keepalive.annotate()` 能靠 pid 匹配上、`keepalive.reap_idle(now=<未来时间戳>)` 能正确回收、正常退出（跑一个立即结束的命令如 `true`）后会话不留残留；测试用的 socket 用完后确认没有残留 `tmux -L pickup-keepalive` 进程（`ps aux | grep "[t]mux -L pickup-keepalive"` 应为空）。改完配置内容（`keepalive` 里的 `_TMUX_CONFIG` 常量）后，额外跑一次 `pip install --target <临时目录> .` 确认真实安装产物里 `src/pickup` 包完整。直启子命令额外验证：把 `claude`/`codex` 指向本机 `true`（或一个会 sleep 的 fake 脚本）放到 `PATH` 最前面，跑 `pickup --no-keepalive claude <参数>` 确认参数原样透传且垫上了危险参数、用户已带危险参数时不重复；默认路径（真实终端内跑 `pickup claude`）确认进入 TUI 侧边栏模式、新会话包进 `tmux -L pickup-keepalive` 并显示在右栏；非真实终端（管道）则确认退回 `tmux -L pickup-keepalive` 包装后的 execvp 全屏接管。**本机若已有其他真实保活会话在跑（`tmux -L pickup-keepalive list-sessions` 能看到非本次测试创建的 `pickup-*`/`sc-*` 会话），冒烟测试一律只操作自己新建的会话名，不得 `kill-session` 或以其他方式影响已存在的会话**——那些通常是该机器上真实在跑的 Agent 会话。

**涉及会话扫描、标题或会话预览（`load_conversation`）时，改完必须至少随机抽查 5 条真实会话验证，不能只靠手写的单测小样例过关。** 优先用真实终端打开预览页肉眼检查内容，或写一次性脚本批量跑 `load_conversation`/`scan_sessions` 扫描本机全部真实会话文件、断言没有异常（如空文本、字面量 `"None"`、角色标错、时间戳缺失或非单调）。本机 Claude/Codex 历史里曾各自藏着单测样例覆盖不到的真实格式坑（`stop_reason` 与文本内容无关、`origin.kind` 区分真人和系统事件、`payload` 字段值可能是 JSON `null` 而不是缺失），这类坑只有跑真实数据才会暴露，见「Claude 扫描」节的具体记录。

**标题生成改动的自测硬要求：完成安装后必须直接运行真实 `pickup --generate-titles`，同时记录缓存条目数和待补会话数。** 若命令因已有后台补全进程持锁而立即返回，必须检查该进程及其 5 路生成子进程、持续观察缓存增长，不能把立即返回误判为未执行或完成；补全结束后再扫描确认只剩没有可提炼任务信息的会话，且这类会话不会继续排队。不得只验证 `pickup list`、源码函数或单测。

## 本机入口

产品代码在 `src/pickup/`（标准 src-layout）。不要再直接跑已删除的根目录 `pickup.py`。

**开发机一次性装好（推荐，彻底避免 pipx 旧副本）：**

```bash
cd cli
bash scripts/dev-install.sh
# 把本仓库 editable 装进「pickup 命令实际用的解释器」（含 pipx venv）
pickup --version   # 应看到 package_file 落在本仓库 …/cli/src/pickup/
pickup --limit 5
```

之后改 `src/` 立刻生效，无需反复 `force-reinstall`；**仍须重启**已打开的 TUI。

备选（无 pipx / 只想装到当前 python3）：

```bash
cd cli
python3 -m pip install --user --force-reinstall --no-deps -e .
pickup --limit 5
# 等价：python3 -m pickup --limit 5
```

**验收必须核对「`pickup` 命令实际加载的包」，不能只信系统 `python3 -c "import pickup"`。** 本机常见：`~/.local/bin/pickup` shebang 指向 **pipx venv**，而 Cursor / 普通 `python3` 可能 import 到仓库源码——单测已绿、敲 `pickup` 仍是旧包。核对：

```bash
pickup --version                 # 或 pickup diagnose → data.package_file / stale_source_warning
command -v pickup
head -1 "$(command -v pickup)"   # 若 #!.../pipx/venvs/pickup/bin/python → 用该解释器验
```

在仓库目录内启动 TUI 若加载了别处的副本，stderr 会打 `[pickup] …改源码不会生效` 告警。期望 `package_file` 落在本仓库 `cli/src/pickup/`（editable）或你有意使用的 site-packages。样式自检：`pickup diagnose` 的 `runtime_label_style_claude` 应为 `bold #D97757`。

## 领域地图（doc-init）

<!-- 覆盖度复核基线：2026-08-01 · 源码指纹 扫描 140 文件 / Python 83 · Rust 1 / 1 子模块 · 基线版本 0.24.33 -->

| 领域 | 入口锚点 |
|------|---------|
| 终端界面 | src/pickup/ui/ · src/pickup/activity_board.py · src/pickup/cli.py · src/pickup/display.py · src/pickup/theme.py · src/pickup/store.py · src/pickup/i18n.py · src/pickup/split_layout.py · src/pickup/ui_prefs.py |
| 会话关注状态 | src/pickup/attention.py · src/pickup/attention_signals.py · src/pickup/cursor_observer.py · src/pickup/store.py · src/pickup/ui/ |
| 会话全文搜索 | src/pickup/search.py · src/pickup/ui/search_modal.py |
| 内嵌实时终端 | src/pickup/embed.py · src/pickup/ui/embed_pane.py |
| 会话扫描与对话内容 | src/pickup/scan/ · src/pickup/scan/pi.py · src/pickup/transcript.py · src/pickup/models.py · src/pickup/runtime/ |
| 跨助手接力与启动 | src/pickup/runtime/ · src/pickup/runtime/pi.py · src/pickup/models.py |
| 新助手接入 | src/pickup/runtime/ · src/pickup/scan/ · src/pickup/runtime/pi.py · src/pickup/scan/pi.py |
| 性能、派生缓存与原生加速 | src/pickup/cache.py · src/pickup/cache_cli.py · src/pickup/native.py · src/pickup/schedprio.py · src/pickup/bootstrap.py · rust/lib.rs · Cargo.toml · scripts/benchmark.py |
| 可观测与诊断 | src/pickup/observe.py · src/pickup/agent_api.py |
| 会话保活 | src/pickup/keepalive.py |
| 直启子命令 | src/pickup/cli.py · src/pickup/projects.py |
| 命令拦截（shim） | src/pickup/shim.py · src/pickup/bootstrap.py · src/pickup/runtime/registry.py |
| 标题补全 | src/pickup/titles.py · src/pickup/titlegen.py |
| Agent 只读查询 | src/pickup/agent_api.py |
| 开源发布与一键安装 | install.sh · .github/workflows/ · scripts/publish-release.sh |
| CI 流水线 | .github/workflows/test.yml · scripts/ci-test.py · .githooks/pre-push · scripts/install-git-hooks.sh |
| 客户端自动更新 | src/pickup/updater.py · src/pickup/ui/update_toast.py |
| 隐私与本地数据边界 | PRIVACY.md |

## 待补充知识库（doc-init backlog）

（当前无待补充项；会话保活、标题补全、Agent 只读查询、直启、开源发布、客户端自动更新仍以维护指南 / SKILL 为主，需要独立知识库时再登记。）
