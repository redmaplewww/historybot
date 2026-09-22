# 全量归一化与双纪年时间修复方案

## 1. 审查结论

当前修复不能覆盖“基于完整前后文重新阅读、完整归一化、主动判断时间指代，并同时补齐公元纪年与年号纪年”的目标。

现有实现完成了若干基础修补：保留新入库短段、恢复年号锚点、修复中文纪年数字解析、增加向前锚点检索、隔离 V3 checkpoint，并把已知失效 V2 输出降级。但它仍是“先逐段独立抽取，再对已有 claim/event 做后置时间修补”的架构，不能找回首次阅读因缺上下文而遗漏的事实，也不能保证归一化和时间结果在整个数据集内一致。

因此不得在当前数据库上直接启动全量 V3。正确路线是建立新的 passage 版本，在完整语料上按卷/篇连续阅读，然后重建实体归一化和时间断言。

## 2. 当前覆盖矩阵

| 目标 | 当前覆盖 | 结论 |
|---|---|---|
| 保留所有非空短段 | 新代码已覆盖；现库未迁移 | 部分覆盖 |
| 首次阅读使用前后文 | `read_one` 每次只传一个 passage | 未覆盖 |
| 独立复核使用前后文 | verifier 只收到当前 passage 和候选结果 | 未覆盖 |
| 向前查找年号锚点 | 最多向前 200 段，取最近锚点 | 部分覆盖 |
| 锚点到目标的连续原文 | 只带筛选出的“含年份”段，不带全部中间叙事 | 未完整覆盖 |
| 目标之后的反向校验上下文 | 没有 next passage / next anchor | 未覆盖 |
| 找回首次阅读遗漏的 claim/event | V3 只处理已存在目标 | 未覆盖 |
| AI 主动判断“是岁/明年/初/先是”等指代 | prompt 有要求，但上下文不足且无第二次验证 | 部分覆盖 |
| 公元纪年与年号纪年双写 | schema 有 `year/era_name/era_year`，实际覆盖很低 | 部分覆盖 |
| 年号与公元一致性校验 | 仅有基础审计，没有物化前强门禁 | 未完整覆盖 |
| 全数据集实体归一化 | 仅 19,968/42,181 个 V2 实体写入 normalization | 未覆盖 |
| 同名异人、称谓、官职的上下文消歧 | 以 surface/type 和 alias union 为主 | 未覆盖 |
| claim/relation/event 使用 canonical entity ID | 仍主要保存字符串 surface | 未覆盖 |
| 时间结果可按 run 撤销 | checkpoint 有 run scope；time link 无 `run_id` | 未覆盖 |
| invalidated run 的时间结果自动失效 | 运行已 invalidated，但 1,177 条 time link 仍活动 | 未覆盖 |
| 全卷完成度门禁 | run 最终状态未强制要求窗口账本 100% 一致 | 未完整覆盖 |

## 3. 当前数据风险

- 65 卷现库缺失 5,295 段、44,908 字，其中 490 段含“年/岁”。
- 203 个现存 passage 不能与当前源文精确一一对齐，存在一对多、多对一和旧锚点误插入。
- 首次 AI 阅读是逐段并发执行，模型没有稳定的卷内时间状态、人物状态和叙事状态。
- 现有实体归一化只覆盖 V2 实体；legacy 实体、claim 主客体、event participants 和 relation 端点没有统一 canonical ID。
- 40,102 条 claim 中仅 1,277 条带任一绝对时间；同时带公元时间与年号字段的只有 483 条。
- 19,313 条 event 中仅 671 条带任一绝对时间；同时带公元时间与年号字段的只有 159 条。
- 当前 1,177 条 V3 time link 没有 run provenance。唯一完成的卷 1 V3 run 已 invalidated，因此这些结果不能继续作为有效时间事实使用。

## 4. 目标数据模型

### 4.1 Passage 版本

新增而不是覆盖现有 passage：

- `corpus_versions`：语料版本、清理器版本、分段器版本、源抓取时间和校验摘要。
- `passage_versions`：`passage_version_id`、`source_id`、`ordinal`、`section_id`、原文、清理文本、文本哈希、版本状态。
- `passage_mappings`：old→new 映射，支持 `1:1`、`1:N`、`N:1`、无法映射；保存方法、相似度、引文覆盖率和人工复核状态。
- `source_sections`：卷、传、标题、纪年段、裴注等结构边界，防止时间和人物状态跨不相关章节传播。

旧 passage 保留为审计基线；所有新抽取结果必须绑定 `passage_version_id` 和 `corpus_version_id`。

### 4.2 实体归一化

新增权威实体层：

- `canonical_entities`：稳定 ID、规范名、类型、时期、政权/地域范围、状态。
- `entity_mentions`：原文 surface、passage、字符区间、局部上下文。
- `entity_identity_assertions`：mention→canonical 的候选、理由、上下文证据、模型与复核状态。
- `entity_alias_assertions`：字、名、号、谥号、官职称谓、简称、指代；必须保存适用时间和上下文范围。
- `entity_ambiguity_sets`：同名异人、称谓歧义、待人工裁决集合。

归一化必须覆盖全部 42,181 个现有 entity 候选，并重写新的 claim/event/relation 投影，使图边使用 canonical ID，同时保留原始 surface。

`先主`、`後主`、`太祖`、`帝`、官职等不得作为全局唯一别名。它们只能在确定的卷、篇、政权、时间范围和人物主题下解析。

### 4.3 时间断言

时间不再直接由 repair 覆盖 claim/event 主字段，而是先写独立断言：

- `temporal_assertions`
  - `object_id/object_kind`
  - `original_expression`
  - `relation_type`：explicit、inherited、same_year、next_year、previous_year、retrospective、range、unknown
  - `anchor_passage_id`、`context_passage_ids`、`next_anchor_passage_id`
  - `regime_id`、`era_name`、`era_year`
  - `gregorian_from/gregorian_to`
  - `month/day/season`、`precision`
  - `reasoning`、`confidence`
  - `run_id`、`prompt_version`、`corpus_version_id`、`context_hash`
  - `verification_status`、`verifier_run_id`
- `era_registry`
  - 年号、政权、君主、起止公元年、改元边界和同名年号消歧信息。

只有 `verification_status=verified` 且一致性门禁通过的时间断言，才投影到 claim、event、relation 和 timeline。

## 5. 上下文感知阅读架构

### 5.1 禁止逐段无状态并发

同一 source/section 内必须按 ordinal 顺序处理。并发只能发生在不同卷或确定互不连续的 section 之间。

为每个 section 持久化 `reading_state_snapshot`：

- 当前年号、年数和公元年范围。
- 上一明确绝对时间锚点及 passage。
- 最近出现的人物、称谓、地点、政权和事件。
- 未解决的代词、简称、承接词和时间指代。
- 已验证的简短滚动摘要。

### 5.2 每个目标的上下文包

上下文包必须由原文构成，摘要只能辅助：

1. section 标题、传主和结构类型。
2. 目标前至少 8-12 个 passage，按 token budget 动态扩展。
3. 目标后 3-5 个 passage，用于识别后文回指和下一时间锚点。
4. 最近前向绝对锚点和最近后向绝对锚点。
5. 两个锚点之间的全部连续原文，不只筛选含“年”的段落。
6. 已验证 reading state；不得使用 stale 或未验证摘要。
7. 当前 canonical entity 候选和歧义集合。

超长区间采用分层上下文：原文近窗 + 锚点间分块摘要；摘要必须可回溯到 passage 列表。

### 5.3 三遍处理

第一遍：结构和状态扫描。

- 识别标题、传主、显式年号、公元年、月日、季节和叙事边界。
- 构建确定性 chronological scaffold，不抽取知识图谱事实。

第二遍：上下文阅读与抽取。

- AI 阅读目标 passage，但可使用完整上下文解析人物、地点和时间指代。
- evidence quote 只能来自目标 passage；上下文推理另存 `context_basis`。
- 输出遗漏检测字段：人物、行为、关系、事件、时间、地点各维度是否已覆盖。

第三遍：独立复核。

- verifier 获取同一个原文上下文包、候选抽取和 deterministic scaffold。
- 分别复核引文、实体消歧、时间承接、年号转换和遗漏。
- verifier 不通过时不物化，并生成明确的 retry/review 任务。

## 6. AI 与确定性时间引擎的职责边界

AI 负责判断：

- “是岁”“明年”“其后”“初”“先是”“后”“会”等指向哪一个上下文锚点。
- 当前叙事是否仍处于同一年号、同一年或发生回溯。
- 目标人物/政权决定使用哪套同名年号。
- 时间表达作用于哪个 claim/event。

确定性代码负责：

- `era_name + era_year + regime_id` 转公元年。
- 公元年在已确定政权上下文中反查年号。
- 月日格式、区间顺序、改元边界和闰月合法性检查。
- `era_start + era_year - 1 == gregorian_year` 一致性门禁。
- 生成双纪年显示值，不允许 AI 自由填写互相矛盾的两个年份。

无法确定政权或锚点时，保留 `uncertain`，列出候选，不强行补齐。

## 7. 迁移与重跑顺序

### P0：停止错误结果继续传播

1. 将全部 1,177 条现有 V3 time link 标记为 `stale_invalidated_run`。
2. 查询层禁止读取 invalidated/stale temporal assertion。
3. 给 time link 增加 `run_id/corpus_version_id/context_hash`；后续运行可完整撤销。
4. 不再直接覆盖 claim/event 时间字段；改为 verified assertion 投影。

### P1：重建语料

1. 从 Wikisource 原文建立新的 corpus version。
2. 保留全部非空短段，解析 section 和标题边界。
3. 用文本哈希、引文定位、序列对齐建立 old→new mapping。
4. 203 个不匹配段进入一对多/多对一映射和人工复核，不做强制覆盖。

### P2：全量上下文重读

1. 对 65 卷全部 passage 重读，不只处理新增 5,295 段。
2. 旧 V2 输出只作为候选对照，不作为上下文状态。
3. 保存上下文包、state snapshot、输入哈希和完整 ledger。
4. 对漏抽、错指、跨段承接分别统计。

### P3：全量实体归一化

1. 确定性清洗和类型标准化。
2. 基于人物表、别名断言和局部上下文生成候选。
3. AI 仅裁决歧义候选，输出证据和适用范围。
4. 重建 claim/event/relation canonical 投影。

### P4：全量时间补齐

1. deterministic scaffold 先标记所有显式锚点。
2. AI 顺序解析相对时间和省略年号。
3. 代码完成双纪年转换和一致性校验。
4. 独立 verifier 复核上下文指代。
5. verified 后才物化 timeline。

### P5：灰度与全量

1. 卷 1：验证“初平三年→四年春→是岁”的已知失败案例。
2. 卷 17、40、46：验证重分段和多段不匹配最严重的卷。
3. 通过后扩到魏、蜀、吴各 3 卷。
4. 最后运行 65 卷；任何卷未达到门禁则整卷不发布。

## 8. 验收门禁

- 新语料 65 卷无静默丢段；source 清理前后字符覆盖有报告。
- 每个 passage 都有 contextual read 状态：verified、review_required、failed 或 not_applicable，不允许无账本缺口。
- verified claim/evidence 引文命中率 100%。
- 所有 entity mention 都有 canonical、ambiguous 或 rejected 状态；不能无状态。
- claim 主客体、event participant、relation 端点 canonical ID 覆盖率 100%，同时保留 surface。
- 每个 claim/event 都有 temporal 状态：verified、uncertain、not_applicable 或 review_required。
- 有年号的 verified 时间，公元/年号双纪年一致率 100%。
- 所有相对时间断言都保存 anchor passage、完整 context passage 列表和 reasoning。
- temporal verifier 通过率、失败率和人工复核量按卷报告。
- run ledger 的 expected/completed/failed/skipped 总数严格相等。
- invalidated run 的任何结果不得出现在查询、图谱或 timeline 投影中。

## 9. 当前修复可复用部分

以下代码可以保留并纳入新架构：

- `clean_wikitext` 的 YL、语言转换、行为开关和标题清理。
- 保留所有非空短段的新 `split_passages`。
- `ERA_STARTS` 和中文年数解析，升级为带政权范围的 era registry。
- run-scoped V3 checkpoint ID。
- quote containment 和 stale output 审计。
- gateway 单窗口失败隔离。

以下部分需要替换而不是继续补丁叠加：

- `read_one([passage])` 的无上下文逐段阅读。
- 只依赖最近 4 段和筛选后年份段的 temporal window。
- 全局 surface union 式实体归一化。
- V3 直接覆盖 claim/event/relation/timeline 的物化方式。
- 没有 run provenance 的 `v3_object_time_links`。
