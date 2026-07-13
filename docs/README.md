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

平台细节位于 `docs/platforms/`。

## 唯一正式入口

正式抓取前统一验证并按需刷新登录态：

```bash
source .venv/bin/activate
python scripts/login_warmup.py --targets all
```

该脚本只处理持久登录态，不抓取内容、不导入数据库。已有登录态有效时直接通过；失效时
等待人工登录，并在关闭、重开同一 profile 后再次验证。

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

正式数量、字段 profile、分页和停止条件只从 `config/crawl_targets.json` 读取。
每个任务会在 `data/runtime/crawl_execution_states/<run_id>/` 生成冻结状态文件；只有状态
文件和正式摘要同时满足执行契约，才能汇报完成。

## 数据边界

- 只采集图文、作者公开可见信息、图片 URL/样本和页面证据；明确视频记录跳过。
- `web_posts` 是用户使用的统一内容主表；`ctf_captures` 是证据和调试底座。
- `published_at` 必须来自平台原始发布时间，保存为 Asia/Shanghai ISO。
- 结构化长期数据以 SQLite 为准，`outputs/` 是运行产物和摘要。
- 携程、去哪儿、豆瓣小组目前是固定 URL 页面任务，不具备平台级批量发现能力。

## 诊断与开发入口

`mediacrawler_crawl.py --no-import`、`mediacrawler_batch_validate.py` 和
`info_collection_benchmark.py` 只用于诊断或开发验证。它们不能替代正式调度状态，
也不能单独作为“平台正式轮次完成”的证据。
