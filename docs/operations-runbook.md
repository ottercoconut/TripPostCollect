# 正式抓取运行手册

本文只维护操作命令、恢复动作和检查顺序。完成模式、成功谓词、错误分类与候选记忆语义以
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
  --completion-mode target-new-posts \
  --max-jobs 5
```

只验证一个已启用 job：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --job-key <job_key> \
  --completion-mode target-new-posts
```

dry-run 会同步调度表并写报告和 execution state，但不访问平台或写内容表。预期只有
`plan_frozen=completed`，其余四阶段保持 `frozen`，任务状态为 `planned`。确认配置、关键词、完成模式、
目标、候选上限、自动恢复位置和 child 命令后再正式执行：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --completion-mode target-new-posts \
  --max-jobs 3
```

指定 job 时，正式命令使用与 dry-run 相同的 `--job-key` 和 `--completion-mode`。只有用户明确要求
抓完当前关键词结果时，才把两条命令都改为 `--completion-mode source-exhausted`；不得用超大目标或
候选上限模拟来源耗尽。

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
  --candidate-hard-limit 20 \
  --target-new-posts 0 \
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

普通新增抓取依赖事务和内容寻址幂等，不要求每轮复制整库。批量修复、清理或人工 SQL 写默认库前，
必须建立 SQLite 一致性备份并记录 SHA-256：

```bash
mkdir -p data/backups
sqlite3 data/trippostcollect.sqlite ".backup 'data/backups/trippostcollect-before-<run_id>.sqlite'"
shasum -a 256 data/backups/trippostcollect-before-<run_id>.sqlite
```

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
  --job-key <job_key> \
  --completion-mode target-new-posts
```

核对规则：

- 抖音深层恢复必须同时有 page、offset 和非空 search ID；小红书必须同时有 page 和 search ID。
- 首次执行从第 1 页开始且顶部刷新为 0；有 checkpoint 后才执行顶部刷新。
- `last_summary_path` 非空时，对应摘要及 JSONL 必须存在，否则冻结应失败。
- child 结束后，摘要中的前沿、SQLite checkpoint 和 `last_run_id` 必须一致。
- 未完成尾批不得推进；边界页允许下轮重取并依靠已知 ID 前置过滤。
- `--no-import` 不得写 checkpoint。

`--start-page`、`--resume-summary` 和 `--recovery-keyword` 只用于用户明确批准的人工恢复。优先修复
自动 checkpoint；不得删除数据库记录后猜页码续跑。平台 cursor 细节见对应平台文档。

## 一次性配置

新关键词、临时数量、平台组合或候选扩容使用 `config/one_off/` 的派生配置，不直接修改长期主配置。
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
     --job-key <job_key> \
     --completion-mode target-new-posts
   ```

4. 正式轮使用完全相同的配置、job 和完成模式，只移除 `--dry-run`。
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
  --account-id xhs-a01 \
  --completion-mode target-new-posts
```

确认计划后，用相同账号、目标、互动参数和完成模式移除 `--dry-run`。不得在轮次中途自动换号、关闭
验证页或手工修改账号级 checkpoint。

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
证据细节只在其平台文档维护。

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
- 完成模式对应的 `new_target_met` 或 `source_exhausted_met` 为 true；
- `behavior_validation.ok`、行为和策略门禁通过；
- `processed_rows`、`inserted_rows`、`updated_rows` 分开报告，数量模式实际新增达到目标；
- 未使用 `--no-import`，`persistence_verified` 没有被诊断性 `skipped` 代替；
- `image_materialization.required/promotion_required/complete` 均为 true，失败数组和失败计数为空；
- manifest 身份、SHA 与计数已由 `artifacts_verified` 复验；
- SQLite 正文图片具有连续 index、项目相对路径、尺寸、MIME、SHA，文件位于 `data/media`；
- 分页证据包含停止事件；只有批次事件而无停止事件时按运行失败处理。

固定 URL 页面任务成功只代表该页面证据完成，不代表平台批量目标完成。

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
