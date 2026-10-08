# 小红书正式抓取 Workflow

本文是小红书账号、登录、执行和恢复的操作权威。完成谓词、候选跳过、媒体事务等共享机器语义见
[正式抓取执行契约](../formal-crawl-contract.md)；数值只从 `config/xhs_targets.json` 和
`config/xhs_pool.json` 读取。

## 硬边界

- 正式抓取与人工登录都只运行 `scripts/xhs_runner.py`；`xhs_accounts.py` 只管理非秘密逻辑槽位与精确
  租约，没有独立登录入口。
- 不把小红书放入通用 runner、warmup、`crawl_targets.json`、benchmark 或通用策略冷却。
- pool 使用 schema v2、target 使用 schema v3，没有 `enabled` 或图片开关；显式 runner 命令是唯一启动动作。
- 必须人工传 `--account-id`；一轮内不自动选号、换号、绕过验证或放宽字段。
- 正式 child 固定下载正文图片并真实入库；`--no-import` 只用于诊断。
- 逻辑账号槽位、租约、checkpoint、seen 和累计摘要按账号 ID 隔离；槽位不保存平台身份、profile、
  Cookie、storage state 或其他登录快照。
- 每个正式轮创建一个空临时 profile，只允许一次 Chrome 启动和一个 BrowserContext；登录、验证与
  业务阶段不得轮内重启。page/context/browser 真关闭时本轮失败，不得 fallback 到第二个浏览器。

## 执行流

```text
检查配置与已显式登记的逻辑账号槽位
  -> dry-run 冻结账号、目标、互动和发现计划
  -> 人工确认
  -> 正式运行并申请账号租约
  -> 创建本轮空临时 profile，启动唯一 Chrome/BrowserContext，人工扫码
  -> xhs_guarded、顶部刷新和深层 page + search_id
  -> 详情、作者粉丝和正文图片
  -> 每个完整批次生成不可变 child 恢复摘要，根 runner 校验并提交 checkpoint/seen/campaign
  -> child 收到根 runner 的提交确认后开始下一批
  -> 根项目复验、媒体晋升和 SQLite 批次事务
  -> 终态对账账号级 checkpoint/seen/campaign；只有正式成功才清空累计摘要
  -> 精确收束进程、删除本轮临时 session、释放租约
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
- 图片请求复用本轮 BrowserContext/API Cookie，不创建或恢复第二份会话，不调用视频 store。
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

- 操作人已显式登记并指定逻辑账号槽位；状态为 `active`，没有活动租约。runner 不会自动创建槽位。
- `target_key` 存在且关键词属于青岛；顶部刷新和超时符合本轮要求。
- `behavior_profile=xhs_guarded` 且使用有头浏览器。
- pool 的 `lease_seconds` 是租期上限，必须覆盖动态租期：目标声明的 runtime 预算、30 秒 child 进程组
  关闭预算和 270 秒根层验证、摘要与数据库收尾预算之和；正式租约只写本目标实际所需时长。该计划
  到期时间用于互斥审计，不是父层整轮墙钟 kill。
- 互动未明确时为 `none`；点赞等真实副作用必须由操作人明确选择。
- pool schema v2 与 target schema v3 配置没有旧开关或数量控制字段。
- checkpoint 引用的累计摘要及全部 JSONL 仍存在。

缺少任一前提即停止，不直接调用 MediaCrawler 探测或绕过门禁。

`xhs_account_leases` 同时记录 `lease_id`、不可公开的 `owner_token`、账号、run、租约类型、host/boot
ID、owner PID、owner 进程启动时间和启动 token、PGID、execution state 路径，以及取得、心跳、计划
到期和分项预算时间。TTL 只表示计划期限；过期行不会被下次 acquire 自动删除，仍须证明精确 owner
及其运行树已死亡。`xhs_lease_processes` 另外登记 child、exporter 和本轮临时 profile Chrome 的精确
PID/启动 token/PGID；账号目录下的 `lease.lock` 用 `flock` 加强同机互斥，但 SQLite owner token 仍是
事实源。

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
`--user-data-dir` 精确等于该 run 临时 profile 的 Chrome。PID 已重用只证明旧 owner 死亡，不把新 PID 当作
旧进程，也不向它发信号；不同 host 或 PID 存在但无法取得精确启动身份时拒绝回收。macOS 使用稳定的
platform UUID、boot session UUID 和 `libproc` 微秒级启动时间，不以秒级 `ps lstart` 代替精确身份。

execution state、最后事件、`adaptive_search_stopped` 和尾批完整性只形成独立的终态审计，不参与账号
互斥释放判定。因此 execution state 缺失、不可读或没有 `adaptive_search_stopped` 时，只要上述精确
进程死亡事实成立，也允许删除这一条精确租约。删除必须同时匹配 `account_id/run_id/lease_id/owner_token`
且 `DELETE rowcount=1`，并在同一事务写 `orphan_lease_reconciled`；错误 owner、字段漂移或并发恢复只能
有一个成功。

孤儿对账的变更范围固定为 `account_mutex_only`：不得补写 execution state 或停止事件，不得提交或推进
checkpoint、cursor、seen、campaign，不得晋升/删除/导入旧 staging，不得写内容 SQLite，也不得改变
账号健康状态。审计事件逐项写明这些 mutation 均为 `false`。回收后仍须重新 dry-run，并由正式 runner
从 SQLite 最后安全 checkpoint 开始新轮；旧孤儿产物不能作为新轮完成证据。

正式抓取和历史修复都由同一个 `LeaseGuard` 覆盖从 acquire 到根层摘要/数据库收尾的完整生命
周期。runner 启动 child 时创建独立进程组，child 启动 exporter 后立即用相同 owner token 登记 exporter
进程组；child/exporter 启动后若精确登记失败，须在继续抛错前有界 TERM/KILL 并回收该新进程组。普通
结束和普通异常都先关闭/等待登记进程与本轮精确 profile Chrome，再由 Guard 在 `finally` 中
释放。收到 `SIGINT/SIGTERM` 时先向完整登记进程组发 `SIGTERM`，在 child 关闭预算内等待，仍存活才发
`SIGKILL`；复核进程与 profile 全部消失后才能删除租约。复核仍有残留时保留 SQLite 租约并写
`lease_release_deferred_live_processes`，不能为了退出码干净而强制释放。`SIGKILL` 和掉电无法执行
`finally`，由上面的孤儿对账恢复。

父层不按整轮墙钟强制结束 child，而是验证 child 的认证心跳：启动宽限 120 秒、陈旧阈值 60 秒、
每 5 秒检查一次；长调度间隙只给同一 sequence 一次 30 秒恢复宽限。心跳须覆盖登录、网络暂停、抓取
和最终摘要写入。认证失败、sequence 回退/复用或超时会形成明确监督证据；不能把单纯“运行较久”当作
关闭 Chrome 的理由。

正常可捕获的搜索或作者补全 `300011` 运行级限制仍必须在进程退出前先写入
`adaptive_search_stopped(runtime_failed, stop_detail=platform_security_limit_300011, batch_complete=false)`；
单独的 behavior evidence 不能推进 checkpoint。硬中止导致该事件来不及写入时，只影响终态完整性，
不再让已经精确证实死亡的 owner 永久占用账号互斥。

## 2. 登记与轮内登录

首次使用某个逻辑账号 ID 时先显式登记槽位；正式 runner 对未知槽位失败关闭，不会顺手创建：

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py ensure-slot \
  --account-id xhs-a01
```

槽位仅是操作人选择的非秘密协调标识；`retired` 不可运行，`quarantined` 只有操作人显式
`activate` 后才可用。它不声明或校验某个长期平台身份，也不保存 profile、Cookie、localStorage、
sessionStorage 或设备指纹。

登录只发生在正式 `xhs_runner.py` 轮次中。runner 取得租约后创建
`data/runtime/xhs/sessions/<run_id>/profile/` 空临时目录，启动唯一 Chrome/BrowserContext，并固定传
`--login-type qrcode`。登录成功由可见身份与 self-info API 共同确认；二维码消失或 Cookie 变化不能
单独放行。本轮 API 签名和图片下载可在内存中复用当前 BrowserContext Cookie，但结束时不把任何登录
状态晋升为跨轮资产。

轮初纯未扫码页面明确显示二维码过期时，只有连续两次确认仍无扫码、手机确认、短信或安全验证进展，
才可点击二维码组件内刷新控件。新二维码出现后重新计算 180 秒整页 reload 下限；未过期二维码页面
至少等待 180 秒才允许 reload。一旦观察到扫码、手机确认、短信验证码或安全验证，人工处理中状态即
锁存，组件刷新和整页 reload 都禁用，直到登录成功或人工预算耗尽。程序只在现有 Chrome 页面显示
二维码，不打开二维码截图或任何操作系统图片预览窗口。

同一正式轮的二维码、手机确认、短信验证码、搜索连续性登录、461/471 和作者页验证共享一个单调
600 秒人工处理预算；在不同 checkpoint 间切换不会重新计时，重叠等待只按实际墙钟计一次。普通抓取
和网络暂停不消耗该人工预算。耗尽时固定以 `xhs_manual_checkpoint_budget_exhausted` 结束本轮，不自动
发送短信、不读取 Redis，也不切换 mobile/cookie 登录模式。

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
- 逻辑账号槽位、关键词、来源耗尽策略、互动和有头模式正确；
- preflight 证明槽位已显式登记且 active、无租约，并且租约覆盖动态预算；
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

命令取得精确租约后创建本轮空临时 profile，并且只启动一次 Chrome 和一个 BrowserContext。初始 CDP
启动失败时直接失败；登录、验证、网络恢复、搜索、详情和作者阶段都不得关闭后重启浏览器。正常完成、
可捕获异常或操作人中断都要先写终态，再精确收束 child/exporter/Chrome、删除临时 session 并释放
租约；任一清理事实无法证明时保留租约并在摘要中明确延期。

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
重新取得独立精确租约、新 run、空 profile 和新二维码，并保持原 target、账号、配置和互动参数；这
不是在原轮次内重启浏览器。相同
`target_key + account_id` 的控制器使用独立非阻塞 `flock`，避免重复计时。

只有以下证据全部成立才进入或继续循环：最新正式终态为 `failed` 且 challenge 精确等于
`platform_security_limit_300011`；child 摘要含完整
`adaptive_search_stopped(runtime_failed, stop_detail=platform_security_limit_300011,
batch_complete=false)`；SQLite checkpoint 的 run、摘要、未完成尾批和停止原因与之精确一致；同一
`lease_id` 存在 owner-token 摘要和 `process_check.safe_to_release=true` 的 `lease_released` 审计；账号
当前没有租约。behavior evidence、PID 文件、TTL 或宽泛进程匹配都不能单独触发重试。

重试再次产生同样的完整 `300011` 终态时，从该轮结束再等待 30 分钟；正式轮完成且精确释放租约后，
控制器以成功结束。登录、验证码、其他频控、配置错误、产物/SQLite 错误或任何证据不完整都会停止
控制器并保留原状态，不换号、不补写终态、不导入失败轮产物，也不改变账号健康状态。中断控制器后
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

### 标签页、网络与人工验证

- 正式浏览器启动后先打开 `/explore` 并确认页面壳已可见，再进入真实关键词搜索页。搜索页在导航
  `commit` 后持续无可见文本时，程序只允许执行一次受控恢复：回到 `/explore` 确认渲染，再重新进入
  同一关键词搜索页；恢复仍为空白则按 `runtime_failed` 停止，不重启浏览器、不换号，也不推进
  checkpoint。启动导航会把 `readyState`、DOM/正文长度、主文档状态、页面脚本错误和失败资源的紧凑
  摘要写入 behavior evidence 同目录的 `behavior_evidence.navigation.json`，白屏超时不得只凭外部关闭
  后的 `TargetClosedError` 分类。
- 搜索卡片和作者链接在匿名页面也可能存在；可见的精确“登录”按钮优先判定为 `login_required`，
  不得仅凭卡片数或作者链接数把匿名页面判为 ready。主页面在搜索导航或行为阶段意外关闭时立即以
  `xhs_main_page_closed_unexpected` 终止本轮；即使 BrowserContext 中另有同域页面，也不得接管、重试
  当前阶段或重新启动 Chrome。原主页面仍存活时，平台主动打开的普通新页继续只由新页守卫管理。
- 正式 BrowserContext 守卫安装后出现的任何新标签页都立即置前，并从出现起至少保留 30 秒；平台
  弹页、作者主页回退、互动和验证辅助页一视同仁。正常返回、异常、Playwright 退出和最终清理都
  不得绕过。首个主页面可豁免，启动时已有的额外页仍受保护。
- 明确登录、扫码或验证码页置前并进入本轮共享的 600 秒人工处理预算，30 秒保护期不能缩短它，切换
  页面也不能重新计时。
- 搜索连续性出现登录要求或图片验证时，保持当前页并写
  `operator_verification_events`；完成后刷新当前内存 Cookie 并继续，人工预算耗尽时失败。
- 搜索 API 461/471 使用响应 `Verifyuuid`、`Verifytype` 在同一 BrowserContext 打开平台人工验证页；
  可见状态必须同时检查顶层页与子 frame；通过后还需连续两次确认已回到原路由且有可见文本，
  再刷新 Cookie 并重试原请求。`Requests too frequent` 等可见频控优先于验证页标题分类，
  立即按运行级阻断停止，不点击刷新绕过。
- 搜索 API 明确登录过期时暂停原请求，置前原有抓取页，使用该页已经出现的二维码等待人工登录；
  不新开登录标签页，也不把最新打开的作者页或辅助页接管为主页面。可见登录 UI 与 self-info API
  都恢复后刷新当前内存 Cookie，并重试同一来源页。等待期间不导航或刷新原页。
- “安全限制”、账号异常、`300011/300012`、`/website-login/error`、频控或封禁属于运行级阻断，
  立即停止，不能进入人工验证码等待或候选跳过。
- 可识别的短时网络错误只进入 `network_paused`，保留同一 Chrome、BrowserContext、Page 和原操作，
  在原处以最短 2 秒、最长 30 秒退避重试；父层继续验证认证心跳，不因网络中断或整轮运行较久主动
  关闭浏览器。runner 固定下发 600 秒网络恢复预算，不受调用者环境覆盖；恢复后继续原请求，预算耗尽
  时写 `network_recovery_timeout` 并保留最后安全 checkpoint，不重启 Chrome、不创建新 Context 或新
  页面重放。
- page、context 或 browser 实际关闭以及 CDP 断开均为终端浏览器错误；生命周期证据必须区分计划关闭
  与非计划关闭，本轮不得启动第二个 Chrome 或 BrowserContext。

### 行为、作者与发现

- `xhs_guarded` 只在真实关键词页执行；使用少量桌面滚轮并验证实际位移，不扫描隐藏 HTML 文本。
- 运行指纹必须完整，`navigator.webdriver` 不得暴露，Chromium UA 与 API Client Hints 主版本一致。
- 搜索并发为 1；搜索、详情、作者主页、翻页分别随机等待并写 `request_pacing_events`，批次间继续写
  `continuity_events`。
- 作者补全先用登录会话的无 token 请求；空结果后随机等待，再用同一 BrowserContext 打开无 token
  作者页。打开前先检查原抓取页，原页已出现登录或验证时直接在原页等待，不再创建作者辅助页；
  作者辅助页遇到登录失效也回到原抓取页恢复会话，成功后重试作者请求。作者专属安全验证仍保留
  对应页面等待操作人，不能因为 HTML 已有作者数据而提前关闭。
- 作者页静态解析只做严格 JSON 解码（`:undefined` 预替换为 `null`）。平台状态里出现
  `new Set(...)` 等 JS 构造时解码失败，无 token 请求路径因此仍会失败，只能靠浏览器回退取数，
  这会多一次作者页访问和暂停。
- 浏览器回退在到达与滚动检查后进入有界就绪等待，预算 10 秒。`domcontentloaded` 只是前置条件，
  超时记 `page_not_ready` 后继续。随后执行固定脚本，读取页面已执行的
  `window.__INITIAL_STATE__.user.userPageData`。只有自有数据属性 `__v_isRef === true` 的节点才按
  Vue ref 解包；脚本按白名单只复制标量字段，不序列化整份状态，也不返回 HTML。白名单分两部分：
  - 仓库内下游读取的字段：`basicInfo` 的用户 ID、昵称、简介、性别、IP 属地，粉丝、关注、笔记与获赞
    指标键，以及 `interactions` 条目的 `type/name/key/count/num/value`；
  - 研究项目经 `creator_profile_json` 读取的字段：`basicInfo.redId`、`interactions[].i18nCount`、
    `tags[].tagType/name`、`verifyInfo.redOfficialVerifyType`。

  头像（`imageb`、`images`、`avatar*`、`icon`）与任何凭据或 token 字段都不投影；计数类字段不投影布尔值。
- 投影前，脚本先在该作者记录内收集头像证据 URL。证据键与路径直接取自 `records.sanitization`：
  `AUTHOR_AVATAR_KEYS` 不区分大小写，另加 `basicInfo.imageb`、`basicInfo.images`。收集有深度与节点
  上限，并检测循环引用。投影后，任何字符串字段去掉首尾空白后若与证据 URL 完全相同即删除。空白集合
  与 Python `str.strip()` 相同（含 `\x85`，不含 BOM）。证据集合不返回，因此同值删除只在该作者记录内
  生效；笔记级头像仍由 Python 清理器按键删除。
- 脚本只经属性描述符读取自有数据属性，不读取访问器属性，不调用 `toJSON`；遇到 Proxy 时，其 trap
  仍可能执行页面代码。遇到超限、循环引用、访问器属性、函数或未知对象类型（Set、Map、Date 除外）时，
  整个投影被拒绝（`projection_avatar_check_incomplete`），立即回退静态解析。未求值的 computed ref
  按缺失处理。
- 就绪与成功条件：粉丝计数必须非布尔，且能按 `records.formal.parse_int` 解析（含“万/亿”）。计数按原值
  保存（如 `1.2万`）；0 是真实观察值，不补值。仅有 `interactions` 数组不算就绪。每次读取投影后，
  先做生命周期、登录、验证、阻断与封禁检查，然后才接受结果或重试，每 0.5 秒重试一次。出现验证标记时，
  先进入既有验证等待。人工验证完成后，每轮做一次检查，再读取一次：先投影，取不到再走严格静态解析；
  这一步不会重入就绪等待。取消以及页面、浏览器关闭照常向上传播。
- 页面数据带有作者 ID 且与请求的 `user_id` 不一致时，判为 `creator_mismatch`，不可用。ID 缺失时
  沿用按 `user_id` 打开的作者页和笔记作者 ID，不凭昵称比对。投影在预算内拿不到粉丝时，回退到
  `page.content()` 加严格静态解析。
- `creator_profile_json` 的形状因取数路径而异：
  - 运行时投影路径写入白名单结构，只含上面列出的字段；
  - 无 token 请求、静态回退与验证后静态读取路径仍写完整的 `userPageData`，头像由 Python 清理器删除。

  下游如果读取白名单外的字段（例如 `extraInfo`、`tags[].icon`），在投影路径的记录里会缺失。按列做
  统计时，需要区分记录来自哪条路径，或只使用白名单字段。
- `creator_profile_parse` 导航诊断的写入时机：
  - 就绪等待结束时写一次，`outcome` 取 `ok`、`ok_static_state` 或原因类别；
  - 就绪等待中转入登录恢复时写 `ok_login_recovery` 或 `login_recovery_empty`；
  - 验证等待中读到作者资料时写 `ok` 或 `ok_static_state`，判定作者不一致时写 `creator_mismatch`。

  以下情况不写：到达或滚动检查直接转入登录恢复；验证等待因人工预算耗尽而抛错；验证等待中转入登录恢复。
- 两条路径都失败时，`candidate_skipped.detail` 写为 `creator_profile_failed:api=<原因>;browser=<原因>`，
  `error_code` 仍为 `creator_profile_unavailable`。repair 路径的失败记录不带原因码。原因只含类别：
  - 无 token 请求：`api_request_failed:<异常类型>`；
  - 静态解析：`state_script_missing`、
    `state_decode_failed:<js_new_expression|js_identifier|invalid_json|truncated>`、`state_null`、
    `user_missing`、`user_page_data_missing`、`user_page_data_empty`，在浏览器侧加 `static_` 前缀；
  - 运行时投影：`page_not_ready`、`runtime_projection_empty`、`runtime_projection_timeout`、
    `runtime_projection_error`、`projection_avatar_check_incomplete`、`followers_unobserved`、
    `creator_mismatch`；
  - 其他分支：`login_recovery_empty`、`verification_wait:<原因>`。
- 成功作者结果只在本轮按作者 ID 缓存，不替代来源证据。
- 有 checkpoint 时用新 search ID 刷新顶部，再用保存的 `page + search_id` 恢复深层；顶部刷新不
  覆盖深层位置。深层耗尽后只刷新顶部。
- 只有未知笔记 ID 才进入候选处理；数据库、账号 seen、累计摘要和本轮已见 ID 在详情前过滤。完整页保存
  下一页，页中途停止保存当前页。
- `stagnant_batches` 按“本批没有有效新增”累计，仅作诊断；它不触发停止。正常停止只接受可验证的
  `source_exhausted`，运行级阻断按失败处理。

## 6. 完成检查

### 批次恢复点

正式搜索的完整非耗尽批次在写出 `adaptive_batch_completed` 后，立即固化当前 JSONL 与 manifest
快照。快照以 `.batch-<sequence>.snapshot` 为后缀保存在源文件旁；后续追加写入原 JSONL 或替换原
manifest 不会改变已提交快照，manifest 的相对 staging 图片路径保持有效。快照不复制登录 profile、
Cookie 或浏览器存储。

导出数据目录按首条实际写入记录延迟创建。完整批次没有新增产物时（例如顶部刷新全部命中已知
候选），允许目录尚不存在，仍提交包含空的本轮文件清单、历史累计产物和分页证据的摘要并等待确认；
不得虚构内容文件或因此重置深层页码。批次已声明有效记录却找不到内容文件时，按
`xhs_batch_checkpoint_content_missing` 失败，不把产物丢失当作空批次。

child 批次摘要及提交确认保存在 `data/runtime/xhs/batch_checkpoints/<run_id>/`。根 runner 通过监督
循环读取摘要，核对精确租约、账号/目标/查询指纹、完整分页事件、JSONL/manifest 哈希与 staging
图片字节，随后在一个 SQLite 事务中提交 checkpoint、seen、累计摘要引用和
`batch_checkpoint_committed` 审计事件，再写确认文件。底层搜索循环不写 SQLite。

child 最长等待 60 秒确认；没有确认即以 `xhs_batch_checkpoint_ack_timeout` 运行级失败结束，不能
继续后续批次。根 runner 在提交确认前消失时最多重取一个边界批次；已提交批次不依赖最终
`run_summary.json` 才能恢复。顶部刷新保存累计成果但保留原深层位置。未完成批次不走批次提交，
来源耗尽批次仍由完整终态门禁处理，不能提前标成耗尽。

批次发布失败会记录 `xhs_runtime_terminal(phase=batch_checkpoint)`，保留具体
`xhs_batch_checkpoint_*` 子原因，并归类为不可自动重试的 `runtime_failed`。本地文件异常和等待
提交确认超时不能归为网络超时；已提交的安全恢复点保持有效，不补写来源耗尽。

批次摘要固定为未完成、未入库，只作为后续正式 runner 的恢复输入；下一轮仍重新扫码，重新验证
累计字段、行为、图片与来源耗尽证据后才可入库。收到中断后不再提交新批次，已确认的恢复点保留。
`--no-import`、dry-run 和历史详情修复均不启用该握手。旧版本缺少批次快照的孤儿轮次仍遵循前述
孤儿对账规则，本机制不会自动补签旧终态或把旧产物导入。

### 终态验收

先按[正式契约](../formal-crawl-contract.md)核对五阶段、来源耗尽、行为/策略、图片和真实持久化。小红书
还必须确认：

- 顶层 `run_summary.json` 为 `completed`，账号租约已经释放；
- `discovery.skipped=false`，checkpoint/seen/campaign 没有提交错误；
- `continuity_ok=true` 且至少覆盖 `search_results`；
- 每条记录具有 note detail 正文、原始发布时间、作者原始 `user_id`/昵称（不哈希、不脱敏）、
  creator profile 粉丝证据和完整本地正文图片；
- 视频和互动只在各自报告中，不计抓取成功；
- `--no-import`、`sqlite_import_failed` 或持久化阶段 skipped 均不能完成正式轮次。

读取顺序：

1. `data/runtime/xhs/runs/<run_id>/run_summary.json`
2. `data/runtime/xhs/execution_states/<run_id>/<target_key>.json`
3. 顶层摘要指向的 child `summary.json`
4. SQLite 新增、字段和图片关系
5. 必要时日志最后 40 行

## 7. 失败分流

登录与验证终态使用“错误族 + 精确子原因”两层语义。child 在异常离开登录状态机前写
`xhs_runtime_terminal`，顶层摘要、execution state 和 `formal_run_finished` 原样保留
`failure_type`、`stop_reason`、`stop_detail`；界面原文只作为 `matched_markers` 证据，不能代替稳定码。
下表这些阻断的正式 `stop_reason` 均为 `runtime_failed`；`failure_type` 是错误族，`stop_detail` 才是
本轮的精确终止原因。普通未完成登录与可见验证码仍分别使用正式 `login_required`、
`captcha_detected`，不得拿错误族替换正式终态。
当前稳定映射为：

| 场景 | `failure_type` | `stop_detail` |
|---|---|---|
| 人工处理总预算耗尽 | `manual_checkpoint_timeout` | `xhs_manual_checkpoint_budget_exhausted`，并附 `checkpoint_kind` |
| SMS `Parameter error` / `参数错误` | `sms_verification_terminal` | `xhs_sms_verification_parameter_error` |
| 当日短信验证码次数上限 | `sms_verification_terminal` | `xhs_sms_verification_daily_limit` |
| 短信验证码请求频控 | `sms_verification_terminal` | `xhs_sms_verification_rate_limited` |
| 明确错误码 `300011` | `platform_security_limit` | `platform_security_limit_300011` |
| 明确错误码 `300012` | `ip_blocked` | `ip_blocked_300012` |
| 无错误码的“安全限制” | `platform_security_limit` | `xhs_platform_security_limit_unspecified` |
| “账号异常”但没有错误码 | `platform_security_limit` | `xhs_account_exception` |
| `/website-login/error` 且没有更具体标记 | `platform_security_limit` | `xhs_login_error_page` |
| 普通平台请求频控 | `rate_limited` | `xhs_rate_limited_terminal` |
| 登录时 Page 全部关闭或 BrowserContext 不可用 | `browser_target_closed` | `xhs_login_browser_pages_closed` / `xhs_login_browser_context_unavailable` |
| 登录状态机的其他未识别异常 | `login_runtime_error` | 原始稳定异常文本，空文本时使用异常类型生成稳定码 |

不得把无编号安全限制、账号异常、登录错误页或频控推断成 `300011`；`--retry-on-300011` 仍只接受
精确 `platform_security_limit_300011` 及完整停止、清理和 checkpoint 对账证据。登录阶段在第一个分页
事件前被阻断时，顶层 `discovery.reason=runtime_blocked_before_pagination` 且保留原 checkpoint；这不是
`terminal_commit_failed`。后者只用于真实 SQLite 终态事务或线性化提交失败。

| 信号 | 处理 |
|---|---|
| 轮初二维码 | 保持本轮唯一 Chrome；只在纯未扫码且明确过期、连续两次无进展时刷新组件 |
| 连续性登录/图片验证、461/471 | 保持页面、人工处理、刷新当前内存 Cookie 并重试原请求；共用单调 600 秒人工预算 |
| 作者页二维码 | 保持作者页置前，人工扫码后继续；计入同一人工预算 |
| 任意新标签页 | 立即置前且至少保留 30 秒；明确验证页按本轮剩余人工预算等待 |
| 短时网络中断 | 同一 Chrome/Context/Page 内暂停并退避；恢复后继续原操作，默认 600 秒 |
| `network_recovery_timeout` | 运行级失败并保留最后安全 checkpoint；不得启动第二个浏览器 |
| page/context/browser 关闭或 CDP 断开 | 终端浏览器失败；记录非计划关闭证据，不得轮内重启 |
| 详情、作者或正文图候选级失败 | 核对 `candidate_skipped` 和 manifest；整帖不入库，ID 写账号 seen，继续候选 |
| 登录、频控、安全限制、封禁 | 运行级失败；保留 page/search ID，不写候选 seen |
| 完整 `300011` 且已精确释放租约 | 可显式用 `--retry-on-300011` 每 30 分钟同账号续跑；其他阻断不自动重试 |
| manifest 身份、哈希或路径错误 | 停止晋升和 checkpoint，修复代码/产物后重跑 |
| 连续批次没有新有效记录 | 只核对诊断计数并继续；不能据此停止或推断耗尽 |
| `source_exhausted` | 只有停止事件、`source_exhausted_met` 与正式持久化门禁全部通过才完成 |
| 超时或缺少停止事件 | `runtime_failed`，不能推断来源耗尽 |
| 保存的 search ID 恢复失败 | 保留 checkpoint，不生成新 ID 猜测深页 |
| 累计摘要或 JSONL 缺失 | 冻结前失败；恢复原文件或停止，不清空路径继续 |
| `sqlite_import_failed` | 不提交 checkpoint、seen 或 campaign；核对数据库和媒体回滚 |
| 正常结束或可捕获普通异常 | `LeaseGuard` 收束登记进程，精确 token 删除且 rowcount 必须为 1；异常不伪装成功 |
| `SIGINT` / `SIGTERM` | 写 `runtime_failed/operator_interrupt` 终态，再精确收束 child、exporter 与本轮 Chrome；确认 session 删除后释放租约 |
| runner 被 `SIGKILL` | 租约与未完成 state 原样保留；child/Chrome 存活时孤儿对账必须拒绝 |
| 系统重启 | 同 host 且 boot ID 已变化可证明旧 PID 全部死亡；仍需确认当前没有该 run 临时 profile Chrome |
| execution state 缺失或无停止事件 | 只降低终态完整性；精确运行树已死亡时允许 `account_mutex_only` 回收 |
| PID 数值被重用 | 启动 token/启动时间/PGID 不匹配即视为新进程；不误杀、不把它当旧 owner 存活 |
| PID 存在但精确启动身份不可读 | 无法证明旧进程死亡，拒绝对账；不得降级为秒级 `ps` 或 TTL 判定 |
| 残留 child/exporter/本轮 Chrome | 无论 TTL 或 state 如何都拒绝释放，先让精确残留进程结束 |
| 两个恢复命令并发 | 同账号 `flock` 与 `BEGIN IMMEDIATE` 串行化；只有精确 DELETE rowcount=1 的一个成功 |
| 错误 lease/run/owner | 删除谓词不匹配并失败关闭；不得写成功恢复事件或改变账号健康 |

小红书不向操作人开放手工 `--resume-summary`、`--start-page` 或 `--start-cursor`。checkpoint/seen 以
`target_key + account_id + query_fingerprint` 隔离；换号不是原账号续跑。SQLite
`resume_search_id`、dry-run `plan.discovery.resume_search_id`、child `--start-cursor` 和分页事件
cursor 表示同一个 client search ID。

来源耗尽且入库成功时，checkpoint 保存耗尽坐标、完整停止证据和本轮摘要身份；后续轮次只做顶部
刷新，只有平台提供新的可验证来源链时才重建深层前沿。seen 保留，累计摘要在成功提交后清空。

## 8. 逻辑槽位与临时会话

- `xhs_accounts` 只保存逻辑槽位 ID、操作状态和租约审计字段；槽位必须由操作人显式
  `xhs_accounts.py ensure-slot` 创建，runner 对未知槽位失败关闭。
- 每个正式 run 只在 `data/runtime/xhs/sessions/<run_id>/profile/` 创建权限受限的空临时 profile。
  不读取或保存跨轮 Cookie、localStorage、sessionStorage、storage state、设备标识或平台身份。
- 本轮 Chrome、BrowserContext 与 API 客户端可以在内存中共享当前 Cookie 供登录验证、签名和图片
  下载；这些值不得写成下一轮登录输入。下一正式轮仍以空 profile 和新二维码开始。
- 正常完成、可捕获异常和 SIGINT/SIGTERM 都须留下精确浏览器生命周期与清理证据。只有登记进程及
  进程组已消失、临时 session 已删除且 owner token 精确删除成功，才能报告
  `runtime_session_removed=true`、`lease_released=true` 和 `lease_cleanup.ok=true`。
- SQLite 精确租约是同账号互斥事实源；每账号 `flock` 只增强同机竞争保护，不能替代 owner token、
  进程启动身份或 SQLite rowcount。槽位的 quarantine、activate 和 retire 均由操作人决定。
