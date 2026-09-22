# 数据快照

SQLite 数据库快照不进入 Git 普通历史，统一作为 GitHub 私有 Release 资产发布。

原因：GitHub 普通仓库单文件限制为 100 MB；当前主库和历史备份约 850 MB/个。

恢复方式：

```powershell
gh release download data-snapshot-20260922 --repo redmaplewww/historybot --pattern "*.db" --dir data
```

快照说明：

- `three_kingdoms_rebuilt_v2.db`：当前主库，已完成质量审查和失效结果降级。
- `three_kingdoms_rebuilt_v2.pre_quality_remediation.db`：质量降级前备份。
- `three_kingdoms_rebuilt_v2.pre_residue_repair.db`：文本残留修复前备份。
- `three_kingdoms_rebuilt_v2.pre_yl_repair.db`：年号锚点修复前备份。
- `contextual_repair_canary.db`：卷 1 上下文修复灰度库。

Release 资产的 SHA-256 清单保存在仓库根目录 `RELEASE_DATA_SHA256SUMS.txt`。
