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
任务为 `planned`。通用任务记录的入库结果为 `skipped: dry_run`；小红书 dry-run 没有 child
或 `import_result`，以后四阶段未启动证明没有入库。这是计划验证成功，不是执行失败。只有真实
运行才要求五个阶段全部 `completed`。

通用 dry-run “不抓取”不等于“不写本地状态”：默认会先把配置同步到 `crawl_jobs`，再写
`crawl_run_reports`、运行摘要和每任务 execution state。没有 `--job-key` 时，只选择
`enabled=1`、状态为 `pending/completed/retry_wait` 且 `next_run_at` 已到期的任务，按
`priority`、`next_run_at`、数据库 ID 排序后取 `--max-jobs`；指定 `--job-key` 时不要求到期，但
该任务仍必须启用。实际选中集合始终以本轮 dry-run 摘要为准。

dry-run 的 `crawl_run_reports.status=completed` 只表示 runner 成功生成并保存计划报告，不表示
任何正式任务抓取完成；任务记录仍应为 `planned`、`completed_count=0`，并满足上面的冻结阶段
语义。需要解释未选中任务时使用只读查询，不猜测调度原因：

```bash
sqlite3 data/trippostcollect.sqlite \
  "SELECT job_key, enabled, status, next_run_at, priority FROM crawl_jobs ORDER BY priority, next_run_at, id;"
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

`crawl_runner.py --no-import` 和 `xhs_runner.py --no-import` 同样只用于诊断。当前执行器可能在
内容与产物校验通过时把这类运行写成 `completed`，但入库被跳过，不能按正式轮次完成汇报。

## 自动恢复、检查与临时候选预算

B站、微博、抖音和知乎的正常正式 workflow 不需要人工拼接恢复参数。`crawl_runner.py` 每次
先按任务与查询指纹读取 `crawl_discovery_checkpoints`：没有记录就从第一页开始；有记录就自动
冻结累计摘要、恢复保存的前沿，并先刷新配置的 `top_refresh_max_pages` 个顶部页面。抖音 child
命令必须同时出现保存的 `--start-page`、`--start-offset` 和 `--start-cursor`；只出现页码或
offset 时停止执行，不得用空 cursor 请求深页。

小红书使用相同的安全前沿原则，但不使用通用表或通用 runner。`xhs_runner.py` 按
`target_key + account_id + query_fingerprint` 读取 `xhs_discovery_checkpoints`；续跑必须同时冻结
保存的页码和非空 `search_id`，先有限刷新顶部，再恢复深层前沿。操作人不手工传恢复参数。

检查记忆状态：

```bash
sqlite3 data/trippostcollect.sqlite \
  "SELECT job_id, platform_key, keyword, resume_page, resume_offset, resume_cursor, status, last_batch_complete, last_stop_reason, last_run_id, updated_at FROM crawl_discovery_checkpoints ORDER BY job_id;"
```

检查小红书账号级记忆：

```bash
sqlite3 data/trippostcollect.sqlite \
  "SELECT target_key, account_id, keyword, resume_page, resume_search_id, status, last_batch_complete, last_stop_reason, last_run_id, campaign_candidate_count, updated_at FROM xhs_discovery_checkpoints ORDER BY target_key, account_id;"
```

已处理候选只看计数，不展开 ID 全量：

```bash
sqlite3 data/trippostcollect.sqlite \
  "SELECT target_key, account_id, query_fingerprint, COUNT(*) AS seen_candidates FROM xhs_discovery_seen_candidates GROUP BY target_key, account_id, query_fingerprint ORDER BY target_key, account_id;"
```

再冻结目标任务，检查生成命令而不抓取：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --no-sync-config \
  --job-key mc_douyin_qingdao_search
```

有 checkpoint 的抖音计划应同时含三个恢复参数和 `--top-refresh-max-pages`；B站、微博、知乎有
checkpoint 时应含保存页码和顶部刷新参数。只有 checkpoint 的 `last_summary_path` 非空且文件
存在时才应再含累计 `--resume-summary`。首次任务没有 checkpoint，不应出现 `--resume-summary`，
顶部刷新值为 0。实际执行结束后同时核对 child `summary.json` 的
`pagination_evidence`、`discovery_checkpoint` 与 SQLite 行；SQLite 的 `last_run_id` 必须等于
本次运行 ID。未达到目标但前沿推进时，任务仍不是正式完成，不过连续失败会清零，下一次按
正常 `schedule_seconds` 调度，而不是立即反复抓顶部。
checkpoint 记录了非空 `last_summary_path` 但文件丢失时，runner 必须在冻结前报错；不得静默
丢弃历史成果并推进游标。

累计摘要保存各次 JSONL，只有合并后的 `valid_new_count` 达到完整目标才一次性入库。
`candidate_hard_limit` 是每次 child 的未知候选预算，每次续跑重新获得完整预算；历史累计候选
只用于报告，不从本次预算扣减。`source_exhausted` 后只做顶部刷新，不再请求已耗尽深页。
`--no-import` 自动禁用 checkpoint 写入，因此诊断不会污染正式记忆。

耗尽 checkpoint 的生成命令仍会携带保存的 page/offset/cursor，用于冻结并保留原前沿；同时出现
的 `--discovery-source-exhausted` 优先控制执行阶段，child 只建立顶部刷新 phase，不请求这些深层
坐标。不要因为命令中仍有坐标就判断它会继续深层抓取。

小红书 dry-run 的 `plan.discovery` 必须与上面的账号级行一致。首次执行应为第 1 页、空
`resume_search_id`、顶部刷新 0；续跑应包含已保存页码、非空 ID、配置的顶部刷新页数和可选
`campaign_summary_path`。正式 child 结束后核对顶层 `run_summary.json` 的 `discovery`、child 的
`pagination_evidence` 和 SQLite `last_run_id`。顶部刷新完成目标时，深层页码与 ID 必须保持不变。
同时核对顶层 `discovery.seen_candidate_count` 与本轮停止事件的候选 ID 数；不要在报告中展开
全部 ID。

字段映射固定为：SQLite `resume_search_id` → dry-run `plan.discovery.resume_search_id` → child
CLI `--start-cursor` →分页事件 `source_cursor/resume_cursor`。这些名称描述同一个小红书 client
search ID，不是四套独立游标。

小红书在一页中途以 `target_new_met` 达标并成功入库时，checkpoint 不删除：若来源仍可继续，
`status` 保持 `active`，`last_stop_reason=target_new_met`，`last_batch_complete=false` 并保留当前
page/search ID；同时清空 `last_summary_path`，把 `campaign_candidate_count` 重置为 0。下轮重取
边界页并依赖持久候选集合跳过已处理 ID，这是成功后的正常状态，不是未提交或活动丢失。

`--start-page`、`--resume-summary` 和 `--recovery-keyword` 仅保留给明确的人工恢复。使用任一显式
恢复参数时，runner 不自动加载现有 checkpoint；操作人必须从状态事件读取未处理位置，且抖音
人工深页恢复目前没有 runner 级 cursor 参数，不能只靠 `--start-page` 执行。优先修复或保留
自动 checkpoint，不要删除记录后靠猜页码续跑。小红书不使用通用表；旧 `search_id` 恢复失败时
按独立文档保留原账号级 checkpoint 并结束失败轮次，不生成新 ID 猜测深页。

临时候选扩容没有命令行覆盖参数，只能通过配置完成。必须按以下顺序操作：

1. 记录原 `candidate_hard_limit`，只修改目标 job 的单次 child 预算；不要把历史累计候选数加到
   临时值中。
2. 激活项目虚拟环境并校验配置：

   ```bash
   source .venv/bin/activate
   python -m json.tool \
     config/crawl_targets.json \
     >/dev/null
   ```

3. 同步配置，再用目标 job 冻结并核对自动生成的恢复命令：

   ```bash
   source .venv/bin/activate
   python scripts/crawl_runner.py --sync-only
   python scripts/crawl_runner.py \
     --dry-run \
     --no-sync-config \
     --job-key <enabled_job_key_from_config>
   ```

   正式轮使用完全相同的 runner 参数，去掉 `--dry-run` 并保留 `--no-sync-config`。
4. 运行结束并完成摘要、状态和 SQLite 检查后，恢复原配置值，再次校验 JSON 并执行
   `--sync-only`。从临时配置第一次 `--sync-only` 开始，直至正式轮摘要、状态和 SQLite 检查
   完成，配置文件不得再修改。

临时值只服务当前恢复或新轮，不得因为一次扩容无意改变长期调度标准。涉及三个及以上参数的
dry-run 或正式命令仍按本项目规则分行展示。

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
  --targets weibo zhihu bilibili \
  --timeout-seconds 600
```

`--targets all` 当前只展开为抖音、知乎、微博和 B站；不包含小红书，也不验证页面证据执行器
使用的独立浏览器 profile。脚本按顺序加载这四个平台正式抓取使用的
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
不抓内容、不写 SQLite。`mediacrawler_login_warmup.py` 是其底层平台实现，正常操作不再单独调用。

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

确认 dry-run 输出为 `planned`、只有 `plan_frozen=completed` 后，经明确批准直接运行：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --target-key qingdao_travel \
  --account-id xhs-a01
```

运行结束后按顺序读取顶层 `run_summary.json`、冻结状态、child summary 和 SQLite 计数。XHS
配置 schema v2 没有 pool/target `enabled` 开关，显式 runner 命令就是唯一启动动作，不修改配置。
登录失效时对原账号运行 `xhs_login.py`；作者页二维码安全验证会
保留当前标签页并置前，最多等待操作人扫码 600 秒，通过后继续。其他验证、频控、拒绝访问或
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
`behavior_validation.ok`、`behavior_validation.behavior_ok` 和
`behavior_validation.policy_ok` 都应为 `true`；每个平台记录应有
`behavior_evidence.events`、`runtime_fingerprint`、`visible_markers`、截图和
`policy_events`。行为阶段占用独立的 240 秒超时预算，不挤占配置中的平台抓取超时。
小红书摘要还应包含 `behavior_evidence.request_pacing_events`，覆盖 search results、note
detail、creator profile 和实际发生的 page navigation 阶段；同时包含
`continuity_events`，证明搜索批次之间仍执行了可见页面停留和鼠标移动。小红书行为开始前
必须确认登录完成且结果卡片可见，滚轮必须产生可观测位移，浏览器 UA 与 API UA Client Hints
主版本必须一致。
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

- 状态文件最终为 `completed`，且真实正式入库运行的五个阶段全部为 `completed`；
- `formal_validation.new_target_met=true`；
- `behavior_validation.ok=true`，且行为与策略平台列表覆盖本轮全部结构化平台；
- 每个平台 `behavior_validation.platforms.<platform>.target_url_ok=true`，证据 URL 对应本轮关键词；
- `formal_validation.behavior_evidence_ok=true`、`policy_evidence_ok=true`；
- `valid_new_count` 和 `inserted_rows` 都达到 `target_new_posts`；
- `valid_existing_count` / `updated_rows` 单独报告且不计入新增目标；
- `import_result.processed_rows`、`inserted_rows`、`updated_rows` 分别存在；
- 命令未使用 `--no-import`，`persistence_verified` 没有以 `skipped` 代替真实入库；
- SQLite 中作者粉丝量、发布时间和图片关系符合平台 profile；
- 视频只出现在跳过计数中。
- `formal_validation.pagination_evidence` 有连续页级事件；未达目标时，`source_exhausted`
  必须有空页或 `has_more=false` 的 `adaptive_search_stopped` 事件。只有批次事件而没有停止
  事件的任务按 `runtime_failed` 排查浏览器、登录态、超时或请求异常。

固定 URL 页面任务只验证该页证据和入库，不得汇报为平台批量目标完成。
