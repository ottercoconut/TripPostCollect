# 小红书正式抓取 Workflow

本文是小红书账号、登录、抓取和失败恢复的操作权威。数量与机器成功语义服从
[`formal-crawl-contract.md`](../formal-crawl-contract.md)，当前数值只从 `config/xhs_targets.json`
和 `config/xhs_pool.json` 读取，不在文档中复制。

> 临时运行约束（2026-07-19）：暂时不抓取小红书。暂停只针对登录和正式抓取执行；账号、
> 配置、checkpoint 与候选记忆必须原样保留。解除该约束前，以下执行清单仅作恢复依据，
> 不得实际运行 `xhs_login.py` 或 `xhs_runner.py`，也不得从通用入口绕过暂停。

## 硬边界

- 正式抓取只运行 `scripts/xhs_runner.py`。
- 账号登记和人工状态变更只运行 `scripts/xhs_accounts.py`；登录只运行 `scripts/xhs_login.py`。
- 不把小红书放入 `crawl_runner.py`、`login_warmup.py`、`config/crawl_targets.json` 或
  `info_collection_benchmark.py`。
- 不直接运行 MediaCrawler 完成正式任务，不复用旧 `browser_data` 或明文 storage state。
- `xhs_runner.py --no-import` 只用于诊断；即使顶层状态显示 `completed`，也不满足正式完成判据。
- 必须人工传入 `--account-id`；同一正式轮次不自动选号、换号、解验证、重试或放宽字段。
- 视频跳过；图文必须保存全部正文图片关系。数据库已有记录只能更新，不计新增目标。
- 缺少粉丝数值、观测标记或可信来源的候选无效，不能用默认 `0`、昵称字段或旧缓存降级。

## 唯一执行流

```text
检查配置和账号
  -> 必要时登记账号
  -> 人工登录并完成关闭/重开复验
  -> dry-run 冻结计划
  -> 人工确认本轮账号、目标、数量和互动副作用
  -> 正式运行并申请单账号互斥租约
  -> xhs_guarded 行为阶段
  -> 有 checkpoint 时刷新顶部，再恢复 page + search_id 深层前沿
  -> 已知 ID 详情前去重、自适应搜索、详情、作者粉丝补全和分页
  -> 正式字段校验
  -> child 摘要形成后提交账号级 checkpoint 和未完成累计摘要
  -> 累计达到有效新增目标后一次性写入 SQLite 并清空累计摘要
  -> 加密最新 storage state、删除临时明文、释放租约
  -> 检查顶层摘要、child summary、冻结状态和 SQLite
```

## 1. 执行前检查

先查看账号，不启动抓取：

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py list
```

继续执行必须同时满足：

- 操作人已经指定账号，账号状态为 `active`，且没有活动租约；
- 账号目录与其他账号隔离，`storage_state.enc` 存在且可解密；
- `target_key` 存在，关键词、有效新增目标、候选硬上限、顶部刷新页数、停滞批次和超时符合本轮要求；
- `behavior_profile` 为 `xhs_guarded`，有头浏览器已启用；
- `lease_seconds >= timeout_seconds + 300`；
- 是否执行评论区访问或点赞已经由操作人明确决定；未明确时必须使用默认 `none`；
- pool 与 target 配置使用 schema v2，已删除 `enabled` 字段；出现旧字段必须在冻结前失败，不保留
  兼容门禁。只有显式执行 `xhs_runner.py` 才会启动抓取；读取配置本身不会调度任务。
- 若该目标和账号已有 checkpoint，其累计摘要及摘要引用的全部 JSONL 必须仍存在；缺失时停止，
  不得丢弃历史成果后推进前沿。

缺少任一前提时停止，不用底层 MediaCrawler 探测登录态，也不临时修改代码绕过门禁。

## 2. 登记与登录

新账号只登记一次：

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py enroll \
  --account-id xhs-a01
```

账号未登录、状态为 `login_required`，或操作人要求复验时运行：

```bash
source .venv/bin/activate
python scripts/xhs_login.py \
  --account-id xhs-a01 \
  --timeout-seconds 600
```

登录必须在该账号的桌面 profile 中人工完成。成功不是“出现 Cookie”，而是：

1. 页面可见“我”链接并得到稳定平台身份；
2. 保存 Cookie、localStorage 和运行时 storage state；
3. 关闭并重开同一 profile；
4. 重开后仍识别为同一身份；
5. 将快照以 AES-GCM 写入该账号的 `storage_state.enc`，账号状态变为 `active`。

登录与关闭/重开复验期间只保留一个浏览器标签页，并复用 profile 中已有页面；登录成功后，
正式抓取可按既有策略打开页面，不受这一限制。

同一平台身份不能登记到两个槽位。`retired` 账号不能重新登录；`quarantine` 账号只有人工
复验后才能执行 `activate`。不要用正式抓取顺便完成登录。

## 3. 冻结计划

dry-run 必须先于正式运行，且使用即将正式运行的同一个账号、目标和互动参数：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --dry-run \
  --target-key qingdao_travel \
  --account-id xhs-a01
```

dry-run 通过的判据：

- 顶层状态为 `planned`；
- `plan_frozen=completed`，其余阶段保持 `frozen`；
- 冻结输入包含 pool、target 和正式契约的 SHA-256；
- 计划中的账号、关键词、有效新增目标、候选硬上限、行为 profile 和互动模式正确；
- `plan.preflight` 明确记录账号为 `active`、没有活动租约、加密状态文件存在且可读，以及
  `lease_seconds >= timeout_seconds + 300`；`plan.headed=true`。密文的实际解密仍在正式执行构造
  child 命令前完成，解密失败不得启动抓取；
- 计划中的 `discovery` 与 `xhs_discovery_checkpoints` 一致：首次运行从第 1 页开始且顶部刷新为
  0；续跑包含保存的页码、非空 `search_id`、顶部刷新页数及可选累计摘要；
- 加密状态存在且账号仍为 `active`。

dry-run 的 `frozen` 后续阶段不是失败。dry-run 不访问内容、不申请正式租约、不写内容表，也不
构造依赖临时解密 storage state 的实际 child 命令；正式命令只出现在真实运行的
`command_executed.evidence.command`。小红书 dry-run 顶层摘要没有 `import_result`，不能把字段缺席
误读成已入库。

## 4. 正式运行

dry-run 经人工确认后，直接用相同账号、目标和互动参数执行正式命令。正式启动不再修改
`config/xhs_pool.json` 或 `config/xhs_targets.json`；这两个文件仍作为数量、行为和发现计划的冻结
输入，运行中不得修改。schema v2 不接受 `enabled` 字段，不存在运行后恢复开关的步骤。

无互动副作用：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --target-key qingdao_travel \
  --account-id xhs-a01
```

显式请求一轮最多一次的帖子互动：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --target-key qingdao_travel \
  --account-id xhs-a01 \
  --post-interaction comment-scroll
```

- `comment-scroll`：访问本轮首个可用图文的评论区并滚动，不采集评论。
- `like-one`：只检查首个可用图文；未点赞时点击一次，已点赞或状态不明时不点击。
- `random`：在上述两项中随机选择；可能产生真实点赞副作用。

互动默认 `none`。普通控件查找失败只记录证据，不否定抓取；页面出现验证、频控、封禁或
登录失效时必须终止本轮。运行结束后直接进入摘要、状态、SQLite 和租约验收，不修改配置。

## 5. 抓取中的固定行为

- `xhs_guarded` 在真实关键词页检查可见阻断；长停留期间每 5 秒复查一次可见页面，发现
  验证、频控、封禁或登录要求立即留证并停止。行为开始前必须确认登录 UI 已就绪、关键词页
  存在搜索卡片和作者链接；未就绪期间只等待，不在登录页面执行模拟行为。
- 前置行为执行少量鼠标移动和低次数桌面滚轮，不使用触摸行为，不扫描隐藏 HTML 关键词。
  滚轮调用后必须观测到窗口或实际滚动容器位置变化；只有调用事件但页面没有位移时，行为
  证据无效。
- 运行时指纹不仅保存，还必须通过门禁：`navigator.webdriver` 不得暴露，语言、平台、UA、
  可见状态和 viewport 必须完整。XHS API 请求头从当前 Chromium 会话生成，UA 与 UA Client
  Hints 主版本不一致时立即失败，禁止使用固定旧版本请求头。
- MediaCrawler 搜索并发固定为 1。搜索结果、笔记详情、作者主页和翻页分别执行随机等待，实际
  秒数写入 `behavior_evidence.request_pacing_events`。
- 每批搜索结果和翻页等待后继续执行短停留与鼠标移动，并写入
  `behavior_evidence.continuity_events`；不能只在抓取开始前执行一次页面行为。
- 搜索页、帖子互动和作者主页浏览器导航使用绝对 deadline。导航事件超时但关键词 URL 已提交
  时交给可见页面就绪门禁判断；URL 未提交或页面调用超过 deadline 时按运行失败停止。
- 作者粉丝补全先请求当前登录会话的无 token 作者主页。空结果时随机等待，再用同一已登录
  BrowserContext 打开无 token 作者页并解析。
- 笔记 `xsec_token` 只属于笔记访问上下文，不能当作作者主页凭据。
- 作者主页浏览器回退出现二维码安全验证时保持该标签页并置前，暂停最多 600 秒供操作人扫码；
  可见验证检查必须先于页面滚动和内嵌状态解析，即使底层 HTML 已包含作者数据也不能提前关闭；
  标记消失且作者数据可读后才关闭页面并继续。等待超时、限流或封禁标记仍抛出运行错误，不能
  静默保存为缺粉丝候选。
- 成功作者结果按作者 ID 缓存；缓存只减少本轮重复请求，不替代当前轮的来源和观测证据。
- 首轮自适应搜索为关键词生成一个 `search_id` 并递增 `page`。后续正式轮先用新 `search_id`
  刷新最多 `top_refresh_max_pages` 个顶部页面，再用 checkpoint 保存的 `page + search_id` 恢复
  深层前沿；深层来源已耗尽时只刷新顶部。
- 实际候选量从 0 开始按页增长，只有完成详情前去重的未知候选才占预算；达到
  `target_new_posts` 后立即停止，不会为了 `candidate_hard_limit` 继续抓满。候选硬上限、停滞批次、
  顶部刷新和超时共同构成单次运行的安全边界；提高正式目标时必须同步核对这些配置以及
  `lease_seconds >= timeout_seconds + 300`，但不得清空原账号的 checkpoint 或候选记忆。
- 搜索卡片 ID 在笔记详情、作者粉丝和媒体处理前与数据库、账号级已处理候选、累计摘要及本轮
  已见集合去重；已知 ID 不占 `candidate_hard_limit`。视频或字段无效候选也在 child 摘要形成后
  写入 `xhs_discovery_seen_candidates`，不靠内容入库才能获得记忆。完整处理一页才保存下一页，
  候选预算在页中耗尽时保存当前页，下轮重取边界页并靠 ID 去重，避免跳过未处理卡片。
- 每批记录真实页码、`search_id`、可恢复页码、批次完整性、发现阶段、原始返回数、`has_more`、
  候选数、有效新增数和停止原因。顶部刷新事件不能覆盖深层 checkpoint，也不累计深层停滞。
- 连续停滞按“该批没有新增有效记录”累计；出现新的无效候选不能重置停滞计数。

## 6. 完成判据

只在以下条件全部成立时汇报完成：

- 顶层 `run_summary.json` 状态为 `completed`，且账号租约已经释放；
- 冻结状态的五个阶段全部为 `completed`，`persistence_verified` 不得因 `--no-import` 跳过；
- child summary 中 `behavior_validation.ok=true`；
- 顶层摘要 `discovery.skipped=false` 且没有 checkpoint 写入错误；
- `behavior_validation.platforms.xhs.continuity_ok=true`，且至少覆盖 `search_results`；
- `formal_validation.behavior_evidence_ok=true`、`policy_evidence_ok=true`；
- `formal_validation.new_target_met=true`；
- `valid_new_count >= target_new_posts`；
- `import_result.inserted_rows >= target_new_posts`，`import_new_target_met=true`；
- `valid_existing_count` 和 `updated_rows` 只单独报告，没有计入新增目标；
- 每条入库图文都有平台原始发布时间、作者 ID/昵称、完整图片关系，以及
  `followers_count`、`followers_observed=true`、`author_followers_source=creator_profile`；
- 视频只出现在跳过计数中；帖子互动结果单独报告，不冒充抓取成功。

检查顺序固定为：

1. `data/runtime/xhs/runs/<run_id>/run_summary.json`；
2. `data/runtime/xhs/execution_states/<run_id>/<target_key>.json`；
3. 顶层摘要指向的 child `summary.json`；
4. SQLite 新增行数、粉丝字段、发布时间和图片关系计数；
5. 必要时才看标准输出/错误尾部，不全文展开日志或 JSONL。

## 7. 失败分流

| 信号 | 本轮结论 | 下一步 |
|---|---|---|
| `login_required` | 失败，不入库 | 对同一账号运行 `xhs_login.py` 人工复验；复验成功后开始新轮次 |
| 作者页二维码安全验证 | 暂停当前作者补全 | 保持验证页置前，等待操作人扫码；通过后继续，600 秒超时则失败且不入库 |
| 非二维码验证、频控、拒绝访问、环境异常 | 失败，不入库 | 保留证据并停止请求；由操作人决定隔离、等待或下一轮切号 |
| `browser_launch_failed` / `runtime_permission_error` | 运行环境失败 | 按运行手册修复 Chrome、HOME、Crashpad 或权限，再重新 dry-run |
| `browser_target_closed` | 页面、context 或浏览器在启动成功后关闭 | 核对是否人工关闭或浏览器崩溃，不自动重试 |
| 缺粉丝数值、来源或观测标记 | 字段补全失败 | 检查作者补全是否启用、是否逐条调用、无 token 请求和浏览器回退；禁止放宽 profile |
| `candidate_hard_limit_reached` | 未达到正式目标 | 本轮失败但保留累计摘要和安全前沿；下轮同账号自动续跑，必要时再调整单轮候选预算 |
| `stagnated` | 连续批次无新增有效记录 | 检查无效原因和分页证据；不能把新无效候选解释为进展 |
| `source_exhausted` | 深层来源明确耗尽但未完成 | 只有深层阶段空响应或 `has_more=false` 证据才接受；checkpoint 保留，下一轮只刷新顶部 |
| 超时或缺少停止事件 | `runtime_failed` | 读取 child summary 和日志尾部；不能推断为来源耗尽 |
| 保存的 `search_id` 恢复请求失败 | `runtime_failed` | checkpoint 保持原位置；保留失败证据，不自动换新 ID 猜测深页，不删除 checkpoint |
| checkpoint 累计摘要或 JSONL 缺失 | 冻结前失败 | 从原运行产物恢复文件或停止；不得清空路径后继续 |
| 互动控件失败且无阻断 | 互动失败、抓取可继续 | 只报告 `post_interaction.ok=false`，仍按正式字段和入库判据决定结果 |

小红书不向操作人开放手工 `--resume-summary`、`--start-page` 或 `--start-cursor`；正常续跑全部由
`xhs_runner.py` 从 SQLite 生成。失败轮次不导入部分有效记录，但 child 摘要存在且分页证据完整时，
Runner 会提交安全前沿并保存累计摘要；下轮同一账号把历史与本轮 JSONL 合并校验，达到完整
`target_new_posts` 后才一次性入库。`candidate_hard_limit` 是每次 child 的未知候选预算，不从历史
累计数扣减。`--no-import` 不写 checkpoint，也不能作为正式完成证据。

若 `target_new_met` 在页面中途触发并成功入库，checkpoint 仍保留当前 page/search ID，
`last_batch_complete=false`；来源未耗尽时 `status=active`。成功入库会清空
`last_summary_path` 并把 `campaign_candidate_count` 重置为 0，但不会删除前沿或已处理候选。

checkpoint 与已处理候选集合均以 `target_key + account_id + query_fingerprint` 定位。更换账号会
开始该账号自己的发现记忆，不共享尚未入库的累计摘要或已处理集合，也不能解释为原账号轮次的
续跑；已经进入 `web_posts` 的 ID 仍会在所有账号的详情请求前跳过。平台若不接受跨进程复用旧
`search_id`，该轮按 `runtime_failed` 停止且不推进位置，不设计猜页、静默换游标或从第一页大量
重抓的降级路径。

字段和命令映射固定为：SQLite `resume_search_id`、dry-run
`plan.discovery.resume_search_id`、child `--start-cursor` 及分页事件
`source_cursor/resume_cursor` 都表示同一个 client search ID。操作人只检查映射，不手工传参。

## 8. 账号与状态存储

- 每个账号固定使用权限 `0700` 的 `data/xhs_accounts/<account_id>/profile/`，不同账号禁止共享。
- profile 是 Chrome 持久目录，不宣称整个目录经过应用层加密。
- Cookie、localStorage 和运行时 storage state 使用 AES-GCM 保存为 `storage_state.enc`。
- 密钥优先读取 `TRIPPOSTCOLLECT_XHS_SNAPSHOT_KEY`，默认使用 macOS Keychain 服务
  `TripPostCollect.XHS`。
- 运行时明文只存在于 `data/runtime/xhs/sessions/<run_id>/`，退出时必须删除。
- SQLite 租约只阻止同一账号并发使用，不实施日预算、最短间隔、自动冷却、健康分或全池熔断。
- 除确认登录失效可将账号置为 `login_required` 外，Runner 不自动改变账号状态；`quarantine`、
  `activate`、`retire` 和账号切换都由操作人明确执行。
