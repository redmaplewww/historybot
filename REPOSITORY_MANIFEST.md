# GitHub 归档清单

仓库：`redmaplewww/historybot`（私有）

## Git 内容

- `src/`：核心 Python 包、SQLite 存储、AI 入库、归一化、时间修复和 Web API。
- `frontend/`：前端源码和可部署构建产物；依赖目录不提交。
- `scripts/`：入库、审计、修复、灰度、快照恢复和运行工具。
- `tests/`：完整回归测试与上下文灰度测试。
- `docs/`：知识图谱方案和完整归一化/双纪年修复方案。
- `HANDOFF.md`：当前状态、恢复方式和下一步审批门禁。
- `artifacts/`：审查报告、灰度报告、待复核中间结果。
- `data/`：待入库材料、OCR 结果、日志、JSON 报告和小型测试数据。
- `data/external_storiap_snapshot/`：外部 Storiap 工作树快照；原始临时目录保留在本机但不作为嵌套 gitlink 提交。
- `.project-to-act/`：项目进度、功能、版本和验收记录。

## Release 内容

数据库快照作为私有 Release 资产发布。小型数据库直接上传；四个约 850 MB 的重建库压缩后拆成 12 个约 50 MB 的分片，以避免大单文件上传链路中断：

- `three_kingdoms_rebuilt_v2.db`
- `three_kingdoms_rebuilt_v2.pre_quality_remediation.db`
- `three_kingdoms_rebuilt_v2.pre_residue_repair.db`
- `three_kingdoms_rebuilt_v2.pre_yl_repair.db`
- `contextual_repair_canary.db`
- 其他 `data/*.db` 快照

大库分片名为 `historybot_large_db_snapshots_20260922.zip.part-001` 至 `part-012`。运行 `scripts/restore_github_snapshots.ps1` 可自动下载、拼接、解压和校验。

资产 SHA-256 见 `RELEASE_DATA_SHA256SUMS.txt`。

## 不提交内容

- `.venv/`、`node_modules/`、Python 缓存和编辑器缓存。
- `.env` 和任何真实 API key/token。
- SQLite WAL/SHM 临时文件。
