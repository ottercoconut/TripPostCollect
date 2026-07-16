# 正式抓取运行手册

## 执行顺序

通用平台正式任务只从调度器开始；小红书使用本手册后文的独立 runner：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --max-jobs 5
```

只验证并执行一个配置中的已启用任务时，普通 dry-run 和正式运行都直接使用同一个
`--job-key`；它不只用于断点续跑：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --job-key <enabled_job_key_from_config>
```

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --job-key <enabled_job_key_from_config>
```

dry-run 的预期状态是仅 `plan_frozen=completed`，`command_executed`、
`artifacts_verified`、`persistence_verified` 和 `task_finalized` 保持 `frozen`；运行摘要中
任务为 `planned`，入库结果为 `skipped: dry_run`。这是计划验证成功，不是执行失败。只有
真实运行才要求五个阶段全部 `completed`。

确认 dry-run 为每个任务生成独立执行状态后，再运行到期任务：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --max-jobs 3
```

状态目录是 `data/runtime/crawl_execution_states/<run_id>/`。每个任务一个 JSON 文件；
执行阶段、冻结输入、失败原因和自适应批次事件以该文件为准。不得手工把 `frozen` 或
`failed` 改成 `completed`。
`run_id` 带六位微秒，格式为 `YYYYMMDDTHHMMSSffffff+0000`；脚本和外部工具不得假定它只到秒。

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

`info_collection_benchmark.py` 只用于通用平台性能和容量评估，不能替代正式状态文件和报告，
也不接受小红书任务。

接近新增目标但未入库时，使用冻结断点续跑；继续同一关键词后续页：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --job-key mc_weibo_jinan_search \
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

通用正式抓取前使用统一登录入口检查全部已实现登录判据的平台：

```bash
source .venv/bin/activate
python scripts/login_warmup.py --targets all
```

也可以只检查指定平台：

```bash
source .venv/bin/activate
python scripts/login_warmup.py \
  --targets weibo zhihu douban_group \
  --timeout-seconds 600
```

当前目标包括抖音、知乎、微博、B站和豆瓣小组，不包含小红书。脚本按顺序加载正式抓取使用的
持久 profile，先验证平台特定 cookie、localStorage、用户接口或页面标记；已有状态有效
时直接通过，失效时在有头窗口等待人工登录。登录成功后关闭并重开同一 profile，只有
重开验证仍成功才写 cookie/storage snapshot 并标记 `ok=true`。单个平台浏览器异常会记
录失败并继续后续平台。

微博登录失效时，脚本用后台移动页检查 `m.weibo.cn/api/config`，同时把可交互的
`passport.weibo.com/sso/signin` 桌面 SSO 登录页置前；人工登录必须在桌面页完成。检测到
SSO 登录后，脚本把该页导航到移动端以刷新 Cookie。关闭并重开 profile 后，只有移动端
接口同时返回 `login=true` 和有效 `uid` 才通过；旧式 `WBPSESS` 不能单独作为成功证据。

统一报告位于 `outputs/login_warmup/<run_id>/summary.json` 和 `summary.md`，每个平台记录
`initial_ok`、`login_refreshed`、`persisted_ok`、profile 路径和错误。该脚本只负责登录，
不抓内容、不写 SQLite。`mediacrawler_login_warmup.py` 和 `ctf_login_warmup.py` 是其底层
平台实现，正常操作不再分别调用。

### 小红书独立登录与运行

完整阶段、字段补全、行为证据、成功判据和失败分流以
[`platforms/xhs.md`](platforms/xhs.md) 为准。本手册只保留操作命令。

新账号先登记槽位；已有账号不要重复登记：

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py enroll \
  --account-id xhs-a01
python scripts/xhs_login.py \
  --account-id xhs-a01 \
  --timeout-seconds 600
```

`xhs_login.py` 只负责人工登录和关闭/重开复验，不抓取内容。正式运行前查看账号状态，并用
同一个账号、目标和互动参数冻结计划：

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py list
python scripts/xhs_runner.py \
  --dry-run \
  --target-key qingdao_travel \
  --account-id xhs-a01
```

确认 dry-run 输出为 `planned`、只有 `plan_frozen=completed` 后，经明确批准同时启用 pool
和目标，再运行：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --target-key qingdao_travel \
  --account-id xhs-a01
```

运行结束后按顺序读取顶层 `run_summary.json`、冻结状态、child summary 和 SQLite 计数，再将
pool 与目标开关恢复为禁用。登录失效时对原账号运行 `xhs_login.py`；验证、频控、拒绝访问或
环境异常时停止请求并等待操作人决定。不得自动解验证、自动重试或在同一正式轮次中途换号。

## 浏览器运行环境

Chrome HOME、Crashpad 和 `uv` 缓存由 `scripts/browser_runtime.py` 指向
`data/runtime/` 下的项目目录；Chromium 统一使用 mock keychain，避免 macOS 在隔离
`HOME` 下弹出 `Keychain Not Found` 并阻塞页面和登录态读取。该弹窗属于确定的浏览器
运行配置错误，不是平台验证码，也不应被忽略或归因于模型大小。浏览器启动失败必须区分：

- `runtime_permission_error`：运行目录或进程权限错误；
- `browser_launch_failed`：Chrome/CDP 启动失败；
- `browser_target_closed`：Chrome/CDP 已启动成功，但页面、context 或浏览器随后关闭；先核对人工关闭或浏览器崩溃，不按启动失败处理，也不自动重试；
- `login_required`：页面明确要求登录；
- `captcha_detected`：平台安全验证或验证码。

遇到一个平台失败时，调度器继续执行其他已选择任务；失败任务的后续业务阶段在状态
文件中保持冻结。运行结束后统一查看 `run_summary.json` 和各任务执行状态。

## MediaCrawler 行为阶段

通用四个结构化平台在正式搜索前必须执行 `social_high_risk`；小红书独立链路必须执行
`xhs_guarded`。正常摘要中的
`behavior_validation.ok`、`behavior_ok` 和 `policy_ok` 都应为 `true`；每个平台记录应有
`behavior_evidence.events`、`runtime_fingerprint`、`visible_markers`、截图和
`policy_events`。行为阶段占用独立的 240 秒超时预算，不挤占配置中的平台抓取超时。
小红书摘要还应包含 `behavior_evidence.request_pacing_events`，覆盖 search results、note
detail、creator profile 和实际发生的 page navigation 阶段。
失败轮次只要 child summary 已生成，Runner 顶层仍必须读取其中的行为与帖子互动证据；退出码
失败不能把已发生的互动错误汇总为空。

验证码、频控或拒绝访问由可见页面文本判定；通用平台记录冷却，小红书只记录证据并等待
人工指挥。不要直接扫描整页 HTML 判断
验证码：站点打包脚本和隐藏组件可能包含 `geetest`、`captcha` 或“请先登录”，会产生
假阳性。行为证据缺失或不完整时保留 JSONL 排障，但禁止入库。

策略状态中的 `max_requests_per_session` 是自动会话冷却：跨 UTC 日，或从
`last_request_finished_at`（缺失时用 `last_request_at`）起已完整空闲 `cooldown_minutes`，会开启
新会话并清零计数。验证码、频控和封禁写入的其他 `cooldown_reason` 是显式冷却，在有效期内
仍必须停止；不要通过删状态或改计数解除。

## 结果检查

正式结构化任务至少检查：

- 状态文件最终为 `completed`，所有阶段均完成或有明确允许的 `skipped`；
- `formal_validation.new_target_met=true`；
- `behavior_validation.ok=true`，且行为与策略平台列表覆盖本轮全部结构化平台；
- 每个平台 `behavior_validation.platforms.<platform>.target_url_ok=true`，证据 URL 对应本轮关键词；
- `formal_validation.behavior_evidence_ok=true`、`policy_evidence_ok=true`；
- `valid_new_count` 和 `inserted_rows` 都达到 `target_new_posts`；
- `valid_existing_count` / `updated_rows` 单独报告且不计入新增目标；
- `import_result.processed_rows`、`inserted_rows`、`updated_rows` 分别存在；
- SQLite 中作者粉丝量、发布时间和图片关系符合平台 profile；
- 视频只出现在跳过计数中。
- `formal_validation.pagination_evidence` 有连续页级事件；未达目标时，`source_exhausted`
  必须有空页或 `has_more=false` 的 `adaptive_search_stopped` 事件。只有批次事件而没有停止
  事件的任务按 `runtime_failed` 排查浏览器、登录态、超时或请求异常。

豆瓣小组搜索发现任务按配置的有效新增和实际插入目标判断；其他固定 URL 页面任务只验证该页
证据和入库，不得汇报为平台批量目标完成。
