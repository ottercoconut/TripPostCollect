# AGENTS.md

TripPostCollect 是一个用于授权 CTF 靶场的低频图文内容抓取、证据保留和 SQLite 入库项目。开始任务前先读 `docs/README.md`，再按问题类型读取对应文档。

## 仓库地图

- `config/`: 长期抓取任务配置，当前入口是 `crawl_targets.json`。
- `db/`: SQLite 表结构，包括平台表、内容表、证据表和调度表。
- `docs/`: 架构、入库、字段覆盖说明；字段能力变化必须同步文档。
- `scripts/`: 调度、MediaCrawler 对接、页面证据抓取、导入和共享策略。
- `tools/MediaCrawler/`: 第三方抓取工具，由项目封装脚本调用，不把其内部命令当作根项目命令。
- `data/`: 默认 SQLite、运行状态、浏览器状态。
- `outputs/`: 抓取暂存产物、日志、摘要；结构化长期数据以 SQLite 为准。
- `temp/`: 临时验证和一次性测试，正式流程不得依赖其中已有文件。

## 全局边界

- 只采集图文内容、作者可见信息、图片 URL/样本和页面证据；视频目标、视频媒体请求和明确视频记录跳过，不作为失败。
- `web_posts` 是用户使用的统一内容主表；`ctf_captures` 是程序和智能代理（Agent）使用的证据/调试底座。
- `published_at` 必须来自平台原始发帖时间，入库保存为 Asia/Shanghai ISO；不要用抓取时间冒充发帖时间。
- 路径定义集中在 `scripts/project_paths.py`；新增代码不要硬编码 `outputs/`、`data/runtime/`、浏览器配置目录等目录。
- 文档和总结默认使用中文。

## 工作方式

- 先用 `rg`、`git status --short` 和定向文件读取确认现状；手工编辑用 `apply_patch`。
- 明确要求运行 Python 前必须 `source .venv/bin/activate`，激活后只用 `python`，不要用系统 `python` / `python3`。
- 优先改现有脚本、配置或数据库结构；不要新增一次性探测脚本。
- 大型抓取产物优先看 `summary.json`、`summary.md`、`run_summary.json`、计数、字段列表、样本和标准输出/标准错误尾部摘要；不要全文展开 JSONL、HTML、过长 JSON 或截图元数据。
- 输出包含 3 个及以上参数、长路径、JSON、环境变量或多个 `--xxx` 选项的命令时，必须用反斜杠 `\` 分行展示；每个参数或逻辑参数组单独一行，避免压缩成长单行。命令很短且参数简单时可以保持单行。
- 根项目未发现 Makefile、CI、测试目录、项目级 package.json 或 pom.xml；不要虚构构建、lint、test 命令。
- 模型输出尽量用中文。

## 常用命令

```bash
source .venv/bin/activate
python scripts/crawl_runner.py --sync-only
```

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --max-jobs 5
```

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --max-jobs 3
```

## 任务路由

- 调度、任务（job）选择、运行报告：读 `docs/crawl-architecture.md`、`config/crawl_targets.json`、`scripts/crawl_runner.py`。
- MediaCrawler 平台抓取：读 `docs/crawl-architecture.md`、`scripts/mediacrawler_crawl.py`、必要时读 `scripts/mediacrawler_login_warmup.py`。
- 页面证据抓取和导入：读 `docs/data-persistence.md`、`scripts/ctf_resource_crawl.py`、`scripts/import_ctf_captures.py`；CTF 链路登录态预热读 `scripts/ctf_login_warmup.py`；豆瓣作者粉丝量补全逻辑见 `docs/crawl-architecture.md` 豆瓣小组节。
- 数据库结构、入库、去重：读 `docs/data-persistence.md`、`db/*.sql`、`scripts/db_bootstrap.py` 和相关导入脚本。
- 字段能力或平台分工：读 `docs/platform-field-coverage.md` 和 `docs/crawl-architecture.md`。

## 完成标准

- Python 脚本改动后先 `source .venv/bin/activate`，再运行 `python -m py_compile <touched files>`。
- JSON 配置改动后先 `source .venv/bin/activate`，再运行 `python -m json.tool <file> >/dev/null`。
- 调度改动至少做 `--dry-run` 试运行或小范围运行验证。
- 入库或数据库结构改动使用临时 SQLite 验证，并用 SQL 检查行数和关键字段。
- 最终说明变更文件、验证命令和关键产物路径；不要粘贴大段日志或原始 JSON。
