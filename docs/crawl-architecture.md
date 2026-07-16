# 抓取架构

## 组件关系

```text
config/crawl_targets.json
  -> scripts/crawl_runner.py
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
      -> scripts/douban_group_crawl.py
          -> 搜索页话题发现 / 候选累计 / 页面证据
          -> scripts/ctf_resource_crawl.py
          -> scripts/import_ctf_captures.py
          -> web_posts / web_post_images

config/xhs_pool.json + config/xhs_targets.json
  -> scripts/xhs_runner.py
      -> explicit --account-id
      -> SQLite xhs_accounts / xhs_account_leases / xhs_account_events / xhs_runs
      -> data/runtime/xhs/execution_states/<run_id>/<target>.json
      -> decrypt account storage state into an ephemeral runtime file
      -> scripts/mediacrawler_crawl.py --platforms xhs --behavior-profile xhs_guarded
          -> tools/MediaCrawler search/detail pagination
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
本批新增、连续停滞次数、平台页码、游标/search ID、原始返回条数和 `has_more`。循环按
实际候选累计，不按名义页大小预先换算最大页数。空页或 `has_more=false` 才能生成
`source_exhausted`；请求异常生成 `runtime_failed`；缺少停止事件时执行器不得猜测数据源
已经耗尽。连续停滞按本批没有新增有效且数据库中不存在的记录计算；新的无效候选和数据库
已有记录不会重置停滞计数。状态事件是过程证据，最终成功仍以正式校验和数据库验证为准。

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

豆瓣小组正式任务由 `douban_group_crawl.py` 从配置的搜索页发现唯一话题 URL，按
`candidate_hard_limit`、`target_new_posts` 和 `max_stagnant_batches` 累计候选；单页证据仍由
`ctf_resource_crawl.py` 生成。搜索页和作者页只进入 `ctf_captures`，符合正式字段条件的话题页
归一化到 `web_posts`。只有有效新增和实际插入数同时达到目标才完成。详细限制见
[页面证据平台](platforms/page-evidence.md)。

豆瓣小组话题页在同一正式轮次执行条件补全：本轮话题 `rendered.html` 中存在可见作者
`/people/{id}/` 链接且尚无粉丝证据时，执行器使用同一持久 profile 和页面证据链抓取该 people
页。people capture 独立保存 HTML、可见文本、截图和 `capture_meta.json`；话题 capture 的
`conditional_enrichment` / `navigation.enrichment` 引用该同轮产物，并保存
`followers_count`、`followers_observed`、`followers_source` 和提取证据。导入器只接受同一批次、
父 capture 匹配且成功的 people capture，不从历史 capture 补签。只有当前 people 页可见明确
隐私限制文案时才记录 `privacy_restricted`；其他缺失保持 NULL，不默认填 0。

## 辅助入口

- `login_warmup.py`：验证并按需刷新通用平台登录态，不包含小红书。
- `xhs_accounts.py`、`xhs_login.py`：小红书账号登记、人工状态管理、隔离登录和持久状态复验。
- `mediacrawler_login_warmup.py`、`ctf_login_warmup.py`：统一入口调用的底层平台实现。
- `info_collection_benchmark.py`：通用平台性能和容量评估。

辅助入口不创建完整正式阶段，不能替代对应平台的正式 runner。
