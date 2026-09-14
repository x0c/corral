# TASKBOARD — 多 Agent 并行协作看板

> 规则见 agentsync 全局 docs/AGENT_TASKBOARD_GUIDE.md。只编辑自己的条目；完成后删除。

| 任务 | 状态 | 影响范围 | 开始 | 最近更新 | 备注 |
|---|---|---|---|---|---|
| 局域网探测失效 + 换网无感 | 进行中 | REMOTE_KB/AGENTS 文档；本机 remote 重启+zeroconf；iOS LanDiscovery（见 ios 看板） | 17:41 | 2026-09-13 17:41 | 不碰 send_turn/sessions；CLI 代码已在 207 |
| RPC 服务端耗时进 audit+status（Slice0 分段计时） | 待发布 | cli/src/corral/remote/service.py（record_rpc_timing+handle 计时）、remote/cli.py（status 耗时列）、tests/test_remote_service.py（3 用例）、docs/OBSERVABILITY_KNOWLEDGE_BASE.md（§5/§7） | 20:52 | 2026-09-13 22:25 | 只读观测，不改协议/会话逻辑；不碰版本文件与并行任务未提交改动（除 service.py 纯追加 helper+4 调用点的回执失败归因外，不动 receipt/command_receipts/agent_api/SKILL/版本bump）。验证：remote_service+remote_status 75 用例绿，ruff 绿，真实 handle+status 冒烟 OK |
| 回执失败归因 remote_input_receipt + iOS detail 透出 | 进行中 | cli/src/corral/remote/service.py（纯追加 _receipt_ms/_receipt_detail/_observe_receipt_outcome + 4 调用点，不改 receipt 结构/协议）、ios/Corral/Models/RemoteModels.swift（CommandStatusResponse 加 detail 可选字段+receipt helper，旧包忽略未知字段） | 22:40 | 2026-09-13 22:40 | 范围外文件一律不动：command_receipts/agent_api/SKILL/版本bump/i18n/rename/cli.py/tests 均为他人进行中；CLI service.py 与 iOS RemoteModels 与他人条目无重叠。验证待补：command_receipts+remote_service 单测、ruff、真机重现 rejected 看 events.log |
| 开发机显示名 rename + 手机本地别名 | 进行中 | cli/src/corral/remote/cli.py、i18n.py、tests/test_remote_rename.py、docs/REMOTE_KNOWLEDGE_BASE.md | 22:07 | 2026-09-13 23:33 | 只加 rename 子命令与文案；不碰 service.py/agent_api/版本文件与他人脏改动 |
| Protect phone-triggered Agent execution | 验证中 | keepalive.py, tests/test_keepalive.py, remote/embedded activity paths as needed, maintainer/remote docs | 12:31 | 2026-09-14 12:44 | Local fix verified (52 focused tests, real detached process); remote restarted; original Pi sessions resumed. Release blocked by existing OscProbeFlushTests failure after retry; full suite 1619 tests. Legacy selftest also blocked by interpreter/PYTHONPATH setup; real resumed Pi execution verified. Logs /tmp/corral-cleanup-tests.log |
