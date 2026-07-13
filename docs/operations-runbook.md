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
  --target-valid-posts 0 \
  --no-import
```

`mediacrawler_batch_validate.py` 只用于开发期字段检查，`info_collection_benchmark.py`
只用于性能和容量评估。两者都不能替代 `crawl_runner.py` 的正式状态文件和报告。

## 登录态

- 小红书：`mediacrawler_login_warmup.py --platforms xhs`，成功后必须生成并验证 storage snapshot。
- 知乎：`mediacrawler_login_warmup.py --platforms zhihu`，成功后必须生成包含 `d_c0/z_c0` 的 cookie snapshot。
- 其他 MediaCrawler 平台：复用各自持久化 profile；登录失败按分类结果处理。
- 豆瓣小组页面链路：使用 `ctf_login_warmup.py`，必须复用站点 profile。

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
- `formal_validation.target_met=true`；
- `valid_unique_count` 达到配置目标；
- `import_result.processed_rows`、`inserted_rows`、`updated_rows` 分别存在；
- SQLite 中作者粉丝量、发布时间和图片关系符合平台 profile；
- 视频只出现在跳过计数中。

固定 URL 页面任务只验证该页面证据和入库，不得汇报为平台批量目标完成。
