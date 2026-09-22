# 历史学 Agent 交接手册

更新时间：2026-09-22

## 项目定位

这是一个证据优先的中国历史史料入库与知识图谱项目，包含 SQLite 存储、AI 阅读、实体归一化、时间断言、API、前端和 Wikisource 史料处理脚本。

## 当前状态

- 主库：`data/three_kingdoms_rebuilt_v2.db`，通过 SQLite integrity check。
- 全库审查：已完成；当前库仍是候选抽取库，不是最终核验知识库。
- 65 卷短段审计：发现缺失 5,295 段、44,908 字；原位迁移不安全。
- 上下文修复灰度：卷 1 从 501 段重建为 653 个 passage version，恢复 152 个短段。
- 手工灰度：初平四年与“是岁”两个目标通过双纪年门禁。
- 真实模型灰度：因网关返回 `401 Invalid API token` 未通过。
- 全量阅读：禁止启动，必须先恢复模型认证并重跑 live canary。

## 重要文档

- `docs/full-normalization-temporal-repair-plan.md`：完整修复路线。
- `artifacts/contextual_repair_approval_report.md`：最近一次灰度审批报告。
- `artifacts/ingestion_quality_audit.md`：全库入库合理性审查。
- `.project-to-act/`：持久化项目进度、功能、版本和验收记录。

## 运行环境

```powershell
uv sync
Copy-Item .env.example .env
# 手工编辑 .env，填入本机密钥；不要提交 .env
.\.venv\Scripts\python.exe -m pytest -q
```

## 恢复数据库快照

数据库快照在 GitHub 私有 Release 中，不在 Git 普通历史中：

```powershell
.\scripts\restore_github_snapshots.ps1
```

## 灰度测试

```powershell
.\.venv\Scripts\python.exe scripts\run_contextual_repair_canary.py
```

如果模型认证不可用，脚本会记录 live failure；不要把 `--manual-replay` 的结果当作真实模型验收。

## GitHub 发布约定

- 仓库：`redmaplewww/historybot`，私有。
- 源码、API、文档、测试、脚本、报告和待入库材料进入 Git。
- SQLite 快照作为 Release `data-snapshot-20260922` 资产。
- 任何 API token、`.env`、本机凭据不得提交。
