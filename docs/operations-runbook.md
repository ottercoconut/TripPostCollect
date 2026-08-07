# 正式抓取运行手册

正式运行先同时应用 `trippostcollect-crawl` 共享核心，并选择恰好一个模式 Skill：定量任务使用
`trippostcollect-crawl-to-target`，明确要求当前关键词来源耗尽时使用
`trippostcollect-crawl-to-source-exhaustion`。模式 Skill 会让 dry-run 与正式命令显式携带相同的
`--completion-mode`；同一任务不得同时应用两个模式。

## 执行顺序

通用平台正式任务只从调度器开始；小红书使用本手册后文的独立 runner：

B站、微博、抖音、知乎和小红书五个平台默认均可运行。通用 runner 只选择
`config/crawl_targets.json` 中现存且 `enabled=true` 的 job；小红书按后文独立账号流程显式执行。

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --completion-mode target-new-posts \
  --max-jobs 5
```

只验证并执行一个配置中的已启用任务时，普通 dry-run 和正式运行都直接使用同一个
`--job-key`；它不只用于断点续跑：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --job-key <enabled_job_key_from_config> \
  --completion-mode target-new-posts
```

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --job-key <enabled_job_key_from_config> \
  --completion-mode target-new-posts
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

CLI 不传 `--completion-mode` 时仍使用正常默认的 `target-new-posts` 数量模式；定量模式 Skill
显式传 `--completion-mode target-new-posts` 以便冻结审计。只有用户明确要求某一轮直到来源耗尽时，
才选择来源耗尽 Skill，并给 dry-run 和正式运行同时传 `--completion-mode source-exhausted`。
这是进程级临时参数，不修改 `target_new_posts`、
`candidate_hard_limit`、`max_stagnant_batches` 或后续轮次的默认设置。

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
  --completion-mode target-new-posts \
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
  --keyword 青岛旅游 \
  --candidate-hard-limit 20 \
  --target-new-posts 0 \
  --download-images \
  --media-root temp/diagnostic_media \
  --no-import
```

诊断也应启用正文图链路，否则只能验证文字字段，不能作为图片功能证据。`--media-root` 覆盖只允许
项目 `temp/` 子目录；`--no-import` 会生成 staging、manifest 和
`image_materialization(promotion_required=false)`，但不会写 `data/media`、SQLite 或 checkpoint。
不要传旧 `--get-media`：该参数会直接失败，且视频始终禁用。

## 图片磁盘预检与备份

每次正式结构化运行前先检查数据库和长期媒体目录所在卷；多平台或较大候选预算还要预留本轮
staging 与最终文件同时存在的空间：

```bash
df -h data outputs
du -sh data/media outputs/mediacrawler_runs 2>/dev/null
```

空间明显不足时在 dry-run 后、正式 child 前停止。不要边下载边删除本轮 staging 或长期文件；
manifest 复验依赖这些字节，部分删除会正确地使 `artifacts_verified` 失败。

普通新增抓取依赖 SQLite 事务和内容寻址文件幂等，不要求每轮复制整库。任何历史补全、批量修复、
清理或人工 SQL 写默认库前则必须先建立 SQLite 一致性备份，并记录备份 SHA-256：

```bash
mkdir -p data/backups
sqlite3 data/trippostcollect.sqlite ".backup 'data/backups/trippostcollect-before-<run_id>.sqlite'"
shasum -a 256 data/backups/trippostcollect-before-<run_id>.sqlite
```

历史图片补全只能在 `MAIN_PROGRAM_READY=true` 后按工程方案 H 阶段执行；主程序开发和新记录验证
不能顺手修改默认库已有行或移动现有图片。

`info_collection_benchmark.py` 只用于通用平台性能和容量评估，不能替代正式状态文件和报告，
也不接受小红书任务。

`crawl_runner.py --no-import` 和 `xhs_runner.py --no-import` 同样只用于诊断。当前执行器可能在
内容与产物校验通过时把这类运行写成 `completed`，但入库被跳过，不能按正式轮次完成汇报。

复核一组已知知乎回答/文章是否真实无图时，将规范 URL 保存为 JSON 数组，并通过
`mediacrawler_crawl.py --platforms zhihu --zhihu-detail-urls-file <文件> --no-import` 执行。
只有 `content_detail_status=detail_observed` 后仍无正文图片才可判为真实无图；请求或解析失败必须
保留为未观察，不能直接运行 `tools/MediaCrawler` 内部命令绕过项目行为与登录门禁。

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
  "SELECT job_id, platform_key, keyword, resume_page, resume_offset, resume_cursor, status, last_batch_complete, last_stop_reason, last_stop_detail, last_run_id, updated_at FROM crawl_discovery_checkpoints ORDER BY job_id;"
```

检查通用平台已处理候选只看分组计数，不展开 ID：

```bash
sqlite3 data/trippostcollect.sqlite \
  "SELECT job_id, platform_key, query_fingerprint, COUNT(*) AS seen_candidates FROM crawl_discovery_seen_candidates GROUP BY job_id, platform_key, query_fingerprint ORDER BY job_id;"
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
  --job-key mc_douyin_qingdao_laoshan_guide_search \
  --completion-mode target-new-posts
```

有 checkpoint 的抖音计划应同时含三个恢复参数和 `--top-refresh-max-pages`；B站、微博、知乎有
checkpoint 时应含保存页码和顶部刷新参数。只有 checkpoint 的 `last_summary_path` 非空且文件
存在时才应再含累计 `--resume-summary`。首次任务没有 checkpoint，不应出现 `--resume-summary`，
顶部刷新值为 0。实际执行结束后同时核对 child `summary.json` 的
`pagination_evidence`、`discovery_checkpoint` 与 SQLite 行；SQLite 的 `last_run_id` 必须等于
本次运行 ID。未达到目标但前沿推进时，任务仍不是正式完成，不过连续失败会清零，下一次按
正常 `schedule_seconds` 调度，而不是立即反复抓顶部。
明确要求某一轮“持续到来源耗尽”时，不使用超大数量或调度间隔模拟无限抓取；应在独立 one-off
配置或明确 job 上，给 dry-run 与正式运行传同一个 `--completion-mode source-exhausted`。模式只对
该次进程生效，长期配置仍保留正常数量限制。`schedule_seconds` 只决定启用 job 何时再次到期：
状态为 `completed` 的周期任务到期后仍可再次被选择，不能把调度间隔当作一次性完成门禁。
checkpoint 记录了非空 `last_summary_path` 但文件丢失时，runner 必须在冻结前报错；不得静默
丢弃历史成果并推进游标。

默认数量模式的累计摘要保存各次 JSONL，只有合并后的 `valid_new_count` 达到完整目标才一次性入库；
显式来源耗尽模式则在取得可验证的 `source_exhausted` 停止证据后入库，即使新增数低于配置目标。
`candidate_hard_limit` 是每次 child 的未知候选预算，每次续跑重新获得完整预算；历史累计候选
只用于报告，不从本次预算扣减。`source_exhausted` 后不再请求原耗尽深页。抖音顶部刷新若取得
不在数据库、累计摘要或持久候选集合中的新候选 ID，且最后一页同时返回 `has_more=true` 和非空下一 search ID，会从刷新链下一页建立
新的 cursor 前沿；摘要的 `pagination_evidence.frontier_reseeds` 必须保留旧坐标、新坐标和触发原因。
没有新候选 ID 或没有可继续游标时仍保持耗尽，不得每轮重扫完整结果集。新前沿中的历史视频、
字段无效项和有效项由 `crawl_discovery_seen_candidates` 在昂贵处理前跳过，不再消耗候选预算。
顶部刷新已经达到本轮新增目标时，以 `target_new_met` 优先结束，不建立新前沿，旧耗尽 checkpoint
保持不变。
`--no-import` 自动禁用 checkpoint 写入，因此诊断不会污染正式记忆。

B站 article 的安全前沿还取决于详情处理是否完成。搜索页返回未知 ID 后，只有详情成功并完成
正式字段判断，或详情明确证明内容已删除、私密、永久不可用时，该 ID 才能写入
`crawl_discovery_seen_candidates`。详情接口 `-509`、HTTP/业务错误、超时或解析失败经过有限退避仍
未恢复时，当前页必须保持 `last_batch_complete=false`，`resume_page` 保持当前页，失败 ID 不进入
累计摘要或候选记忆。不要通过删除 checkpoint 或扩大候选预算绕过详情失败。

抖音新鲜游标链在第 1 页收到 `data=[]、has_more=false` 时，不直接创建耗尽 checkpoint。
执行器必须检查当前可见搜索页：存在 `/video/`、`/note/` 或搜索结果卡片时停止为
`empty_api_response_with_visible_results`；没有结果卡片、但也没有明确可见的“无结果”提示时停止为
`ambiguous_empty_first_page`。两者都属于 `runtime_failed`，恢复坐标保持 page 1、offset 0、
空 search ID。只有页面明确显示无结果时才允许 `verified_empty_first_page`。主执行器还会拒绝
旧版本产生的抖音第 1 页 `empty_page` / `has_more_false` 耗尽证据，防止它进入正式完成或入库门禁。
checkpoint 额外保存 `last_stop_detail`；历史记录没有
`verified_empty_first_page` 时，runner 会忽略其 `exhausted` 状态并从 page 1、offset 0、空 search ID
重新请求，不需要手工删除 checkpoint。

耗尽 checkpoint 的生成命令仍会携带保存的 page/offset/cursor，用于冻结并保留原前沿；同时出现
的 `--discovery-source-exhausted` 表示禁止直接请求这些旧深层坐标。B站、微博和知乎只建立顶部
刷新 phase；抖音只有满足上一段的新候选与连续游标双重证据才建立新 cursor 前沿。不要因为
命令中仍有旧坐标就判断它会继续原深层抓取。

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

### 一次性配置与临时候选预算

新关键词、临时数量、临时平台组合或临时候选扩容使用 `config/one_off/` 下的独立配置，不直接编辑
`config/crawl_targets.json`。只有用户明确要求改变长期调度标准时才修改主配置。

通用 one-off 配置必须从当前主配置完整派生：保留 `defaults` 和所有长期 job 原值，再新增一次性
job，或只在派生副本中调整目标 job。不能只写一次性 job，否则调度同步会禁用未出现在该文件中的
长期 job。临时配置中的既有 job key、平台和来源查询参数必须保持与主配置一致；新关键词使用新的
job key 和查询指纹。

按以下顺序操作：

1. 记录目标 job 的原值。临时候选扩容只在派生配置中修改单次 child 的
   `candidate_hard_limit`，不要把历史累计候选数加到临时值中。来源耗尽模式不得放大目标、候选
   上限或停滞批次来模拟无限抓取。
2. 激活项目虚拟环境并校验派生配置：

   ```bash
   source .venv/bin/activate
   python -m json.tool \
     config/one_off/<task-config>.json \
     >/dev/null
   ```

3. 用完整派生配置同步调度表，再用同一配置和目标 job 冻结并核对自动生成的恢复命令：

   ```bash
   source .venv/bin/activate
   python scripts/crawl_runner.py \
     --config config/one_off/<task-config>.json \
     --sync-only
   python scripts/crawl_runner.py \
     --config config/one_off/<task-config>.json \
     --dry-run \
     --no-sync-config \
     --job-key <enabled_job_key_from_config> \
     --completion-mode target-new-posts
   ```

4. 正式轮使用完全相同的 `--config`、`--no-sync-config`、job 和完成模式，去掉 `--dry-run`。
   从 one-off 配置第一次 `--sync-only` 开始，直至正式轮摘要、状态和 SQLite 检查完成，派生配置
   不得再修改。
5. 任务验收后执行主配置的 `python scripts/crawl_runner.py --sync-only`，恢复长期调度范围和参数；
   不手工编辑 SQLite。保留 one-off 文件作为本轮冻结输入和 Git 证据，后续任务不得直接复用旧
   job key。

临时值只服务当前恢复或新轮，不得因为一次任务无意改变长期调度标准。涉及三个及以上参数的
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

`xhs_login.py` 只负责人工登录和关闭/重开复验，不抓取内容。它在打开浏览器前申请与正式抓取相同
的账号租约，整个登录与复验期间持有，并在结束时释放；账号忙时只返回 `blocked`，不改变账号登录
状态。初次登录或关闭重开复验出现可见安全验证/验证码时，工具必须保留当前单页、置前并按每个
阶段的 `--timeout-seconds` 等待人工处理；检测到验证不能立即返回或关闭 context。验证消失且同一
身份重新可见后继续，超时后才截图并失败；工具不自动识别或绕过验证。登录租约覆盖两段完整等待
及清理时间。正式运行前查看账号状态，并用同一个账号、目标和互动参数冻结计划：

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py list
python scripts/xhs_runner.py \
  --dry-run \
  --target-key qingdao_travel \
  --account-id xhs-a01 \
  --completion-mode target-new-posts
```

确认 dry-run 输出为 `planned`、只有 `plan_frozen=completed` 后，经明确批准直接运行：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --target-key qingdao_travel \
  --account-id xhs-a01 \
  --completion-mode target-new-posts
```

小红书实际候选量从 0 按页增长，达到配置的有效新增目标即停止；`candidate_hard_limit` 只是单次
child 的未知候选安全上限。永久提高检索目标时只修改 `config/xhs_targets.json` 和必要的
`config/xhs_pool.json`，同步核对候选上限、停滞批次、顶部刷新、超时以及
`lease_seconds >= timeout_seconds + 300`，再运行 JSON 校验和上述 dry-run。目标与运行预算不进入
查询指纹，因此不得删除或手工改写原账号 checkpoint、累计摘要、page 或 search ID；dry-run
应继续显示原前沿并冻结新的数量计划。

运行结束后按顺序读取顶层 `run_summary.json`、冻结状态、child summary 和 SQLite 计数。XHS
配置 schema v2 没有 pool/target `enabled` 开关，显式 runner 命令就是唯一启动动作，不修改配置。
启动前登录失效时对原账号运行 `xhs_login.py`。正式抓取中，搜索连续性阶段出现可见登录要求或
图片验证，以及作者页出现二维码安全验证时，都会保留当前标签页并置前，最多等待操作人处理
600 秒，通过后继续；等待状态写入 `behavior_evidence.operator_verification_events`。系统不自动
点击、识别或绕过验证。搜索 API 明确返回登录已过期时，即使错误被请求重试器包装，
也必须暂停原请求、刷新当前可见页但不关闭任何标签页，并置前最新的小红书页等待操作人恢复；
可见登录 UI 与 self-info API 均恢复后刷新 Cookie/storage state 并重试同一来源页，600 秒超时
才写 `login_required`，且 checkpoint 保持当前页。搜索 API 返回 461/471 时，使用响应中的
`Verifyuuid`、`Verifytype` 和状态码打开平台 `/website-login/captcha` 人工验证页；通过后刷新
同一会话 Cookie 并重试原请求。
正式抓取 BrowserContext 的守卫安装后，任何新标签页都必须立即置前并无条件保留至少 30 秒，
无论它由平台弹出，还是 crawler 为互动、作者主页回退或验证辅助而创建。门禁不依赖页面文本、
验证码识别、滚动是否有位移或创建来源；异常退出、Playwright 退出以及最终 BrowserContext/CDP
清理也必须先等待保护期。浏览器启动时保留的首个主页面可以豁免，启动时已经存在的额外页仍受
保护。其他路径明确识别出的验证页继续执行原有 600 秒人工等待，不得用 30 秒最低保护期缩短。
独立 `xhs_login.py` 是单页登录/复验工具，不安装正式抓取的新标签页守卫。
频控、拒绝访问或环境异常仍立即停止请求并等待操作人决定；不得自动
重试或在同一正式轮次中途换号。

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
主版本必须一致。抓取中若等待人工登录或图片验证，证据还必须包含
`operator_verification_events`；只有对应事件为 `completed` 且后续连续性事件完成，才能恢复
正式抓取。
排查“弹窗被关闭”时还要核对 child 日志中的 `New browser tab opened` 和
`New-tab minimum hold completed`：两者之间必须至少覆盖 30 秒，且浏览器上下文关闭不得早于
后者。不要以日志中没有识别到验证码文本为理由跳过保护期。
失败轮次只要 child summary 已生成，Runner 顶层仍必须读取其中的行为与帖子互动证据；退出码
失败不能把已发生的互动错误汇总为空。

验证码、频控或拒绝访问由可见页面文本判定；通用平台记录冷却。小红书搜索连续性登录/图片
验证和作者页二维码验证保留页面等待人工处理，频控或拒绝访问则记录证据并停止。不要直接扫描整页 HTML 判断
验证码：站点打包脚本和隐藏组件可能包含 `geetest`、`captcha` 或“请先登录”，会产生
假阳性。行为证据缺失或不完整时保留 JSONL 排障，但禁止入库。

小红书不使用通用 `scrapling_throttle.json` 的站点预算或验证码冷却；其独立 runner、租约和
`xhs_guarded` 已承担这些门禁。正式入口发现历史 XHS 通用策略状态时删除该平台条目，其他平台
状态必须保留。不得因接口 461/471 写入通用三小时冷却而阻止操作人完成平台验证。

策略状态中的 `max_requests_per_session` 是自动会话冷却：跨 UTC 日，或从
`last_request_finished_at`（缺失时用 `last_request_at`）起已完整空闲 `cooldown_minutes`，会开启
新会话并清零计数。验证码、频控和封禁写入的其他 `cooldown_reason` 是显式冷却，在有效期内
仍必须停止；不要通过删状态或改计数解除。达到会话上限后的首次拒绝只写入上述既定空闲截止
时间，后续被拒绝的检查不能从检查时刻重新加满一个冷却周期。
`wait_seconds` 对未满整秒的剩余时间必须向上取整，保证 Runner 的 `next_run_at` 不早于
`cooldown_until`，避免在截止前一秒产生无意义的再次拒绝。
当 child 因该门禁返回 `policy_blocked` 时，外层 Runner 必须保留 child 给出的
`wait_seconds` 和原因来计算 `next_run_at`，不能被汇总层的 `behavior_evidence_failed` 降级成
通用 600 秒重试。断点和候选记忆在冷却期间保持不变，到期后再从同一 checkpoint 续跑。

## 结果检查

报告读取顺序固定为：runner 顶层 `run_summary.json` → 对应 execution state → child `summary.json` →
child 的 `image_materialization.manifest_evidence` 所指 manifest → SQLite 与 `data/media` 本地文件；
只有前一层出现失败或计数不一致时才查看平台日志尾部。不要先全文展开 JSONL、响应体或所有图片。

正式结构化任务至少检查：

- 状态文件最终为 `completed`，且真实正式入库运行的五个阶段全部为 `completed`；
- 默认 `target-new-posts` 模式要求 `formal_validation.new_target_met=true`；显式
  `source-exhausted` 模式改为要求 `formal_validation.source_exhausted_met=true`；
- `behavior_validation.ok=true`，且行为与策略平台列表覆盖本轮全部结构化平台；
- 每个平台 `behavior_validation.platforms.<platform>.target_url_ok=true`，证据 URL 对应本轮关键词；
- `formal_validation.behavior_evidence_ok=true`、`policy_evidence_ok=true`；
- 默认数量模式要求 `valid_new_count` 和 `inserted_rows` 都达到 `target_new_posts`；来源耗尽模式
  允许低于目标，但必须真实执行持久化并报告实际计数；
- `valid_existing_count` / `updated_rows` 单独报告且不计入新增目标；
- `import_result.processed_rows`、`inserted_rows`、`updated_rows` 分别存在；
- 命令未使用 `--no-import`，`persistence_verified` 没有以 `skipped` 代替真实入库；
- 冻结计划 `local_image_storage_required=true`，child 命令包含 `--download-images` 和正式
  `--media-root`，不包含 `--get-media`；
- `image_materialization.required=true`、`promotion_required=true`、`complete=true`，并满足
  `candidate_posts == complete_posts`、
  `expected_images == downloaded_images == validated_images == promoted_images + reused_images`；
- `retryable_failures=0`、`terminal_failures=0`、`failures=[]`，manifest 列表、单文件 SHA 和聚合
  `manifest_sha256` 均被 `artifacts_verified` 复验；
- SQLite 中作者粉丝量、发布时间和图片关系符合平台 profile；每张正文图具有连续 index、非空
  项目相对 `local_path`、尺寸、真实 MIME、SHA，实际文件位于 `data/media` 且哈希相等；
- 头像、作者主页、封面、搜索预览、视频、音乐和知乎公式图不出现在正文 manifest、长期目录或
  `content` 图片关系；作者头像只允许保留 `author_avatar` URL 参考，视频只出现在跳过计数中。
- `formal_validation.pagination_evidence` 有连续页级事件；未达目标时，`source_exhausted`
  必须有空页、明确缺失继续 cursor 或 `has_more=false` 的 `adaptive_search_stopped` 事件。只有批次事件而没有停止
  事件的任务按 `runtime_failed` 排查浏览器、登录态、超时或请求异常。
- 抖音第 1 页零候选时还必须检查 `douyin_search_response_observed` 和
  `douyin_empty_first_page_checked`。`empty_page`、`has_more_false`、页面仍有结果或页面状态不明确，
  都不能作为新鲜第 1 页的耗尽证明。

固定 URL 页面任务只验证该页证据和入库，不得汇报为平台批量目标完成。

### 图片失败分流

| 信号 | 分类 | 处理 |
|---|---|---|
| `image_download_retryable` | 平台会话、临时网络或响应可恢复失败 | 保留当前安全前沿，不写已处理候选；检查登录态和平台日志尾部后从 runner 新开一轮重试 |
| `missing_image_manifest` / `image_manifest_count_mismatch` | staging/manifest 不完整 | 停止入库，核对 child 实际 artifact 和平台 store；禁止手工补空 manifest |
| `image_manifest_identity_mismatch` | URL、平台、帖子、顺序、来源字段或稳定键不一致 | 视为代码/产物版本错误，修复后重跑整帖 |
| `image_path_escape` / `image_file_missing` | 路径边界或文件缺失 | 停止晋升，检查 symlink、清理程序和 artifact 完整性 |
| `image_non_raster_response` / `image_decode_failed` | 返回 HTML/JSON/视频或损坏图片 | 检查登录/验证和 URL 选择；不得改后缀伪装成图片 |
| `image_too_large` | 单文件或解码像素超过安全上限 | 作为终态失败报告；如需改上限必须走代码、测试和治理变更 |
| `image_hash_mismatch` / `image_manifest_metadata_mismatch` | staging 字节与 manifest 不一致 | 停止并保留证据，排查写入竞态或文件篡改 |
| `image_existing_conflict` / `image_promotion_conflict` | staging 整帖目录或长期内容寻址目标已有不同字节 | 停止覆盖，保留两侧证据并排查稳定键、旧文件或并发写入 |
| `image_materialization_missing` / `image_materialization_incomplete` | child 摘要缺少统一图片结果或完成等式失败 | 视为执行器/版本契约错误，不允许 runner 降级完成 |
| `persistence_verified` 失败 | 晋升文件与 SQLite 路径、SHA、MIME、尺寸或计数不一致 | 不 finalize、不推进 checkpoint；从摘要身份逐帖修复并重新执行正式事务 |
