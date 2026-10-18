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
- 搜索、详情、作者、图片与登录态只来自本轮页面自身请求的被动截获响应或页面状态（L1）：不在页面内
  主动请求（L2），不用 Playwright 请求接口（L3），也没有浏览器外 HTTP 客户端的例外。每条记录以
  `content_visit_refs` 引用访问账本，入库前须 `content_visit_ok=true`；分级见
  [正式契约](../formal-crawl-contract.md#取数来源与真实访问门禁)，逐条规则见下文“访问证据”。

## 执行流

```text
检查配置与已显式登记的逻辑账号槽位
  -> dry-run 冻结账号、目标、互动和发现计划
  -> 人工确认
  -> 正式运行并申请账号租约
  -> 创建本轮空临时 profile，启动唯一 Chrome/BrowserContext，人工扫码，截获 user/me 确认登录态
  -> 首页输入关键词回车，筛选面板点选“图文”后收起，xhs_guarded 行为阶段
  -> 从目标会话顶部全量滚到 has_more=false：已知 ID 滑过，未知图文就地点开
  -> 浮层详情（截获 feed）、翻图（截获正文图）、点击作者名打开作者页（读页面状态投影）
  -> 每个有新增决定的完整批次生成不可变 child 恢复摘要（含访问账本快照），根 runner 校验并提交
     checkpoint/seen/campaign
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
- 正文图字节取自浏览器翻看浮层时截获的图片响应体，不另发请求，不创建或恢复第二份会话，不调用
  视频 store；头像主机的响应从不读取。
- 作者粉丝必须来自浮层内点击作者名打开的作者页页面状态，保存数值、`followers_observed=true` 和
  `author_followers_source=creator_profile`。
- 平台层写 `<child_artifact>/xhs/data/xhs/` 下的 staging/manifest；根项目负责通用字节复验、晋升和
  SQLite。共享重试和失败分类不在本文重复。

## 1. 执行前检查

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py list
```

继续前逐项确认：

- 操作人已显式登记并指定逻辑账号槽位；状态为 `active`，没有活动租约。runner 不会自动创建槽位。
- `target_key` 存在且关键词属于青岛。target 不含 `timeout_seconds` 或已删除的 `top_refresh_max_pages`，
  出现即配置错误；正式轮不设任务时长上限，只受统一的 1200 秒无持久进展看门狗约束（见下文）。
- `behavior_profile=xhs_guarded` 且使用有头浏览器。
- pool 的 `lease_seconds` 是租约 TTL，不含任何任务时长：取得租约时写入 `expires_at = 取得时刻 + TTL`，
  此后父层每收到一次新的认证心跳 sequence、且距上次续期已达 TTL 一半时，按精确 `lease_id` 与
  owner token 把 `heartbeat_at` 推进到当前时刻、`expires_at` 推进到当前时刻加 TTL。child 结束后（正常退出
  先续出完整 TTL；心跳失效或中断收束同样适用），父侧校验、发现提交和收尾期间由父进程保活继续按 TTL
  一半续期，直到精确释放前才停止；所有者仍在工作，租约就不会过期。释放被延期（进程仍存活、收尾超
  时等）时保活随本次关闭结束，之后到期只表示 owner 已不再续期。TTL 的一半必须覆盖父层心跳最长静默
  窗口（启动宽限 120 秒与“陈旧 60 秒 + 恢复宽限 30 秒”取大，再加 5 秒检查间隔）、30 秒 child 进程组
  关闭和 270 秒根层收尾预算，即 `lease_seconds` 至少 850 秒，不足时 runner 拒绝启动；当前配置为 900 秒。
  续期不记账号事件，SQLite 暂时忙时留到下一次心跳或保活重试；owner token 不符时 child 阶段按失败收束，
  收尾阶段停止保活并由精确释放报告所有权错误。到期时间只用于互斥审计，不是父层整轮墙钟 kill，也不
  授权任何隐式释放。
- 互动未明确时为 `none`；点赞等真实副作用必须由操作人明确选择。
- pool schema v2 与 target schema v3 配置没有旧开关或数量控制字段。
- checkpoint 引用的累计摘要及全部 JSONL 仍存在。

缺少任一前提即停止，不直接调用平台 worker 探测或绕过门禁。

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
信号路径：Guard 只向 child（中间层）进程组发 SIGTERM，exporter 在独立会话中不会直接收到。中间层把
首个 SIGTERM 转为可捕获中断，只向 exporter 转发一次温和信号，等待其清理后把 exporter 日志经头像清洗
落盘（命中时两路替换为带各路命中行数与模块标签的审计标记，格式见运行手册），并在退出前把 exporter
标记为已退出；Guard 随后的收束因此不会再对 exporter 发第二次温和信号，
只处理仍存活的登记进程。锁存之后才观察到 child 正常退出时同样按中断处理。预算嵌套：Guard 自转发起
给 child `child_shutdown_seconds` 减 5 秒（默认 25 秒）自行退出，期间不再发 SIGTERM，超时才 SIGKILL
并最多回收 5 秒；中间层等待 exporter 至多 12 秒（超时对 exporter 组 SIGKILL），再加 5 秒输出回收与
6 秒收尾余量，严格小于该宽限，因此正常路径下日志一定在兜底前落盘。中间层仍被强杀时，Guard 对仍存活
的 exporter 只发 SIGKILL，不发第二次 SIGTERM；watchdog 超时等非中断路径仍保持先 SIGTERM 后 SIGKILL。
child 退出后回收其管道输出最多等 5 秒，后代仍占管道时显式关闭管道，残留进程按登记进程组收束。

父层不按整轮墙钟强制结束 child，而是验证 child 的认证心跳：启动宽限 120 秒、陈旧阈值 60 秒、
每 5 秒检查一次；长调度间隙只给同一 sequence 一次 30 秒恢复宽限。心跳须覆盖登录、网络暂停、抓取
和最终摘要写入。认证失败、sequence 回退/复用或超时会形成明确监督证据；不能把单纯“运行较久”当作
关闭 Chrome 的理由。child 运行期间由新鲜心跳驱动上文的租约续期，心跳陈旧时不再续期；child 结束后改由
父进程保活续期到精确释放。

心跳只证明中间层与 exporter 身份仍在，不证明抓取在前进。“进程活着但不再前进”由中间层统一的
1200 秒无持久进展看门狗识别：execution state 事件、内容 JSONL、图片 manifest 与行为证据连续 1200 秒
都没有变化时，以 `adaptive_search_stopped(runtime_failed, no_progress_timeout)` 收束，尾批不完整、不导入；
首次进展之前另加 240 秒行为预算。网络恢复暂停期间，父层对新鲜的 `network_paused` 诊断冻结该计时，
同一次连续断网最多 675 秒。

人工等待期间同样暂停该计时，机制与网络暂停一致。共享 600 秒人工预算的计费区间（扫码登录、轮中登录
恢复、前端验证（规则端点 461/471）、作者页验证）以及搜索就绪验证与连续性验证（各自 600 秒上限，不计入共享预算）都通过
同一个“人工等待中”信号，写 behavior evidence 同目录的 `behavior_evidence.operator_wait.json`：进入时写
`waiting`，等待期间每 30 秒刷新，结束时写 `ended`；自动化子操作暂停人工计费时同样写 `ended`。父层只对
新鲜（90 秒内）、结构有效且来自已核验 exporter 的 `waiting` 冻结计时，只在读到明确的 `ended` 时结束该
暂停段，冻结的是剩余量，不重置；陈旧或格式错误的诊断不获得暂停时间。该文件不计为持久进展，1200 秒
阈值、启动宽限和进展判定都不变。600 秒人工预算照常生效，耗尽时按 `xhs_manual_checkpoint_budget_exhausted`
失败；父层对同一段连续人工等待设 675 秒上限，只防 child 失联后持续声称等待，超过时以
`parent_operator_wait_timeout` 收束。

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
`--login-type qrcode`。登录成功由可见身份与截获到 `guest=false` 的 `user/me` 响应共同确认；UI 已确认
但 30 秒内没有截获时只允许整页 reload 一次，仍没有即 `xhs_login_state_unconfirmed`。二维码只判 DOM
就绪，二维码消失或 Cookie 变化不能单独放行。结束时不把任何登录状态晋升为跨轮资产。

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
- preflight 证明槽位已显式登记且 active、无租约，并且租约由父层心跳续期
  （`lease_renewed_by_parent_heartbeat=true`）；`plan.lease_budget` 记录 TTL、续期间隔与收尾预算，计划中
  不含任务时长字段；
- discovery 与该账号 checkpoint 一致：`checkpoint_found`、`last_status`、`last_stop_reason`、
  `seen_candidate_count`、`campaign_summary_path` 与累计摘要携带的访问账本快照数
  `campaign_visit_ledger_count`；计划中没有页码、search ID 或顶部刷新页数，每轮都从首页搜索后全量扫描。

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

Chrome 由共享 CDP 启动器（`runtime/browser_launcher.py`）自启：
- 调试端口：在 `127.0.0.1` 上绑定端口 0 取得系统分配的空闲端口，关闭后交给 Chrome；不传
  `--remote-debugging-address`，只监听本机回环地址。端口号只在 worker 进程内使用（连接、就绪检查、
  生命周期错误文本与日志），不写入租约、进程登记、访问账本或执行状态；租约按 `--user-data-dir` 识别
  本轮 Chrome。
- 不传 `--no-sandbox`、`--disable-blink-features=AutomationControlled`、`--exclude-switches=enable-automation`、
  `--disable-infobars`，也不传后台定时器节流、渲染进程降级与 IPC 防洪三个后台参数。
- 保留 `--disable-backgrounding-occluded-windows`：窗口被遮挡或最小化时页面会变为 hidden，导致行为门禁
  （要求 `visibility_state=visible`）失败、IntersectionObserver 翻页与懒加载停止、等待动画帧卡住；页面本身
  测不出这个参数。该参数让被遮挡的窗口仍按可见处理，最小化是否同样覆盖未实测，正式轮期间不要最小化
  Chrome 窗口。
- 正式轮不向页面注入任何脚本：不注入 `stealth.min.js`，也不改写 `navigator`。自启 Chrome 不带
  `--enable-automation`，`navigator.webdriver` 保持原生 `false`，主线程与 Worker 读到的都是原生值。
- 行为证据的 `runtime_fingerprint` 另记 `window_size`（`outer_width`、`outer_height`）与 `screen`（`width`、
  `height`、`avail_width`、`avail_height`），`viewport.device_pixel_ratio` 照旧；这些只记录、不参与门禁。用途是
  核对固定窗口 1450×900 能否放进屏幕可用区域：操作人 Mac 的可用区域是 1470×863，高度差 37px，Chrome
  是否把窗口收进可用区域待实测。

正式轮（含 repair）的鼠标动作都经 `runtime/human_pointer.py`（#84）。算法移植自 CloakBrowser 包装层
`cloakbrowser/human/`（MIT，提交 `fbf005bc`，许可见 `resources/licenses/CloakBrowser-LICENSE`），参数按操作人
2026-10-17 在 Mac 上用触控板手工录制的样本校准。键盘不模拟真人，关键词仍逐字 `keyboard.type`。
- 每次 `mouse.move` / `mouse.wheel` 只发一个事件，事件之间按 60Hz（约 16.7ms，±3%）真实等待。每个页面
  记住自己的光标位置；本页第一个指针动作前，光标先出现在视口中下部（横 30%–70%、纵 60%–90%），停
  0.15–0.4 秒再走轨迹，不从 (0,0) 画长线。
- 接近目标分两段：快速主移动（三次 Bezier、速度前段达峰，时长约 110+6·log2(1+距离/8) 毫秒，700px 级约
  150ms）落在目标附近（多数越过、少数没够到，偏离全程的 6%–22%、至多 115px），静止 90–260ms 后再做
  1 次、约三分之一的情况 2 次修正（每次约 200ms），最后精确落到目标点；60px 以内一笔到位。修正结束后
  静止 20–550ms 才按下，按住 80–135ms。
- 落点是元素框内的整数坐标，按截断正态取：横向 25%–75%（中心 50%），纵向 25%–55%（中心 40%）；小目标
  （筛选标签文字、作者名）离元素边至少 2px。卡片封面的落点纵向再限在可点击带内（可点击判定仍只看封面中心）。
  定位器上的控件（登录按钮、二维码刷新、点赞）先取元素框，再按同样方式点击，不用 `locator.click`。
- 滚动是触控板手势序列：每个手势 3–6 个事件内升到峰值，再衰减到 1，可能以一个 0 结束、偶尔跟 1–2 个
  反向的 1；`deltaY` 为整数，总和恰为该次滚动距离。峰值随距离增长（150px 约 10、400px 约 40，上限 50），
  每个手势至少 8 个事件、至多约 400px，更长的滚动拆成多个手势，手势间停 0.4–0.95 秒。`deltaX` 是
  `deltaY` 的 0–0.4 倍同向漂移，方向整轮固定。
- 需要把元素滚进可视区（评论区、点赞控件）时，光标先移进该元素所在滚动区域，用手势按需要的位移滚动后
  重新测量，至多 8 轮，连续两轮位置不变即停；仍不可见按本次互动失败处理，不退回 `scroll_into_view_if_needed`。
- 证据：账本中点击类动作（`apply_filter`、`click_card`、`close_overlay`、`click_author`、按箭头的
  `flip_image`，以及聚焦搜索框那次点击随 `type_search`）带 `pointer`：`x_ratio`、`y_ratio`（落点在元素框
  中的比例）、`approach_ms`、`moves`、`settle_ms`、`hold_ms`，本页第一次时另有 `entered=true`；扫描 `scroll`
  动作带 `pointer.gestures`（每个手势的 `events`、`delta_y`、`delta_x`）。这些都在执行前计划好、随动作行写出；
  筛选面板因悬停已打开而没有按下时，`panel_open` 行的 `pointer` 仍是计划值（见“搜索与全量扫描”）。
  行为证据的 `wheel_scroll` 事件带同样的 `gestures`，互动另记 `wheel_into_view` 与 `pointer_click`。作者页的
  一次小幅滚动不写记录。

显式互动在一轮最多一次：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --target-key qingdao_travel \
  --account-id xhs-a01 \
  --post-interaction comment-scroll
```

`comment-scroll` 只访问并滚动评论区，不采集评论；`like-one` 只在明确未点赞时点击一次；`random` 在
两者中随机选择并可能产生点赞副作用。控件普通失败（含互动辅助页自身关闭）只影响互动证据；频控、
封禁、验证、浏览器生命周期失败和网络恢复超时仍终止运行。

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

修复先在首页输入 `--keyword` 搜索并完成行为阶段（不进入全量扫描），再逐个目标串行处理：打开本篇
图片窗口，写 `navigate` 动作后在主页面直开保存的笔记链接，读取页面状态 `note_detail_state` 的白名单
投影（与 feed `note_card` 同形，须通过同一形状校验），详情停顿后翻图截获正文图，作者停顿后点击页面内
作者名读取作者页投影（同页跳转到作者页时读完写 `go_back` 返回）。链接跳转 `/404` 记
`repair_link_unavailable`（不可重试）；页面壳未出现记 `note_page_not_rendered`，10 秒内读不到状态记
`note_detail_not_observed`，登录恢复或验证后第二次重开仍中断记 `note_redo_interrupted`（均可重试）；
视频记 `video_skipped`。repair 账本 `mode=repair`，访问门禁没有搜索访问要求，触发动作为 `navigate`。
最终失败会记录真实 `attempts`、`failure_scope` 和 `error_code`。普通候选失败不序列化空壳记录，不阻塞同批成功记录，也不阻止下一批；
登录、验证码、频控、封禁、安全限制、网络恢复预算耗尽和浏览器整体失败仍立即停止，当前未完成批次
不写入 `batches`；Chrome 进程退出、CDP 断开、BrowserContext 或主页面意外关闭写入 `runtime_blocker`，
`error_code=browser_target_closed`，具体生命周期码在 `reason`；Playwright 直接报告 page/context/browser
已关闭（`TargetClosedError`）同样记 `browser_target_closed`。网络恢复预算耗尽记
`error_code=runtime_failed`、`reason=network_recovery_timeout`，不按候选失败继续等待下一候选。
child `summary.json` 的
`pagination_evidence.skipped_candidate_failures` 与平台记录的 `repair_report` 是失败清单，顶层
`repair_xhs_posts.py` 摘要也会转存该报告。

修复详情若省略点赞、收藏、评论或分享聚合数，只在 XHS repair 路径保留 `web_posts` 已有的对应
标准化数值；fresh detail 非空值（包括真实 `0`）始终优先。回填字段必须在记录中写
`repair_fallback_evidence.metrics.<field>.source=existing_web_posts_metric`，不能据此放宽普通新抓取
门禁，也不能用旧标题、搜索摘要或旧正文替代详情正文。

运行中不得修改 pool/target。正式轮固定抓到可验证来源耗尽，不提供数量或完成模式切换。

## 5. 抓取中的固定行为

### 标签页、网络与人工验证

- 正式浏览器启动后先打开 `/explore` 并确认页面壳已可见，再在首页搜索框逐字输入关键词并回车，不直接
  导航到搜索结果页。回车后须截获第 1 页搜索且主页面进入 `/search_result_ai`、`.search-layout` 可见；
  首页未渲染、搜索未出结果或关键词不符时只允许一次受控恢复：重新打开 `/explore` 再输入；仍失败按
  `runtime_failed/xhs_navigation_recovery_failed:<code>` 停止，不重启浏览器、不换号，也不推进 checkpoint。启动导航会把 `readyState`、DOM/正文长度、主文档状态、页面脚本错误和失败资源的紧凑
  摘要写入 behavior evidence 同目录的 `behavior_evidence.navigation.json`，白屏超时不得只凭外部关闭
  后的 `TargetClosedError` 分类。该文件中所有诊断的页面地址与失败资源地址都只留到路径（不含查询串），也不读取
  页面标题。文件按 `stage` 各保留最近 30 条、整体按写入顺序排列，低频阶段（如 `search_filter`）不会被每访问
  一次作者页就写一条的 `creator_profile_parse` 挤掉。
- 搜索卡片和作者链接在匿名页面也可能存在；可见的精确“登录”按钮优先判定为 `login_required`，
  不得仅凭卡片数或作者链接数把匿名页面判为 ready。主页面在搜索导航、行为、登录恢复或作者补全
  等阶段意外关闭时立即以 `xhs_main_page_closed_unexpected` 终止本轮（浏览器生命周期失败；搜索阶段，
  包括页内登录过期后的恢复等待，记为 `runtime_failed/main_page_closed`，不写
  `candidate_skipped`，不提交未完成批次）；同时观察到
  Chrome 进程、CDP 或 BrowserContext 的生命周期失败时，仍以后者的代码为准。即使 BrowserContext
  中另有同域页面，也不得接管、重试当前阶段或重新启动 Chrome。原主页面仍存活时，平台主动打开的
  普通新页继续只由新页守卫管理。
- 正式 BrowserContext 守卫安装后出现的任何新标签页都立即置前，并从出现起至少保留 5 秒；平台
  弹页、作者主页回退、互动和验证辅助页一视同仁。作者标签页每次打开时另抽一次 5–9 秒的保留时长，
  同样从出现起计，读到作者投影之前不关（见作者补全）。依据：操作人 2026-10-14 要求全面提速（#79），
  此前为一律 20 秒。正常返回、异常、Playwright 退出和最终清理都不得绕过。首个主页面可豁免，启动时
  已有的额外页仍受保护。
- 明确登录、扫码或验证码页置前并进入本轮共享的 600 秒人工处理预算，新标签页保护期不能缩短它，切换
  页面也不能重新计时。
- 搜索连续性出现登录要求或图片验证时，保持当前页并写
  `operator_verification_events`；完成后继续，人工预算耗尽时失败。
- 规则端点（搜索 `search/notes`、详情 `feed`、`user/me`）返回 461/471 时被动等待：30 秒内在所有页面的
  顶层与子 frame 观察验证界面（可见标记、`/website-login/captcha` 路由、人工检查选择器），出现即置前并
  在共享人工预算内等它消失，再连续两次确认主页面有可见文本才算完成，写 `operator_verification_events`；
  随后搜索阶段重新滚动（前端已重搜则按会话重置），详情阶段整篇重做一次。程序不自行拼接或打开验证页
  地址。30 秒内既无验证界面、原数据也没有到达时为 `runtime_failed/xhs_verification_not_surfaced`。
  `Requests too frequent` 等可见频控优先于验证页标题分类，立即按运行级阻断停止，不点击刷新绕过。
- 状态哨兵只对规则端点做运行级判定。非规则端点（埋点、安全脚本、评论、分享码等）的
  401/403/429/461/471 只写账本 `signal` 诊断，不读响应体；其中 461/471 同一批共用一次 30 秒的验证
  界面检查，出现界面按上一条处理，否则继续运行。
- 规则端点出现业务码 -101 或登录过期文案、截获 `user/me` 为 `guest=true`，或主页面出现可见登录 UI 时，
  置前原有抓取页，使用该页已经出现的二维码等待人工登录；不新开登录标签页，也不把最新打开的作者页
  或辅助页接管为主页面。可见登录 UI 恢复、且恢复开始后截获到 `guest=false` 的 `user/me` 才算恢复；
  前端没有自行请求 `user/me` 时只允许在 UI 确认后 reload 一次。恢复后主页面仍在原搜索列表则继续扫描，
  否则重新从首页进入搜索；单篇处理中的恢复整篇重做一次。等待期间不导航原页。恢复等待中出现终态
  弹窗、人工预算耗尽、网络恢复超时、`300012`、Playwright 浏览器错误或生命周期失败时，与直接遇到
  同类失败写同一 `runtime_failed/<stop_detail>` 停止事件，不写 `source_exhausted`。
- “安全限制”、账号异常、`300011/300012`、`/website-login/error`、频控或封禁属于运行级阻断，
  立即停止，不能进入人工验证码等待或候选跳过。
- 规则请求 `requestfailed` 的可识别 `net::ERR_*` 或导航失败只进入 `network_paused`，保留同一 Chrome、
  BrowserContext 和 Page，以最短 2 秒、最长 30 秒退避后重触发原动作（搜索重滚一步、详情整篇重做、
  作者重点作者名）；父层继续验证认证心跳，不因网络中断或整轮运行较久主动
  关闭浏览器。runner 固定下发 600 秒网络恢复预算，不受调用者环境覆盖；预算耗尽时写 `network_recovery_timeout` 并保留最后安全 checkpoint，不重启 Chrome、不创建新 Context 或新
  页面重放。
- page、context 或 browser 实际关闭以及 CDP 断开均为终端浏览器错误；生命周期证据必须区分计划关闭
  与非计划关闭，本轮不得启动第二个 Chrome 或 BrowserContext。

### 搜索与全量扫描

- 搜索固定为“综合 + 图文”。回车后进入 `/search_result_ai`，前端先发不带 `filters` 的“综合 + 不限”
  第 1 页，这一会话只截获、不处理、不滚动。结果页有两种已知布局（依据：V16 与 Mac 人工侦察 2026-10-13）：
  V16 的入口在 `.search-layout` 内，面板 `.filters-wrapper` 在其旁按显隐切换；AI 对话布局
  （`.ai-feeds-page.with-ai-chat`，右侧有可独立滚动的对话栏）的入口在 `.ai-chat-inner` 内，面板挂在入口
  内部，收起时从 DOM 移除，`.search-layout` 内没有入口。因此入口在全文档 `div.filter` 中取第一个可见、
  且直属 `span` 文字精确为“筛选”或“已筛选”的节点（前面可能有隐藏或零尺寸的同类节点）；入口文字只读
  这个 `span`（面板开着时入口的整体文字含面板内容），开面板与回退收起时点它（落点在 `span` 框内）。选项每个标签有一份坐标
  重叠的 `aria-hidden="true"` 副本，排除自身或祖先带该属性的节点后按精确文字取可见的那个。入口未渲染时
  每次尝试按 0.25 秒轮询最多等 15 秒，出现后停顿 0.6–1.8 秒，光标接近入口并按样本静止后先读一次状态：
  面板已因悬停打开且有“图文”就不再按下入口（按下会把它关上，#81），只记一行 info 日志
  `Filter panel opened on hover`，不另写账本动作；否则按下入口，再按 0.25 秒轮询最多 20 次等面板与选项出现，
  最后一次读到面板可见却没有选项为 `filter_option_missing`，否则为 `filter_panel_not_opened`。随后按精确文字
  点选“图文”（各组 `.tags` 结构相同，不按位置）。面板由悬停打开时，光标离开入口与面板就会收起（第 6 轮
  实测 2026-10-18：悬停打开后第 1 次点选落空、等满约 30 秒报 `filter_not_confirmed`，失败时 `panel_count=0`）：
  从入口到“图文”这一段的接近轨迹（主移动、越过或没够到的落点与修正段）每个点都夹在入口框（`span`）与面板框
  （第一个可见的 `.filters-wrapper`）的并集内，时长、事件间隔与落点分布照常；按下入口打开的照常接近。两种情况
  都在到达“图文”并静止后、按下前再读一次状态，面板仍可见且选项仍在才按下，否则不按，本次尝试立即以
  `filter_panel_closed_before_option` 失败并重试（账本里这次的 `note_type` 动作没有对应的按下）。以入口文字变为
  “已筛选”且截获到目标会话第 1 页（5 项 `filters`：`sort_type=general`、`filter_note_type=普通笔记`，其余三项
  “不限”）判定成功。入口始终未出现、面板未打开、选项缺失、点选前面板已收起或点选后未确认都只算本次尝试失败，
  重试一次后报 `target_filter_not_applied`。任何滚动之前收起面板（#81）：先把光标移出入口与面板的并集，
  落点取结果列表区域（同滚动的列表区域，#88）中入口与面板下沿以下、留出 24px 的部分，落点分布与轨迹按人类
  样本、不静止也不按下，再按 0.25 秒轮询至多 8 次确认面板不可见；确认不了（或该部分不足 40px 高）才按原来的
  方式点入口并确认收起，失败重试一次后报 `filter_panel_not_closed`，回退时记一行 info 日志
  `Filter panel stayed open after the pointer left`。原先从“图文”移回入口再按下，途中面板先自行收起、进入口
  又被悬停打开、按下再关上（开 → 关 → 再开）。两种方式都写 `apply_filter` 的 `panel_close` 动作，`target.method`
  为 `pointer_leave`（指针记录只有落点比例、接近时长与移动事件数）或 `entry_press`；成功时的筛选动作序列为
  `panel_open`、`note_type`、`panel_close(pointer_leave)`，回退时末尾再加 `panel_close(entry_press)`。目标会话第 1 页
  的关键词与目标不符时重新输入一次，仍不符报 `search_keyword_mismatch`。三者都以
  `xhs_navigation_recovery_failed:<code>` 停止。
- 筛选诊断写入 `behavior_evidence.navigation.json`，`stage=search_filter`，只在失败时统计：`.search-layout`
  个数、全文档 `div.filter` 个数、入口可见性、`.filters-wrapper` 个数（`panel_count`）与其中可见的个数
  （`panel_visible_count`；轮询的 `panel_visible` 只看第一个）、精确文字节点个数，视口尺寸与滚动位置，以及至多
  2 条文字节点的 6 层祖先链（class 内长字母数字片段遮为 `<id>`），不含正文或地址。
  - 每次失败的打开或点选尝试（包括第 1 次失败、重试成功）写一条，`outcome` 为 `attempt_<n>:<子原因>`
    （`filter_entry_missing`、`filter_panel_not_opened`、`filter_option_missing`、`filter_panel_closed_before_option`
    或 `filter_not_confirmed`），另带 `moments`：接近入口前（`before_approach`）、光标到达入口静止后按下前
    （`before_press`）、按下后第 1 次轮询（`first_poll`）与轮询结束时（`poll_end`）、到达“图文”静止后按下前
    （`before_option_press`）的 `panel_visible`、`option`（布尔）与 `entry_text`（只取“筛选”“已筛选”或空串）；
    入口始终未出现时 `moments` 为空，悬停已打开时没有两次轮询。日志对应一行不含地址的
    `Target filter attempt <n> failed`，带同样的计数与 `moments`。
  - 两个筛选停止码在停止前再追加一条最终诊断，`outcome` 为最后一次尝试的子原因或 `filter_panel_not_closed`，
    不带 `moments`；日志同时有一行不含地址的 `Search filter failed`。
- 列表的滚动方式（依据：Mac 人工测量 2026-10-13）：AI 对话布局下结果列表在 `.search-layout` 的某个祖先
  容器内滚动，`window.scrollY` 始终为 0，顶部约 116px 的搜索框区域固定不动；右侧对话栏与列表并排、不重叠，
  可独立滚动，里面有笔记链接与 `data-note-id` 元素，但没有 `section.note-item`。关闭对话栏后的布局（V16）
  整页滚动。程序不关闭对话栏，也不为布局增加界面动作，两种布局按同一口径运行：滚动容器取 `.search-layout`
  （优先 `.ai-feeds-page` 下的）自身或向上第一个 `overflow-y` 为 `auto`、`scroll` 或 `overlay` 且
  `scrollHeight` 比 `clientHeight` 大 1px 以上的元素（到文档根为止），找不到为文档。“滚动位置”指该容器的
  `scrollTop`（文档时为 `window.scrollY`），到底判断（滚动位置加容器可见高度达到 `scrollHeight` 减 2px）、
  回到顶部和账本 `scroll_y_before` 都用它；“列表可见区”是容器可见区与视口的交集，文档时为整个视口。
  容器类型变化时日志记一行 `List scroll container`，只含几何数值。卡片和就绪计数里的笔记链接、作者链接只在
  `.search-layout` 内找（取不到时为全文档），对话栏里的不算。
- 扫描滚动时光标位于结果列表 `.search-layout` 与列表可见区相交的矩形内，横纵各内缩 20%，以免 AI 对话
  布局下滚轮落在对话栏上；取不到该矩形时为列表可见区内宽 30%–70%、高 35%–75% 的区域。光标已在区域内就
  不动，否则按轨迹移进区域。每一步的位移由若干触控板手势凑成，总和恰为该步距离。行为阶段与连续性行为
  使用同一个内缩矩形，但取不到时不退回可见区（见“节奏与行为”）。
- 地址栏里的关键词经两次编码；行为门禁的 `target_url_ok` 对地址逐层解码（最多 3 层），任一层含关键词
  即为真，通用平台单次编码的结果不变。“真实关键词页”另由账本证明：目标会话第 1 页的
  `keyword_matches_target=true`。
- 查询指纹并入目标排序与类型的语义标签（`search_sort=general`、`search_note_type=image`）。升级后首轮
  使用新指纹：dry-run 显示 `checkpoint_found=false`；已入库笔记照常滑过；旧指纹 seen 中未入库的 ID 重新
  判定一次；旧的未完成累计不再续上；旧 checkpoint 与 seen 行保留作审计；升级前不需要先跑完旧轮次。
- 每轮从目标会话顶部全量滚到末页 `has_more=false`，不保存深层位置，也没有顶部刷新。按 `data-index`
  顺序处理列表可见区内的卡片：数据库、账号 seen、累计摘要和本轮已决定的 ID 直接滑过；搜索项为视频的在
  点击前记决定；其余未知图文在可点击时就地点开。可点击指封面中心在可点击带内（列表可见区顶部加其高度
  10% 起，到其底边上方 8px 止），且该点 `elementFromPoint` 落在这张卡片内；被固定头部、对话栏或弹层遮住
  的不点。单步滚动不超过列表可见区高度的 0.8 倍；可见区高于 80px 时单步小于可点击带高度，封面中心一步
  越不过整条带，不会跳过一行。已滑到可点击带上方仍未决定的卡片向上补漏，同一卡片补漏 3 次仍够不着即
  `xhs_search_items_unresolved`。
- 扫描以外的滚动（网络恢复或验证后的重触发、行为阶段、关闭浮层后前端恢复滚动位置）可能在扫描读卡片之前
  就越过一行（#89：重触发一步滚过第 1 页首行，第 2 页首行出现即把第 1 页判为越过）。因此已越过的页里从未
  读到的未决定项先向上补漏（#92）：用同一种滚轮手势在列表区域内向上滚一步（`scroll` 动作 `phase=backfill_unread`，
  光标照常限定在列表区域），立即读一次可见卡片，交回扫描；读到的项照常登记、决定，日志记一行
  `Backfill reached unread search items: page=…, found=…, index=[a, b], still_unread=…, steps=…`。列表已到顶，或
  本页已向上补漏 3 步仍没读到，才按 `xhs_search_items_unresolved` 停止；这些项不记候选失败，也不跳过。
- 非笔记条目（依据：Mac 人工取证 2026-10-14）：搜索响应里 `model_type` 不是 `note` 的条目（如 `hot_query`
  相关搜索卡，id 不是 24 位十六进制）在列表中同样占一个 `data-index` 位置，渲染为没有封面的
  `section.note-item`。截获响应时把带 id 的非笔记条目登记为该会话、该页的已知非笔记条目；扫描读到本会话
  已知非笔记条目的卡片直接跳过：不计未登记、不作候选、不点击、不写账本，也不参与“本页卡片全部出现过”与
  “越过本页”的判断（两者只看笔记条目）。该页完成时若有非笔记条目，日志记一行
  `Search page <k> finished: non_note_items=<响应条目数>, non_note_cards_skipped=<列表中见到的不同 id 数>`。
- 前端自行重搜（出现新的第 1 页会话）时：目标会话直接采用；丢了“图文”则重新点选并收起面板；关键词
  不符则回首页重新输入；都从新会话顶部继续。会话重置时未完成的页记一次 `batch_complete=false` 的批次
  事件，不发布。
- 运行级停止（都不推断来源耗尽）：有 `data-note-id`、却既不是本会话笔记也不是已知非笔记条目（响应里从未
  出现过），且连续 2 次读数仍未登记的卡片为
  `xhs_search_card_unregistered`；目标会话第 k 页到达时缺第 k−1 页为 `xhs_search_page_gap`；已越过的页内
  未知项向上补漏到顶或满 3 步仍没读到、或到底部仍有未决定项为 `xhs_search_items_unresolved`；到底部等不到新页，检查可见状态
  并上下抖动 3 次后仍没有新页为 `xhs_search_load_stalled`；搜索响应未知业务码为 `search_request_failed`。
- 每页已越过且全部决定后写 `adaptive_batch_completed`；只有本页有新增决定时才发布批次。末页
  `has_more=false` 完成即 `source_exhausted(has_more_false)`，停止事件的 `source_visit_id` 指向该搜索访问。

### 单篇处理

- 点击封面前打开本篇唯一的图片窗口：窗口先持有图片响应句柄、不读体，feed 给出期望 URL 后只对比对键相同
  的句柄读体，因此首图早于 feed 到达也能认领。比对键把 http 与 https 视为同一地址，主机、路径（含时间段、
  签名段、文件 ID 与 `!` 之后的样式）与查询串仍须完全一致：feed 给的是 http 地址，https 页面上的浏览器会把
  这类混合内容图片自动升级为 https 再请求（第 6 轮 96 篇全部 `image_response_not_observed`，#93）。清单
  `source_url` 与暂存资产保持 feed 原值。浮层出现并截获本篇 feed 后写详情访问；feed 中为视频则
  关闭浮层、记决定。浮层未出现 3–6 秒后重点，出现但没有 feed 时先关闭、4–8 秒后重开，两次都没有 feed
  记可重试候选 `note_detail_not_observed`；连续 3 篇浮层都不出现为 `xhs_overlay_not_opening`。
- 正文可见后做详情停顿，再翻图：每次翻到下一张前停 0.8–1.8 秒（只写账本 `note_image` 停顿），约四分之一
  按 → 键、其余点右箭头，按页码 k/N 核实前进，不前进时换一种方式再试一次。作者见下一小节。有效记录
  如请求了互动，在当前浮层内执行，不新开标签页。最后点关闭按钮关闭浮层（不按 Esc，5 秒内确认，重试
  一次仍关不掉为 `xhs_overlay_close_failed`），再暂存截获的图片并写记录。
- 互动计数取证（#94，只取证、判定未变）：翻图之后、点作者之前（repair 整页在同一位置）读一次详情视图根
  （浮层为 `div.note-detail-mask`，repair 另认整页根）内的互动栏与评论区。结构按操作人 2026-10-18 实地查看：
  互动栏根 `.buttons.engage-bar-style`，其 `.left` 内为点赞 `.like-wrapper span.count`（可另带 `like-active`
  等状态类）、收藏 `.collect-wrapper span.count`、评论 `.chat-wrapper span.count`；分享 `.share-wrapper` 只有
  图标，网页上不显示分享数；评论区总数行 `div.total`（“共 N 条评论”）、空状态 `.no-comments-text`，另记评论区
  容器 `.comments-container` 是否存在。评论区的评论也有 `.like-wrapper span.count`，所以三处计数只在互动栏
  `.left` 内找。评论区还没给出总数行或空状态时每 0.25 秒再读，至多 2 秒。读数失败只记类别（`timeout`、
  `script_error`，视图不在为 `view_missing`），不影响本篇的处理。有效性判定不变：四个计数仍须非空，空串仍算
  缺失，相应笔记仍判无效（用户 2026-10-18 决定：空串须有明确依据才能按 0 处理）。
- 单篇内的登录恢复、验证完成或网络恢复后整篇重做一次：关闭残留浮层、在可点击带内重新找到卡片再点开，
  沿用同一图片窗口；同一文档重开时图片可能走内存缓存而没有响应，记录可以引用早先尝试截获的图片访问。
  第二次登录或网络中断照常上抛给扫描；第二次因验证或作者页登录重做、或重做时卡片已不在可点击带内，
  交回扫描重新处理。重做后仍缺图记可重试候选 `image_response_not_observed`。

### 节奏与行为

- 搜索并发为 1。`xhs_guarded` 只在搜索结果页执行；使用少量触控板手势滚动并验证实际位移，不扫描隐藏 HTML
  文本。运行指纹必须完整，`navigator.webdriver` 必须是原生 `false`（`undefined` 或 `true` 都不通过）。
- 行为阶段（`dwell_on_list` 的随机移动与滚动）和连续性行为的随机移动都限定在结果列表区域内（#88）：区域与
  扫描滚动相同，即 `.search-layout` 与列表可见区相交的矩形横纵各内缩 20%。AI 对话布局下列表可见区是滚动容器
  的可见区，整页滚动的布局为视口；内缩让目标点与滚轮落点离开右侧对话栏（含筛选入口）和顶部固定的搜索框区域，
  轨迹越过目标至多 115px，也不会进入对话栏。随机移动开始时取一次区域，滚动每一轮重取。行为阶段判断滚动
  是否有效只看列表自身的滚动位置（AI 对话布局为容器的 `scrollTop`，整页滚动为 `window.scrollY`，滚动容器类型
  须前后一致），对话栏或其他节点的滚动不算，也不使用触摸滚动。证据中 `mouse_moves` 与 `scroll_target` 事件
  另记 `area`（所用区域的 `left`、`top`、`width`、`height`）与 `scroll_container`（`element` 或 `document`），
  `wheel_scroll` 的 `before`、`after` 为列表的 `scroll_y` 与 `scroll_container`。其他平台与档位仍按整个视口与
  全页可滚动节点。
- 取不到区域（页面没有 `.search-layout` 为 `search_layout_missing`，列表与可见区不相交为 `list_not_in_view`）
  时不退回整个视口。行为阶段的 `mouse_moves` 是必需事件，按 `human_behavior_failed:xhs:…xhs_list_area_unavailable:<原因>`
  失败（证据 `status=failed`、`error` 带原因），扫描随即报行为阶段未完成。连续性行为的随机移动只是装饰性动作，
  跳过这次移动、在事件里记 `{"event": "mouse_moves_skipped", "reason": <原因>}`，之后照常做最终状态检查，
  记录照常为 `completed`（例如人工验证刚结束、列表尚未渲染）；页面不对由扫描自身的检查处理。
- 各档停顿写行为证据 `request_pacing_events` 与账本 `pause`；搜索两档另执行连续性行为（先停 0.8–2 秒，
  再在列表区域内移动鼠标）并写 `continuity_events`。`xhs_guarded` 的详情长停留与详情间冷却不启用。
- 取值依据：操作人 2026-10-14 要求全面提速（#79）。各档取值是代码中的具名常量（行为阶段列表停留在共享
  运行时 `xhs_guarded` 档位），不设配置项；门禁只核对停顿是否存在及其位置，不核对秒数。

| 档位 | 取值 | 落点 |
|---|---|---|
| 行为阶段列表停留 `list_dwell_initial` | 8–20 秒 | 每轮一次的行为阶段开头（搜索结果页）；只写行为证据 `events`，不是节奏档位 |
| `search_results` | 2–5 秒 | 每页第一张未处理卡片点击前，每页至多一次 |
| `search_page` | 4–10 秒 | 每截获一页 `has_more=true` 的目标会话页后、下一次程序滚动之前；已登记页的重复送达（例如网络恢复后前端重新请求同一页）同样要停；初始行为阶段之前那一次即第 1 页的应用，停顿中到达的页需再停 |
| `note_detail` | 2–4 秒 | 浮层正文可见后、翻图前（repair 为整页状态读到后、翻图前） |
| `creator_profile` | 2–5 秒 | 点击作者名之前；读到投影后不额外停留，按打开时抽到的 5–9 秒保留关闭 |
| 翻图 `note_image` | 0.8–1.8 秒 | 每次翻到下一张之前；只写账本，不入 `request_pacing_events`，不列入必需阶段 |

### 作者补全

- 作者补全只走浏览器：作者停顿后在浮层（repair 为整页）内点击作者名，等待前台新标签页（每次 10 秒，最多
  两次，都没打开记 `author_tab_not_opened`）。作者链接按作者 ID 定位（依据：Mac 人工取证 2026-10-14）：在详情
  视图根节点内取路径为 `/user/profile/<本篇作者 ID>`（作者 ID 取自 feed 的 note_card，repair 取自页面状态投影）、
  可见且不在评论区（祖先不含 `.comment-item`、`.comment-inner-container`、`.comments-container`、
  `.comment-list`）的链接，优先有文字的 `a.name`，没有时用头像链接。旧位置 `div.author` 下隐藏的作者名、
  评论者链接与作者本人的评论都不点；不能简单取第一个可见的 `.author a.name`（评论者的作者名也在
  `div.author` 下）。点击前先检查原抓取页，原页已出现登录或验证时直接在原页
  等待，不再点击；作者页遇到登录失效也回到原抓取页恢复会话，成功后整篇重做。作者专属安全验证仍在
  对应页面等待操作人。作者标签页每次打开时抽一次 5–9 秒保留时长（从出现起计，操作人 2026-10-14 要求
  全面提速，#79），读到投影之前不关；读完时已过保留期则立即关闭，否则等到期满。关闭前写 `close_tab`，
  `target.hold_seconds` 为本次抽到的保留秒数，供实测统计；随后主页面置前。repair 中点击后主页面同页
  跳转到作者页时，读完投影写 `go_back` 返回笔记页。
- 作者页先做到达与小幅滚动检查，再进入有界就绪等待，预算 10 秒。`domcontentloaded` 只是前置条件，
  超时记 `page_not_ready` 后继续。随后经页面取数通道读取状态投影 `creator_page_data`（接受时写作者
  访问），只读页面已执行的 `window.__INITIAL_STATE__.user.userPageData`。只有自有数据属性
  `__v_isRef === true` 的节点才按 Vue ref 解包；脚本按白名单只复制标量字段，不序列化整份状态，也不
  返回 HTML。白名单分三部分：
  - 仓库内下游读取的字段：`basicInfo` 的用户 ID、昵称、简介、性别、IP 属地，粉丝、关注、笔记与获赞
    指标键，以及 `interactions` 条目的 `type/name/key/count/num/value`；
  - 研究项目经 `creator_profile_json` 读取的字段：`interactions[].i18nCount`、`tags[].tagType/name`、
    `verifyInfo.redOfficialVerifyType`；
  - 平台号 `basicInfo.redId`：字符串，同样参与头像同值删除。

  头像（`imageb`、`images`、`avatar*`、`icon`）与任何凭据或 token 字段都不投影；计数类字段不投影布尔值。
- 投影前，脚本先在该作者记录内收集头像证据 URL。证据键与路径直接取自 `records.sanitization`：
  `AUTHOR_AVATAR_KEYS` 不区分大小写，另加 `basicInfo.imageb`、`basicInfo.images`。收集有深度与节点
  上限，并检测循环引用。投影后，任何字符串字段去掉首尾空白后若与证据 URL 完全相同即删除。空白集合
  与 Python `str.strip()` 相同（含 `\x85`，不含 BOM）。证据集合不返回，因此同值删除只在该作者记录内
  生效；笔记级头像仍由 Python 清理器按键删除。
- 脚本只经属性描述符读取自有数据属性，不读取访问器属性，不调用 `toJSON`；遇到 Proxy 时，其 trap
  仍可能执行页面代码。遇到超限、循环引用、访问器属性、函数或未知对象类型（Set、Map、Date 除外）时，
  整个投影被拒绝（`projection_avatar_check_incomplete`）。未求值的 computed ref 按缺失处理。
- 就绪与成功条件：粉丝计数必须非布尔，且能按 `records.formal.parse_int` 解析（含“万/亿”）。计数按原值
  保存（如 `1.2万`）；0 是真实观察值，不补值。仅有 `interactions` 数组不算就绪。每次读取投影后，
  先做生命周期、登录、验证、阻断与封禁检查，然后才接受结果或重试，每 0.5 秒重试一次。出现验证标记时，
  先进入既有验证等待；人工验证完成后，每轮做一次检查，再读取一次投影，这一步不会重入就绪等待。读到
  结果后、接受之前，依次复查生命周期（主页面与作者页都必须存活）、可见阻断与封禁、人工流程（验证重现
  时丢弃本次结果，在同一 ticket 的预算内继续等待；出现登录要求时转入登录恢复）和 ticket 预算（已耗尽时
  按 `xhs_manual_checkpoint_budget_exhausted` 失败，不接受超时之后才拿到的数据）。取消以及页面、浏览器
  关闭照常向上传播。
- 作者页地址路径 `/user/profile/<id>` 中的作者 ID 必须与 feed 的 `user_id` 一致，否则判 `creator_mismatch`。
  `creator_profile_json` 只含上面列出的白名单字段；下游如果读取白名单外的字段（例如 `extraInfo`、
  `tags[].icon`），在记录里会缺失。
- `creator_profile_parse` 导航诊断在就绪等待结束时写一次（`outcome` 取 `ok` 或原因类别），就绪等待中
  转入登录恢复时写 `ok_login_recovery` 或 `login_recovery_empty`，验证等待中读到作者资料且复查通过时写
  `ok`，判定作者不一致时写 `creator_mismatch`。到达或滚动检查直接转入登录恢复、验证等待因人工预算耗尽
  而抛错、验证等待中转入登录恢复和复查时验证重现，都不写。
- 失败时 `candidate_skipped.error_code=creator_profile_unavailable`，`detail` 为
  `creator_profile_failed:<原因>`（多个原因以逗号连接），原因只含类别：

| 原因 | 可重试 |
|---|---|
| `author_tab_not_opened`、`author_tab_closed` | 是 |
| `page_not_ready`、`runtime_projection_empty`、`runtime_projection_timeout`、`runtime_projection_error`、`followers_unobserved` | 是 |
| `login_recovery_empty`、`verification_wait:<原因>` | 是 |
| `creator_mismatch`、`projection_avatar_check_incomplete` | 否（决定性结果，重试不会改变） |

- 成功作者结果只在本轮按作者 ID 缓存，不替代来源证据；缓存命中的记录引用首次打开该作者时的访问。

### 访问证据

- 每轮在行为证据同目录写只追加的 `behavior_evidence.content_visits.jsonl`，以单一单调序号记录动作
  （导航、输入、回车、筛选、滚动、点卡片、翻图、点作者、关闭）、页面响应与页面状态访问、停顿、决定和
  诊断信号；不截断，不写 token、查询值、正文、头像或完整图片 URL，并列入 1200 秒看门狗的进展路径。
  批次快照固化账本并计入哈希；累计摘要的记录携带各轮账本快照，跨轮累计记录逐条核验。
- 每条记录的 `content_visit_refs` 引用 `ledger_id` 与搜索、详情、作者、逐张图片的访问 ID，随
  `raw_sample_json` 入库。`content_visit_ok` 按访问序号逐条核验（问题码 R1–R7）：
  1. 引用齐全且都能在账本中解析，作者与图片访问和详情访问同属一个账本；
  2. 搜索访问属于目标会话、状态 200，含该 ID 且该项不是视频；
  3. 详情访问是本篇 feed 的页面响应（状态 200），由同一 ID 的 `click_card` 触发，且晚于搜索访问；
  4. 作者访问是作者页页面状态，作者 ID 与页面地址一致，由某次详情访问之后的 `click_author` 触发，并晚于
     该动作；
  5. 图片访问的资产键与 manifest 的 `source_asset_key`、哈希与 manifest 的 sha256 一致，且晚于该笔记在
     本账本中的首个 `click_card`（允许首图早于 feed、允许引用早先尝试）；
  6. 搜索结果、详情、作者三档停顿分别位于该页首次点卡片、本篇首个后续动作、点作者名之前；
  7. 一个详情或图片访问只被一条记录引用。
- 全轮规则：G1 目标会话第 1 页由点选“图文”触发且关键词相符，之前有回车与非目标第 1 页；G2 来源耗尽
  的停止事件指向目标会话 `has_more=false` 的搜索访问，且同会话各页连续；G3 相邻两页之间若有程序滚动，
  第一次滚动之前须有始于前一页（重复送达时取最近一次）之后的 `search_page` 停顿，没有滚动则记为前端自行加载
  （`frontend_self_loads`），缺前一页判失败。
- 账本级检查（G1、G3、必需节奏与连续性）只对被引用的账本逐个判定：有效记录的 `content_visit_refs`
  （搜索、详情、作者、逐张图片）或 discovery 停止事件的 `source_visit_id` 能解析到的账本。累计摘要携带、
  但没有任何引用的账本（例如停在筛选的失败轮）照常加载，只用于解析引用；依据为 Mac 实测 2026-10-14：
  第 1 轮失败账本随累计摘要进入第 2、3 轮，报 G1。一个账本都加载不到仍报 `LEDGER`；加载到账本但没有
  被引用的账本时不做账本级检查，`request_pacing_ok`、`continuity_ok` 为 false。门禁结果的 `ledger_count`
  为加载的账本数，`checked_ledger_count` 为被引用的账本数。
- 小红书的 `request_pacing_ok` 与 `continuity_ok` 改由被引用的账本按本轮实际访问推导：有详情访问才要求
  `search_results`、`note_detail`，有点作者才要求 `creator_profile`，至少一次下一页由程序滚动触发才要求
  `search_page`；必需的搜索两档各至少有一次连续性完成。整轮没有新笔记时不要求详情与作者阶段。
- 任一规则不满足即 `behavior_evidence_ok=false`；门禁平台缺少门禁结果同样失败
  （`visit_validation_missing`）。
- 失败诊断写入 `behavior_evidence.navigation.json`（不进账本）：
  `xhs_search_card_unregistered` 停止前追加 `stage=search_scan`、`outcome=card_unregistered`，列出每个未登记
  ID 的卡片结构（`data-index`、有无封面、封面路径首段、`tag.class`、在列表可见区的位置）与它在本轮全部搜索
  响应（含非目标会话与非笔记条目，只在内存中保留）中的出处（会话起始序号、是否目标会话、页码、页内位置、
  `model_type`、卡片类型 `note_card.type`，从未出现为 `null`）；已越过的页内未知项向上补漏仍没读到而以
  `xhs_search_items_unresolved` 停止前追加 `stage=search_scan`、`outcome=items_never_rendered`：
  本页页码、条目数、未决定数、从未出现数、已观察数及其 `data-index` 范围、已跳过的非笔记卡片数、可见卡片的
  `data-index` 范围、列表滚动位置与可见区，`passed_by` 为首次把本页判为越过的来源（`later_page` 为后面某页的
  卡片出现：该卡片的页码、ID、页内位置、`data-index`、相对列表可见区顶部的纵坐标与当时的滚动位置；
  `list_bottom` 为末页已到底），`items` 列出至多 10 个从未出现的 ID 的页内位置、卡片类型、目标会话中也含它的
  其他页（跨页重复）、结果列表内的卡片读数（没有为 `nodes=0`）与同上的响应出处；`backfill` 为补漏记录：已补漏
  步数 `attempts` 与上限 `limit`（3）、`target_index`（这些项中页内位置最靠前者的 `data-index` 下限：按本页已读到
  项的页内位置推算，跨页重复项不参与）、停止时是否到顶 `at_top`，`steps` 为每步滚动后的滚动位置、可见卡片
  `data-index` 范围与是否到顶。日志另记一行 `Search items were never rendered`（含
  `backfill=<步数>/<上限>:target=<下限>:top=<是否到顶>`）；某篇有未截获的正文图时，在记失败行并抛出之前追加 `stage=note_images`、
  `outcome=image_response_not_observed`（只诊断，仍是第一张缺失即本篇可重试失败）：笔记 ID、期望与截获张数、
  全部缺失的 `source_index`；本篇窗口内见到、没有匹配上任何期望的图片响应总数及按（协议、主机、样式）分组的
  计数（至多 16 组）；逐个缺失项的期望协议、主机、样式，是否见过比对键相同的响应（读体失败或句柄被挤出时为
  true），是否见过同一文件 ID（路径最后一段 `!` 之前）的其他响应及差异部分（`scheme`、`host`、`path` 即文件 ID
  之前的路径段、`style`、`query`）；窗口的读体失败数与通道 `dropped_responses` 在本窗口期间的增量；失败请求
  只留协议、主机与样式。窗口为此另记至多 256 个不同响应地址的摘要与形态（超出部分计入 `untracked_responses`），
  诊断与日志 `Note images not captured` 都不含图片地址、时间段、签名段、文件 ID 或查询串；作者标签页两次都没打开时追加 `stage=creator_profile`，最后一次没有可见作者名链接为
  `author_link_missing`、点击后新标签页超时为 `author_tab_timeout`（另记点击后主页面路径模板），含浮层内作者名
  链接、作者主页链接与指向本篇作者的主页链接的个数、可见个数，以及前 4 个主页链接的 `target` 与祖先链。目标会话第 1 页的触发动作不是本次点选“图文”
  （G1 将失败）时只写一行 warning 日志，带触发动作、`target` 与回车会话的序号，判定不变。
- 互动计数取证（#94）逐篇追加到 `behavior_evidence.engagement.jsonl`（不进账本、不截断；整篇重做时同一笔记
  有多行）：笔记 ID、`mode`、`read`，读数成功时另记互动栏根 `bar` 与 `.left` 是否存在。四个计数各记 feed 原值
  类别 `feed`（`number` 带 `feed_value`，按 `parse_int` 解析、含“万”；`empty_string`；`missing` 即键不存在；
  `null`；`other`）与页面显示类别 `page`（`number` 带 `page_value`；`label` 为非空的非数字文字，只记类别；
  `empty` 为元素在但文字为空；`absent` 为找不到元素，带互动栏 `.left` 内匹配个数 `matches` 与整个视图内同类
  包装元素个数 `wrappers`；分享有按钮无 `.count` 为 `icon_only`；读数失败为 `unread`）。评论区记 `section`
  （`total` 带总数 `total`、`unparsed_total`、`empty_state`、`container_only`、`not_rendered`、`unread`）与
  `container`、`empty_state` 两个布尔值。不写页面原文、正文、评论、作者或地址；笔记 ID 不进日志。子摘要平台
  记录的 `output.engagement_observation` 按笔记汇总（重做取最后一行）：`lines`、`notes`、`reads`、
  `bar_missing`；`counts.<计数>` 与 `comment_section`（feed 评论数对评论区）各有 `feed_by_page`（feed 类别 ×
  页面类别的篇数）、`both_numbers`（两边都是数字时 `equal`、`different` 的篇数）与 `feed_empty_page`（feed 为
  空串时页面一侧的分布：数字按 `0`、`1-9`、`10+` 分档，其余按类别）。

## 6. 完成检查

### 批次恢复点

正式搜索的完整非耗尽批次在写出 `adaptive_batch_completed` 后，只有本页有新增决定
（`batch_candidate_identity_count > 0`）才发布：立即核验访问账本并固化当前 JSONL、manifest 与访问账本
快照，账本快照计入哈希。没有新增决定的页仍写 `adaptive_batch_completed` 供审计与看门狗，但不触发根
事务。快照以 `.batch-<sequence>.snapshot` 为后缀保存在源文件旁；后续追加写入原 JSONL 或替换原
manifest 不会改变已提交快照，manifest 的相对 staging 图片路径保持有效。快照不复制登录 profile、
Cookie 或浏览器存储。

导出数据目录按首条实际写入记录延迟创建。发布的批次没有新增内容产物时（例如本页新增决定都是视频
或候选失败），允许目录尚不存在，仍提交包含空的本轮文件清单、历史累计产物和分页证据的摘要并等待确认；
不得虚构内容文件。批次已声明有效记录却找不到内容文件时，按
`xhs_batch_checkpoint_content_missing` 失败，不把产物丢失当作空批次。

child 批次摘要及提交确认保存在 `data/runtime/xhs/batch_checkpoints/<run_id>/`。根 runner 通过监督
循环读取摘要，核对精确租约、账号/目标/查询指纹、完整分页事件、JSONL/manifest 哈希与 staging
图片字节，随后在一个 SQLite 事务中提交 checkpoint、seen、累计摘要引用和
`batch_checkpoint_committed` 审计事件，再写确认文件。底层搜索循环不写 SQLite。

child 最长等待 60 秒确认；没有确认即以 `xhs_batch_checkpoint_ack_timeout` 运行级失败结束，不能
继续后续批次。根 runner 在提交确认前消失时，未确认的批次由下一轮全量扫描自然覆盖；已提交批次不依赖
最终 `run_summary.json` 才能恢复。未完成批次不走批次提交，
来源耗尽批次仍由完整终态门禁处理，不能提前标成耗尽。

批次发布失败会记录 `xhs_runtime_terminal(phase=batch_checkpoint)`，保留具体
`xhs_batch_checkpoint_*` 子原因，并归类为不可自动重试的 `runtime_failed`。本地文件异常和等待
提交确认超时不能归为网络超时；已提交的安全恢复点保持有效，不补写来源耗尽。

批次摘要固定为未完成、未入库，只作为后续正式 runner 的恢复输入。本轮在累计记录后追加的平台记录为
`status=incomplete`，`output` 只有 `jsonl_files`、`image_manifest_paths`、`content_visit_paths`，没有计数；
后续轮次的 `summary.md` 把这类记录的计数列显示为“—”，不按 0 计（#91）。下一轮仍重新扫码，重新验证
累计字段、行为、图片与来源耗尽证据后才可入库。收到中断后不再提交新批次，已确认的恢复点保留。
`--no-import`、dry-run 和历史详情修复均不启用该握手。旧版本缺少批次快照的孤儿轮次仍遵循前述
孤儿对账规则，本机制不会自动补签旧终态或把旧产物导入。

### 终态验收

先按[正式契约](../formal-crawl-contract.md)核对五阶段、来源耗尽、行为/策略、图片和真实持久化。小红书
还必须确认：

- 顶层 `run_summary.json` 为 `completed`，账号租约已经释放；
- `discovery.skipped=false`，checkpoint/seen/campaign 没有提交错误；
- `behavior_validation.platforms.xhs.content_visit_ok=true`；`request_pacing_ok`、`continuity_ok` 由访问账本
  按本轮实际访问推导且均为 true（见“访问证据”）；
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
| 登录 UI 已确认但 30 秒与一次 reload 后仍无 `guest=false` 的 `user/me` | `login_runtime_error` | `xhs_login_state_unconfirmed` |

不得把无编号安全限制、账号异常、登录错误页或频控推断成 `300011`；`--retry-on-300011` 仍只接受
精确 `platform_security_limit_300011` 及完整停止、清理和 checkpoint 对账证据。登录阶段在第一个分页
事件前被阻断时，顶层 `discovery.reason=runtime_blocked_before_pagination` 且保留原 checkpoint；这不是
`terminal_commit_failed`。后者只用于真实 SQLite 终态事务或线性化提交失败。

全量扫描与单篇处理的停止码和候选码如下。“进入 seen”指该 ID 随完整批次写入跨轮
`xhs_discovery_seen_candidates`：运行级失败一律不写；候选失败只有 `retryable=false` 的写入，
`retryable=true` 的只算本轮已决定，下一轮全量扫描到时就地重试。

| 场景 | `stop_detail` / `error_code` | 级别 | 进入 seen |
|---|---|---|---|
| 首页未渲染、搜索未出结果、点选“图文”未生效（含筛选入口缺失）、面板收不起、关键词不符（各含一次受控恢复） | `xhs_navigation_recovery_failed:explore_not_rendered` / `:search_not_rendered` / `:target_filter_not_applied` / `:filter_panel_not_closed` / `:search_keyword_mismatch` | 运行级 | 否 |
| 搜索响应未知业务码或不可恢复的请求失败 | `search_request_failed` | 运行级 | 否 |
| 到底部等不到新页（抖动 3 次后） | `xhs_search_load_stalled` | 运行级 | 否 |
| 有 `data-note-id`、不是本会话笔记或已知非笔记条目的卡片连续 2 次读数未登记 | `xhs_search_card_unregistered` | 运行级 | 否 |
| 目标会话第 k 页到达时缺第 k−1 页 | `xhs_search_page_gap` | 运行级 | 否 |
| 已越过的页内未知项向上补漏到顶或满 3 步仍没读到、补漏 3 次仍够不着或到底部仍未决定 | `xhs_search_items_unresolved` | 运行级 | 否 |
| 连续 3 篇浮层不出现 | `xhs_overlay_not_opening` | 运行级 | 否 |
| 浮层重试一次仍关不掉 | `xhs_overlay_close_failed` | 运行级 | 否 |
| 规则端点 461/471 后 30 秒内既无验证界面也无数据 | `xhs_verification_not_surfaced` | 运行级 | 否 |
| 规则端点 HTTP 401/403 | `login_required` | 运行级 | 否 |
| 规则端点 HTTP 429 | `rate_limited` | 运行级 | 否 |
| 认领到的期望图片响应为 401/403/429 | `image_auth_required` / `image_rate_limited` | 运行级 | 否 |
| 搜索项或 feed 为视频 | 决定（`consider(valid=False)`，账本 `decision` 行） | 决定 | 是 |
| feed 两次都未截获（含非网络类请求失败） | `note_detail_not_observed`，`attempts=2` | 候选，可重试 | 否 |
| feed 未知业务码 | `detail_request_failed` | 候选，可重试 | 否 |
| feed -510000/-510001 | `note_not_found` | 候选，不可重试 | 是 |
| feed 内 ID 不符或形状不合格（`note_card_problems`） | `note_id_mismatch` / `detail_parse_failed` | 候选，不可重试 | 是 |
| 作者补全失败 | `creator_profile_unavailable`，`detail=creator_profile_failed:<原因>`（见“作者补全”） | 候选，按原因 | 按原因 |
| 图片响应未截获（含重做后仍缺）或临时错误 | `image_response_not_observed` 等可重试图片码 | 候选，可重试 | 否 |
| 图片 404/400、格式或解码失败 | 现有 `classified_http_image_error` 与暂存码 | 候选，不可重试 | 是 |
| repair：链接跳转 `/404` | `repair_link_unavailable` | 候选，不可重试 | —（repair 不写 seen） |
| repair：页面壳未出现、10 秒内读不到状态、第二次重开仍中断 | `note_page_not_rendered` / `note_detail_not_observed` / `note_redo_interrupted` | 候选，可重试 | — |
| repair：视频 | `video_skipped`（`content_policy`） | 候选 | — |

| 信号 | 处理 |
|---|---|
| 轮初二维码 | 保持本轮唯一 Chrome；只在纯未扫码且明确过期、连续两次无进展时刷新组件 |
| 连续性登录/图片验证 | 保持页面、人工处理后继续；共用单调 600 秒人工预算 |
| 规则端点 461/471 | 被动等待验证界面消失，完成后搜索重滚或整篇重做；共用单调 600 秒人工预算 |
| 非规则端点 401/403/429/461/471 | 只写账本 `signal`；461/471 出现验证界面才进入等待，否则继续 |
| 作者页二维码 | 保持作者页置前，人工扫码后继续；计入同一人工预算 |
| 任意新标签页 | 立即置前且至少保留 5 秒（作者标签页按打开时抽到的 5–9 秒）；明确验证页按本轮剩余人工预算等待 |
| 短时网络中断 | 同一 Chrome/Context/Page 内暂停并退避；恢复后继续原操作，默认 600 秒 |
| `network_recovery_timeout` | 运行级失败并保留最后安全 checkpoint；不得启动第二个浏览器 |
| page/context/browser 关闭或 CDP 断开 | 终端浏览器失败；记录非计划关闭证据，不得轮内重启 |
| 详情、作者或正文图候选级失败 | 核对 `candidate_skipped` 和 manifest；整帖不入库，只有 `retryable=false` 的 ID 写账号 seen（`retryable=true` 的下一轮重试），继续候选 |
| 登录、频控、安全限制、封禁 | 运行级失败；不写候选 seen，下一轮全量重滚 |
| 完整 `300011` 且已精确释放租约 | 可显式用 `--retry-on-300011` 每 30 分钟同账号续跑；其他阻断不自动重试 |
| manifest 身份、哈希或路径错误 | 停止晋升和 checkpoint，修复代码/产物后重跑 |
| 连续批次没有新有效记录 | 只核对诊断计数并继续；不能据此停止或推断耗尽 |
| `source_exhausted` | 只有停止事件、`source_exhausted_met` 与正式持久化门禁全部通过才完成 |
| 超时或缺少停止事件 | `runtime_failed`，不能推断来源耗尽 |
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

小红书不向操作人开放手工 `--resume-summary`、`--start-page` 或 `--start-cursor`；runner 只在有未完成
累计时把上一份摘要交给 child。checkpoint/seen 以 `target_key + account_id + query_fingerprint` 隔离；
换号不是原账号续跑。分页事件中的页码与 search ID 取自前端搜索请求，只作审计，不是恢复位置。

来源耗尽且入库成功时，checkpoint 保存 `status=exhausted`、完整停止证据和本轮摘要身份，只作审计；
下一轮仍从首页搜索后全量扫描。seen 保留，累计摘要在成功提交后清空。

## 8. 逻辑槽位与临时会话

- `xhs_accounts` 只保存逻辑槽位 ID、操作状态和租约审计字段；槽位必须由操作人显式
  `xhs_accounts.py ensure-slot` 创建，runner 对未知槽位失败关闭。
- 每个正式 run 只在 `data/runtime/xhs/sessions/<run_id>/profile/` 创建权限受限的空临时 profile。
  不读取或保存跨轮 Cookie、localStorage、sessionStorage、storage state、设备标识或平台身份。
- 本轮 Chrome 与 BrowserContext 在内存中持有当前登录状态；项目不读取或复制 Cookie 用于任何浏览器外
  请求，这些值也不得写成下一轮登录输入。下一正式轮仍以空 profile 和新二维码开始。
- 正常完成、可捕获异常和 SIGINT/SIGTERM 都须留下精确浏览器生命周期与清理证据。只有登记进程及
  进程组已消失、临时 session 已删除且 owner token 精确删除成功，才能报告
  `runtime_session_removed=true`、`lease_released=true` 和 `lease_cleanup.ok=true`。
- SQLite 精确租约是同账号互斥事实源；每账号 `flock` 只增强同机竞争保护，不能替代 owner token、
  进程启动身份或 SQLite rowcount。槽位的 quarantine、activate 和 retire 均由操作人决定。
