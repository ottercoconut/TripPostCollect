---
name: sqlite-import-validation
description: 仅在改动或验证 TripPostCollect SQLite 数据库结构、初始化、导入映射、去重或字段持久化时使用。
---

# SQLite 入库验证

## 触发条件

- 改动涉及 `db/*.sql`、`scripts/db_bootstrap.py`、`scripts/mediacrawler_crawl.py` 或 `scripts/import_ctf_captures.py`。
- 用户询问表结构、导入正确性、去重、`published_at`、作者字段、图片或行数。
- 抓取结果必须在 SQLite 中验证。

## 输入材料

- `docs/data-persistence.md`
- `db/source_platforms.sql`
- `db/web_posts.sql`
- `db/ctf_captures.sql`
- `db/crawl_scheduler.sql`
- `scripts/db_bootstrap.py`
- 被测试来源对应的导入脚本

## 工作流程

1. 检查被改字段或表对应的数据库结构和导入器边界。
2. 除非用户明确要求更新默认数据库，否则用临时 SQLite 数据库验证代码。
3. 通过现有入口或带临时数据库的 `crawl_runner.py --sync-only` 执行初始化。
4. 导入一个小而明确的产物，或把一次小范围抓取写入临时数据库。
5. 查询行数、关键标识、时间戳、作者字段、图片行和来源/证据链接；当前应保持 8 个启用平台和 8 个启用任务，不应恢复废弃平台。
6. 如果持久化字段契约变化，更新 `docs/data-persistence.md`；平台能力变化时同步更新 `docs/platform-field-coverage.md`。

## 验证

- 先运行 `source .venv/bin/activate`，之后只用 `python`。
- Python 改动：`python -m py_compile scripts/db_bootstrap.py scripts/mediacrawler_crawl.py scripts/import_ctf_captures.py`
- 数据库结构健全性：SQLite 能打开临时数据库，且必需表存在。
- 导入健全性：代表性 `SELECT` 查询显示预期行，且没有意外的视频或跳过行。
- 配置/数据库结构同步：`crawl_jobs` 只包含当前配置的任务类型。
- 临时数据库或临时抓取产物用完后删除本次创建的 `temp/<task>/`。

## 常见错误

- 本可用临时数据库证明改动，却直接用默认数据库测试。
- 未经用户明确要求就回填旧记录。
- 现有归一化字段或 JSON 载荷已足够时，仍新增平台专用列。
- 导入器逻辑预期替换图片行时，只更新 `web_posts` 而未重建关联的 `web_post_images`。

## 参考文件

- `docs/data-persistence.md`
- `db/*.sql`
- `scripts/db_bootstrap.py`
- `scripts/mediacrawler_crawl.py`
- `scripts/import_ctf_captures.py`

## 脚本

- 现有：`scripts/db_bootstrap.py`
- 现有：`scripts/mediacrawler_crawl.py`
- 现有：`scripts/import_ctf_captures.py`
