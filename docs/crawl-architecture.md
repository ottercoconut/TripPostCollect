# 抓取架构

## 组件关系

```text
config/crawl_targets.json
  -> scripts/crawl_runner.py
      -> SQLite crawl_discovery_checkpoints
      -> SQLite crawl_discovery_seen_candidates
      -> data/runtime/crawl_execution_states/<run_id>/<job>.json
      -> scripts/mediacrawler_crawl.py
          -> scripts/crawl_policy.py
          -> scripts/mediacrawler_behavior.py
          -> tools/MediaCrawler
          -> formal_validation
          -> web_posts / web_post_images
      -> scripts/ctf_resource_crawl.py
          -> ctf_captures / ctf_capture_images
          -> scripts/import_ctf_captures.py
          -> web_posts / web_post_images
config/xhs_pool.json + config/xhs_targets.json
  -> scripts/xhs_runner.py
      -> explicit --account-id
      -> SQLite xhs_accounts / xhs_account_leases / xhs_account_events / xhs_runs
      -> SQLite xhs_discovery_checkpoints / xhs_discovery_seen_candidates
      -> data/runtime/xhs/execution_states/<run_id>/<target>.json
      -> decrypt account storage state into an ephemeral runtime file
      -> scripts/mediacrawler_crawl.py --platforms xhs --behavior-profile xhs_guarded
          -> tools/MediaCrawler top refresh + page/search ID frontier
          -> known-ID pre-detail filtering
          -> signed-in no-token creator request
          -> signed-in BrowserContext creator fallback
          -> formal_validation
          -> web_posts / web_post_images
      -> encrypt refreshed storage state and remove runtime plaintext
```

`crawl_runner.py` 是通用平台正式入口；`xhs_runner.py` 是小红书唯一正式入口。两者负责
选择任务、冻结计划、执行逐步门禁和生成报告，执行器不能绕过状态文件宣布完成。

结构化平台进入搜索前统一经过两层强制门禁：父执行器用 `crawl_policy.py` 维护平台会话
间隔、随机抖动、预算和冷却；MediaCrawler 使用已经完成登录确认的当前浏览器页调用
`mediacrawler_behavior.py`，执行随机停留、鼠标和滚动并写出证据。B站 article 是项目的
自定义 API 搜索，因此先在同一持久 profile 完成行为会话并导出该会话 cookie，再执行原
article API；微博、抖音和知乎在各自 MediaCrawler 浏览器内执行 `social_high_risk`；
小红书在独立账号 profile 内执行更慢的 `xhs_guarded`，并在交互前后检查可见挑战。

行为阶段只包围现有抓取逻辑，不修改分页、候选累计、粉丝补全、视频过滤或入库映射。
行为和策略证据任何一项缺失时，JSONL 可以保留用于诊断，但不得进入 SQLite。

`crawl_policy.py` 的 `session_count` 表示连续活跃会话，不是永久累计值：跨 UTC 日或距最后一次
请求完成已达到站点 `cooldown_minutes` 时开始新会话并清零。`max_requests_per_session` 产生的
自动冷却遵循同一空闲重置规则；验证码、频控、封禁等显式冷却仍保持到 `cooldown_until`，
不能被空闲或会话计数重置绕过。

Runner、执行器和登录/诊断输出的 UTC 运行标识统一包含六位微秒
（`YYYYMMDDTHHMMSSffffff+0000`），避免同一秒并发子会话共享输出目录或状态目录。

## 冻结状态

调度器为每个任务冻结配置、执行契约、任务参数和命令。业务阶段固定为计划、命令、
产物、持久化和最终确认。每次进入下一阶段都重新读取磁盘状态并校验 SHA-256；失败
后的阶段保持冻结。

MediaCrawler 的自适应分页会向同一状态文件追加批次事件，包括实际候选、有效唯一数、
本批新增、连续停滞次数、平台页码、游标/search ID、下一恢复位置、批次完整性、发现阶段、
原始返回条数和 `has_more`。循环按
实际候选累计，不按名义页大小预先换算最大页数。空页、明确缺失继续 cursor 或 `has_more=false` 才能生成
`source_exhausted`；请求异常生成 `runtime_failed`；缺少停止事件时执行器不得猜测数据源
已经耗尽。抖音旧 cursor 耗尽后，只有顶部刷新同时发现持久记忆中不存在的新候选 ID、
`has_more=true` 和非空连续 cursor 才建立新
前沿，并记录 reseed 事件。抖音、知乎和小红书按本批没有新增有效且数据库中不存在的记录计算
连续停滞；微博按是否出现不在数据库、累计摘要、`crawl_discovery_seen_candidates` 和本 child 已见集合中的新微博 ID 计算，避免综合搜索连续出现纯文本/视频时
过早停止。状态事件会写 `stagnation_basis`；最终成功仍以正式校验和数据库验证为准。

候选硬上限只限制单次 child 可进入昂贵处理的未知候选，不是预先抓满的页数或记录数。小红书
从 0 开始按页增加实际候选，先做数据库、账号级候选记忆、累计摘要和本轮集合去重，达到有效
新增目标后立即停止；提高目标只改变运行预算，不创建新查询指纹，也不重置已有前沿。

通用结构化任务的发现位置保存在 `crawl_discovery_checkpoints`，唯一键是任务 ID 与查询指纹。
runner 启动 child 前读取 checkpoint，自动冻结上一份累计摘要并传入页码；抖音额外传入 offset
和 opaque search ID。child 先做有限顶部刷新，再走深层前沿；顶部刷新不覆盖 checkpoint。
执行器完成摘要构造后，在同一事务提交下一恢复位置和本轮已处理候选 ID，runner 再把本次摘要
路径写回 checkpoint。这个提交顺序保证游标和候选记忆不会先于可累计产物前移。达到完整入库目标后
只清空累计摘要，不删除发现位置或候选记忆。通用控制面用
`crawl_discovery_seen_candidates` 保存视频、字段无效和有效候选，跨轮在详情、作者与媒体处理前跳过。

小红书独立 runner 不读写通用 checkpoint 表，而是在 `xhs_discovery_checkpoints` 中按目标、账号和
查询指纹保存 `page + search_id`，在 `xhs_discovery_seen_candidates` 保存已完成处理的候选 ID。
它在冻结时同时纳入累计摘要及其 JSONL，child 摘要形成后才由 `xhs_runner.py` 在同一事务提交
安全前沿、候选 ID 和活动摘要。顶部刷新使用新 search ID 且不覆盖深层位置；深层续跑复用保存
的 search ID，详情请求前跳过数据库、已处理候选、累计摘要和本轮已见 ID。换号产生独立活动，
不共享尚未入库的摘要或候选集合。

## 结构化平台

| 平台 | 入口 | 平台文档 |
|---|---|---|
| B站 article | `mediacrawler_crawl.py --platforms bilibili` | [B站](platforms/bilibili.md) |
| 微博 | MediaCrawler 搜索 | [微博](platforms/weibo.md) |
| 小红书 | `xhs_runner.py` 人工选择隔离账号 + MediaCrawler 搜索/作者主页 | [小红书](platforms/xhs.md) |
| 抖音 | MediaCrawler 搜索 + 图文作者主页 | [抖音](platforms/douyin.md) |
| 知乎 | MediaCrawler 搜索 | [知乎](platforms/zhihu.md) |

结构化执行器先生成 JSONL，再按正式 profile 过滤视频、去重、校验图片/时间/作者/粉丝
和互动字段。只有有效唯一集合达到目标才进入入库；导入报告区分处理、新增和更新。

## 页面证据平台

固定 URL 页面证据由 `ctf_resource_crawl.py` 生成并写入证据层；内容就绪的页面可由
`import_ctf_captures.py` 归一化到 `web_posts`。页面错误、搜索页、中间页和验证码页只保留证据，
单页成功不代表平台批量目标完成。当前 `config/crawl_targets.json` 没有
`ctf_resource_crawl` 正式任务，因此直接运行页面执行器只属于开发或诊断验证；以后若新增固定
URL 正式任务，必须在该配置中声明并从 `crawl_runner.py` 进入。详细限制见
[页面证据平台](platforms/page-evidence.md)。

## 辅助入口

- `login_warmup.py`：验证并按需刷新 B站、微博、抖音和知乎登录态；不包含小红书，也不覆盖
  页面证据执行器的独立 profile。
- `xhs_accounts.py`、`xhs_login.py`：小红书账号登记、人工状态管理、隔离登录和持久状态复验。
- `mediacrawler_login_warmup.py`：统一入口调用的底层平台实现。
- `info_collection_benchmark.py`：通用平台性能和容量评估。

辅助入口不创建完整正式阶段，不能替代对应平台的正式 runner。
