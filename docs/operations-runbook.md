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
来源耗尽策略、自动恢复位置和 child 命令后再正式执行：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --max-jobs 3
```

指定 job 时，正式命令使用与 dry-run 相同的 `--job-key`。正式结构化抓取固定抓到可验证来源耗尽，
不接受数量目标、候选硬上限、停滞停止或完成模式选择参数。

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
缺失的元数据；正文、图片和 `answer_detail`/`article_detail` 来源仍必须来自本次详情访问。若补齐后仍无
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
`--timeout-per-batch` 是传给 MediaCrawler 的基础平台预算；修复总控会在此基础上额外保留正式抓取同款
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

旧租约 schema 不提供运行时兼容。版本切换前先确认全部旧 runner、child、exporter 和账号 profile
Chrome 已退出；控制库初始化随后重建空的精确租约表，并为每条被丢弃的旧互斥行写入
`lease_schema_cutover_discarded`。不要迁移旧 owner、等待其 TTL 或把该切换当成 orphan release；抓取恢复
仍只读取 SQLite checkpoint 及其引用的安全累计摘要。

对已采用精确租约的控制库，先列出租约；`list` 不公开 owner token，只显示其 SHA-256 供审计：

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
child/exporter 进程组以及 argv 中 `--user-data-dir` 精确等于该账号 profile 的 Chrome；随后在
`BEGIN IMMEDIATE` 内再次核对并用 `account_id/run_id/lease_id/owner_token` 删除，rowcount 必须为 1。
不同 host、任一精确残留进程、PID 存在但启动身份不可读、错误身份、错误 owner 或并发漂移都会拒绝；
不得降级使用秒级 `ps lstart`、TTL 或 PID 文件推断死亡。

execution state 是否存在、是否含 `adaptive_search_stopped`、尾批和终态是否完整只写入审计，不再决定
能否释放账号互斥。即使 state 缺失或没有停止事件，只要旧 runner/child/exporter/profile Chrome 已被
精确证明全部死亡，也允许写 `orphan_lease_reconciled` 并只回收该租约。该动作不得补写 state、推进
checkpoint/cursor/seen/campaign、导入或删除旧 staging、写内容 SQLite，或改变账号状态。随后必须先用
同账号、同配置 dry-run，再由新正式轮从最后安全 checkpoint 恢复。

`--start-page`、`--resume-summary` 和 `--recovery-keyword` 只用于用户明确批准的人工恢复。优先修复
自动 checkpoint；不得删除数据库记录后猜页码续跑。平台 cursor 细节见对应平台文档。

## 一次性配置

新关键词、平台组合、顶部刷新或超时调整使用 `config/one_off/` 的派生配置，不直接修改长期主配置。
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

微博必须在桌面 SSO 页完成人工登录，再回到移动端刷新 Cookie；最终只有移动接口同时返回
`login=true` 和有效 `uid` 才成功。`WBPSESS` 不能单独作为成功证据。

### 小红书

小红书不使用通用 warmup。完整流程见[小红书 Workflow](platforms/xhs.md)，这里只给出入口：

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py list
python scripts/xhs_login.py \
  --account-id xhs-a01 \
  --timeout-seconds 600
python scripts/xhs_runner.py \
  --dry-run \
  --target-key qingdao_travel \
  --account-id xhs-a01
```

确认计划后，用相同账号、目标和互动参数移除 `--dry-run`。不得在轮次中途自动换号、关闭
验证页或手工修改账号级 checkpoint。

若正式轮以完整的 `platform_security_limit_300011` 终态停止，且精确租约释放审计和 SQLite
checkpoint 对账均通过，可启动 30 分钟定时续跑：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --target-key qingdao_travel \
  --account-id xhs-a01 \
  --retry-on-300011
```

等待期间不占账号租约；到期后的每次尝试都是完整正式轮并取得新租约。再次出现同样的完整 300011
时，从该轮结束再等 30 分钟，正式轮成功且精确释放后停止。其他错误、终态/停止事件不完整、checkpoint
不一致或租约仍存在时立即停止，不自动换号、补状态或导入失败产物。`--retry-on-300011` 不得与
`--dry-run` 同用；重启同一命令会按 SQLite 最新终态的 `finished_at` 恢复计时。控制状态位于
`data/runtime/xhs/retry_states/`，相同 target/account 由控制器 `flock` 保证单实例。

小红书的短 Cookie 不能脱离设备连续性单独续期。`xhs_login.py` 与 `xhs_runner.py` 必须复用同一账号
profile、加密 storage state、原生窗口参数和 Chrome 运行环境；关闭前快照还必须包含平台的
sessionStorage 设备标识。启动时 profile 的现存状态优先，快照只补缺，避免旧短 Cookie 把刚刷新
的 profile 回滚成“新设备”会话。

## 浏览器与行为证据

Chrome HOME、Crashpad 和缓存由 `scripts/browser_runtime.py` 放在 `data/runtime/`，Chromium 使用 mock
keychain。浏览器失败需区分：

| 状态 | 含义 |
|---|---|
| `runtime_permission_error` | 运行目录或进程权限错误 |
| `browser_launch_failed` | Chrome/CDP 启动失败 |
| `browser_target_closed` | 启动后页面、context 或浏览器关闭 |
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

稳定错误码、优先级、重试和媒体事务的完整定义分别见[正式契约](formal-crawl-contract.md)与
[数据持久化](data-persistence.md)。
