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

执行到期任务：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --max-jobs 3
```

通用平台正式数量、字段 profile、分页和停止条件只从 `config/crawl_targets.json` 读取；结构化平台
同时强制执行正式契约定义的行为与请求策略门禁。
通用 runner 会为 B站、微博、抖音和知乎自动读取 SQLite 发现 checkpoint：先有限刷新顶部，
再从已保存前沿继续；正常运行不需要人工传 `--start-page` 或 `--resume-summary`。小红书不使用
该通用记忆，仍按独立 workflow 处理。
每个任务会在 `data/runtime/crawl_execution_states/<run_id>/` 生成冻结状态文件；只有状态
文件和正式摘要同时满足执行契约，才能汇报完成。

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

dry-run 通过并经人工确认后，才同时启用 `config/xhs_pool.json` 和目标项并去掉
`--dry-run`。运行结束后检查顶层摘要、child summary、冻结状态和 SQLite，再关闭两个
`enabled` 开关。小红书状态位于 `data/runtime/xhs/execution_states/<run_id>/`；其余平台
仍位于通用状态目录。

## 数据边界

- 只采集图文、作者公开可见信息、图片 URL/样本和页面证据；明确视频记录跳过。
- `web_posts` 是用户使用的统一内容主表；`ctf_captures` 是证据和调试底座。
- `published_at` 必须来自平台原始发布时间，保存为 Asia/Shanghai ISO。
- 结构化长期数据以 SQLite 为准，`outputs/` 是运行产物和摘要。

## 诊断与开发入口

任意 runner 或执行器使用 `--no-import`，以及运行 `info_collection_benchmark.py`，都只属于
诊断或开发验证。它们不能替代正式入库，也不能作为正式完成证据；小红书仍必须经独立账号
runner 执行，不能直接交给通用 MediaCrawler 入口。
