# 正式抓取运行手册

## 执行顺序

正式任务只从调度器开始：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --max-jobs 5
```

确认 dry-run 为每个任务生成独立执行状态后，再运行到期任务：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --max-jobs 3
```

状态目录是 `data/runtime/crawl_execution_states/<run_id>/`。每个任务一个 JSON 文件；
执行阶段、冻结输入、失败原因和自适应批次事件以该文件为准。不得手工把 `frozen` 或
`failed` 改成 `completed`。

只同步配置和调度库：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py --sync-only
```

## 诊断入口

单平台字段诊断可以直接运行执行器，但不代表正式轮次完成：

```bash
source .venv/bin/activate
python scripts/mediacrawler_crawl.py \
  --platforms weibo \
  --keyword 济南旅游 \
  --candidate-hard-limit 20 \
  --target-new-posts 0 \
  --no-import
```

`mediacrawler_batch_validate.py` 只用于开发期字段检查，`info_collection_benchmark.py`
只用于性能和容量评估。两者都不能替代 `crawl_runner.py` 的正式状态文件和报告。

接近新增目标但未入库时，使用冻结断点续跑；继续同一关键词后续页：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --job-key mc_xhs_jinan_search \
  --start-page 8 \
  --resume-summary outputs/mediacrawler_runs/<run_id>/summary.json
```

单关键词明确耗尽时，可换同城市补充关键词：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --job-key mc_douyin_jinan_search \
  --resume-summary outputs/mediacrawler_runs/<run_id>/summary.json \
  --recovery-keyword 济南旅行
```

续跑会把上一轮摘要及 JSONL 加入冻结输入，并将其中已收集 ID 注入底层去重集合。只有
合并后的有效新增数和实际新增行同时达到完整目标才入库。

## 登录态

正式抓取前使用唯一公开登录入口检查全部已实现登录判据的平台：

```bash
source .venv/bin/activate
python scripts/login_warmup.py --targets all
```

也可以只检查指定平台：

```bash
source .venv/bin/activate
python scripts/login_warmup.py \
  --targets xhs zhihu douban_group \
  --timeout-seconds 600
```

当前目标包括抖音、知乎、微博、小红书、B站和豆瓣小组。脚本按顺序加载正式抓取使用的
持久 profile，先验证平台特定 cookie、localStorage、用户接口或页面标记；已有状态有效
时直接通过，失效时在有头窗口等待人工登录。登录成功后关闭并重开同一 profile，只有
重开验证仍成功才写 cookie/storage snapshot 并标记 `ok=true`。单个平台浏览器异常会记
录失败并继续后续平台。

统一报告位于 `outputs/login_warmup/<run_id>/summary.json` 和 `summary.md`，每个平台记录
`initial_ok`、`login_refreshed`、`persisted_ok`、profile 路径和错误。该脚本只负责登录，
不抓内容、不写 SQLite。`mediacrawler_login_warmup.py` 和 `ctf_login_warmup.py` 是其底层
平台实现，正常操作不再分别调用。

携程和去哪儿当前正式配置只抓公开固定页面，尚无经过验证的账号登录标记，不纳入统一
登录清单；不得以任意匿名 cookie 存在作为登录成功。

## 浏览器运行环境

Chrome HOME、Crashpad 和 `uv` 缓存由 `scripts/browser_runtime.py` 指向
`data/runtime/` 下的项目目录；Chromium 统一使用 mock keychain，避免 macOS 在隔离
`HOME` 下弹出 `Keychain Not Found` 并阻塞页面和登录态读取。该弹窗属于确定的浏览器
运行配置错误，不是平台验证码，也不应被忽略或归因于模型大小。浏览器启动失败必须区分：

- `runtime_permission_error`：运行目录或进程权限错误；
- `browser_launch_failed`：Chrome/CDP 启动失败；
- `login_required`：页面明确要求登录；
- `captcha_detected`：平台安全验证或验证码。

遇到一个平台失败时，调度器继续执行其他已选择任务；失败任务的后续业务阶段在状态
文件中保持冻结。运行结束后统一查看 `run_summary.json` 和各任务执行状态。

## 结果检查

正式结构化任务至少检查：

- 状态文件最终为 `completed`，所有阶段均完成或有明确允许的 `skipped`；
- `formal_validation.new_target_met=true`；
- `valid_new_count` 和 `inserted_rows` 都达到 `target_new_posts`；
- `valid_existing_count` / `updated_rows` 单独报告且不计入新增目标；
- `import_result.processed_rows`、`inserted_rows`、`updated_rows` 分别存在；
- SQLite 中作者粉丝量、发布时间和图片关系符合平台 profile；
- 视频只出现在跳过计数中。
- `formal_validation.pagination_evidence` 有连续页级事件；未达目标时，`source_exhausted`
  必须有空页或 `has_more=false` 的 `adaptive_search_stopped` 事件。只有批次事件而没有停止
  事件的任务按 `runtime_failed` 排查浏览器、登录态、超时或请求异常。

固定 URL 页面任务只验证该页面证据和入库，不得汇报为平台批量目标完成。
