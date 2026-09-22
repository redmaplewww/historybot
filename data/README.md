# 数据快照

SQLite 数据库快照不进入 Git 普通历史，统一作为 GitHub 私有 Release 资产发布。

原因：GitHub 普通仓库单文件限制为 100 MB；当前主库和历史备份约 850 MB/个。

恢复方式：

```powershell
.\scripts\restore_github_snapshots.ps1
```

脚本会下载直接发布的小型数据库，并下载
`historybot_large_db_snapshots_20260922.zip.part-001` 至 `part-012`。随后脚本按名称顺序拼接 ZIP、解压四个约 850 MB 的重建库，并对分片、ZIP 和全部数据库执行 SHA-256 校验。

快照说明：

- `three_kingdoms_rebuilt_v2.db`：当前主库，已完成质量审查和失效结果降级。
- `three_kingdoms_rebuilt_v2.pre_quality_remediation.db`：质量降级前备份。
- `three_kingdoms_rebuilt_v2.pre_residue_repair.db`：文本残留修复前备份。
- `three_kingdoms_rebuilt_v2.pre_yl_repair.db`：年号锚点修复前备份。
- `contextual_repair_canary.db`：卷 1 上下文修复灰度库。

Release 资产、重建 ZIP 和解压后数据库的 SHA-256 清单保存在仓库根目录 `RELEASE_DATA_SHA256SUMS.txt`。
