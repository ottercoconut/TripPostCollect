# 小红书正式抓取 Workflow

本文是小红书登录、执行和恢复的操作权威。完成谓词、候选跳过、媒体事务等共享机器语义见
[正式抓取执行契约](../formal-crawl-contract.md)。目标超时、租期和发现参数只从
`config/xhs_targets.json`、`config/xhs_pool.json` 读取；本文件明确的二维码 180 秒、人工等待 600 秒
和普通标签页 30 秒是代码治理常量，不是 target/pool 可配置项。

## 硬边界

- 正式抓取和登录只运行 `scripts/xhs_runner.py`；没有独立登录入口，也不保存跨轮平台登录态或 Chrome
  profile。逻辑槽位、租约审计和 checkpoint 仍按各自生命周期保存。
- `xhs_accounts.py` 只管理 checkpoint/租约使用的非秘密逻辑槽位与孤儿租约，不接触平台登录态。
- 每个正式轮次只创建一个临时 profile、启动一次 Chrome 并创建一个 BrowserContext。轮内登录、验证、
  搜索和详情不得重启浏览器；初始 CDP 启动失败或运行中 page/context/browser 关闭时，本轮直接失败，
  不得 fallback 到另一个浏览器实例或新的 BrowserContext。
- 不把小红书放入通用 runner、warmup、`crawl_targets.json`、benchmark 或通用策略冷却。
- pool 使用 schema v2、target 使用 schema v3，没有 `enabled` 或图片开关；显式 runner 命令是唯一启动动作。
- 必须人工传 `--account-id`；一轮内不自动选号、换号、绕过验证或放宽字段。
- 正式 child 固定下载正文图片并真实入库；`--no-import` 只用于诊断。
- 租约、checkpoint、seen 和累计摘要按逻辑账号 ID 隔离；浏览器 profile 只属于本轮临时 session。

## 执行流

```text
检查配置与逻辑账号 ID
  -> dry-run 冻结账号、目标、互动和发现计划
  -> 人工确认
  -> 正式运行并申请账号租约
  -> 创建本轮临时 profile、启动唯一 Chrome/BrowserContext、打开二维码并人工登录
  -> xhs_guarded、顶部刷新和深层 page + search_id
  -> 详情、作者粉丝和正文图片
  -> 根项目复验、媒体晋升和 SQLite 批次事务
  -> 成功后提交账号级 checkpoint/seen/campaign
  -> 收束浏览器进程、删除本轮临时 session/profile、释放租约
  -> 检查顶层摘要、状态、child 摘要和 SQLite
```

## 正文与图片差异

- 正式发现抓取的正文必须来自笔记详情非空 `desc`，保存
  `content_detail_status=detail_observed`、`content_detail_source=note_detail`。既有记录修复另允许平台原生的
  无 `desc` 图文笔记：必须由本轮 `note_detail` 同时观察到非空 `title` 和至少一张详情 `image_list`
  正文图；标题仍只写 `title`，不得复制到 `content_text`，也不得使用搜索卡片标题或摘要补正文。
- 正文图只来自详情 `image_list`；每个对象按 `url_default`、`url`、`url_pre` 选择一个 URL，并用稳定
  notes 路径生成资产键。头像、作者主页图片、封面、搜索预览和视频不进入图片候选或下载链；作者
  主页仍只用于观察粉丝量等研究所需作者指标。
- 图片请求复用本轮 BrowserContext/API Cookie，不加载第二份会话，不调用视频 store。
- 作者粉丝必须来自当前登录会话的作者主页，保存数值、`followers_observed=true` 和
  `author_followers_source=creator_profile`。笔记 `xsec_token` 不能作为作者主页凭据。
- 平台层写 `<child_artifact>/xhs/data/xhs/` 下的 staging/manifest；根项目负责通用字节复验、晋升和
  SQLite。共享重试和失败分类不在本文重复。

## 1. 执行前检查

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py list
```

继续前逐项确认：

- 操作人已指定逻辑账号 ID；槽位状态为 `active` 且没有活动租约。首次使用会自动建立非秘密槽位。
- `target_key` 存在且关键词属于青岛；顶部刷新和超时符合本轮要求。
- `behavior_profile=xhs_guarded` 且使用有头浏览器。
- pool 的 `lease_seconds` 是租期上限，必须覆盖动态租期：目标 `timeout_seconds`、30 秒 child 进程组
  关闭预算和 270 秒根层临时目录清理、验证、摘要与数据库收尾预算之和；正式租约只写本目标实际所需时长。
- 互动未明确时为 `none`；点赞等真实副作用必须由操作人明确选择。
- pool schema v2 与 target schema v3 配置没有旧开关或数量控制字段。
- checkpoint 引用的累计摘要及全部 JSONL 仍存在。

缺少任一前提即停止，不直接调用 MediaCrawler 探测或绕过门禁。

`xhs_account_leases` 同时记录 `lease_id`、不可公开的 `owner_token`、账号、run、租约类型、host/boot
ID、owner PID、owner 进程启动时间和启动 token、PGID、execution state 路径、本轮临时 profile 路径，
以及取得、心跳、计划
到期和分项预算时间。TTL 只表示计划期限；过期行不会被下次 acquire 自动删除，仍须证明精确 owner
及其运行树已死亡。`xhs_lease_processes` 另外登记 child、exporter 和本轮 profile Chrome 的精确
PID/启动 token/PGID；`data/runtime/xhs/locks/<account_id>.lock` 用 `flock` 加强同机互斥，但 SQLite
owner token 仍是事实源。

若宿主、终端或 runner 被硬中止，先运行 `list` 取得精确 `account_id/run_id/lease_id`，再使用受审计
入口对账：

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py recover-orphan-lease \
  --account-id <account_id> \
  --run-id <run_id> \
  --lease-id <lease_id>
```

命令先取得同账号非阻塞 `flock`，再执行两次精确进程对账；第二次位于 `BEGIN IMMEDIATE` 内。只有下列
事实全部成立才删除租约：当前 host 与租约 host 一致；boot 已变化，或同一 boot 下 owner PID 已不存在/
启动 token 或 PGID 已不匹配；登记的 child/exporter 及其进程组全部消失；不存在 argv 中
`--user-data-dir` 精确等于租约所记本轮 profile 的 Chrome。PID 已重用只证明旧 owner 死亡，不把新 PID 当作
旧进程，也不向它发信号；不同 host 或 PID 存在但无法取得精确启动身份时拒绝回收。macOS 使用稳定的
platform UUID、boot session UUID 和 `libproc` 微秒级启动时间，不以秒级 `ps lstart` 代替精确身份。

execution state、最后事件、`adaptive_search_stopped` 和尾批完整性只形成独立的终态审计，不参与账号
互斥释放判定。因此 execution state 缺失、不可读或没有 `adaptive_search_stopped` 时，只要上述精确
进程死亡事实成立，也允许删除这一条精确租约。删除必须同时匹配 `account_id/run_id/lease_id/owner_token`
且 `DELETE rowcount=1`，并在同一事务写 `orphan_lease_reconciled`；错误 owner、字段漂移或并发恢复只能
有一个成功。

孤儿对账的进度变更范围固定为 `account_mutex_only`：不得补写 execution state 或停止事件，不得提交或推进
checkpoint、cursor、seen、campaign，不得晋升/删除/导入旧 staging，不得写内容 SQLite，也不得改变
账号槽位状态。审计事件逐项写明这些 mutation 均为 `false`。精确证明运行树死亡并删除租约后，还会
删除该租约对应的临时 session 目录。回收后仍须重新 dry-run，并由正式 runner 从 SQLite 最后安全
checkpoint 开始新轮；旧孤儿产物不能作为新轮完成证据。

正式抓取和历史修复都由同一个 `LeaseGuard` 覆盖从 acquire、轮内登录到根层摘要/数据库收尾的完整生命
周期。runner 启动 child 时创建独立进程组，child 启动 exporter 后立即用相同 owner token 登记 exporter
进程组；child/exporter 启动后若精确登记失败，须在继续抛错前有界 TERM/KILL 并回收该新进程组。普通
结束和普通异常都先关闭/等待登记进程与精确本轮 profile Chrome、删除临时 session，再由 Guard 在 `finally` 中
释放。收到 `SIGINT/SIGTERM` 时先向完整登记进程组发 `SIGTERM`，在 child 关闭预算内等待，仍存活才发
`SIGKILL`；复核进程与 profile 全部消失后才能删除租约。复核仍有残留时保留 SQLite 租约并写
`lease_release_deferred_live_processes`，不能为了退出码干净而强制释放。`SIGKILL` 和掉电无法执行
`finally`，由上面的孤儿对账恢复。

正常可捕获的搜索或作者补全 `300011` 运行级限制仍必须在进程退出前先写入
`adaptive_search_stopped(runtime_failed, stop_detail=platform_security_limit_300011, batch_complete=false)`；
单独的 behavior evidence 不能推进 checkpoint。硬中止导致该事件来不及写入时，只影响终态完整性，
不再让已经精确证实死亡的 owner 永久占用账号互斥。

## 2. 轮内登录与逻辑槽位

`--account-id` 只是人工选择的租约/checkpoint 命名空间，不是平台身份档案。runner 首次看到新的合法
ID 时自动建立 `active` 槽位；也可显式预建：

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py ensure-slot \
  --account-id xhs-a01
```

dry-run 不打开浏览器。每次正式抓取或历史修复取得租约后，runner 都新建
`data/runtime/xhs/sessions/<run_id>/profile/`，以 `qrcode` 模式启动真实 Chrome，等待操作人扫码并由
页面“我”和 self-info 共同确认登录，然后在同一 BrowserContext 内继续行为、搜索、详情、作者和图片
阶段。二维码只显示在这一个有头 Chrome 页面内，不额外打开操作系统图片预览窗口。不读取上一轮
Cookie、localStorage、sessionStorage、身份哈希或设备状态，不生成独立 JSON
登录快照，也没有独立登录命令。

轮初登录状态机的总人工预算为 600 秒：

- 页面仍是纯未扫码二维码且组件明确显示已过期时，连续两次确认未登录、未进入人工流程且没有终端
  安全阻断后，只点击组件内“点击刷新/重新获取二维码”；不因组件过期提前 reload 当前页。新二维码
  可用后从该时刻重新计算 180 秒整页 reload 下限。只有满 180 秒且仍无任何登录进展，才允许 reload
  当前页取得新二维码；组件刷新控件缺失也不得提前 reload。
- 一旦任一可见页面或 frame 出现已扫码、手机确认、短信/图片验证码或安全验证状态，立即锁存为
  `operator_in_progress`。锁存后禁止组件刷新和按二维码周期 reload，即使原 180 秒边界随后到达也
  必须保持原页，直到登录成功或 600 秒人工预算耗尽。
- 登录成功必须由可见身份与 self-info 共同确认；只看到二维码消失、普通页面文本或 Cookie 变化都
  不能单独视为成功。频控、封禁、`300011/300012` 和安全限制不是人工等待，直接按运行级阻断失败。

可见登录/验证码页必须置前并等待操作人，标记消失且身份恢复后才继续，不自动识别或绕过验证。
`retired` 槽位不可运行，`quarantined` 只能由操作人明确 activate。登录失败只写本轮运行级事件，不会
把可复用登录状态写回槽位；下一轮仍从空 profile 重新登录。

同一轮次只有上述一次 Chrome 启动和一个 BrowserContext。登录页转换、平台弹出验证页或业务阶段
重新要求登录，都只能在现有 context 中继续；初始 CDP 连接失败、context/browser 关闭或主页面无法
接管时直接结束本轮，不启动第二个 Chrome，也不降级到另一种启动模式。

child 退出后 Guard 先证明 child、exporter 和精确临时 profile Chrome 全部死亡，再递归删除这一条
session 目录，最后释放租约。可捕获异常和 SIGINT/SIGTERM 走同一清理路径；SIGKILL/掉电留下的目录
只允许在孤儿租约精确对账成功后删除。SIGINT/SIGTERM 还必须把 execution state 终结为
`runtime_failed/operator_interrupt`，写顶层 `run_summary.json`、退出码和 `lease_cleanup`；中断轮不得
提交 discovery checkpoint、seen/campaign 或伪造来源耗尽。

## 3. 冻结计划

dry-run 必须使用正式轮相同的账号、目标和互动参数：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --dry-run \
  --target-key qingdao_travel \
  --account-id xhs-a01
```

确认：

- 顶层为 `planned`，只有 `plan_frozen=completed`，其余阶段为 `frozen`；
- pool、target、正式契约及其 SHA-256 已冻结；
- 逻辑账号、关键词、来源耗尽策略、轮内二维码登录、互动和有头模式正确；
- preflight 证明槽位 active、无租约、不使用持久 profile/登录态且租约覆盖超时；
- discovery 与该账号 checkpoint 一致：首次 page 1、顶部刷新 0；续跑有保存的 page、非空 search ID、
  顶部刷新页数及可选累计摘要。

dry-run 不申请正式租约、不创建临时 profile、不构造 child 命令，也没有 `import_result`；这些缺席不是
失败或入库证据。

## 4. 正式运行

无互动：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --target-key qingdao_travel \
  --account-id xhs-a01
```

显式互动在一轮最多一次：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --target-key qingdao_travel \
  --account-id xhs-a01 \
  --post-interaction comment-scroll
```

`comment-scroll` 只访问并滚动评论区，不采集评论；`like-one` 只在明确未点赞时点击一次；`random` 在
两者中随机选择并可能产生点赞副作用。控件普通失败只影响互动证据，频控、封禁或验证仍终止运行。

### 4.1 `300011` 定时续跑

已经完成同账号、同配置 dry-run 后，可以为可捕获的 `300011` 运行级安全限制启动专用
控制器：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --target-key qingdao_travel \
  --account-id xhs-a01 \
  --retry-on-300011
```

`--retry-on-300011` 不能与 `--dry-run` 同用。它按触发运行的 `finished_at + 30 分钟` 计算下一次执行，
不是缩短租约 TTL，也不是固定墙钟 cron。等待期间不持有账号租约；每次到期都调用完整正式 runner，
重新取得独立精确租约，并保持原 target、账号、配置和互动参数。每次尝试都是新的 run：只有上轮
child/exporter/Chrome 已精确收束、临时 session 已删除且租约已释放，才创建新的空 profile、启动新的
唯一 Chrome/BrowserContext 并显示新二维码；这不是同一轮内的浏览器重启。相同
`target_key + account_id` 的控制器使用独立非阻塞 `flock`，避免重复计时。

只有以下证据全部成立才进入或继续循环：最新正式终态为 `failed` 且 challenge 精确等于
`platform_security_limit_300011`；child 摘要含完整
`adaptive_search_stopped(runtime_failed, stop_detail=platform_security_limit_300011,
batch_complete=false)`；SQLite checkpoint 的 run、摘要、未完成尾批和停止原因与之精确一致；同一
`lease_id` 存在 owner-token 摘要和 `process_check.safe_to_release=true` 的 `lease_released` 审计；账号
当前没有租约。behavior evidence、PID 文件、TTL 或宽泛进程匹配都不能单独触发重试。

重试再次产生同样的完整 `300011` 终态时，从该轮结束再等待 30 分钟；正式轮完成且精确释放租约后，
控制器以成功结束。登录、验证码、其他频控、配置错误、产物/SQLite 错误或任何证据不完整都会停止
控制器并保留原状态，不换号、不补写终态、不导入失败轮产物，也不改变逻辑槽位状态。中断控制器后
可用同一命令重启；它从 SQLite 最新终态重新计算截止时间，不会因为重启立即重试。

控制状态写入 `data/runtime/xhs/retry_states/<account_id>/<target-hash>.json`；SQLite 审计事件依次使用
`security_limit_retry_scheduled`、`security_limit_retry_attempt_started`、
`security_limit_retry_completed` 或 `security_limit_retry_stopped`。

已有不完整记录只能使用独立修复入口；默认最多选 20 条，并在同一 BrowserContext 内每 5 条分批，
避免为每批重启浏览器和制造新设备会话：

```bash
source .venv/bin/activate
python scripts/repair_xhs_posts.py \
  --target-key qingdao_travel \
  --account-id xhs-a01 \
  --max-items 20 \
  --batch-size 5
```

详情 API、HTML 回退、作者资料与图片各自沿用有界请求重试；最终失败会记录真实 `attempts`、
`failure_scope` 和 `error_code`。普通候选失败不序列化空壳记录，不阻塞同批成功记录，也不阻止下一批；
登录、验证码、频控、封禁、安全限制和浏览器整体失败仍立即停止。child `summary.json` 的
`pagination_evidence.skipped_candidate_failures` 与平台记录的 `repair_report` 是失败清单，顶层
`repair_xhs_posts.py` 摘要也会转存该报告。

修复详情若省略点赞、收藏、评论或分享聚合数，只在 XHS repair 路径保留 `web_posts` 已有的对应
标准化数值；fresh detail 非空值（包括真实 `0`）始终优先。回填字段必须在记录中写
`repair_fallback_evidence.metrics.<field>.source=existing_web_posts_metric`，不能据此放宽普通新抓取
门禁，也不能用旧标题、搜索摘要或旧正文替代详情正文。

运行中不得修改 pool/target。正式轮固定抓到可验证来源耗尽，不提供数量或完成模式切换。

## 5. 抓取中的固定行为

### 标签页与人工验证

- 正式浏览器启动后先打开 `/explore` 并确认页面壳已可见，再进入真实关键词搜索页。搜索页在导航
  `commit` 后持续无可见文本时，程序只允许执行一次受控恢复：回到 `/explore` 确认渲染，再重新进入
  同一关键词搜索页；恢复仍为空白则按 `runtime_failed` 停止，不重建临时 profile、不换号，也不推进
  checkpoint。启动导航会把 `readyState`、DOM/正文长度、主文档状态、页面脚本错误和失败资源的紧凑
  摘要写入 behavior evidence 同目录的 `behavior_evidence.navigation.json`，白屏超时不得只凭外部关闭
  后的 `TargetClosedError` 分类。
- 正式 BrowserContext 守卫安装后出现的任何新标签页都立即置前。普通业务弹页从出现起至少保留
  30 秒，之后才允许按业务结果关闭；正常返回、异常、Playwright 退出和最终清理都不得绕过这 30 秒。
  首个主页面可豁免，启动时已有的额外页仍受保护。
- 新标签页若出现登录、扫码、手机确认、验证码或人工安全验证，不适用“30 秒后可关闭”：守卫将它
  锁存为人工验证页、持续置前，并在本轮最长 600 秒人工预算内等待验证完成。只有标记消失且身份/
  原业务状态恢复后才允许继续或关闭。频控、封禁、安全限制和 `300011/300012` 仍立即失败。
- 搜索连续性出现登录要求或图片验证时，保持当前页并写
  `operator_verification_events`；完成后刷新会话并继续，超时失败。
- 搜索 API 461/471 使用响应 `Verifyuuid`、`Verifytype` 在同一 BrowserContext 打开平台人工验证页；
  可见状态必须同时检查顶层页与子 frame；通过后还需连续两次确认已回到原路由且有可见文本，
  再刷新 Cookie 并重试原请求。`Requests too frequent` 等可见频控优先于验证页标题分类，
  立即按运行级阻断停止，不点击刷新绕过。
- 搜索 API 明确登录过期时暂停原请求，保留全部标签页并置前最新 XHS 页；可见登录 UI 与 self-info
  API 都恢复后刷新本轮 Cookie，并重试同一来源页。
- “安全限制”、账号异常、`300011/300012`、`/website-login/error`、频控或封禁属于运行级阻断，
  立即停止，不能进入人工验证码等待或候选跳过。

### 行为、作者与发现

- `xhs_guarded` 只在真实关键词页执行；使用少量桌面滚轮并验证实际位移，不扫描隐藏 HTML 文本。
- 运行指纹必须完整，`navigator.webdriver` 不得暴露，Chromium UA 与 API Client Hints 主版本一致。
- 搜索并发为 1；搜索、详情、作者主页、翻页分别随机等待并写 `request_pacing_events`，批次间继续写
  `continuity_events`。
- 作者补全先用登录会话的无 token 请求；空结果后随机等待，再用同一 BrowserContext 打开无 token
  作者页。二维码验证先等待操作人，不能因为 HTML 已有作者数据而提前关闭。
- 成功作者结果只在本轮按作者 ID 缓存，不替代来源证据。
- 有 checkpoint 时用新 search ID 刷新顶部，再用保存的 `page + search_id` 恢复深层；顶部刷新不
  覆盖深层位置。深层耗尽后只刷新顶部。
- 只有未知笔记 ID 才进入候选处理；数据库、账号 seen、累计摘要和本轮已见 ID 在详情前过滤。完整页保存
  下一页，页中途停止保存当前页。
- `stagnant_batches` 按“本批没有有效新增”累计，仅作诊断；它不触发停止。正常停止只接受可验证的
  `source_exhausted`，运行级阻断按失败处理。

## 6. 完成检查

先按[正式契约](../formal-crawl-contract.md)核对五阶段、来源耗尽、行为/策略、图片和真实持久化。小红书
还必须确认：

- 顶层 `run_summary.json` 为 `completed`，账号租约已经释放；
- `discovery.skipped=false`，checkpoint/seen/campaign 没有提交错误；
- `continuity_ok=true` 且至少覆盖 `search_results`；
- 每条记录具有 note detail 正文、原始发布时间、作者 ID/昵称、creator profile 粉丝证据和完整
  本地正文图片；
- 视频和互动只在各自报告中，不计抓取成功；
- `--no-import`、`sqlite_import_failed` 或持久化阶段 skipped 均不能完成正式轮次。

读取顺序：

1. `data/runtime/xhs/runs/<run_id>/run_summary.json`
2. `data/runtime/xhs/execution_states/<run_id>/<target_key>.json`
3. 顶层摘要指向的 child `summary.json`
4. SQLite 新增、字段和图片关系
5. 必要时日志最后 40 行

## 7. 失败分流

| 信号 | 处理 |
|---|---|
| 轮初纯未扫码二维码 | 明确过期时双重确认后只刷新二维码组件；新二维码重新计算 180 秒整页 reload 下限，总人工预算 600 秒 |
| 已扫码、手机确认、验证码或安全验证 | 锁存人工处理中，禁止组件刷新和整页 reload；成功或 600 秒超时后结束等待 |
| 初始 CDP 失败或 page/context/browser 关闭 | 本轮运行级失败；不 fallback、不轮内重启、不创建第二个 BrowserContext |
| 连续性登录/图片验证、461/471 | 保持页面、人工处理、刷新会话并重试原请求；600 秒超时失败 |
| 作者页二维码 | 保持作者页置前，人工扫码后继续；超时失败 |
| 普通新标签页 | 立即置前且至少保留 30 秒，之后才允许按业务结果关闭 |
| 登录或验证新标签页 | 立即置前并锁存，最长等待 600 秒；不能在普通 30 秒保护期结束时关闭 |
| 详情、作者或正文图候选级失败 | 核对 `candidate_skipped` 和 manifest；整帖不入库，ID 写账号 seen，继续候选 |
| 登录、频控、安全限制、封禁 | 运行级失败；保留 page/search ID，不写候选 seen |
| 完整 `300011` 且上轮 session/租约已精确清理 | 可显式用 `--retry-on-300011` 每 30 分钟启动新 run、新 profile 和新二维码；其他阻断不自动重试 |
| manifest 身份、哈希或路径错误 | 停止晋升和 checkpoint，修复代码/产物后重跑 |
| 连续批次没有新有效记录 | 只核对诊断计数并继续；不能据此停止或推断耗尽 |
| `source_exhausted` | 只有停止事件、`source_exhausted_met` 与正式持久化门禁全部通过才完成 |
| 超时或缺少停止事件 | `runtime_failed`，不能推断来源耗尽 |
| 保存的 search ID 恢复失败 | 保留 checkpoint，不生成新 ID 猜测深页 |
| 累计摘要或 JSONL 缺失 | 冻结前失败；恢复原文件或停止，不清空路径继续 |
| `sqlite_import_failed` | 不提交 checkpoint、seen 或 campaign；核对数据库和媒体回滚 |
| 正常结束或可捕获普通异常 | `LeaseGuard` 收束登记进程，精确 token 删除且 rowcount 必须为 1；异常不伪装成功 |
| `SIGINT` / `SIGTERM` | 写 `runtime_failed/operator_interrupt` state 与 run summary，不推进 checkpoint；再 TERM/KILL 精确进程、删 session、释放租约并写 `lease_cleanup` |
| runner 被 `SIGKILL` | 租约与未完成 state 原样保留；child/Chrome 存活时孤儿对账必须拒绝 |
| 系统重启 | 同 host 且 boot ID 已变化可证明旧 PID 全部死亡；仍需确认当前没有租约所记精确 profile Chrome |
| execution state 缺失或无停止事件 | 只降低终态完整性；精确运行树已死亡时允许 `account_mutex_only` 回收 |
| PID 数值被重用 | 启动 token/启动时间/PGID 不匹配即视为新进程；不误杀、不把它当旧 owner 存活 |
| PID 存在但精确启动身份不可读 | 无法证明旧进程死亡，拒绝对账；不得降级为秒级 `ps` 或 TTL 判定 |
| 残留 child/exporter/本轮 Chrome | 无论 TTL 或 state 如何都拒绝释放，先让精确残留进程结束 |
| 两个恢复命令并发 | 同账号 `flock` 与 `BEGIN IMMEDIATE` 串行化；只有精确 DELETE rowcount=1 的一个成功 |
| 错误 lease/run/owner | 删除谓词不匹配并失败关闭；不得写成功恢复事件或改变逻辑槽位状态 |

小红书不向操作人开放手工 `--resume-summary`、`--start-page` 或 `--start-cursor`。checkpoint/seen 以
`target_key + account_id + query_fingerprint` 隔离；换号不是原账号续跑。SQLite
`resume_search_id`、dry-run `plan.discovery.resume_search_id`、child `--start-cursor` 和分页事件
cursor 表示同一个 client search ID。

来源耗尽且入库成功时，checkpoint 保存耗尽坐标、完整停止证据和本轮摘要身份；后续轮次只做顶部
刷新，只有平台提供新的可验证来源链时才重建深层前沿。seen 保留，累计摘要在成功提交后清空。

## 8. 运行状态存储

- 不创建 `data/xhs_accounts/`，不保存跨轮 Chrome profile、Cookie、localStorage、sessionStorage、平台
  身份哈希、登录快照或 Keychain 密钥，也不生成独立 JSON 登录状态文件。
- 每轮权限 `0700` 的 profile 只存在于 `data/runtime/xhs/sessions/<run_id>/`；登录与抓取共享本轮唯一
  一次 Chrome 启动和唯一 BrowserContext，下一轮不得恢复这一 profile。
- 正常结束、可捕获异常和 SIGINT/SIGTERM 必须在释放租约前删除 session 目录；摘要中的
  `runtime_session_removed=true` 是清理证据。残留精确进程时不得抢删目录或释放租约。
- SQLite `xhs_accounts` 只保留逻辑槽位 ID、状态、最后使用时间和审计时间，不保存账号身份或浏览器
  路径；checkpoint/seen 继续按这个人工 ID 隔离。
- SQLite 精确租约是同槽位互斥事实源；`data/runtime/xhs/locks/` 中的 `flock` 只增强同机竞争保护，
  不能替代 owner token、进程启动身份或 SQLite rowcount。quarantine、activate 和 retire 均由操作人决定。
