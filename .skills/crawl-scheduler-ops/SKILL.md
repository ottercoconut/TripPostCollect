---
name: crawl-scheduler-ops
description: 仅用于 TripPostCollect 抓取调度配置、到期任务选择、行为配置、节流/可见浏览器设置、运行报告和调度失败分类。
---

# 抓取调度操作

## 触发条件

- 用户询问哪些抓取任务会运行、某个任务为何运行或未运行，或如何调度某个平台。
- 改动涉及 `config/crawl_targets.json`、`scripts/crawl_runner.py`、`scripts/failure_classifier.py` 或调度器数据库结构。
- 需要排查 `run_summary.json`、`crawl_attempts` 或 `crawl_run_reports` 问题。

## 输入材料

- `docs/crawl-architecture.md`
- 调整调度、可见/无界面浏览器、节流、冷却或行为配置时读 `docs/anti-automation-behavior.md`
- `config/crawl_targets.json`
- `scripts/crawl_runner.py`
- `scripts/failure_classifier.py`
- `scripts/db_bootstrap.py`
- `db/crawl_scheduler.sql`

## 工作流程

1. 在推理任务前先验证 `config/crawl_targets.json` 语法。
2. 检查目标任务：`job_key`、`site_key`、`job_kind`、启用标记、优先级、调度、参数和行为配置。
3. 通用任务以 `config/crawl_targets.json` 当前启用项为准；不要把已删除的平台重新写入 `source_platforms` 或启用任务。
4. 涉及反自动化敏感改动时，检查 `docs/anti-automation-behavior.md`；调度间隔需符合站点策略，避免使用 `--no-throttle`，正式取证优先使用可见浏览器运行。
5. 验证配置或数据库结构改动时，把配置同步到临时数据库。
6. 执行前用 `--dry-run` 输出验证命令构造和选中的到期任务。
7. 对已执行的运行，检查 `run_summary.json`、`crawl_attempts`、`crawl_run_reports`、产物路径和失败分类。
8. 具体任务的抓取失败交给 MediaCrawler 或页面证据抓取技能处理，不在调度器层面排查所有细节。

## 页面级任务命令

`ctf_resource_crawl` 调度任务必须把 `crawl_targets.json` 的 `target_url` 传给抓取脚本，同时保留 `site_key` 对应的站点策略和 profile。预期命令形态是 `ctf_resource_crawl.py --urls <target_url> --site-label <site_key> --configured-site-urls ...`，不是只传 `--sites <site_key>` 后隐式使用 `web_sites.py` 默认 URL。

## 验证

- 先运行 `source .venv/bin/activate`，之后只用 `python`。
- JSON 配置：`python -m json.tool config/crawl_targets.json >/dev/null`
- Python 改动：`python -m py_compile scripts/crawl_runner.py scripts/failure_classifier.py scripts/db_bootstrap.py`
- 调度器同步：`crawl_jobs` 包含配置中的活跃任务，已停用任务类型被禁用或迁移。
- `--dry-run`：选中的命令符合预期的 `job_kind` 和参数。
- 页面级 `--dry-run`：命令包含 `--urls`、配置中的 `target_url`、`--site-label` 和 `--configured-site-urls`。
- 使用 `temp/` 临时数据库或产物时，提取结论后删除本次创建的 `temp/<task>/`。

## 常见错误

- 直接编辑 SQLite 调度状态，而不是更新 `config/crawl_targets.json` 并同步。
- 不看失败分类和产物路径，就把子抓取器失败当作调度器缺陷。
- 未确认 `--dry-run` 输出前运行大量到期任务。
- 新增任务类型时未同步更新数据库结构和初始化迁移逻辑。
- 未检查反自动化策略就提高调度频率或关闭节流。

## 参考文件

- `docs/crawl-architecture.md`
- `docs/anti-automation-behavior.md`
- `config/crawl_targets.json`
- `scripts/crawl_runner.py`
- `scripts/failure_classifier.py`
- `db/crawl_scheduler.sql`

## 脚本

- 现有：`scripts/crawl_runner.py`
- 现有：`scripts/failure_classifier.py`
