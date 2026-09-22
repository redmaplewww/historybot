# 历史学 Agent 知识图谱方案审查与落地设计

## 1. 审查结论

总体方向合理，但原方案需要三处收紧：

1. **先增强现有 SQLite 图谱，不立即迁移 Neo4j。** 当前系统已经有实体、事件、主张、证据、冲突和 `nodes/edges` API；直接更换存储会把主要精力放在基础设施迁移，而不是提升历史推理质量。
2. **把“事实”“主张”“证据”分层。** 图上的边不能只表示 LLM 认为成立的关系。边应指向一个或多个主张，主张再挂证据、极性、审查状态和置信度。
3. **时间必须同时支持事件时间和事实有效期。** 历史材料的发表时间、事件发生时间、主张有效区间不是同一个字段，不能用单一 `date` 替代。

参考项目中，LLM Wiki 的增量 Wiki、来源追踪、图谱相关性和社区分析最适合作为产品设计；Graphiti 的事实有效期、episode provenance 和增量更新适合作为数据模型参考；ai-knowledge-graph 与 Knowledge Graph Extractor 适合作为事实边和图交互参考；Neo4j LLM Graph Builder 适合作为完整文档处理链路参考。

## 2. 当前系统基线

- LangGraph 流程：`extract → review → contradictions`。
- 抽取阶段已生成 `mentions / identity_links / events / claims`。
- 审查阶段已生成 `Claim` 和 `EvidenceLink`，并记录 `supports / refutes / partial / unrelated`。
- API 已将实体、事件、主张和证据投影成图数据。
- 前端使用 Cytoscape，已有“人物关系、全部节点、主张关系、事件共现”模式、置信度过滤和节点检查器。

因此首个里程碑应是“可追溯、可按时间查询的证据图谱”，而不是“换图数据库”。

## 3. 目标架构

```text
原始来源/段落
      ↓
实体、别名、事件、时间表达式抽取
      ↓
主张 Claim（subject, predicate, object）
      ↓
证据 EvidenceLink（quote, passage, polarity, source）
      ↓
事实边 Relation（claim 的图投影，可带有效期和版本）
      ↓
图谱查询 + 证据子图检索
      ↓
Agent 生成带引用、冲突和时间范围的回答
```

原则：

- 原始段落不可变；抽取结果可修订但必须保留审计记录。
- 任何图边都必须能够回溯到至少一个 `claim_id` 和一个 `passage_id`。
- 推断关系必须标记 `inference_method`，并与原文明确关系区分。
- 冲突关系保留两边证据，不用覆盖方式“解决”历史争议。

## 4. 推荐数据模型

### 4.1 Entity

保留现有实体表，补充：

```json
{
  "id": "ent_liubei",
  "canonical_name": "刘备",
  "entity_type": "person",
  "aliases": ["玄德"],
  "summary": "...",
  "resolution_status": "resolved",
  "resolution_confidence": 0.94
}
```

### 4.2 Claim

主张是语义和审查的中心对象：

```json
{
  "id": "clm_123",
  "subject": "ent_liubei",
  "predicate": "governed",
  "object": "ent_xuzhou",
  "statement": "刘备曾治理徐州",
  "status": "machine_reviewed",
  "confidence": {"score": 0.82},
  "time": {"from": 196, "to": 200, "precision": "year"},
  "metadata": {"extraction_method": "explicit"}
}
```

### 4.3 EvidenceLink

沿用现有结构，建议补充：

```json
{
  "claim_id": "clm_123",
  "passage_id": "pas_456",
  "polarity": "supports",
  "quote": "...",
  "independent_source": true,
  "confidence": 0.86,
  "locator": {"ordinal": 12}
}
```

### 4.4 Relation / FactVersion

关系表是图查询的投影，不取代主张表：

```json
{
  "id": "rel_123",
  "source": "ent_liubei",
  "target": "ent_xuzhou",
  "predicate": "governed",
  "claim_ids": ["clm_123"],
  "evidence_ids": ["ev_789"],
  "valid_from": 196,
  "valid_to": 200,
  "asserted_at": "2026-09-13T00:00:00Z",
  "superseded_by": null,
  "inference_method": "explicit",
  "confidence": 0.82
}
```

`valid_from/to` 表示事实在历史叙事中的有效区间；`asserted_at` 表示系统何时录入；来源本身的成书或发表时间应放在 Source 元数据中。若时间未知，字段保持 `null`，不能用抽取日期代替。

### 4.5 Episode / Audit

可先用现有 `passage` 和 `audit_log` 实现。若后续需要对话记忆或多轮 ingest，再引入独立 `episodes` 表，将一次导入、一次对话或一次研究任务作为 provenance 根节点。

## 5. 抽取与审查流程

### 阶段 A：结构化抽取

要求模型只返回：实体、事件、时间表达式、原文明确主张和 passage id。输出 schema 必须拒绝无证据主张。

### 阶段 B：实体归一化

先做确定性规则：空白、标点、繁简、常见别名；再做候选召回和 LLM 判定。不要把所有现有节点一次性发给模型，候选集按名称、类型和向量相似度限制在小范围内。

### 阶段 C：证据审查

保留现有 `evidence_review`。审查结果决定 `Claim.status`，但不删除抽取结果；不充分证据进入 `disputed`。

### 阶段 D：时间解析

从事件和主张中抽取 `from/to/precision`。如“建安五年”先转为候选公历或年号区间，并保留原始表达式；转换不确定时标记 `time_confidence`。

### 阶段 E：关系投影

只有存在证据的主张才投影为图边。推断边单独标记，默认不进入“明确史料”视图。

### 阶段 F：冲突检测

继续使用现有 contradiction 节点。冲突应关联两个 claim，而不是直接把两个实体标红；前端点击冲突时展示两组证据和时间范围。

## 6. Agent 查询设计

建议在 `queries.py` 或独立 `graph_queries.py` 暴露以下接口：

```text
graph_search_entities(query, entity_type=None)
graph_expand(entity_id, hops=1, min_confidence=0.5, time_range=None)
graph_find_path(source_id, target_id, max_hops=4)
graph_get_claim_evidence(claim_id)
graph_get_entity_timeline(entity_id, time_range=None)
graph_find_conflicts(entity_id=None)
```

查询流水线：

1. 对问题做实体和时间范围识别。
2. 关键词召回实体、主张和段落。
3. 从种子实体扩展 1～2 跳子图。
4. 按证据充分性、来源可靠性、时间匹配、独立来源数排序。
5. 生成限定 token 的上下文：先放主张和证据，再放图路径，最后放相关段落。
6. 要求模型引用 `claim_id` 或 `passage_id`，回答中明确写出冲突和未知项。

默认不要把整张图发送给 LLM，也不要让图的连接数直接等价于历史重要性。

## 7. 前端落实

当前继续使用 Cytoscape。按优先级实施：

### P0

- 点击边打开证据面板。
- 边颜色表示 polarity；边宽表示 confidence。
- 时间范围筛选。
- 证据状态和来源类型筛选。
- “当前节点 2-hop 子图”按钮。

### P1

- 节点按 entity type 着色，按度数做平方根缩放。
- 冲突关系专用视图。
- 节点悬停时邻居高亮，其余节点降透明度。
- 缓存布局位置，数据刷新时避免节点跳动。

### P2

- Louvain 社区检测。
- 介数中心性、桥接节点和孤立节点洞察。
- 最短路径和研究空白提示。

大图优化：默认只加载种子子图；节点超过约 500 个时禁止一次性渲染全库，改用分页、聚类或按时间/类型分片。具体阈值应通过真实数据测量确定。

## 8. 存储和迁移策略

### 当前阶段：SQLite

适用于单机研究、可复现测试和当前规模。新增字段和索引即可：

```sql
CREATE INDEX IF NOT EXISTS idx_claim_subject ON claims(subject);
CREATE INDEX IF NOT EXISTS idx_claim_object ON claims(object);
CREATE INDEX IF NOT EXISTS idx_relation_valid_time ON relations(valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_evidence_claim ON evidence(claim_id);
```

### 迁移触发条件

只有出现以下情况才考虑 Neo4j/Kuzu/FalkorDB：

- 单库节点或关系达到实际查询瓶颈；
- 多用户并发写入成为需求；
- 需要复杂 Cypher/图算法而 SQLite 查询难以维护；
- 需要独立图数据库生态和可视化工具。

迁移前必须先定义 SQLite 到图数据库的导出 contract，并保留 JSONL 快照作为回滚和测试依据。

## 9. 参考项目的取舍

- **采用 LLM Wiki：** 增量 ingest、来源追踪、图谱扩展检索、社区和图谱洞察。
- **采用 Graphiti：** 时间有效期、事实失效化但保留历史、episode provenance、混合检索思路。
- **采用 ai-knowledge-graph：** SPO 边、实体标准化、证据句、GraphML/Cypher 导出。
- **采用 Knowledge Graph Extractor：** 事实边卡片、流式显示、语义去重、来源和置信度同屏。
- **谨慎采用 Neo4j LLM Graph Builder：** schema 配置、文档接入和图谱问答流程；暂不照搬 Neo4j 基础设施。

## 10. 里程碑与验收标准

### M1：数据契约

- 新增关系投影和时间字段。
- 旧数据库可读取；旧 API 字段保持兼容。
- 每条关系能回溯 claim、evidence、passage。

### M2：查询闭环

- 支持实体搜索、2-hop 扩展、时间线、路径和冲突查询。
- agent 能使用子图和证据生成引用回答。
- 无证据关系不会出现在明确史料视图。

### M3：可视化闭环

- 点击节点、边、主张可以互相跳转。
- 时间、置信度、来源类型和争议状态筛选生效。
- 数据刷新不导致布局明显跳动。

### M4：质量评估

至少准备一组人工标注样本，测量：

- 实体归一化准确率；
- 有证据关系比例；
- 时间解析准确率；
- 子图检索召回率；
- 回答引用覆盖率；
- 冲突漏报率。

## 11. 风险与控制

| 风险 | 控制措施 |
|---|---|
| LLM 臆造关系 | schema 强制 passage_ids；无证据不建边 |
| 同名人物误合并 | 类型、时期、来源和别名联合判定；保留 resolution 状态 |
| 时间表达误解析 | 保存原文表达式、精度和转换置信度 |
| 推断边污染史料视图 | `inference_method` 分层，默认隐藏推断边 |
| 图过密导致不可读 | 子图、聚类、时间和类型筛选 |
| 图谱成为第二套真相 | Claim/Evidence 作为权威层，Relation 只是投影 |
| 迁移成本过高 | 先 SQLite，先建立 JSONL/contract，再评估图数据库 |

## 12. 最终建议

这套方案合理，但实施顺序应从“产品愿景”收敛成“证据图谱基础层 → 时间事实 → 子图检索 → 前端交互 → 社区洞察”。第一批代码应集中在数据 contract、时间字段、关系投影和查询接口；不应一开始引入 Neo4j、向量库或复杂社区算法。

