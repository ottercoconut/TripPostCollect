# 正式抓取运行手册

本文只维护操作命令、恢复动作和检查顺序。来源耗尽成功谓词、错误分类与候选记忆语义以
[正式抓取执行契约](formal-crawl-contract.md)为准；平台特有步骤以 `docs/platforms/` 为准。

## 通用平台执行顺序

B站、微博、抖音和知乎只从 `crawl_runner.py` 正式执行。先验证登录态：

```bash
source .venv/bin/activate
python scripts/login_warmup.py --targets all
```

冻结到期任务计划：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --max-jobs 5
```

只验证一个已启用 job：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --job-key <job_key>
```

dry-run 会同步调度表并写报告和 execution state，但不访问平台或写内容表。预期只有
`plan_frozen=completed`，其余四阶段保持 `frozen`，任务状态为 `planned`。确认配置、关键词、
来源耗尽策略、自动恢复位置、child 命令及 `scheduling` 后再正式执行：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --max-jobs 3
```

通用 runner 默认使用 `--max-parallel-platforms 4`：本轮选中的不同平台并行，同一平台 job 串行；
`--max-jobs` 只控制选择数量。根摘要的 `scheduling.platform_lane_count`、`planned_workers`、
`effective_workers`、`execution_started`、`parallel_execution` 和 `lane_keys` 必须与冻结计划及运行类型
一致；dry-run 的 `effective_workers=0`、`execution_started=false`。资源诊断或逐平台排障需要串行时使用：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --max-jobs 3 \
  --max-parallel-platforms 1
```

并行只覆盖 B站、微博、抖音和知乎通用入口；不得把小红书命令、账号租约或 profile 加入同一调用。
一个平台失败时读取该任务状态并停止其后续阶段，其他平台继续，最终摘要仍按调度选择顺序排列。

指定 job 时，正式命令使用与 dry-run 相同的 `--job-key`。正式结构化抓取固定抓到可验证来源耗尽，
不接受数量目标、候选硬上限、停滞停止或完成模式选择参数。

`timeout_per_platform` / `--timeout-per-platform` 是“无持久进展看门狗”，不是从进程启动累计的总运行
时限。execution state 事件、内容 JSONL、图片 manifest 或行为证据任一发生变化都会重置计时；因此只要
来源耗尽抓取仍在推进，就允许总运行时间超过该值。启动阶段另保留行为预算。所有受监控证据持续不变
达到阈值时才记录 `adaptive_search_stopped(runtime_failed, no_progress_timeout)`，尾批标记为不完整，禁止
导入并从最后完整批次恢复。看门狗收束先只向监督进程发送一次 SIGTERM，最多等待 20 秒让 child、
exporter 和浏览器退出；仍有进程组成员才发送 SIGKILL。摘要中的 `timeout_reason`、
`last_progress_age_seconds`、`forced_termination` 和 `timeout_state_event` 用于复核该路径。

通用 runner 的操作人中断（终端 Ctrl+C、对 runner 发 SIGTERM 或终端挂断 SIGHUP）只锁存首个信号，
锁存保持到进程退出，后续信号不改变原因与退出码；SIGQUIT 不处理。本段只描述 `crawl_runner.py`
通用路径；小红书租约链路仍由 `xhs_runner.py` 的 LeaseGuard 转发，中间层保持默认 SIGTERM 处理，
不使用下述转换，其中断日志与收尾见 #58。

- 信号路径：`mediacrawler_crawl.py` 中间层和 worker 各在独立会话中运行，终端信号只到 runner。runner
  只向运行中的中间层发一次 SIGTERM；中间层入口把它转为可捕获中断（KeyboardInterrupt 子类，落在
  asyncio 任务内也会向外抛），再只向 worker 发一次 SIGTERM，worker 在自身清理流程中关闭它启动的
  浏览器。B站在中间层进程内运行，没有 worker，由中间层自身的退出流程关闭浏览器。中间层最多等
  20 秒让 worker 进程组退出，runner 最多等 40 秒让中间层进程组退出；超时才对中间层进程组 SIGKILL，
  并按中间层登记在 execution state 旁的 `<job_key>.json.process-groups.jsonl`（含启动标识）对仍匹配的
  worker 进程组 SIGKILL。
- 遗留风险：CDP Chrome 由 `runtime/browser_launcher.py` 以 `setsid` 自成会话、带 `--remote-debugging-port`
  启动，stdio 接 `/dev/null`，既不在中间层进程组，也不在登记的 worker 组内。正常收束时由 worker 清理
  流程关闭；一旦走到强杀兜底，它不会因控制端断开而自行退出，launcher 也不再清理，会一直残留，须
  人工处理。B站在中间层进程内经 Playwright pipe 启动的浏览器，在中间层被强杀后随 pipe 断开退出。
  systemd 等对整个 cgroup 同时发 SIGTERM 的场景，worker 与 Chrome 会直接收到信号，不符合“逐层一次”，
  worker 可能因第二次信号跳过清理。
- 强杀兜底后的残留核对与清理：先确认没有仍在运行的通用 runner、executor、worker 或 warmup（复用迁移
  第 1 步的进程模式，但不加 `-l`，不打印完整命令行），再只读列出使用项目登录资料目录的 Chrome PID。
  Chrome 收到的 `--user-data-dir` 是 `core.paths` 解析出的物理绝对路径，所以匹配时用 `$(pwd -P)`，
  不用可能含符号链接的 `$PWD`。profile 位于
  `data/runtime/platform_sessions/<platform>/`（`cdp_profile`，共享 profile 时为 `profile`）。T14 删除 fork 后
  没有进程再使用旧 `tools/MediaCrawler/browser_data/`；该旧备份目录可能仍在磁盘上，不要用于运行：

  ```bash
  pgrep -f "crawl_runner.py|mediacrawler_crawl.py|trippostcollect.platforms.entry|login_warmup.py"
  pgrep -f -- "--user-data-dir=$(pwd -P)/data/runtime/platform_sessions/"
  ```

  第一条无输出时才清理第二条列出的 PID：先 `kill -TERM <pid>...`，等待数秒（例如 `sleep 5`）后用同样的
  `pgrep` 复核，确认仍有残留才 `kill -KILL <pid>...`。小红书临时 profile 不在这些目录，按其平台文档处理，不用此命令。
- 派发：首信号后排队 job 不再派发，不租约、不写 attempt、不启动 child，`crawl_jobs.status` 保持原值
  （通常为 `pending`），仅 execution state 写成中断终态、run_summary 记录为 `retry_wait`。已派发 job 若
  在启动前看到信号也不启动 child。信号前已结束且已被 runner 观察到的平台结果照常验证并保留；child
  退出若在锁存之后才被观察到，按中断处理。
- 终态：被收束 job 的 execution state 写 `runtime_failed:operator_interrupt:<SIGNAL>` 并 finalize 为失败，
  调度表记 `retry_wait/runtime_failed`，中断不累计失败次数；runner 不读取摘要、不更新 campaign、
  不写 `adaptive_search_stopped`。`run_summary.json/.md` 仍写出并标注 `interrupt`，runner 以
  `128+首个信号` 退出。
- 中间层已提交的数据：中间层在 worker 正常结束后会自行提交 checkpoint/seen（`persist_discovery_checkpoint`）。
  信号落在提交之前，checkpoint/seen 不推进，下轮从中断前的安全前沿恢复；落在提交之后、中间层退出
  之前，已提交的 checkpoint/seen 属于真实确认的数据，下轮从该前沿恢复，但 runner 不再更新 campaign，
  campaign 累计摘要与候选计数可能少记本轮。runner 判定完成不读取 campaign 计数，正式完成仍须由
  后续轮次自身取得 `adaptive_search_stopped(source_exhausted)` 证据。
- 日志：两层 child 的 stdout/stderr 在中断后同样落盘，runner 层位于 `<run_dir>/jobs/<job_key>/`，worker
  层位于 child 批次的 `logs/<platform>/`；写盘前两路共享头像审计，任一路命中即两路整体替换。

只同步主配置：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py --sync-only
```

状态位于 `data/runtime/crawl_execution_states/<run_id>/`。不得编辑状态文件补签阶段。

## 诊断入口

单平台字段或图片链路诊断可以直接运行 child，但不构成正式完成证据：

```bash
source .venv/bin/activate
python scripts/mediacrawler_crawl.py \
  --platforms weibo \
  --keyword 青岛旅游 \
  --timeout-per-platform 120 \
  --download-images \
  --media-root temp/diagnostic_media \
  --no-import
```

诊断媒体根只允许项目 `temp/` 子目录。`--no-import` 不写 `data/media`、SQLite 或 checkpoint；不要
传已经禁用的 `--get-media`。`info_collection_benchmark.py` 只用于通用平台容量诊断，不接受小红书，
也不是正式完成证据。

## 运行前预检与备份

正式运行前检查数据库、staging 和媒体目录所在卷：

```bash
df -h data outputs
du -sh data/media outputs/mediacrawler_runs 2>/dev/null
```

空间不足时在 child 启动前停止，不要删除本轮 staging 或长期文件来勉强继续。

正式来源耗尽抓取依赖事务和内容寻址幂等，不要求每轮复制整库。批量修复、清理或人工 SQL 写默认库前，
必须建立 SQLite 一致性备份并记录 SHA-256：

```bash
mkdir -p data/backups
sqlite3 data/trippostcollect.sqlite ".backup 'data/backups/trippostcollect-before-<run_id>.sqlite'"
shasum -a 256 data/backups/trippostcollect-before-<run_id>.sqlite
```

## 通用平台历史详情修复

`scripts/repair_post_details.py` 只处理抖音、微博或知乎中已经存在、但
`raw_sample_json.content_detail_status != detail_observed` 的记录。它不是新内容发现入口：每个 child
只接收冻结清单中的平台 ID 和详情入口，强制 `--no-checkpoint-write`，成功子集仍经过正文、作者粉丝、
图片 staging/manifest、行为策略、长期媒体晋升和 SQLite 事务门禁。修复时逐条保留原记录的关键词，
不会把本轮行为搜索词冒充原始发现词。

知乎 detail 页可能不返回作者粉丝或 answer 创建时间。修复 child 会按平台 ID 从同一 SQLite 旧行读取
已持久化的 `published_at`、`followers_observed`、`author_followers_source` 和粉丝数，仅补齐详情载荷
缺失的元数据。本轮 `followers_observed=true` 且带有数值的粉丝量（包括真实 `0`）始终优先，不得被旧行
覆盖；本轮未观测时，旧行的粉丝数、`followers_observed` 和来源整组回填，保证数值与来源一致。正文、图片和 `answer_detail`/`article_detail` 来源仍必须来自本次详情访问。若补齐后仍无
有效详情，child 将本批标记为 `repair_no_valid_detail` 且不得把搜索载荷直接当作详情成功；无人值守总控
按下文规则记录该批失败并继续后续批次，只有运行级阻断才停止平台总控。

先检查通用平台登录，再按平台分别冻结计划。三个平台必须使用三个独立进程；不要在一个命令中混合：

```bash
source .venv/bin/activate
python scripts/repair_post_details.py \
  --platform douyin \
  --keyword 青岛旅游 \
  --batch-size 20 \
  --dry-run
```

dry-run 会列出当前可执行目标数、拒绝目标样本、批次数、每批 child 命令和冻结状态，但不访问平台、
不备份或写内容。正式运行默认写 `data/trippostcollect.sqlite`，必须显式确认；程序会在第一个 child
前自动建立 SQLite 一致性备份并记录 SHA-256：

```bash
source .venv/bin/activate
python scripts/repair_post_details.py \
  --platform douyin \
  --keyword 青岛旅游 \
  --batch-size 20 \
  --confirm-default-db-repair
```

微博和知乎只替换 `--platform`。单轮会按 `--batch-size` 扫描当前所有可执行待修复记录；
`--max-items N` 用于小批试跑，`--post-id ID` 用于精确重试。详情或图片候选失败只留下该旧记录继续
待修复，成功子集可以入库；运行级阻断（登录、验证码、频控、安全或策略、浏览器整体失败、运行权限
或行为证据失败）以及 SQLite、媒体持久化一致性失败会停止后续批次。
`--timeout-per-batch` 是传给平台 worker 的基础平台预算；修复总控会在此基础上额外保留正式抓取同款
`HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS` 行为预算（当前 240 秒），并对整个进程组执行先 `SIGTERM`、
后 `SIGKILL` 的收束。这样浏览器行为预算不会被父进程过早截断；微博等慢平台可显式提高该参数，
例如 `--timeout-per-batch 1800`，但不得绕过批次、备份和状态门禁。
无人值守模式下，单批超时、child 缺摘要、普通详情运行失败或 `repair_no_valid_detail` 只记录在
批次结果中并跳过该批，随后继续清单中的后续批次；只有上述运行级阻断以及 SQLite、媒体持久化
一致性失败才会停止该平台总控。
通用修复 child 会按冻结目标集合和最终有效集合的差集写
`formal_validation.skipped_candidate_failures`；父摘要同步提供 `candidate_failure_count` 和最多 20 条
`candidate_failures_sample`。`repair_target_no_valid_output` 表示平台子进程没有产生可正式入库的目标记录，
`evidence_source=repair_target_output_difference` 表示不能从现有结构化产物可靠细分为详情或图片原因，
监督程序不得猜测更具体错误。
摘要状态 `completed_with_remaining` 表示本轮已有可验证进展但选中目标仍有残留，不表示库存清零；
继续运行同一平台，直到摘要同时满足 `status=completed`、`all_selected_targets_recovered=true` 和
`remaining_pending_count=0`。child 的 `repair_import_met=true` 只允许成功子集入库，不能代替
`completion_met=true` 或父摘要的完整完成条件。若一轮 `recovered_count=0`，父摘要使用
`post_detail_repair_no_progress` 并失败退出；先按候选失败样本排障，不做无限循环。只有 child 声称成功
入库但 SQLite 或媒体复验不一致时才使用 `post_detail_repair_persistence_not_verified`。

用户在复核有限重试证据后明确表示某些微博、知乎或抖音帖子“不再修复”时，保留原帖和真实详情状态，
把精确帖子登记到 `post_detail_repair_waivers`。登记前必须备份默认 SQLite，并在单一事务中保存原因、
`authorized_by=user`、授权时间及来源 repair run；不得改 `content_detail_status` 或借用 discovery 排除表。
后续自动选择和显式 `--post-id` 都把这些记录以 `post_detail_repair_waived` 拒绝。若本次范围全部已豁免，
摘要为 `no_actionable_repair_targets_all_waived` no-op；监督程序读取
`remaining_waived_pending_count` 与 `remaining_unwaived_pending_count`，不得把 waiver 数量解释为修复成功数。
撤销 waiver 需要用户重新明确指定精确平台帖子 ID，不能批量清空整表。

控制面产物位于：

- `outputs/post_detail_repair/<platform>-<run_id>/run_summary.json`
- `data/runtime/post_detail_repair/<platform>-<run_id>/execution_state.json`
- `data/backups/post_detail_repair/<platform>-<run_id>/trippostcollect.sqlite`

三个独立平台进程可以并行访问平台。长期媒体晋升和 SQLite 导入仍使用项目全局跨进程锁，因此写入
阶段会自动串行；不得绕过该锁或手工改 execution state。小红书继续使用独立的
`scripts/repair_xhs_posts.py`，不能放入此入口。小红书修复入口使用 `--batch-size` 在同一个浏览器会话
内分批；候选的详情、作者或图片重试耗尽后写入 `repair_report` 并继续同批成功项及后续批次。
登录/验证码/频控/安全限制/浏览器整体失败仍停止整轮；SQLite 或媒体一致性门禁也不会因部分成功而
放宽。某个 child 全部候选失败且不存在上述运行级或持久化阻断时，父入口把失败清单作为已完成尝试
持久化到 `xhs_runs.report_json`，不会用非零父进程状态阻断后续修复任务；后续自动选取会跳过已有失败
记录并继续更深库存，操作人仍可用 `--post-id` 显式重试指定记录。

小红书既有记录修复允许一种平台原生完整形态：本轮 `note_detail` 已同时观察到非空 `title` 和至少一张
详情 `image_list` 正文图时，`desc` 为空不记为 `missing_content`。该例外只作用于 repair 模式，不把
标题复制成正文，也不放宽正式发现抓取的正文契约；标题、正文和图片都为空仍是内容缺失。

## 自动恢复与检查

正常 workflow 不手工传页码、cursor 或摘要。runner 按 job/目标、查询指纹及小红书账号自动读取
checkpoint，先有限刷新顶部，再恢复深层前沿。检查控制面时只看计数和坐标，不展开候选 ID 或长 JSON。

通用 checkpoint：

```bash
sqlite3 data/trippostcollect.sqlite \
  "SELECT job_id, platform_key, keyword, resume_page, resume_offset, resume_cursor, status, last_batch_complete, last_stop_reason, last_stop_detail, last_run_id, updated_at FROM crawl_discovery_checkpoints ORDER BY job_id;"
```

通用 seen 与人工排除：

```bash
sqlite3 data/trippostcollect.sqlite \
  "SELECT job_id, platform_key, query_fingerprint, COUNT(*) AS seen_candidates FROM crawl_discovery_seen_candidates GROUP BY job_id, platform_key, query_fingerprint ORDER BY job_id;"
sqlite3 data/trippostcollect.sqlite \
  "SELECT job_id, platform_key, platform_post_id, reason, authorized_run_id, authorized_at FROM crawl_discovery_candidate_exclusions ORDER BY authorized_at;"
```

小红书 checkpoint 与 seen：

```bash
sqlite3 data/trippostcollect.sqlite \
  "SELECT target_key, account_id, keyword, resume_page, resume_search_id, status, last_batch_complete, last_stop_reason, last_run_id, campaign_candidate_count, updated_at FROM xhs_discovery_checkpoints ORDER BY target_key, account_id;"
sqlite3 data/trippostcollect.sqlite \
  "SELECT target_key, account_id, query_fingerprint, COUNT(*) AS seen_candidates FROM xhs_discovery_seen_candidates GROUP BY target_key, account_id, query_fingerprint ORDER BY target_key, account_id;"
```

不重新同步配置地冻结一个任务，用于核对 runner 自动生成的恢复参数：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --no-sync-config \
  --job-key <job_key>
```

核对规则：

- 抖音深层恢复必须同时有 page、offset 和非空 search ID；小红书必须同时有 page 和 search ID。
- 首次执行从第 1 页开始且顶部刷新为 0；有 checkpoint 后才执行顶部刷新。
- `last_summary_path` 非空时，对应摘要及 JSONL 必须存在，否则冻结应失败。
- child 结束后，摘要中的前沿、SQLite checkpoint 和 `last_run_id` 必须一致。
- 未完成尾批不得推进；边界页允许下轮重取并依靠已知 ID 前置过滤。
- `--no-import` 不得写 checkpoint。

先列出精确租约；`list` 不公开 owner token，只显示其 SHA-256 供审计：

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py list
```

从输出复制同一行的 `account_id`、`run_id` 和 `lease_id`，再执行：

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py recover-orphan-lease \
  --account-id <account_id> \
  --run-id <run_id> \
  --lease-id <lease_id>
```

该入口取得每账号 `flock`，核对 lease owner 的 host/boot、PID、启动时间/token 和 PGID，再核对登记的
child/exporter 进程组以及 argv 中 `--user-data-dir` 精确等于该 run 临时 profile 的 Chrome；随后在
`BEGIN IMMEDIATE` 内再次核对并用 `account_id/run_id/lease_id/owner_token` 删除，rowcount 必须为 1。
不同 host、任一精确残留进程、PID 存在但启动身份不可读、错误身份、错误 owner 或并发漂移都会拒绝；
不得降级使用秒级 `ps lstart`、TTL 或 PID 文件推断死亡。

execution state 是否存在、是否含 `adaptive_search_stopped`、尾批和终态是否完整只写入审计，不再决定
能否释放账号互斥。即使 state 缺失或没有停止事件，只要旧 runner/child/exporter/本轮 Chrome 已被
精确证明全部死亡，也允许写 `orphan_lease_reconciled` 并只回收该租约。该动作不得补写 state、推进
checkpoint/cursor/seen/campaign、导入或删除旧 staging、写内容 SQLite，或改变账号状态。随后必须先用
同账号、同配置 dry-run，再由新正式轮从最后安全 checkpoint 恢复。

`--start-page`、`--resume-summary` 和 `--recovery-keyword` 只用于用户明确批准的人工恢复。优先修复
自动 checkpoint；不得删除数据库记录后猜页码续跑。平台 cursor 细节见对应平台文档。

## 一次性配置

新关键词、平台组合、顶部刷新或无进展看门狗调整使用 `config/one_off/` 的派生配置，不直接修改长期主配置。
派生文件必须保留主配置的 defaults 和全部长期 job；否则同步会禁用遗漏的任务。

1. 确认关键词属于青岛范围，记录目标 job 原值。
2. 校验 JSON：

   ```bash
   source .venv/bin/activate
   python -m json.tool \
     config/one_off/<task-config>.json \
     >/dev/null
   ```

3. 同步派生配置并冻结目标 job：

   ```bash
   source .venv/bin/activate
   python scripts/crawl_runner.py \
     --config config/one_off/<task-config>.json \
     --sync-only
   python scripts/crawl_runner.py \
     --config config/one_off/<task-config>.json \
     --dry-run \
     --no-sync-config \
     --job-key <job_key>
   ```

4. 正式轮使用完全相同的配置和 job，只移除 `--dry-run`。
5. 验收后执行主配置 `python scripts/crawl_runner.py --sync-only` 恢复长期调度范围。

派生配置从首次同步至验收完成不得修改，并作为该轮冻结证据保留；不要复用旧 one-off job key。

## 登录

### 通用平台

```bash
source .venv/bin/activate
python scripts/login_warmup.py \
  --targets weibo zhihu bilibili douyin \
  --timeout-seconds 600
```

脚本使用各平台正式持久 profile，必要时等待人工登录，并在关闭、重开同一 profile 后复验。报告位于
`outputs/login_warmup/<run_id>/summary.json` 和 `summary.md`。它不抓内容或写内容表。

四个通用平台的持久 profile 位于 `data/runtime/platform_sessions/<platform>/profile`（`<platform>` 为
`bilibili`、`weibo`、`douyin`、`zhihu`），Cookie 快照位于同级的
`data/runtime/platform_sessions/<platform>/trippostcollect_cookie_snapshot.json`，权限 `0600`；位置只由
`trippostcollect.core.paths` 定义。旧版放在 `tools/MediaCrawler/browser_data/` 下的 profile 需在首次运行前按
下一节迁移；运行期不再检测旧目录，未迁移时会在新位置创建空 profile，需要重新人工登录。新位置残留
迁移中断留下的 `profile.partial`/`cdp_profile.partial` 时，warmup、`crawl_runner.py` 与 worker 仍以
`platform_session_migration_required:<platform>` 拒绝启动。

微博必须在桌面 SSO 页完成人工登录，再回到移动端刷新 Cookie；最终只有移动接口同时返回
`login=true` 和有效 `uid` 才成功。`WBPSESS` 不能单独作为成功证据。

### 小红书

小红书不使用通用 warmup，也没有独立登录命令。逻辑账号槽位不保存平台身份；首次使用前必须显式
登记，runner 不会自动创建。完整流程见[小红书 Workflow](platforms/xhs.md)，这里只给出入口：

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py ensure-slot \
  --account-id xhs-a01
python scripts/xhs_accounts.py list
python scripts/xhs_runner.py \
  --dry-run \
  --target-key qingdao_travel \
  --account-id xhs-a01
```

确认计划后，用相同账号、目标和互动参数移除 `--dry-run`。正式轮创建空临时 profile，并在唯一
Chrome/BrowserContext 内显示二维码供操作人登录；不得读取或保存跨轮 Cookie/storage state，不得在
轮次中途重启浏览器、自动换号、关闭验证页或手工修改账号级 checkpoint。

若正式轮以完整的 `platform_security_limit_300011` 终态停止，且精确租约释放审计和 SQLite
checkpoint 对账均通过，可启动 30 分钟定时续跑：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --target-key qingdao_travel \
  --account-id xhs-a01 \
  --retry-on-300011
```

等待期间不占账号租约；到期后的每次尝试都是完整正式轮，取得新租约并创建空 profile 与新二维码，
不是在原轮次内重启浏览器。再次出现同样的完整 300011 时，从该轮结束再等 30 分钟，正式轮成功且
精确释放后停止。其他错误、终态/停止事件不完整、checkpoint
不一致或租约仍存在时立即停止，不自动换号、补状态或导入失败产物。`--retry-on-300011` 不得与
`--dry-run` 同用；重启同一命令会按 SQLite 最新终态的 `finished_at` 恢复计时。控制状态位于
`data/runtime/xhs/retry_states/`，相同 target/account 由控制器 `flock` 保证单实例。

二维码、手机确认、短信验证码、搜索 API 461/471 与作者页验证共享同一个单调 600 秒人工处理预算；
切换验证页面或验证类型不会重新计时。只有轮初纯未扫码状态下明确显示二维码过期，且连续两次确认
没有登录进展时，程序才可点击二维码组件内刷新控件；一旦观察到扫码、手机确认、短信或安全验证，
人工处理中状态即锁存，本轮禁止二维码组件刷新和整页 reload。程序不打开二维码截图或任何操作系统
图片预览窗口。

登录终态诊断优先读取 child 在异常离开前写出的 `xhs_runtime_terminal`，再读取当次 record 的
`failure_classification`；不得由 stderr 中偶然出现的“login/扫码”字样覆盖结构化终态。SMS 参数错误、
当日额度、SMS 频控、`300011`、`300012` 和人工预算耗尽分别保留精确 `stop_detail`，完整映射见
`docs/platforms/xhs.md`。只有仍停在普通登录/二维码状态且不存在更具体终态时才记为
`login_required`；只有 SQLite 终态事务或线性化提交真实失败时才记为 `terminal_commit_failed`。

可恢复的 API 或导航 transport 中断不会创建新会话、关闭浏览器或重新拉起 Chrome：
当前 Chrome、BrowserContext 和执行原操作的 Page 必须保持不变。从该操作首次可恢复失败起，
共用一份不因重试而重置的、固定 600 秒单调恢复预算，按 2、4、8……30 秒上限退避后重试原操作。
网络恢复后继续同一轮；预算耗尽则由 child 写
`network_recovery_timeout` 并保留最后安全 checkpoint。

父层只对新鲜、结构有效且来自已核验 exporter 进程的 `network_paused` transport 诊断
暂停无持久进展看门狗，再将该状态写入通过认证的 runtime status。暂停会冻结
中断前的剩余量，不会重置为完整时限。同一次连续断网的父层上限为 675 秒，只有明确
`online` 才结束该暂停段；陈旧、格式错误或非 transport 诊断既不获得看门狗时间，也不能
冒充恢复信号。child 首次写出 `network_recovery_timeout` 后，父层只给一次 20 秒终态写入和
进程收束时间；后续同类事件不延长这个窗口。

正文图片字节下载不使用上述 600 秒 API/导航恢复循环，仍按正式契约做候选级有限重试
（当前最多 3 次）。耗尽后记录 `image_download_retryable` 与 `candidate_skipped`，不得伪称曾等待 600 秒。

网络错误文本本身不证明 CDP 已死亡。只有生命周期监视确认 Chrome 进程退出
（`xhs_browser_process_exited`）、CDP browser 意外断开（`xhs_cdp_disconnected_unexpected`）或
BrowserContext 意外关闭（`xhs_browser_context_closed_unexpected`）时，才把对应的 CDP 死亡当作
终端证据。主 Page 自身确已关闭同样是终端错误；以上任一情况都失败当前轮次，不在轮内
重启或替换浏览器。

## T14 非小红书登录资料迁移

T14 起，B站、微博、抖音和知乎的持久 profile 与 Cookie 快照不再放在 fork 目录。旧目录与新位置一一对应：

| 平台 | 旧 profile（`tools/MediaCrawler/browser_data/`） | 新位置（`data/runtime/platform_sessions/`） |
|---|---|---|
| B站 | `bili_user_data_dir` | `bilibili/profile` |
| 微博 | `wb_user_data_dir` | `weibo/profile` |
| 抖音 | `dy_user_data_dir` | `douyin/profile` |
| 知乎 | `zhihu_user_data_dir` | `zhihu/profile` |
| 任一平台 | `cdp_<code>_user_data_dir`（若存在） | `<platform>/cdp_profile` |
| 任一平台 | `<code>_user_data_dir/trippostcollect_cookie_snapshot.json`（复制，旧文件保留） | `<platform>/trippostcollect_cookie_snapshot.json` |

`cdp_` 前缀目录只在 CDP 模式且未声明共享 profile 时使用；正式知乎 CDP 共享普通 profile，微博和抖音
不开 CDP，所以正式轮次只用普通 profile。旧 `cdp_` 目录若存在也要按表迁移。
小红书仍每轮在 `data/runtime/xhs/sessions/` 创建空 session，不在本迁移范围内，也不得迁入任何旧小红书目录。

T14-A 曾在运行期对“旧目录存在而新目录不存在”和新位置残留 `profile.partial`/`cdp_profile.partial` 两种情况
失败关闭。T14 删除批移除了旧目录对照检查，只保留 `.partial` 残留检查（错误码仍为
`platform_session_migration_required`）。运行期只读写新位置：新 profile 不存在时按首登流程创建空目录，
不会回退读取旧目录，也不会提示未迁移，所以必须在首次运行 warmup 或正式 runner 之前完成本节；
若迁移前已生成空的新 profile，第 2 步会因目标已存在而跳过，须先由操作人确认并删除该新 profile 再迁移。以下命令在 macOS 项目根执行，全程不打印 Cookie 内容。第 2、3 步
依赖 bash 语法（进程替换、`read -d ''`），已用 `bash <<'SH' … SH` 包裹，在 macOS 默认的 zsh 中也直接
整段粘贴执行。

1. 前置清点：确认没有正式 runner、executor、worker 或 warmup 在运行，没有 leased 任务，也没有进程
   打开旧 profile。前三条命令都应无输出，第四条应输出 `0`。任一不符时先等待或收束对应轮次，不要
   强行复制。`Singleton*` 计数非 0 说明旧 profile 有浏览器崩溃残留，需人工确认浏览器确已退出；
   本手册不自动删除这些文件。

   ```bash
   pgrep -fl "crawl_runner.py|mediacrawler_crawl.py|trippostcollect.platforms.entry|login_warmup.py"
   sqlite3 -readonly data/trippostcollect.sqlite \
     "SELECT job_key, status FROM crawl_jobs WHERE status = 'leased';"
   lsof +D tools/MediaCrawler/browser_data 2>/dev/null
   find tools/MediaCrawler/browser_data -name 'Singleton*' | wc -l
   ```

2. 逐平台迁移：快照与 profile 都只复制、不移动，旧目录（含其中的快照）原样保留为备份。若本批被回退，
   旧代码仍从旧位置读取快照与 profile，所以旧位置不得缺少任何文件。
   - 脚本用 `set -eu -o pipefail`，但不依赖 `set -e` 在函数和 `&&` 链中的隐式语义：`mkdir`、`ditto`、
     `rm`、`touch`、`cp`、`mv` 每一步都显式写 `|| fail`，`fail` 打印原因后 `exit 1` 终止整个脚本；
     第一个出错的平台之后不再处理任何平台。
   - 每个平台先做前置检查：新位置存在 `profile.partial`、`cdp_profile.partial` 或
     `trippostcollect_cookie_snapshot.json.partial`（上次中途失败的残留）时，提示删除这些 **新侧**
     `.partial` 后重跑，并非零退出。目标已存在时打印跳过提示，不覆盖，保持重跑幂等。
   - 新建的 `<platform>` 目录用 `mkdir -p -m 700`：`-m` 只作用于最后一级，即 `<platform>` 本身，
     不改上级目录；目录已存在时不改动其权限。
   - profile 用 `ditto` 而不是 `cp -Rp`：`ditto` 默认保留权限、时间、扩展属性、ACL 和 Chrome 的
     `Singleton*` 符号链接，并且语义固定为“把源目录内容复制到目标目录”，不受尾部 `/` 影响。先复制到
     `<目标>.partial`，删除随之复制进来的快照副本（新代码只读同级快照），再用 `touch -r` 恢复根目录 mtime。
   - 快照用 `cp -p` 先复制到同级 `.partial` 再 `mv` 落地，保留 `0600` 权限、属主与时间。拒绝覆盖的方式是
     显式 `[ -e 目标 ]` 判断后跳过，不调用 `cp`（不用 `cp -n`，因为它静默跳过且不报错）。
   - 落地顺序：两类 profile 的 `.partial` 与快照全部准备成功、快照先落地，最后才把 `profile.partial`
     改名为 `profile`。新 `profile` 出现是唯一的“提交点”：之前任何一步失败时 `profile` 都不存在
     （有 `.partial` 残留时运行期拒绝启动；尚未生成 `.partial` 时运行期会以空 profile 首登，因此失败后先修复
     并重跑第 2 步，再运行 warmup 或 runner）；若反过来先落地 profile、后复制快照失败，
     运行期会接受一个没有快照的新 profile，因此不采用该顺序。
   - 普通 profile 与 `cdp_` profile 各自独立判断，只有 `cdp_` 旧目录时也会迁移。

   <!-- t14-migrate:step2 -->
   ```bash
   bash <<'SH'
   set -eu -o pipefail
   snap=trippostcollect_cookie_snapshot.json
   legacy_root=tools/MediaCrawler/browser_data
   fail() {
     echo "FAILED: $*" >&2
     exit 1
   }
   for pair in bili:bilibili wb:weibo dy:douyin zhihu:zhihu; do
     code=${pair%%:*}
     platform=${pair#*:}
     old=$legacy_root/${code}_user_data_dir
     old_cdp=$legacy_root/cdp_${code}_user_data_dir
     new=data/runtime/platform_sessions/$platform
     if [ ! -d "$old" ] && [ ! -d "$old_cdp" ]; then
       echo "skip $platform: no legacy profile"
       continue
     fi
     for leftover in "$new/profile.partial" "$new/cdp_profile.partial" "$new/$snap.partial"; do
       if [ -e "$leftover" ] || [ -L "$leftover" ]; then
         fail "$platform: $leftover left by an earlier run; delete the new-side .partial entries and rerun"
       fi
     done
     mkdir -p -m 700 "$new" || fail "$platform: mkdir"
     staged=()
     for kind in "$old:profile" "$old_cdp:cdp_profile"; do
       src=${kind%:*}
       dst=$new/${kind##*:}
       [ -d "$src" ] || continue
       if [ -e "$dst" ]; then
         echo "skip $platform ${kind##*:}: target exists, not overwritten"
         continue
       fi
       ditto "$src" "$dst.partial" || fail "$platform ${kind##*:}: ditto"
       rm -f "$dst.partial/$snap" || fail "$platform ${kind##*:}: rm snapshot copy"
       touch -r "$src" "$dst.partial" || fail "$platform ${kind##*:}: touch"
       staged+=("$dst")
     done
     if [ -f "$old/$snap" ]; then
       if [ -e "$new/$snap" ]; then
         echo "skip $platform snapshot: target exists, not overwritten"
       else
         cp -p "$old/$snap" "$new/$snap.partial" || fail "$platform snapshot: cp"
         mv "$new/$snap.partial" "$new/$snap" || fail "$platform snapshot: mv"
       fi
     fi
     for dst in ${staged[@]+"${staged[@]}"}; do
       mv "$dst.partial" "$dst" || fail "$platform: mv $dst"
     done
     echo "migrated $platform"
   done
   SH
   ```
   <!-- /t14-migrate:step2 -->

   任一步失败时脚本以非零状态停止并打印 `FAILED: <平台> <步骤>`。按提示只删除新侧 `.partial`（以及确认
   为复制错误的新侧目录）后重跑；旧目录与旧快照始终不动。

3. 校验：逐相对条目比较每对 profile 目录。只在旧侧排除快照（新侧已删掉该副本）；新侧不排除，若新
   profile 里残留快照副本会表现为条目集合不同。条目集合用
   `find -print0 | sort -z` 比较；每个条目用 BSD `stat -f '%HT %p %z %m %Su:%Sg'` 比较类型、权限、大小、
   mtime 与属主属组，普通文件再用 `cmp -s` 逐字节比较。符号链接只比较类型、属主属组与链接目标
   （`readlink`），不比较链接自身的权限位、大小和 mtime：macOS 上 `ditto`/`cp` 不保证保留链接自身的
   mtime 与权限位，而 Chrome 只关心链接目标（如 `SingletonLock` 指向的主机与进程），这些属性对 profile
   没有意义。目录与普通文件仍比较全部字段。每组输出 `mismatch_detail=<类型>:<字段>=<次数>,…`
   （类型为 file/dir/link，字段为 type/mode/size/mtime/owner/content/target/missing，无不一致时为 `none`），
   只给分类计数、不含路径；类型不同的条目只记 `<类型>:type`，不再比较其余字段，按不一致处理而非命令错误。
   `extra=N` 是新侧多出的条目数（例如新 profile 里残留的快照副本），N 不为 0 同样计入不一致。
   `cd`、`find`、`stat`、`cmp`、`readlink`、`shasum` 自身的报错不输出（避免把路径打到终端），出错时只看
   `FAILED:` 行。全段不使用 here-string 或内层 heredoc（bash 5.1 前二者借临时文件实现）。另对照（不计符号链接）
   `find <dir> -perm +077 | wc -l` 的 group/other 权限条目数。profile 根本身单独核对类型、权限、属主与
   mtime：新旧根权限必须相同（`root_mode_same`）；`root_mode_700` 只作信息输出，不计入失败（旧根若是 755，
   `ditto` 会原样复制，不应要求操作人改权限）。快照比较大小、权限、mtime、属主属组与 SHA-256，并要求新快照为
   `-rw-------`。输出只含平台名、目录种类、布尔值和计数，不打印路径、内容、哈希或 Cookie。
   脚本不创建临时文件（条目列表经进程替换读入数组，列表不完整即判失败），只读不写。
   `stat`、`find`、`cmp`、`readlink`、`shasum` 等任一命令出错都以非零状态立即停止并打印 `FAILED:` 行；此时前面已通过的平台
   可能已经打印了“一致”，所以 **最终结论只以退出码和最后的 `problems=N` 为准**。全部命令成功时脚本打印
   `problems=N`（不一致项数），N 不为 0 时以状态 2 退出。脚本结束后紧接着执行 `echo "exit=$?"` 确认退出码。

   <!-- t14-migrate:step3 -->
   ```bash
   bash <<'SH'
   set -eu -o pipefail
   snap=trippostcollect_cookie_snapshot.json
   legacy_root=tools/MediaCrawler/browser_data
   end_mark=__T14_ENTRIES_COMPLETE__
   problems=0
   fail() {
     echo "FAILED: $*" >&2
     exit 1
   }
   yes_no() {
     if [ "$1" = "$2" ]; then echo yes; else echo no; fi
   }
   entries() {
     if [ "$2" = old ]; then
       (set -o pipefail; cd "$1" 2>/dev/null && find . -mindepth 1 ! -path "./$snap" -print0 2>/dev/null | LC_ALL=C sort -z)
     else
       (set -o pipefail; cd "$1" 2>/dev/null && find . -mindepth 1 -print0 2>/dev/null | LC_ALL=C sort -z)
     fi
   }
   listing() {
     # 进程替换的退出码不会传给读取方：只有 entries 成功才追加结束标记，读取方据此判定成败。
     if entries "$1" "$2"; then printf '%s\0' "$end_mark"; fi
   }
   load_list() {
     local rel complete=no
     loaded=()
     while IFS= read -r -d '' rel; do
       if [ "$rel" = "$end_mark" ]; then
         complete=yes
       else
         loaded+=("$rel")
       fi
     done < <(listing "$1" "$2")
     [ "$complete" = yes ] || fail "list $2 entries"
   }
   kind_of() {
     if [ -L "$1" ]; then echo link
     elif [ -d "$1" ]; then echo dir
     elif [ -f "$1" ]; then echo file
     else echo other
     fi
   }
   split_stat() {
     # 用参数展开拆分 `|` 分隔的字段；不用 here-string（bash 5.1 前借临时文件实现）。
     local rest=$1
     f_type=${rest%%|*}; rest=${rest#*|}
     f_mode=${rest%%|*}; rest=${rest#*|}
     f_size=${rest%%|*}; rest=${rest#*|}
     f_mtime=${rest%%|*}; f_owner=${rest#*|}
   }
   compare_tree() {
     local label=$1 a=$2 b=$3 same=yes total=0 bad=0 missing=0 extra i rel kind sa sb la lb rc perm_a perm_b
     local keys entry_bad detail ta pa za ma oa
     local -a old_list new_list
     # 不用临时文件：条目列表经进程替换读入数组，列表不完整时 load_list 已 fail。
     load_list "$a" old
     old_list=(${loaded[@]+"${loaded[@]}"})
     load_list "$b" new
     new_list=(${loaded[@]+"${loaded[@]}"})
     if [ "${#old_list[@]}" -ne "${#new_list[@]}" ]; then
       same=no
     else
       i=0
       while [ "$i" -lt "${#old_list[@]}" ]; do
         [ "${old_list[$i]}" = "${new_list[$i]}" ] || same=no
         i=$((i + 1))
       done
     fi
     keys=""
     for rel in ${old_list[@]+"${old_list[@]}"}; do
       total=$((total + 1))
       kind=$(kind_of "$a/$rel") || fail "classify entry"
       if [ ! -e "$b/$rel" ] && [ ! -L "$b/$rel" ]; then
         bad=$((bad + 1))
         missing=$((missing + 1))
         keys="$keys$kind:missing "
         continue
       fi
       sa=$(stat -f '%HT|%p|%z|%m|%Su:%Sg' "$a/$rel" 2>/dev/null) || fail "stat old entry"
       sb=$(stat -f '%HT|%p|%z|%m|%Su:%Sg' "$b/$rel" 2>/dev/null) || fail "stat new entry"
       split_stat "$sa"
       ta=$f_type; pa=$f_mode; za=$f_size; ma=$f_mtime; oa=$f_owner
       split_stat "$sb"
       if [ "$ta" != "$f_type" ]; then
         # 类型不同是不一致而非命令错误：不再对其做 readlink/cmp。
         bad=$((bad + 1))
         keys="$keys$kind:type "
         continue
       fi
       entry_bad=""
       [ "$oa" = "$f_owner" ] || entry_bad="$entry_bad$kind:owner "
       if [ "$kind" = link ]; then
         la=$(readlink "$a/$rel" 2>/dev/null) || fail "readlink old"
         lb=$(readlink "$b/$rel" 2>/dev/null) || fail "readlink new"
         [ "$la" = "$lb" ] || entry_bad="$entry_bad$kind:target "
       else
         [ "$pa" = "$f_mode" ] || entry_bad="$entry_bad$kind:mode "
         [ "$za" = "$f_size" ] || entry_bad="$entry_bad$kind:size "
         [ "$ma" = "$f_mtime" ] || entry_bad="$entry_bad$kind:mtime "
         if [ "$kind" = file ]; then
           rc=0
           cmp -s "$a/$rel" "$b/$rel" 2>/dev/null || rc=$?
           [ "$rc" -le 1 ] || fail "cmp entry"
           [ "$rc" -eq 0 ] || entry_bad="$entry_bad$kind:content "
         fi
       fi
       if [ -n "$entry_bad" ]; then
         bad=$((bad + 1))
         keys="$keys$entry_bad"
       fi
     done
     # 新侧多出的条目数：新侧总数减去旧侧条目中在新侧存在的数目（新侧不排除快照）。
     extra=$(( ${#new_list[@]} - (total - missing) ))
     detail=$(printf '%s\n' $keys | LC_ALL=C sort | uniq -c | awk 'NF == 2 {printf "%s%s=%s", sep, $2, $1; sep=","}') \
       || fail "summarize mismatches"
     [ -n "$detail" ] || detail=none
     perm_a=$(find "$a" ! -type l ! -path "$a/$snap" -perm +077 2>/dev/null | wc -l | tr -d ' ') || fail "find old perms"
     perm_b=$(find "$b" ! -type l -perm +077 2>/dev/null | wc -l | tr -d ' ') || fail "find new perms"
     echo "$label: same_entry_set=$same entries=$total mismatched=$bad mismatch_detail=$detail extra=$extra" \
       "group_other_perm=$perm_a/$perm_b"
     if [ "$same" != yes ] || [ "$bad" -ne 0 ] || [ "$extra" -ne 0 ] || [ "$perm_a" != "$perm_b" ]; then
       problems=$((problems + 1))
     fi
   }
   compare_root() {
     local label=$1 a=$2 b=$3 ta tb ma mb oa ob ra rb
     ta=$(stat -f %HT "$a" 2>/dev/null) || fail "stat old root"
     tb=$(stat -f %HT "$b" 2>/dev/null) || fail "stat new root"
     ma=$(stat -f %Lp "$a" 2>/dev/null) || fail "stat old root"
     mb=$(stat -f %Lp "$b" 2>/dev/null) || fail "stat new root"
     oa=$(stat -f %Su "$a" 2>/dev/null) || fail "stat old root"
     ob=$(stat -f %Su "$b" 2>/dev/null) || fail "stat new root"
     ra=$(stat -f %m "$a" 2>/dev/null) || fail "stat old root"
     rb=$(stat -f %m "$b" 2>/dev/null) || fail "stat new root"
     echo "$label: root_type_same=$(yes_no "$ta" "$tb") root_dir=$([ -d "$b" ] && [ ! -L "$b" ] && echo yes || echo no)" \
       "root_mode_same=$(yes_no "$ma" "$mb") root_mode_700=$(yes_no "$mb" 700)" \
       "root_owner_same=$(yes_no "$oa" "$ob")" \
       "root_mtime_same=$(yes_no "$ra" "$rb")"
     if [ "$ta" != "$tb" ] || [ ! -d "$b" ] || [ "$ma" != "$mb" ] || [ "$oa" != "$ob" ] || [ "$ra" != "$rb" ]; then
       problems=$((problems + 1))
     fi
   }
   for pair in bili:bilibili wb:weibo dy:douyin zhihu:zhihu; do
     code=${pair%%:*}
     platform=${pair#*:}
     for kind in "${code}_user_data_dir:profile" "cdp_${code}_user_data_dir:cdp_profile"; do
       old=$legacy_root/${kind%%:*}
       new=data/runtime/platform_sessions/$platform/${kind#*:}
       [ -d "$old" ] || continue
       if [ ! -d "$new" ]; then
         echo "$platform ${kind#*:}: missing"
         problems=$((problems + 1))
         continue
       fi
       # 直接在当前 shell 调用（不放进 $(...)），fail 才能终止整个脚本、计数才能累加。
       compare_root "$platform ${kind#*:} root" "$old" "$new"
       compare_tree "$platform ${kind#*:}" "$old" "$new"
     done
     old_snap=$legacy_root/${code}_user_data_dir/$snap
     new_snap=data/runtime/platform_sessions/$platform/$snap
     [ -f "$old_snap" ] || continue
     if [ ! -f "$new_snap" ]; then
       echo "$platform snapshot: 不一致"
       problems=$((problems + 1))
       continue
     fi
     sa=$(stat -f '%z %Sp %m %Su:%Sg' "$old_snap" 2>/dev/null) || fail "stat old snapshot"
     sb=$(stat -f '%z %Sp %m %Su:%Sg' "$new_snap" 2>/dev/null) || fail "stat new snapshot"
     mode=$(stat -f %Sp "$new_snap" 2>/dev/null) || fail "stat new snapshot"
     ha=$(shasum -a 256 < "$old_snap" 2>/dev/null) || fail "hash old snapshot"
     hb=$(shasum -a 256 < "$new_snap" 2>/dev/null) || fail "hash new snapshot"
     if [ "$sa" = "$sb" ] && [ "$mode" = "-rw-------" ] && [ "$ha" = "$hb" ]; then
       echo "$platform snapshot: 一致"
     else
       echo "$platform snapshot: 不一致"
       problems=$((problems + 1))
     fi
   done
   echo "problems=$problems"
   [ "$problems" -eq 0 ] || exit 2
   SH
   echo "exit=$?"
   ```
   <!-- /t14-migrate:step3 -->

   通过标准是最后两行为 `problems=0` 与 `exit=0`：此时每行 root 结果除信息项 `root_mode_700` 外均为 `yes`，
   每行 profile 结果为 `same_entry_set=yes`、`mismatched=0`、`mismatch_detail=none`、`extra=0` 且 `group_other_perm` 两侧计数相等，快照为“一致”。
   `exit=1` 表示某个命令出错（看 `FAILED:` 行），`exit=2` 表示存在不一致。非 root 账号复制时属主或属组可能与旧侧不同；即使只有属组不同，也按“不一致”交人工
   确认，不得擅自 `chmod`/`chown`。确认是复制错误时，只删除新侧对应的 `profile`/`cdp_profile` 目录或
   新快照文件后重做第 2 步；旧目录与旧快照始终不动，可重复复制。

4. 逐平台验证登录仍有效，只看报告中的状态字段：

   ```bash
   source .venv/bin/activate
   python scripts/login_warmup.py \
     --targets weibo zhihu bilibili douyin \
     --timeout-seconds 600
   ```

   在 `outputs/login_warmup/<run_id>/summary.md` 中确认每个 target 为 `ok`，profile 列为
   `data/runtime/platform_sessions/<platform>/profile`。若某平台要求重新人工登录，说明新 profile 不含旧登录
   资料（未迁移，或迁移前已生成空 profile），停止该平台并回到第 2 步核对。出现
   `platform_session_migration_required` 说明新位置残留迁移中断的 `.partial`，删除新侧残留后重做第 2 步。

5. 用通用 dry-run 核对冻结计划仍可生成；dry-run 不启动 worker，计划命令是 executor 调用：

   ```bash
   source .venv/bin/activate
   python scripts/crawl_runner.py \
     --dry-run \
     --max-jobs 5
   ```

   worker 命令在正式执行时才构造，首个正式轮结束后在 `logs/<platform>/command.txt` 中确认其为
   `python -P -m trippostcollect.platforms.entry ...`；profile 位置以第 4 步报告与下列只读输出为准：

   ```bash
   source .venv/bin/activate
   python - <<'PY'
   from trippostcollect.core import paths
   for key in ("bilibili", "weibo", "douyin", "zhihu"):
       profile = paths.platform_profile_dir(key)
       print(key, profile, profile.is_dir())
   PY
   ```

6. 旧目录 `tools/MediaCrawler/browser_data/` 保留为备份，新代码不再读取，也不要用于运行。T14 删除批已合并，
   fork 子模块不再受 Git 跟踪；拉取后若旧 fork 工作树（含 `browser_data`）仍在磁盘，`.gitignore` 已忽略
   `/tools/MediaCrawler/`，它仅作回退备份。旧目录中的
   快照与 profile 一起作为回退备份（回退到 T14 之前的代码时旧代码仍从这里读取）；迁移后各平台首个正式轮
   成功后，由操作人删除整个旧 `tools/MediaCrawler/` 目录；删除前再次执行第 1 步的进程检查。

### 拉取 T14 删除批（T14-C）

前提：先在 T14-A 版本（仍含 fork gitlink 的 `main`）上按第 1–5 步完成迁移并核对通过，再拉取 T14-C。
T14-C 同批改动了冻结资产 `docs/crawl-architecture.md` 与 `docs/data-persistence.md`（并更新登记哈希）；本机
这两份文件带不可变标志（macOS `uchg`，Linux `chattr +i`）时，`git pull` 无法替换它们，fast-forward 会中途
停下、工作树只更新一部分。按下列顺序执行，不要跳步：

1. 确认没有 runner、executor、worker 或 warmup 在运行（命令应无输出），并确认第 1–5 步已在 T14-A 版本上
   完成并核对。建议另把旧 profile 备份到仓库外（含登录态，目标目录只允许本人读写，不得放进仓库或同步盘）：

   ```bash
   pgrep -fl "crawl_runner.py|xhs_runner.py|mediacrawler_crawl.py|trippostcollect.platforms.entry|login_warmup.py"
   mkdir -m 700 /仓库外绝对路径/tpc-browser-data-backup
   ditto \
     tools/MediaCrawler/browser_data \
     /仓库外绝对路径/tpc-browser-data-backup/browser_data
   ```

   Linux 没有 `ditto`，用 `cp -a` 代替。

2. 在当前 `main`（拉取前）验证冻结资产，必须通过：

   ```bash
   source .venv/bin/activate
   python scripts/verify_frozen_files.py
   ```

3. 列出本次拉取会改动、且本机带不可变标志的文件，记下清单供第 5 步恢复。T14-C 改动两份冻结文档和
   登记文件 `config/frozen_files.json`（同批更新登记哈希）；登记文件本身不在冻结登记中，但本机若也给它设了
   标志，同样会挡住拉取。macOS 看 `ls -lO` 的标志列是否含 `uchg`，Linux 看 `lsattr` 的属性是否含 `i`：

   ```bash
   git fetch origin
   git diff --name-only HEAD..origin/main
   ls -lO \
     docs/crawl-architecture.md \
     docs/data-persistence.md \
     config/frozen_files.json
   ```

   Linux 把 `ls -lO` 换成 `lsattr`。只对清单中的文件解除标志。macOS（三份都带 `uchg` 时）：

   ```bash
   chflags nouchg \
     docs/crawl-architecture.md \
     docs/data-persistence.md \
     config/frozen_files.json
   ```

   Linux（只有两份冻结文档带 `i` 时）：

   ```bash
   sudo chattr -i \
     docs/crawl-architecture.md \
     docs/data-persistence.md
   ```

4. 确认未设置 `submodule.recurse`（应无输出；若输出 `true`，先移除该设置再拉取），然后只做快进拉取，
   不带 `--recurse-submodules`：

   ```bash
   git config --get submodule.recurse
   git pull --ff-only
   ```

   拉取若报错停下，先看 `git status --short`，不要用 `reset --hard`、`clean` 或 `checkout -- .` 处理，
   把输出交人工确认。

5. 按第 3 步记下的清单恢复不可变标志（不给原本没有标志的文件新加），再重新验证，必须通过。macOS：

   ```bash
   chflags uchg \
     docs/crawl-architecture.md \
     docs/data-persistence.md \
     config/frozen_files.json
   source .venv/bin/activate
   python scripts/verify_frozen_files.py
   ```

   Linux：

   ```bash
   sudo chattr +i \
     docs/crawl-architecture.md \
     docs/data-persistence.md
   source .venv/bin/activate
   python scripts/verify_frozen_files.py
   ```

6. 不要运行 `git submodule deinit tools/MediaCrawler`：它会删除旧子模块工作树及其中的 `browser_data`
   备份。拉取后留在磁盘上的 `tools/MediaCrawler/` 已被 `.gitignore` 忽略，按上文第 6 步保留和删除。

## 浏览器与行为证据

Chrome HOME、Crashpad 和缓存由 `src/trippostcollect/runtime/browser_runtime.py` 放在 `data/runtime/`，Chromium 使用 mock
keychain。浏览器失败需区分：

| 状态 | 含义 |
|---|---|
| `runtime_permission_error` | 运行目录或进程权限错误 |
| `browser_launch_failed` | Chrome/CDP 启动失败 |
| `browser_target_closed` | 已启动的主 Page 真实关闭，或确认 Chrome 进程退出、CDP browser 断开、BrowserContext 关闭；本轮不重启 |
| `network_paused` | 可恢复网络中断；同一页面内退避，浏览器保持运行 |
| `network_recovery_timeout` | 600 秒内未恢复；本轮失败并保留安全 checkpoint |
| `parent_network_pause_timeout` | 同一次连续 `network_paused` 达到 675 秒，父层收束失联 child |
| `parent_network_terminal_unwind_timeout` | child 首次报告网络恢复耗尽后，20 秒内未完成终态写入和收束 |
| `runtime_status_startup_timeout` / `runtime_status_stale` | 认证心跳未按监督窗口推进 |
| `login_required` | 平台明确要求登录 |
| `captcha_detected` | 平台安全验证或验证码 |

通用平台使用 `social_high_risk`，小红书使用 `xhs_guarded`。正常摘要至少要求
`behavior_validation.ok=true`，并包含事件、运行时指纹、可见阻断标记、截图和策略证据。验证码判断
使用可见页面状态，不扫描整页 HTML 中的隐藏脚本字符串。小红书的标签页保护、人工验证等待和互动
证据细节只在其平台文档维护。监督进程不得在小红书内部搜索页就绪门禁完成前，仅因窗口仍在加载就
关闭浏览器；白屏先读取 behavior evidence 同目录的 `behavior_evidence.navigation.json`。正式启动会先
预热 `/explore`，搜索页面壳持续为空时只做一次同路由恢复，仍失败再由 runner 正常写摘要并释放租约。
搜索 API 的 461/471 人工验证页同时检查子 frame 可见文本；英文 `Requests too frequent`
按频控处理，不得因顶层 body 为空或验证标题消失而误判通过。

小红书父层不设置整轮墙钟超时；它只监督 child 写出的认证心跳。启动宽限为 120 秒，心跳陈旧阈值
为 60 秒，每 5 秒检查一次；长调度间隙只允许同一 sequence 一次 30 秒恢复宽限。网络暂停和最终摘要
写入期间也必须继续心跳，避免把仍健康的 child 误杀。正常结束、可捕获异常和 SIGINT/SIGTERM 均须
在摘要中保留精确 child/exporter/Chrome 生命周期、临时 session 删除及租约释放证据；无法证明进程
消失或目录已删除时保留租约并明确延期清理。

## 结果检查

按以下顺序读取，前一层没有异常时不要展开下一层的大文件：

```text
runner run_summary.json
  -> execution state
  -> child summary.json
  -> image_manifest.jsonl 的计数与短样本
  -> SQLite 与 data/media
  -> 必要时 logs/*.log 最后 40 行
```

正式任务至少确认：

- 顶层任务为 `completed`，五个冻结阶段全部 `completed`；
- `source_exhausted_met=true`，并存在对应的 `adaptive_search_stopped(source_exhausted)`；
- `behavior_validation.ok`、行为和策略门禁通过；
- `processed_rows`、`inserted_rows`、`updated_rows` 分开报告，并与 SQLite 实际事务结果一致；
- 未使用 `--no-import`，`persistence_verified` 没有被诊断性 `skipped` 代替；
- `image_materialization.required/promotion_required/complete` 均为 true，失败数组和失败计数为空；
- manifest 身份、SHA 与计数已由 `artifacts_verified` 复验；
- SQLite 正文图片具有连续 index、项目相对路径、尺寸、MIME、SHA，文件位于 `data/media`；
- 本轮新 JSONL、摘要、child stdout/stderr 和活库不含已知头像键、经这些键证明的头像 URL 或头像
  关系；子进程输出若触发整段头像审计标记属于正确清除结果。只核对本轮产物与活库，不为日常验收
  宽泛扫描历史 `outputs/`、备份或冻结证据；
- 分页证据包含停止事件；只有批次事件而无停止事件时按运行失败处理。

固定 URL 页面任务成功只代表该页面证据完成，不代表平台批量来源耗尽。页面证据执行器只记录图片
请求聚合计数，不保存任意图片响应 URL 或响应体；截图仍是整页证据附件，不拆分为图片关系。

### 常见失败分流

| 信号 | 动作 |
|---|---|
| `candidate_skipped` | 核对范围、错误码、尝试次数、seen 和安全前沿；该帖不应入库或计数 |
| `image_download_retryable` | 核对 manifest 尝试次数与平台日志尾部；整帖跳过后继续候选 |
| `missing_image_manifest` / count mismatch | 停止入库，核对 artifact 与平台 store，禁止手工补 manifest |
| identity / metadata / hash mismatch | 视为代码或产物错误，保留证据并重跑整帖 |
| path escape / file missing | 检查路径、symlink、清理程序和 staging 完整性 |
| non-raster / decode / too-large / source-unavailable | 保留终态错误并跳过候选，不改后缀或伪造成功 |
| HTTP 401/403、429、验证码、封禁 | 运行级阻断；保留前沿，不写候选 seen |
| `sqlite_import_failed` | 确认整批数据库和本轮新媒体已回滚，checkpoint/seen/campaign 未推进 |
| `persistence_verified` 失败 | 不 finalize；从摘要身份逐帖核对 SQLite 与文件 |
| `platform_session_migration_required:<platform>` | 迁移中断残留 `.partial`，任务记为 `failed_final`；删除新侧残留后重做“T14 非小红书登录资料迁移”第 2 步，再用 `--job-key <job_key>` 重跑 |

稳定错误码、优先级、重试和媒体事务的完整定义分别见[正式契约](formal-crawl-contract.md)与
[数据持久化](data-persistence.md)。
