# pickup 性能知识库

## 什么时候读

改、评审、优化或排查启动、会话扫描、对话预览、内嵌终端渲染、**侧边栏列表重建与分屏加格**、缓存、原生扩展、安装包或发布流水线时先读本文；**排查「电脑忙时 pickup 卡、自身占用却不高」「同类会话管理 / 内嵌终端 TUI 的性能坑」时也读**（见「系统高负载下的调度优先级」与「同类应用踩坑地图」）。**性能优化动手前先做一轮外部调研**（同类 TUI / 终端工具的公开优化经验），再结合本地计时拆解，不要只靠本地 profile 闭门造车（机主 2026-08-17 纠正；本地计时的做法见「新开分屏（加格）链路」节）。各助手历史语义仍以 `SESSION_SCANNING_KNOWLEDGE_BASE.md` 为准，终端交互语义仍以 `EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md` 为准。

## 系统高负载下的调度优先级（为什么「自己不重却卡」）

用户常见体感：电脑 CPU 已经被浏览器 / IDE 打满时，pickup 占用并不高，界面却开始掉帧、按键迟钝。根因通常不是业务逻辑变慢，而是**调度等级偏低**：

- **macOS**：未标注 QoS 的线程落在 Default（图形 App 主线程才是 User Interactive）。系统忙时会优先给更高档的进程 CPU，CLI TUI 可被饿死。业界同构事故：键盘重映射工具 kanata 在编译打满 CPU 时按键处理被饿 100–275ms，系统误判长按并自动连发；修法是把处理线程抬到 `QOS_CLASS_USER_INTERACTIVE`（[kanata#2040](https://github.com/jtroo/kanata/pull/2040)）。Apple 官方也写明：界面相关工作要用 User Interactive，否则界面会像冻住（[Energy Efficiency Guide](https://developer.apple.com/library/archive/documentation/Performance/Conceptual/power_efficiency_guidelines_osx/PrioritizeWorkAtTheTaskLevel.html)）。
- **Apple Silicon**：QoS 还会影响更偏向性能核还是能效核。Background 档会被钉在能效核；交互档优先性能核。别把「慢」一两个原因混为一谈——单线程算法慢 ≠ 被钉到能效核（见 Eclectic Light 对命令行工具与 QoS 的辨析）。
- **对策**（`schedprio.py`，v0.24.72 起进入 TUI 时生效）：启动时先撤销遗留的 macOS 后台让位标记，再把主线程提到 User Interactive；抓帧 / 控制通道读 / 鼠标发送线程使用 User Initiated。Linux 尽力 `nice(-5)`，Windows 尽力抬到 Above Normal。调用失败一律忽略，不得挡启动。
- **前台会话不得被后台治理误伤**：pickup 正在展示或接收输入的界面及其托管助手属于用户正在等待结果的工作，绝不能被本机性能治理工具标记为后台让位；工具必须识别并拒绝此类目标。排查“列表不慢、但首帧或输入很卡”时，先检查会话及其运行时是否被后台降级，再归因到扫描或 Cursor 重绘。
- **不要**给标题生成守护进程、纯扫描后台也抬到 Interactive——那些可以让路；只保「用户正在看的界面」。
- **优先级反转**：界面线程若同步等更低 QoS 的辅助进程（如未抬档的 tmux 子进程），高负载下仍可能一起卡。macOS 对 Mach IPC 有 QoS override，但对「fork 出去的普通 tmux 客户端」不保证同等提权——因此热路径应走常驻控制通道，并给喂画面的线程也抬档。
- 这解决的是**被别人抢走时间片**，不是替代抓帧节流 / 原生解析等业务侧优化。若空闲时也卡，仍按本文其它节与下方踩坑地图排查。

## 同类应用踩坑地图（会话管理 / 内嵌终端 TUI）

与 pickup 同形态的产品（多会话列表 + 内嵌实时终端 + 常驻 tmux）在公开仓库里反复踩过这些坑。评审新优化或排查「卡 / 烫 / 风暴」时按图索骥；**已在 pickup 落地的用「已做」标出，其余是警戒线**。

| 坑类 | 典型症状 | 业界证据 / 教训 | pickup 现状 |
|------|----------|-----------------|-------------|
| 调度档偏低 | 系统忙时掉帧、自身 CPU 不高 | kanata 抬 QoS；Apple QoS 指南 | **已做** `schedprio`（v0.24.72） |
| 每会话 fork 风暴 | 自身 100%+ CPU，大量短命 `tmux` 子进程 | [agent-deck#1728](https://github.com/asheshgoplani/agent-deck/issues/1728)：~60 会话扫状态时 `capture-pane`/`show-environment` 狂 fork；修复含**否定缓存**、超时、合并刷新 | **已做** 控制通道优先 + 存活证据缓存 + 通道池 LRU；禁止在热路径无 `max_age` 判活 |
| 控制客户端过多 | 启动风暴、tmux 服务端背压、交互抢焦 | agent-deck：多实例 × 全会话常驻 `tmux -C` 会挤爆单线程服务端；改为「焦点 / 最近查看」小 LRU 才挂活管 | **已做** `_MAX_CHANNELS`（须 > `MAX_PANES`）；多开窗口时仍要注意别再开「全会话挂管」 |
| 无截止的周期命令 | 系统卡死后客户端挂死，恢复后越扫越疯 | agent-deck：每个节奏性 `tmux` 调用必须带 deadline，否则卡住的客户端占满 CPU 且无法自愈 | 热路径多有 timeout；**新增周期轮询时必须带超时，禁止裸 `subprocess` 无限等** |
| 刷新积压不合并 | 卡顿结束后突然狂刷 | agent-deck#1728 假设：stall 期间定时器积压，恢复后背靠背跑完 | 选择跟随有节流；**新定时器必须「超时则丢旧 tick」，禁止串行还债** |
| tmux 服务端 livelock | 整个 socket 无响应，要 `kill -9` | [tmux#5024](https://github.com/tmux/tmux/issues/5024)：控制模式高压 + 宽字符/emoji 可卡在重绘；Unicode 重的 pane 更危险 | 少开多余 `-C` 客户端；升级本机 tmux；异常时查是否服务端 100% 而非 pickup |
| Textual / Python GIL | 界面冻、后台其实在算 | Textual 官方：CPU 活必须 `@work(thread=True)`，UI 更新走 `call_from_thread`；后台线程过重仍会抢 GIL | 抓帧已在后台线程；**禁止**在消息处理里同步重解析 / 全表刷新 |
| 把后台活抬到 Interactive | 耗电、挤掉真正交互 | Apple：只有真正交互才用最高档 | 标题守护 / 全量扫描保持默认或更低 |

**排查分诊（先问「空闲也卡还是只忙时卡」）**：

1. **只在电脑忙时卡、pickup 自身占用低** → 先信调度档（本机可看线程 QoS；已装 ≥0.24.72 仍如此则查是否卡在等 tmux / 磁盘）。
2. **空闲也卡或自身 CPU 高** → 查 fork 数（是否狂出 `tmux`）、控制客户端个数、抓帧是否退回外部 fork、Textual 主线程是否被同步活堵住。
3. **连 `tmux ls` 都卡住** → 怀疑 tmux 服务端本身（livelock），别只在 pickup 里加日志。

更细的控制通道协议与「禁止主线程调 tmux」见 `EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md`。

## 性能架构

pickup 的热路径分为四层：

1. 轻量入口只处理版本、缓存维护、只读 Agent 命令和更新命令；只有进入交互界面时才加载 Textual 与完整界面模块。
2. Claude、Codex、Kimi、Cursor 的历史元数据按源文件精确签名保存为本地派生缓存；OpenCode 继续使用自身 SQLite 查询与注册表内存签名。所有运行时仍并行扫描，缓存写入在一次扫描结束后批量提交。
3. 完整对话先查进程内缓存，再查本地派生缓存；只有源文件签名变化才重新解析。TUI 与 Agent 深度查询共用这一份结果。
4. ANSI 屏幕解析进入 Rust 原生扩展。屏幕解析在释放 Python 全局锁后完成，并直接返回合并后的行文本、样式区间和指纹，避免为每个终端格创建 Python 对象。扩展不可用或显式关闭时自动走语义相同的 Python 参考实现。**JSON 解码一律走标准库 `json`，不进原生扩展**——原因见下面「Rust 的适用边界」。

静态对话预览还缓存完整布局结果；滚动只切可见窗口，不再对每一可见行重复排版整篇对话。实时终端继续按行指纹比较，只重建和刷新变化行。

## 切换选中会话时的右栏更新

侧边栏换一个**活跃**会话，右栏要跟着换实时画面。2026-07-26 在 suzhou 用真实托管会话逐段实测（Pilot 驱动真实事件循环，非估算），一次切换端到端约 170–200ms，构成：

| 阶段 | 实测 | 是否卡住主线程 |
|---|---|---|
| 事件派发 + 活跃判定 | ~32ms（峰值 45ms） | 是 |
| 排队等主线程空闲 | ~6ms | — |
| 右栏整排拆掉重建 | ~30ms | — |
| 重建后铺静态回退内容 | ~55ms | — |
| 开控制通道 + resize | ~14ms | 是 |
| 首帧抓取与渲染 | ~30–60ms | — |

同机单次调用基线：`has-session` fork 约 4.8ms；`capture` 走 fork 约 5.3ms、走已建好的控制通道只要 **0.4ms**；首次建通道约 18ms。**结论是开销几乎全在「判活的 fork」和「整排重建」上，不在画面本身。**

已落地的两项（v0.24.16）：

- **存活证据缓存**（`embed.note_alive` / `is_alive(name, max_age=...)`）：抓帧、状态查询、开通道、创建托管成功都算一次「确认它还活着」并打时间戳；界面层判活（`MainScreen._session_is_active` / `_is_session_active`，TTL `_ALIVE_EVIDENCE_TTL`=3s）先读证据，命中就不 fork。右栏在显示的会话每轮抓帧都会刷新证据，所以这条路几乎永远命中。**判定「会话是否已结束」一律不传 `max_age`**（`EmbedPane._capture_loop` 的三次失败确认），缓存只能加速「确认活着」，不能替代宣告死亡。实测主线程阻塞中位 18.2ms → 9.3ms，命中缓存时 0.6ms。
- **选择跟随节流**（`MainScreen._schedule_follow_selection`，窗口 `_FOLLOW_THROTTLE`=120ms）：leading-edge + trailing，单次方向键零额外延迟，连按时窗口内只保留最后一次。**不能改成纯 debounce**——那会给「按一下」也加上固定延迟，单步反而更迟钝。后台重扫和搜索框过滤后的刷新也走这条节流（它们同样会整排重建右栏）。实测积压 5 次高亮：跟随 6 次 → 2 次，主线程累计阻塞 5.7ms → 0.7ms。定时器延迟下限必须 > 0，Textual 的 `Timer` 用间隔做除法，`interval=0` 会在停表时抛 `ZeroDivisionError` 把屏幕卸载流程带崩。回归：`test_rapid_highlights_are_throttled_but_still_settle`（既断言合并，也断言停下来一定收敛到最后一项）、`test_embed.py` 的三条存活证据用例。

v0.24.17 又补了三项，把上面表里「整排重建 + 重铺回退 + 重开通道」那三段基本消掉：

- **格子就地改绑，不再整排重建**（`PaneCell.rebind` / `SplitPaneArea._mount_panes_async`）：新旧格数相同就复用现有格子，只有多出来的才新挂、超出的才卸。`EmbedPane.focus_session` 本来就支持切换会话（提升抓帧代次、拦住旧回调），不需要靠销毁控件来换会话。**`cell_id` 必须沿用旧的**——格子里 `EmbedPane` 的 DOM id 是 compose 时按它生成的。关格回调也必须改成「按此刻绑着的 spec」解析（`PaneCell._close_self`），构造时闭包捕获的那一个在改绑后就过期了，会关错会话。
- **按会话缓存最后一屏**（`embed_pane._screen_cache`，上限曾为 6）：切走时把网格存起来，切回来先摆上去、后台抓帧几毫秒后用新帧覆盖。恢复必须走 `_sync_strips` 而不是直接赋 `_grid`——`render_line` 的实时分支只认 `_strips`，只设网格会渲染成整片空白。会话确认结束时必须 `forget_cached_screen`，否则再选中它会先摆一屏「像还在跑」的旧画面。
- **控制通道池加 LRU 上限**（`embed._MAX_CHANNELS`=8，须严格大于 `MAX_PANES`）：格子不再卸载，也就不再顺手关掉自己的通道；没有上限的话在侧边栏一路翻下去会攒出几十个 `tmux -C attach` 子进程。淘汰按最久未用，正在显示的格子每轮抓帧都会经 `_active_channel` 续期，天然不会被淘汰。

A/B 实测（同一进程内把挂载协程换回旧实现对照，n=6，口径「按下方向键 → 新画面出现在屏上」）：

| | 右栏换好 | 画面就绪 |
|---|---|---|
| 改动前（整排重建） | 24.9ms | **80.2ms** |
| 改动后（第一次看这个会话） | 32.5ms | **37.1ms** |
| 改动后（切回看过的会话） | 17.0ms | **17.3ms** |

「右栏换好」在冷缓存下反而略高，是因为改绑把 `focus_session`（开通道 / resize）搬进了挂载协程内同步做完，旧实现是挂完再 `call_after_refresh` 补——所以只看这一列会误判，以「画面就绪」为准。

### 分组切换丝滑化（接续）

跨**会话组**切换时身份必变，走改绑而非 inplace；若屏缓存未命中，旧逻辑会同步铺 Markdown 对话回退，观感就像 runtime 整窗重载。另外浏览已有组时误走 `set_group` 会抬 `updated_at` 并整表写盘，堵主线程。

已落地：

- **浏览已有组只 `set_focus`**（`layout_controller._show_session_group`）：目标 keys 与 store 里该组成员一致时走 `_persist_split_focus()`；只有组合真的变了（加格/关格/多选开屏等）才 `_persist_split_composition()` → `set_group`。禁止浏览路径抬 `updated_at`。
- **固定格池**（`SplitPaneArea`）：首次挂满 `MAX_PANES` 个 `PaneCell`，多余格 `-spare` 隐藏；跨组 2↔4 只 rebind/显隐，关格 `park()` 回收进池，不 `remove`。`cells()` / `hosted_identity()` / `ordered_session_keys()` 只报绑定中的可见格。可见最左格用 `-leading` 去左边距（闲置格仍占 DOM，不能靠 `:first-child`）。
- **格数改变时按最终尺寸立即重设，且绝不铺旧宽画面**：单格切多格、或多格切单格时，`rebind` 发生的瞬间旧格仍保留旧宽度；读取它来 resize 会先写错尺寸，随后布局完成又按新宽度写一次。反过来只等布局后的 200ms 防抖，虽避开错误 resize，却会把旧的半宽屏缓存拉伸到新单格左侧。分栏区须根据最终格数和间距预先算出每格内容尺寸，立即 resize；这次格数变化同时清空当前/缓存的旧尺寸画面，首帧只接收新尺寸内容。布局随后发来的相同尺寸回报必须去重，不能在 200ms 后再 resize 或冻结抓帧。**只有格数不变的普通会话切换**才允许复用屏缓存并按当前格即时同步，不能为消除跳变而给每次切换都增加等待。
- **屏缓存扩到 `MAX_PANES * 4`（16）**：覆盖约四个最近分组。冷切换默认空白画布等首帧（`focus_session` 不跑 Markdown 回退）；`detail_until_frame=True` 保留旧回退行为给测试/特例。跟随稳定后后台 `prefetch_cached_screen` 预抓当前组缺缓存的托管帧。**预抓必须先 `parse_screen_rows` 再入缓存**：`embed.capture` 返回的是 ANSI 原文，直接塞进 `_screen_cache` 会在恢复时对字符串逐字符 `_row_to_strip`，真机直接 `AttributeError: 'str' object has no attribute 'wide_cont'` 崩掉（v0.24.61）。`_cache_screen` / `_take_cached_screen` 也要拒绝非行网格脏数据。
- **格池已满时同步改绑**（`_schedule_mount`）：无需新建控件时直接 `_apply_pane_bindings`，少一帧旧画面停顿。

回归：`test_browsing_existing_groups_persists_focus_not_composition`、`test_pane_count_change_reuses_pool_without_remount`、`test_pane_count_change_resizes_once_at_final_layout_size`、`test_two_to_one_discards_half_width_screen_before_first_frame`、`test_cold_hosted_switch_skips_markdown_fallback`。

### 新开分屏（加格）链路（2026-08-17 本轮实测与修复，200 卡规模）

用 Pilot 驱动真实事件循环 + 真实 tmux 托管会话分段计时，加一格的主线程构成与修复后数值：

| 环节 | 修复前 | 修复后 | 手段 |
|---|---|---|---|
| 改绑/开通道 `_apply_pane_bindings` | 12~43ms | 12~20ms（真实终端更低：新格通道由后台 `host_session` 预开，主线程是池命中 ~0ms） | 未动；勿把 `open_channel` 搬主线程外（测试同步断言多、真实收益小） |
| 记忆库写 `persist_split_composition` | 8~18ms | ~1ms | `SidebarLayoutDB` 常驻连接（见下） |
| 列表重建（第 2 格：独立卡→两人组） | **全量重建 951~1079ms，主线程冻结** | splice 27~45ms | 区段 splice（见下） |
| 列表重建（首格/新独立卡置顶） | 360~766ms | 13~45ms | 条纹相位锚定改段尾（见下） |
| 端到端（点击→新格首帧） | 43~155ms（第 2 格另计上面 1 秒冻结） | 57~134ms | 合计 |

三条硬约束（写反了都不报错，只是回到秒级卡顿）：

1. **区段 splice**（`session_list._region_splice` + `_splice_region`）：公共前后缀夹出唯一变化区段，区段外行原样保留，只删/插中间；单行插删是特例，「独立卡→会话组（同位置删 1 插 3）」必须命中它而不是退回全量重建。超过 `_MAX_SPLICE_REGION`（当前 8）或整体换血/重排（一行没保留）仍走全量。固定头（＋新建/看板）不在时（clear() 之后）禁止走 splice，只能全量回补。回归：`test_region_splice_matches_single_and_local_region_changes`、`test_rebuild_falls_back_to_full_rebuild_when_session_set_changes`。
2. **条纹相位锚段尾**（`_assign_block_stripes`）：每次类变更（`set_class`）都触发一次 Textual 全量样式重匹配，200 卡全翻 ≈ 0.7 秒。锚段尾后段首插块零翻转；改动条纹相关代码前先想清楚哪些操作会翻转多少块。区段 splice 后必须 `_apply_stripes(rows)`（奇偶可能翻转）。
3. **`SidebarLayoutDB` 常驻连接**：读写都持实例锁，连接缓存出错就丢弃重开（自愈）；禁止改回每次 `connect+PRAGMA+建表+迁移探测+close`（单次写 8~18ms，且 `read_revision` 是每秒轮询路径）。多窗口互斥仍由 `BEGIN IMMEDIATE` 保证。

## 开屏首卡响应（2026-08-17 第二轮修复：快照秒开 + 首铺分片）

用户可感：启动白屏停在「Pick a session or tap a runtime above」约 2 秒。真实拆解（218 卡规模、真机实测）：

| 阶段 | 修复前 | 修复后 | 手段 |
|---|---|---|---|
| 首帧内容 | 空骨架+提示，卡片要等扫描 | 首帧直接带卡（快照秒开） | `SessionStore._save_sidebar_snapshot` / `hydrate_from_snapshot`（SWR） |
| 首次铺表 | 全量一次性挂载 218 卡，主线程冻结 0.8~1.9 秒 | rebuild 返回时只挂首批 40 行（~几十 ms），尾部空闲帧分批补齐 | `_MOUNT_CHUNK` / `_begin_tail_mount` / `_mount_tail_batch` |
| 扫描完成后的收敛 | （旧版即在此刻全量铺表） | 原地更新 5~20ms | 复用已有 in_place/splice 路径 |

机制与硬约束：

1. **快照（stale-while-revalidate）**：扫描完成后把合并后的会话桶与 `_order` 写进 `~/.cache/pickup/sidebar-snapshot.json`（遵循 `PICKUP_CACHE_DIR`，`PICKUP_CACHE=0` 全禁用，原子写）；启动时同步读快照填入 store（~几十 ms，218 卡约 270KB），标记 `hydrated`（≠ `loaded`）。**必须在后台加载线程启动前 hydrate**，否则会被真扫描的合并覆盖。快照只存展示元数据与顺序，不存 hosted/占位等进程内运行时态；运行状态/标题可能滞后一两秒，真扫描经原地更新收敛（实测 10~20ms）。`loaded` 语义不变：空态提示、启动分屏恢复仍等真扫描。
2. **首铺分片**：全量重建同步只挂前 `_MOUNT_CHUNK`（40）行，尾部每 `_TAIL_MOUNT_INTERVAL`（10ms，**必须 > 0**，Textual Timer 间隔做除法）挂一批，批次间可交互。作废机制：`_rebuild_seq` 递增即作废旧尾（rebuild 入口已递增；`clear()` 不走 rebuild，自己手动作废）。分片批必须持 `_rebuild_lock`（与 rebuild 同闸门，防两条消息泵交错），持锁后再验 token。批后幂等重贴分屏标与斑马纹。分片中途身份比对只看已挂前缀，新重建自然走全量再分片，正确性不变。
3. 回归：`SidebarSnapshotTests`（roundtrip/收敛/幂等/损坏降级）、`MainScreenNavigationTests.test_full_rebuild_mounts_first_chunk_and_fills_tail_in_frames`（首批/作废/补齐/条纹一致）。observe `list_rebuild` 新增 `chunked` 字段。

边界（未做，已评估）：敲命令到首帧之间还有 ~0.5s（Python 导入）+ OSC 探测 ≤0.25s，与提示窗口无关；直启子命令路径是同步全扫后进 TUI（另一条流）。Textual 官方 `Reveal` 每 20ms 只挂 1 个（218 卡要 4 秒+），节奏不可用，故自实现按批分片。启动首建 200 卡全量重建的 ~0.6 秒冻结已由本轮分片挂载消除（observe `chunked=True`，首帧只挂首批）。

## 全文搜索索引

`search.ConversationIndex` 是全文搜索弹窗（`Ctrl+F`）的内存索引。它**不自己读历史文件**，一律经 `SessionStore.get_conversation()` 拿正文，因此天然复用进程内 dict 缓存和 SQLite 派生缓存里的对话；新增的磁盘读取量为零。

关键结论：正文体量远小于历史文件体量，别被 JSONL 的大小吓退。本机实测（默认 `limit=50` 共 168 个会话；`limit=200` 共 461 个会话）：

| 指标 | 168 个会话 | 461 个会话 |
|---|---|---|
| 原始历史文件合计 | 725 MB | 1.2 GB |
| 提取出的对话正文合计 | 约 97 万字符 | 约 538 万字符 |
| 建索引（正文已在派生缓存里） | 234 ms | 603 ms |
| 签名全命中的增量刷新 | 0.5 ms | 1.2 ms |
| 查询：`tmux`（窄） | 5 ms | 11 ms |
| 查询：`的` / `a`（最坏，几乎全命中） | 21～30 ms | 29～35 ms |
| 索引常驻内存 | 6.8 MB | 37.8 MB |

由此定下的约定：

- **不引入倒排索引 / FTS5 / 外部搜索库。** 语料量级下朴素子串匹配就是毫秒级，额外索引结构只会增加维护面。SQLite FTS5 的 trigram 分词器对中文尤其不划算——1～2 个字的查询（中文最常见的查询长度）根本索引不到，还得再挂 `LIKE` 兜底。
- **`search()` 必须先判定+排序、再只对要展示的前 `top` 条提取命中行。** 命中行提取（逐行 lower + 定位 + 开窗）是整个查询里最贵的一步，对着几百条命中全做一遍会把界面线程实打实卡住：461 个会话搜单字母实测 305～441 ms，改成只算前 60 条后降到 35 ms。排序键只依赖会话时间、不依赖命中行，所以先排后截不改变前 `top` 条的内容。`SearchOutcome.total` 仍是命中总数，状态行据此如实告诉用户「还有多少条没显示」，不做静默截断。
- **`_clean()` 用 `str.translate` + 懒查表，不要写回逐字符 `unicodedata.category()` 循环。** 建索引原本 90% 的时间花在那个循环上（461 个会话 1289 ms）；查表后整轮建索引降了一半以上。两种写法在 8672 条真实消息上逐条比对过，替换结果完全等价。
- **索引构建必须在后台线程**（`MainScreen._warm_search_index`，`@work(thread=True)`），且**要等首屏画完再开始**（`_schedule_search_index_warm`，延后 `_SEARCH_INDEX_WARM_DELAY`）。后台线程也受 GIL 影响：解析正文期间界面每帧多滞后 4～5 ms（p95 9～14 ms），直接在首屏那一秒开跑实测让首次出卡片慢了 110～165 ms，而首屏目标本来就只有 1 秒。
- **按会话签名增量重建**：签名取扫描结果里的 `path` / `size_bytes` / `file_mtime`，不额外 `stat`（真正读取时 `get_conversation` 自己会校验文件签名）。增量刷新只要 0.5～1.2 ms，所以**每次打开弹窗都要刷一遍**——否则首屏预热之后新产生的会话和新追加的消息永远搜不到（这是最容易漏的一条：索引建好后不再刷新，pickup 开着不动几小时就搜不到当天的新会话）。
- `refresh()` 内部持锁串行，预热与弹窗侧的刷新同时触发也不会把同一批会话解析两遍。
- 搜索结果只带会话键和正文命中，展示用的标题 / 时间 / 运行中状态由调用方从当前 `store` 快照取；索引里不存展示态，避免建索引那一刻的旧标题被钉死。
- **内存**：索引把正文存两份（原始大小写的行 + 小写 blob）。默认规模下 6.8 MB，不值得为省这点内存改成「只存 blob、命中再切行」——那样会丢掉角色和时间戳，命中时还得回头重读对话。另外 `_build_entry` 走 `store.get_conversation`，会把全部会话的对话灌进 `store.conversations`（该 dict 无淘汰），预热后实测净增约 10 MB；会话数量级再上一个台阶时，要先给这个 dict 加淘汰，而不是先动索引。
- **JSON 解析一律用标准库**，不要试图为建索引再引入原生 JSON 加速，原因见下面「Rust 的适用边界」里的实测记录。

## 派生缓存边界

- 默认位置：`~/.cache/pickup/performance-cache.sqlite3`；遵循 `XDG_CACHE_HOME`，也可用 `PICKUP_CACHE_DIR` 改目录。
- 默认上限 256 MiB；可用 `PICKUP_CACHE_MAX_MB` 调整，最小 16 MiB。超过上限时优先淘汰完整对话，元数据保留以保障启动速度。
- 文件签名包含设备、inode、字节数和纳秒修改时间；Codex 额外包含标题索引签名，Cursor 额外包含提示历史和正文数据库签名。任一输入变化都视为未命中。
- 缓存目录权限为当前用户独占，数据库为当前用户读写。内容只来自用户本来可读的本机会话历史，不上传、不进入项目日志。
- 数据库损坏、锁竞争、只读文件系统或原生扩展缺失都必须降级为未命中，不能阻断原始历史读取。
- `PICKUP_CACHE=0` 可完全关闭；`pickup cache status` 查看状态，`pickup cache clear --dry-run` 预览，`pickup cache clear` 幂等清空。

### 暖缓存扫描的两处开销（v0.24.22 修，别改回去）

暖缓存下扫描已经不是解析瓶颈，而是**缓存访问本身的重复开销**。profile 曾显示一次 Codex 扫描里 `posix.mkdir` / `posix.chmod` 各被调用约 950 次、SQLite `execute` 约 950 次——都不是在读历史，是在重复做无用功。

- **目录准备只在新建连接时做。** `PerformanceCache._connect` 原先每次进入都 `mkdir` + `chmod` 一遍父目录，哪怕线程本地连接早就建好；一次 Codex 扫描白做约 1900 次系统调用。现在连接已存在就直接复用（仅 `create=True` 的热路径；`create=False` 的 `status`/`clear` 保持原样，它们还要靠 `path.exists()` 判断库在不在）。
- **一轮扫描内每个运行时的元数据只查一次库。** 扫描要在上千个候选文件里筛出最近几十条（大量候选会被子代理线程、空会话、目录已删等规则滤掉），逐条查库意味着 Codex 一次扫描发起约 950 次独立查询。`begin_scan()` / `end_scan()` 圈出一轮扫描，期间 `get_session` 走按运行时一次性读入的快照。`registry.scan_all` 和 `agent_api._scan_runtimes` 两个并发扫描入口都要成对调用，`end_scan` 必须放在 `finally` 里。

两条硬约束：

1. **快照严格限定在一轮扫描内。** 做成长期缓存会让同一进程里后续扫描看不到本轮新写入的会话。不在扫描期间的调用方（`store` / `titles`）继续走逐条查询，行为不变。
2. **payload 解码必须保持惰性。** 快照装着该运行时的全部条目（Codex 2686 条 / 2.3 MB），本轮只用得到其中一小部分；建快照时就解码等于白做大量无用功，收益会被吃光。只有签名与解析器版本都校验通过才 `json.loads`。

实测（同一进程内把行为还原成改动前做 A/B，n=15，本机 168 个会话）：`scan_all` 暖缓存中位 **251.9 ms → 203.3 ms**（−19%），最快 218.4 ms → 175.8 ms。验收差分：走快照与 `PICKUP_CACHE=0` 现解析，5 个运行时的扫描结果逐字段完全一致。

## 原生扩展与分发

### Rust 的适用边界

**原生加速只用在「大量输入压缩成少量结果」的场景**，目前仅终端画面解析（一屏带 ANSI 转义的文本 → 若干紧凑行元组，实测约 27 倍）。

**不要往原生层加 JSON 解析。** 曾经有过一个 serde_json + PyO3 的 `loads`，实测比标准库 C 实现的 json **慢 2.4～2.5 倍**（400 条 payload / 4.8 MB：标准库 19 ms、原生 46 ms），已于 v0.24.22 连同 `serde_json` 依赖一起移除。根因是产出物本身就是一大棵 Python 对象树：Rust 侧要先解析成中间对象树、再逐节点转成 Python 对象，同一份数据构建两遍，还要先 `str.encode('utf-8')`；标准库那条路是 C 直接建 Python 对象。判断新的加速候选时按这条准则看**产出物形状**，不要按「这活是不是 CPU 密集」下判断。

**不要把扫描内核改写成 Rust。** 2026-07-29 专门评估过一次（本机真实数据：Claude 历史 633 MB、Codex 3.2 GB、2124 个 Codex 会话文件），结论是不划算，三条理由按重要性排：

1. **启动耗时的大头 Rust 够不着。** 实测 546 ms = 加载 pickup 自身 117 ms + 加载 Textual 198 ms + 扫描 232 ms。前两项合计 315 ms（58%）是 Python 与第三方界面库的导入成本，任何 Rust 重写都动不了，收益上限就摆在那。
2. **日常路径不是解析瓶颈。** 暖缓存下扫描卡在缓存访问的重复开销上（见「派生缓存边界」那两条），Rust 帮不上忙，改架构才有用。
3. **扫描器的产出天然是 Python 字典**，正好落在原生加速「输」的那一侧（同上面 JSON 的根因）。理论上可以写一个「给定文件路径直接返回一小段元数据、全程不建 Python 对象树」的提取器绕开这点，但要把 5 种助手的历史格式怪癖（子代理线程识别、`stop_reason` 与正文无关、`origin.kind` 区分真人与系统事件、`payload` 值可能是 JSON null、标题生成噪音会话过滤）在 Rust 里重写一遍并长期与 Python 参考实现做差分维护，而它**只对「第一次启动」有效**——派生缓存已经把这变成一次性成本。

重新评估的条件：首次扫描成为真实用户投诉点，或派生缓存机制被取消。

顺带记一条被否决的微优化：`scan/codex.py` 的 `_read_session_head` 把文件头最多 30 行逐行完整解析（真实行平均 7.4 KB），加子串预筛可省 28%，**但不能做**——该函数的调用方 `_build_session_info` 会对**每一条**头部条目取 `_entry_time()` 来推 `event_time`，预筛掉的行会改变会话时间进而改变列表排序。抽查 300 个真实文件，300 个都会被改变。

- 原生扩展使用稳定的 Python 3.10 ABI，一个平台产物覆盖该平台的 Python 3.10 及以上版本。
- 正式发布必须构建 macOS 通用轮子，以及 glibc/musl 的 Linux x86_64、aarch64 轮子，并附源代码包和校验和。
- 一键安装脚本按操作系统、CPU 架构和 Linux libc 直接选择预编译轮子；找不到匹配产物时才退回源码安装。项目支持范围仍是 macOS 与 Linux，不声明 Windows 支持。
- Homebrew 源码配方构建时必须声明 Maturin 与 Rust 构建依赖，并在隔离环境中生成轮子，不能继续调用旧的纯 Python 安装入口。
- `PICKUP_NATIVE=0` 可强制走 Python 回退，用于差分测试和故障隔离；正常用户不需要设置。

## 测量与验收

仓库的 `scripts/benchmark.py` 只输出计时与数量，不输出真实会话正文。性能改动至少记录：

```bash
python3 scripts/benchmark.py
PICKUP_NATIVE=0 python3 scripts/benchmark.py
python3 -c "import time; from pickup.runtime import default_registry; r

<!-- 该文档整理/压缩于 2026-08-08 -->
