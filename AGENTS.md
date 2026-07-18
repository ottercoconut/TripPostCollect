# AGENTS.md

TripPostCollect 是一个用于授权 CTF 靶场的低频图文内容抓取、证据保留和 SQLite 入库项目。开始任务前先读 `docs/README.md`，再按问题类型读取对应文档。

## 仓库地图

- `config/`: 长期抓取任务配置；通用入口是 `crawl_targets.json`，小红书使用独立 `xhs_*.json`。
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
- 通用正式任务从 `scripts/crawl_runner.py` 进入；小红书只从 `scripts/xhs_runner.py` 进入，禁止放回通用 job 或登录流程。
- 小红书配置使用 schema v2，不再有 pool/target `enabled` 开关；显式 `xhs_runner.py` 命令是唯一启动动作，不为每轮修改或恢复配置开关，旧字段直接视为配置错误。
- 通用状态写入 `data/runtime/crawl_execution_states/`；小红书状态写入 `data/runtime/xhs/execution_states/`。进入下一阶段前重新读取状态并确认上一阶段完成，不得手工解冻或补签。
- 正式结构化抓取以 `candidate_hard_limit`、`target_new_posts` 和 `required_fields_profile` 为准；数据库已有记录只算更新，未达到 `valid_new_count` 新增目标不得汇报完成。
- 五个正式结构化搜索平台都由 runner 自动维护 SQLite 抓取记忆：首次从第一页开始，续跑先有限刷新顶部再恢复深层前沿；正常 workflow 不手工传页码、摘要或游标。通用平台保存安全前沿和有效累计摘要；小红书额外保存所有已处理候选 ID，不能宣称去重强度完全相同。
- 通用 `--dry-run` 不访问平台内容，但默认会同步调度表并写 run report、摘要和 execution state；小红书 dry-run 不构造 child 命令且没有 `import_result`，以后四阶段保持 `frozen` 证明未执行。
- B站、微博、小红书、抖音、知乎粉丝量为必需字段；数值、来源和 `followers_observed=true` 必须同时存在，平台不提供时只能由配置声明 `ignored`。
- 路径定义集中在 `trippostcollect.core.paths`；新增代码不要硬编码 `outputs/`、`data/runtime/`、浏览器配置目录等目录。
- 文档和总结默认使用中文。

## 工作方式

- 先用 `rg`、`git status --short` 和定向文件读取确认现状；手工编辑用 `apply_patch`。
- 明确要求运行 Python 前必须 `source .venv/bin/activate`，激活后只用 `python`，不要用系统 `python` / `python3`。
- 优先改现有脚本、配置或数据库结构；不要新增一次性探测脚本。
- 不为已废弃的命令、字段、数据类型或文档保留兼容层；确认当前流程无调用后直接删除，历史需要从 Git 查询。
- 大型抓取产物优先看 `summary.json`、`summary.md`、`run_summary.json`、计数、字段列表、样本和标准输出/标准错误尾部摘要；不要全文展开 JSONL、HTML、过长 JSON 或截图元数据。
- 输出包含 3 个及以上参数、长路径、JSON、环境变量或多个 `--xxx` 选项的命令时，必须用反斜杠 `\` 分行展示；每个参数或逻辑参数组单独一行，避免压缩成长单行。命令很短且参数简单时可以保持单行。
- Python 测试使用 `pytest`，当前测试目录是 `tests/` 和 `apps/admin_api/tests/`；开发依赖通过 `python -m pip install -e '.[dev]'` 安装。
- 模型输出尽量用中文。
- `info_collection_benchmark.py` 只用于通用平台诊断/开发，不能作为正式轮次完成证据，也不接受小红书。

## 上下文和 Token 消耗强约束

以下限制优先级高于一般排查便利性。默认必须遵守；只有问题无法继续定位时，才可以向用户申请临时解限。

- 不对 `outputs/**/logs/*.log`、JSONL、HTML、截图元数据做宽泛 `rg`。
- 查日志只用 `tail -n 40`、`summary.json`、字段列表、计数和样本。
- 对第三方目录 `tools/MediaCrawler` 只查精确文件，不做全树大范围搜索。
- 长跑 server 会话不直接 poll 大缓冲；需要停服务时先判断是否有日志积压，必要时只用进程/端口状态确认。
- 文档只读相关章节，不重复整篇读取。
- 对大文件验证用小脚本输出计数、字段名和短样本，不把原文吐回上下文。

如果上述限制导致问题无法解决，先暂停扩大读取范围，向用户申请临时解限。申请必须说明：

- 要放开的具体限制。
- 需要读取的文件、目录或命令范围。
- 预计输出规模和控制方式。
- 为什么现有摘要、计数、样本不足以继续定位。

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

- 通用正式抓取、数量、停止和成功：读 `docs/formal-crawl-contract.md`、`config/crawl_targets.json`、`scripts/crawl_runner.py`。
- 小红书账号、登录、抓取和失败恢复：先完整执行 `docs/platforms/xhs.md` 的阶段清单，再读
  `config/xhs_*.json` 和对应的 `scripts/xhs_accounts.py`、`scripts/xhs_login.py`、`scripts/xhs_runner.py`；
  不得把小红书放入通用 runner、warmup、benchmark 或中途自动换号。
- 登录、Chrome、阻断恢复：读 `docs/operations-runbook.md`；通用入口是 `scripts/login_warmup.py`，小红书不得使用该入口。
- MediaCrawler 平台实现：读 `docs/platforms/<platform>.md`、`scripts/mediacrawler_crawl.py`，必要时只读对应第三方精确文件。
- 页面证据抓取和导入：读 `docs/platforms/page-evidence.md`、`docs/data-persistence.md`、
  `scripts/ctf_resource_crawl.py`、`scripts/import_ctf_captures.py`。当前正式配置没有页面证据任务；
  直接运行只用于开发或诊断，且其独立 profile 不由 `scripts/login_warmup.py` 验证。以后若新增
  固定 URL 正式任务，必须配置后从 `scripts/crawl_runner.py` 进入。
- 数据库结构、入库、去重：读 `docs/data-persistence.md`、`db/*.sql`、`trippostcollect.db.bootstrap` 和相关导入脚本。
- 字段能力或平台分工：读 `docs/platform-field-coverage.md` 和对应 `docs/platforms/*.md`。

## 完成标准

- Python 脚本改动后先 `source .venv/bin/activate`，再运行 `python -m py_compile <touched files>`。
- Python 行为改动运行 `python -m pytest`；静态检查按需运行 `python -m ruff check <touched files>`。
- JSON 配置改动后先 `source .venv/bin/activate`，再运行 `python -m json.tool <file> >/dev/null`。
- 调度改动至少做 `--dry-run` 试运行或小范围运行验证。
- 正式结构化任务未达目标时必须检查页级状态；没有 `adaptive_search_stopped` 证据不得写成
  `source_exhausted`，应按 `runtime_failed` 继续排查。
- 入库或数据库结构改动使用临时 SQLite 验证，并用 SQL 检查行数和关键字段。
- 最终说明变更文件、验证命令和关键产物路径；不要粘贴大段日志或原始 JSON。
