# TripPostCollect 文档入口

TripPostCollect 用于授权 CTF 靶场中的低频图文抓取、证据保留和 SQLite 入库。
正式运行尽量由脚本和状态机完成，不依赖 Agent 临场解释数量、成功或重试语义。

## 首先阅读

根据任务只读取对应文档：

| 任务 | 权威文档 |
|---|---|
| 正式抓取、数量和成功语义 | [正式抓取执行契约](formal-crawl-contract.md) |
| 运行、登录、Chrome 和故障处理 | [正式抓取运行手册](operations-runbook.md) |
| 调度器、执行器和数据流 | [抓取架构](crawl-architecture.md) |
| SQLite、字段映射和入库 | [数据持久化](data-persistence.md) |
| 当前平台字段能力 | [平台字段覆盖](platform-field-coverage.md) |
| 管理端开发 | [管理客户端开发](admin-client-development.md) |
| 小红书账号、登录、正式抓取和恢复 | [小红书正式抓取 Workflow](platforms/xhs.md) |

平台细节位于 `docs/platforms/`。

## 正式入口

B站、微博、抖音和知乎的正式结构化抓取，在执行前统一验证并按需刷新登录态：

```bash
source .venv/bin/activate
python scripts/login_warmup.py --targets all
```

该脚本只处理持久登录态，不抓取内容、不导入数据库。已有登录态有效时直接通过；失效时
等待人工登录，并在关闭、重开同一 profile 后再次验证。`--targets all` 只展开为上述四个平台；
不包含小红书，也不验证页面证据执行器使用的独立浏览器 profile。

查看任务计划：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --max-jobs 5
```

该命令不访问平台内容，但不是文件系统/SQLite 只读操作：默认会同步 `crawl_jobs`，并写本轮运行
摘要、run report 和 execution state。未指定 `--job-key` 时只计划已启用且到期的任务，因此同步
任务数可能大于本轮选中任务数；以 dry-run 摘要的 `jobs_selected` 和每任务状态为准。

执行到期任务：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --max-jobs 3
```

通用平台正式数量、字段 profile、分页和停止条件只从 `config/crawl_targets.json` 读取；结构化平台
同时强制执行正式契约定义的行为与请求策略门禁。
通用 runner 会为 B站、微博、抖音和知乎自动读取 SQLite 发现 checkpoint：先有限刷新顶部，
再从已保存前沿继续；正常运行不需要人工传 `--start-page` 或 `--resume-summary`。小红书仍走
独立 workflow，但 `xhs_runner.py` 会按目标、人工指定账号和查询指纹自动读取自己的 checkpoint，
冻结未完成累计摘要，先刷新顶部，再使用已保存的 `page + search_id` 继续深层发现。
每个任务会在 `data/runtime/crawl_execution_states/<run_id>/` 生成冻结状态文件；只有状态
文件和正式摘要同时满足执行契约，才能汇报完成。

## 抓取记忆速查

“所有平台都有抓取记忆”只指五个正式结构化搜索平台；固定 URL 页面证据任务没有分页发现
前沿，每个已配置 URL 仍是独立任务。五个平台的记忆作用域和能力并不完全相同：

| 平台 | 控制面记忆 | 保存的深层前沿 | 跨轮详情前去重 |
|---|---|---|---|
| B站、微博、知乎 | `crawl_discovery_checkpoints`，按 job 与查询指纹隔离 | 下一安全页 | SQLite 已入库 ID、累计摘要 ID、`crawl_discovery_seen_candidates` 中所有已处理候选 ID |
| 抖音 | `crawl_discovery_checkpoints`，按 job 与查询指纹隔离 | page、offset、响应 search ID 必须成组恢复 | SQLite 已入库 ID、累计摘要 ID、`crawl_discovery_seen_candidates` 中所有已处理候选 ID |
| 小红书 | `xhs_discovery_checkpoints`，按目标、人工指定账号与查询指纹隔离 | page 与 client search ID 必须成组恢复 | SQLite 已入库 ID、累计摘要 ID，以及 `xhs_discovery_seen_candidates` 中所有已处理候选 ID |

首次运行从第一页开始且顶部刷新页数为 0；存在 checkpoint 后才先刷新配置限定的顶部页，再从
保存的深层前沿继续。顶部刷新不推进深层前沿。目标或候选上限在一页中途触发时，checkpoint
保留当前请求位置，下一轮允许重取这个边界页；已进入上述去重集合的 ID 会在昂贵详情或作者补全
前跳过。

抖音的 `exhausted` 只结束已保存 search ID 的游标链：顶部刷新发现持久记忆中不存在的新候选 ID 且获得可继续的
新 search ID 时，从刷新链下一页建立新前沿；否则保持耗尽，不重复深扫旧结果。微博综合搜索的
连续停滞按“没有新微博 ID”计算，纯文本或视频页不会因暂时没有有效图文而过早截断。

五个平台都会在 child 摘要形成后持久记忆视频、字段无效和有效候选。通用平台写
`crawl_discovery_seen_candidates`，按 job 与查询指纹隔离；小红书写独立表并额外按人工指定账号隔离。
正常运行一律让 runner 自动生成恢复参数；人工恢复仅按
[运行手册](operations-runbook.md) 的限制处理。

小红书不进入上述通用登录和调度链路。先完整读取
[小红书正式抓取 Workflow](platforms/xhs.md)，再使用独立账号目录、加密 storage state 和
人工指定账号执行。以下命令以新账号为例；已有账号跳过 `enroll`，先用
`xhs_accounts.py list` 核对状态：

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py enroll \
  --account-id xhs-a01
python scripts/xhs_login.py \
  --account-id xhs-a01
python scripts/xhs_runner.py \
  --dry-run \
  --target-key qingdao_travel \
  --account-id xhs-a01
```

dry-run 通过并经人工确认后，直接用相同账号、目标和互动参数去掉 `--dry-run`。XHS 配置
schema v2 已删除 pool/target 的 `enabled` 开关；正式运行只由显式 runner 命令触发，不再为每轮
修改配置或在结束后重新冻结。运行结束后检查顶层摘要、child summary、冻结状态和 SQLite。
dry-run 计划中的 `discovery` 必须与所选账号的
`xhs_discovery_checkpoints` 一致。小红书状态位于
`data/runtime/xhs/execution_states/<run_id>/`；其余平台仍位于通用状态目录。

## 数据边界

- 只采集图文、作者公开可见信息、图片 URL/样本和页面证据；明确视频记录跳过。
- `web_posts` 是用户使用的统一内容主表；`ctf_captures` 是证据和调试底座。
- `published_at` 必须来自平台原始发布时间，保存为 Asia/Shanghai ISO。
- 结构化长期数据以 SQLite 为准，`outputs/` 是运行产物和摘要。

## 诊断与开发入口

任意 runner 或执行器使用 `--no-import`，以及运行 `info_collection_benchmark.py`，都只属于
诊断或开发验证。它们不能替代正式入库，也不能作为正式完成证据；小红书仍必须经独立账号
runner 执行，不能直接交给通用 MediaCrawler 入口。
