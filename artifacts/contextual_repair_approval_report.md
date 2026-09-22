# 上下文归一化与双纪年修复灰度汇报

## 一、结论

本轮完成了新语料版本、连续上下文阅读、canonical 实体、双纪年时间断言、独立复核和灰度持久化的工程实现。

- 工程链路灰度：`passed_manual`
- 真实外部模型灰度：`failed`
- 主数据库：未修改
- 65 卷全量阅读：未启动
- 当前审批建议：暂不批准全量阅读；先恢复有效模型凭据并重复同一 live canary

真实模型失败原因是网关返回 `401 Invalid API token`。两个目标均在阅读请求阶段失败，没有产生或物化模型结果。失败原始记录保存在 `artifacts/contextual_repair_canary_live_failed.json`。

## 二、实施内容

### 1. 新语料版本

灰度使用独立数据库 `data/contextual_repair_canary.db`，从 Wikisource 重新抓取《三国志·卷01》并使用“保留全部非空段”规则重分段。

| 指标 | 结果 |
|---|---:|
| 旧 passage | 501 |
| 新 passage version | 653 |
| 精确 old→new 映射 | 501 |
| 恢复的短段 | 152 |

主数据库 `data/three_kingdoms_rebuilt_v2.db` 没有写入。

### 2. 连续上下文包

每个目标包含：

- 卷首/传主上下文。
- 从最近绝对年号锚点到目标后的连续原文。
- 后向下一个绝对年号锚点。
- 确定性纪年候选骨架。
- 可复核的 `context_hash`。

“四年春”目标使用 55 个连续 passage；“是岁”目标使用 64 个连续 passage。该实现不再使用旧 V2 的单段阅读方式。

### 3. 双纪年门禁

AI/人工回放负责判断时间承接关系；代码负责执行确定性换算：

`初平元年=190`，因此 `初平四年=190+4-1=193`。

若模型同时填写年号和公元年但二者不一致，`validate_reading` 会拒绝该结果。原文只给相对范围时保留 `uncertain`，不伪造结束年份。

### 4. 新灰度数据表

- `corpus_versions`
- `passage_versions`
- `passage_mappings`
- `context_packets`
- `canary_runs/canary_steps`
- `canonical_entities/entity_mentions`
- `canary_claims/canary_events`
- `temporal_assertions`

时间断言保存 `run_id`、`anchor_passage_id`、年号、年数、公元年、关系类型、`context_hash` 和 verification status，不再依赖无法追踪运行来源的旧 `v3_object_time_links`。

## 三、灰度案例

### 案例 A：四年春，军鄄城

- 上下文链：初平元年 → 二年春 → 三年春 → 四年春。
- 省略主语：依据卷首传主及连续叙事解析为曹操。
- 年号纪年：初平四年。
- 公元纪年：193 年。
- canonical 实体：曹操、鄄城。
- 工程门禁：通过。

### 案例 B：是岁，孙策受袁术使渡江

- “是岁”承接最近的初平四年。
- 年号纪年：初平四年。
- 公元纪年：193 年。
- 明确事件：孙策受袁术派遣渡江。
- “数年间遂有江东”保存为相对范围，没有伪造单一结束年。
- canonical 实体：孙策、袁术、江东。
- 工程门禁：通过。

## 四、灰度落库结果

| 表 | 行数 |
|---|---:|
| corpus_versions | 1 |
| passage_versions | 653 |
| passage_mappings | 501 |
| context_packets | 2 |
| canary_steps | 4 |
| canonical_entities | 5 |
| entity_mentions | 5 |
| canary_claims | 3 |
| canary_events | 3 |
| temporal_assertions | 3 |

三条时间断言：

1. 曹操军鄄城：初平四年 / 193 年，`verified`。
2. 孙策受命渡江：初平四年 / 193 年，`verified`。
3. 孙策数年间取得江东：相对区间，`uncertain`，未伪造绝对结束年。

灰度 SQLite `integrity_check=ok`。

## 五、测试证据

- 完整测试：77 passed。
- 新增上下文修复测试：4 passed。
- Ruff：新增模块、运行脚本和测试全部通过。
- Python compileall：通过。

新增测试覆盖：

- 初平元年、二年、三年、四年和“是岁”的连续纪年状态。
- 前向锚点到目标的连续原文以及后向锚点。
- 目标原文引文 containment。
- 年号与公元年冲突拒绝。

## 六、未通过项与风险

1. 外部模型凭据无效，live reading/verifier 尚未验收。
2. 当前工程通过结果明确标记为 `codex-manual-canary`，不能替代真实模型评测。
3. 主库现有 1,177 条旧 V3 time link 仍需统一失效和查询隔离。
4. 卷 17、40、46 的复杂重分段尚未灰度。
5. 尚未验证长上下文在真实模型上的 token、延迟、内容策略和 JSON 稳定性。

## 七、审批门禁

恢复有效模型凭据后，必须原样重跑两个卷 1 目标，并同时满足：

- 两个目标 live reading 成功。
- 两个独立 verifier 均通过。
- 引文命中率 100%。
- 初平四年/193 年双纪年一致率 100%。
- `expected = completed + failed + skipped`。
- 不复用 manual 输出。

以上通过后，可提交下一次审批，扩大到卷 17、40、46。只有复杂卷灰度也通过，才建议启动 65 卷全量连续阅读。
