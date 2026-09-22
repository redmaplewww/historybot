# 项目功能

> 功能范围与状态的唯一清单。功能变化后同步进度；未验证的功能不得标记为已完成。

## 状态定义

- 候选：尚未批准进入范围
- 已规划：已确认但未开始
- 进行中：正在实现
- 已阻塞：等待外部条件
- 已完成：完成条件已满足且有证据
- 已取消：明确退出范围并保留原因

## 功能清单

| 功能 | 优先级 | 状态 | 依赖 | 完成条件 | 证据 |
|---|---|---|---|---|---|
| 证据图谱数据契约 | P0 | 已完成 | 现有 SQLite schema | 每条关系可回溯 claim/evidence/passage，旧 API 兼容 | 18 段真实史料回放；`42 passed` |
| 时间事实与版本 | P0 | 已阻塞 | 数据契约、完整 passage 上下文 | 支持 valid_from/to、原始时间表达式和未知时间，年界继承可验证 | relation columns 已入库；V3 单卷试跑因短段缺失与账本不完整已 invalidated |
| 子图检索工具 | P0 | 已规划 | 数据契约 | 支持实体搜索、2-hop、路径、时间线、冲突 | docs/knowledge-graph-adoption-review.md |
| 图谱证据交互 | P1 | 已规划 | 子图 API | 点击边查看证据，支持时间/来源/置信度筛选 | docs/knowledge-graph-adoption-review.md |
| AI-first 史料入库与外部记忆适配 | P0 | 进行中 | SQLite、ModelGateway、可版本化 passage | AI 阅读、独立复核、证据图谱、Wiki 发布、记忆存取和历史记录可验证；失效 checkpoint 不得复用 | `tests/test_ai_ingestion_v2.py`；失效缓存回归测试；`73 passed` |
| 入库质量审计与失效降级 | P0 | 已完成 | 当前数据库 | 检查引用命中、状态覆盖、重复、时间、运行账本，并保留可复核报告 | `artifacts/ingestion_quality_audit.json` / `.md`；失效行均已标记 stale/review_required/disputed |
| 无损短段分段 | P0 | 进行中 | Wikisource 原文、passage 版本映射 | 新入库不丢非空短段；旧库可迁移且不破坏 provenance | 代码与测试完成；65 卷原位迁移审计仅 6 卷安全，转入新语料层方案 |
| 上下文感知连续阅读 | P0 | 进行中 | 新 passage 版本、section 边界、reading state | 同卷/同篇按序阅读；每段使用前后原文、双向时间锚点和已验证状态；可补回遗漏事实 | 卷 1 manual canary 通过；live 模型 401 待验收 |
| 全量 canonical 实体归一化 | P0 | 进行中 | 上下文阅读、实体候选库 | 全部 mention 有 canonical/ambiguous/rejected 状态；claim/event/relation 使用 canonical ID | 灰度表与 5 个 canonical entity 已验证；全量未运行 |
| 双纪年时间断言与独立复核 | P0 | 进行中 | era registry、上下文阅读、temporal assertion schema | AI 判断时间指代；代码完成公元/年号转换；独立 verifier 通过后物化 | 3 条灰度 assertion 落库；双纪年冲突门禁通过；live verifier 401 |

## 功能变更历史

按时间倒序追加：日期、功能、变化、原因、影响、确认来源。

- 2026-09-17：V2 阅读结果增加 claims/identity_links，并在独立复核后物化为现有证据图谱；外部模型与 Mem0 的生产评测状态不变。
- 2026-09-20：新增全库质量审计、失效输出降级、run-scoped V3 checkpoint 和短段无损分段。现库原位迁移未通过安全门禁，因此不标记完成。
- 2026-09-20：审查确认首次阅读/复核没有前后文，V3 只补已有对象时间且不能找回漏抽；新增上下文连续阅读、全量 canonical 归一化和双纪年时间断言三项 P0 功能计划。
- 2026-09-20：完成卷 1 工程灰度实现。新增 corpus/passage version、old→new mapping、连续 context packet、canonical entity 和 temporal assertion；manual canary 通过两个已知失败案例，真实模型仍因 401 阻塞。
