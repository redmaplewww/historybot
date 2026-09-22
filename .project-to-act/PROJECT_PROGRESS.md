# 项目进度

> 记录当前执行状态与有效工作节点；普通查看、搜索和无状态变化的命令不写入。

## 当前任务

| 任务 | 状态 | 负责人 | 完成条件 | 证据 |
|---|---|---|---|---|
| 知识图谱方案审查与落地规划 | 已完成 | Codex | 设计文档完成并定义 M1-M4 验收条件 | [docs/knowledge-graph-adoption-review.md](../docs/knowledge-graph-adoption-review.md) |
| AI-first 史料入库 V2 | 后端与图谱物化完成，外部模型评测待执行 | Codex | 阅读、独立复核、草稿门禁、图谱、Wiki/记忆适配器和 API 可运行 | `42 passed`；18 段真实史料图谱回放通过 |
| M1 数据契约与兼容迁移 | 进行中 | Codex | 关系 provenance/time 字段入库，旧库可迁移，测试通过 | `pytest -q`：24 passed |
| 史料入库合理性审查 | 进行中 | Codex | 全库证据/文本/账本审计；失效输出降级；短段迁移路线可安全执行 | `artifacts/ingestion_quality_audit.md`；`73 passed`；SQLite `ok` |
| 上下文归一化与双纪年修复设计 | 已完成 | Codex | 核查当前实现覆盖范围，定义可执行的数据模型、流水线和验收门禁 | `docs/full-normalization-temporal-repair-plan.md` |
| 卷 1 上下文修复灰度 | 工程通过，真实模型阻塞 | Codex | 新语料版本、连续上下文、canonical 实体、双纪年断言、独立复核全部通过真实模型灰度 | `artifacts/contextual_repair_approval_report.md`；manual canary 通过；live 401 |

## 阻塞项

| 阻塞 | 影响 | 解除条件 | 状态 |
|---|---|---|---|
| Anthropic 兼容网关返回 401 | 无法完成真实外部模型端到端评测 | 更新 `ANTHROPIC_AUTH_TOKEN` 或切换可用网关 | 进行中 |
| 旧分段规则丢失短史料段 | 时间边界和上下文不完整，禁止全量 V3 时间修复 | 建立新 passage 语料层，迁移 provenance 后重新抽取/复核 | 进行中 |
| 现库 passage 无法全量原位精确对齐 | 强制插入会移动边界并破坏证据引用 | 对 203 个旧段落建立显式 old→new 映射；无法映射者转人工复核 | 进行中 |

## 下一步

1. 新建无损 passage 版本，保留全部非空短段并保存 old→new 映射。
2. 对新增 5,295 段及 203 个不匹配旧段重新阅读，复核 evidence quote containment。
3. 用修复后的 run-scoped checkpoint 做单卷 V3 重验；账本完整且年份边界正确后再扩大范围。
4. 继续 M2 子图查询，但查询结果必须显式区分候选、争议、待复读和已核验状态。
5. 在任何新时间修复运行前，使现有 1,177 条无 run provenance 的 V3 time link 失效，并禁止查询层读取 invalidated run 结果。

## 进度历史

按时间倒序追加：日期、完成事项、验证证据、遗留问题、下一步。不要覆盖旧记录。

- 2026-09-13：复审 GitHub 项目并完成详细落地设计；实现 relations provenance/time/inference/confidence 字段及旧库兼容迁移；使用 `.venv` 运行测试通过（24 passed）；时间字段尚未从抽取 schema 自动填充，下一步补充时间抽取和 M2 查询接口。
- 2026-09-13：修复事件时间线排序与精度标记；事件入库自动生成 `date_precision/date_sort_key/date_expression`，保留“六月/春正月”等无年份原文，不伪造公历年份；全量测试通过（27 passed）。
- 2026-09-17：实现 AI-first 史料入库 V2；新增阅读运行/步骤、知识草稿、证据锚点、同步任务和本地记忆表；增加 Mem0 与 LLM Wiki Agent 适配器、AI 阅读＋独立复核门禁及 `/api/v2` 存取接口；V2 与旧功能测试通过。
- 2026-09-17：用《三国志·卷01》18 个真实段落完成拆分、证据门禁、Wiki 发布和记忆检索演示；生成 `artifacts/ai_ingestion_live_report.md`。外部 Anthropic 网关真实调用返回 401，失败运行已正确持久化为 `failed`，未产生发布数据。
- 2026-09-17：补通 V2 阅读到证据图谱的物化链路；阅读 schema 新增结构化主张和身份链接，复核后写入 entities/entity_aliases/events/claims/evidence/relations，并拒绝跨段 passage 引用。18 段《三国志》回放生成 31 节点、49 边、18 条主张与 18 条证据，完整性检查均为 0 缺失；全量 42 项测试通过。
- 2026-09-20：完成全库入库合理性审查。确认 1,422 条 evidence 引文、4,184 个 V2 锚点不再命中当前 passage；已把对应 1,422 条主张降级为 disputed/needs_reread，将 1,845 个 step 标为 stale、4,185 个 draft 标为 review_required，并把 79 个 V2 与 3 个 V3 假运行状态改为 interrupted。卷 1 V3 试跑因 474/475 账本不完整且缺短段上下文改为 invalidated。数据库备份：`data/three_kingdoms_rebuilt_v2.pre_quality_remediation.db`。
- 2026-09-20：修复新入库分段逻辑，取消 12 字下限；修复 V3 checkpoint 全局主键冲突，改为 run-scoped ID 并兼容旧记录；完整测试 73 项通过。
- 2026-09-20：完成 65 卷短段迁移只读审计。发现缺失 5,295 段、44,908 字，其中 490 段含年/岁时间词；仅 6 卷可安全原位迁移，203 个现存段无法精确对齐，因此未写入短段，路线转为新语料层重建。
- 2026-09-20：完成上下文/归一化/时间修复覆盖审查。确认 V2 首次阅读和 verifier 均为单段输入；V3 只对已有 claim/event 后置补时，不能补回漏抽；实体 normalization 仅覆盖 19,968/42,181；当前 1,177 条 V3 time link 无 run_id 且来源 run 已 invalidated。形成完整重建方案和验收门禁。
- 2026-09-20：实现独立灰度数据库与上下文修复链路。卷 1 从 501 段重建为 653 个 passage version，精确映射 501 段并恢复 152 个短段；新增连续上下文包、chronology scaffold、canonical 实体和带 run/context provenance 的 temporal assertion。两个已知失败目标在 manual canary 中均解析为初平四年/193 年，范围事件保持 uncertain；完整测试 77 项通过。真实模型请求因网关 401 未通过，因此未启动全量阅读。
