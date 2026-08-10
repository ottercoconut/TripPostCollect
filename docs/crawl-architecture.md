# 抓取架构

> **受限冻结：** 本文件属于治理基线；普通抓取、排障或顺手同步不得修改，只有用户明确授权治理变更，并同步核验对应代码、测试与关联文档时才允许更新。

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
              -> platform session image download
              -> <artifact>/<platform>/image_manifest.jsonl + staging files
          -> formal_validation
          -> explicit content-image projection + manifest/byte verification
          -> data/media/<platform>/<post>/<index>-<asset_hash>.<ext>
          -> web_posts / web_post_images (same SQLite transaction)
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
          -> signed-in body-image download + staging manifest
          -> formal_validation
          -> root verification + data/media promotion
          -> web_posts / web_post_images (same SQLite transaction)
      -> encrypt refreshed storage state and remove runtime plaintext
```

`crawl_runner.py` 是通用平台正式入口；`xhs_runner.py` 是小红书唯一正式入口。两者负责
选择任务、冻结计划、执行逐步门禁和生成报告，执行器不能绕过状态文件宣布完成。

青岛主题范围由操作人或 Agent 在计划冻结时核对，不在通用配置同步、小红书 target 读取、恢复
参数、结构化诊断或页面证据导入中设置关键词硬门禁。数据库不增加城市列，正文内容也不参与
城市启发式判定。

结构化平台进入搜索前统一经过两层强制门禁：父执行器用 `crawl_policy.py` 维护平台会话
间隔、随机抖动、预算和冷却；MediaCrawler 使用已经完成登录确认的当前浏览器页调用
`mediacrawler_behavior.py`，执行随机停留、鼠标和滚动并写出证据。B站 article 是项目的
自定义 API 搜索，因此先在同一持久 profile 完成行为会话并导出该会话 cookie，再执行原
article API；微博、抖音和知乎在各自 MediaCrawler 浏览器内执行 `social_high_risk`；
小红书在独立账号 profile 内执行更慢的 `xhs_guarded`，并在交互前后检查可见挑战。

行为阶段只包围现有抓取逻辑，不修改分页、候选累计、粉丝补全、视频过滤或入库映射。
行为和策略证据任何一项缺失时，JSONL 可以保留用于诊断，但不得进入 SQLite。

## 正文图片本地化数据流

五个平台共用一条由平台会话到根项目的单向链路：

```text
平台详情/正文结构
  -> 平台显式 ImageCandidate（只允许 role=content）
  -> 使用当前登录/签名会话下载到本轮 staging
  -> 原子写 image_manifest.jsonl（URL + 稳定资产键 + SHA/MIME/尺寸）
  -> 根项目重建同一候选集合并逐项核对 manifest 身份
  -> 根项目重新读取并解码文件，验证路径边界、SHA、MIME、后缀、尺寸
  -> 正式模式以内容寻址文件名原子晋升 data/media；已存在同 SHA 文件幂等复用
  -> MaterializedImage 注入统一入库映射
  -> web_posts 与 web_post_images 在同一 SQLite savepoint 中提交
```

显式投影边界分别是 B站详情 `image_urls`、微博 `image_list`、XHS `image_list`、抖音
`note_download_url` 和知乎 `image_list`。头像、作者主页、搜索预览、封面、视频、音乐和知乎公式
图片没有从平台对象进入正文候选的边；这项过滤发生在下载前，不依赖下载后文件名或尺寸猜测。
作者头像可以在统一入库层保留为 `author_avatar` URL 参考，但它没有通向下载、manifest 或
`data/media` 的边，也不参与正文图计数。
各平台 store 只负责当前会话下载、staging 和 manifest，不拥有长期路径或 SQLite schema；根项目
统一拥有安全复验、晋升与事务持久化，因此没有五套互不一致的本地路径实现。

正式 runner 固定向 child 传 `--download-images --media-root <data/media>`，冻结计划写
`local_image_storage_required=true`。诊断执行器可把媒体根限制到项目 `temp/`，但
`--no-import` 不执行晋升。`--get-media` 被拒绝，MediaCrawler 的视频 store、音乐和视频下载路径
不会因正文图片功能变得可达。

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
新增目标后立即停止；提高目标只改变运行预算，不创建新查询指纹，也不重置已有前沿。这是正常
默认的 `target-new-posts` 模式。用户针对单轮显式指定 `source-exhausted` 时，执行器临时忽略新增
目标、候选硬上限和停滞停止条件，直到可验证来源耗尽或运行阻断；该模式不写回配置。

通用结构化任务的发现位置保存在 `crawl_discovery_checkpoints`，唯一键是任务 ID 与查询指纹。
runner 启动 child 前读取 checkpoint，自动冻结上一份累计摘要并传入页码；抖音额外传入 offset
和 opaque search ID。child 先做有限顶部刷新，再走深层前沿；顶部刷新不覆盖 checkpoint。
执行器完成摘要构造后，在同一事务提交下一恢复位置和本轮已处理候选 ID，runner 再把本次摘要
路径写回 checkpoint。这个提交顺序保证游标和候选记忆不会先于可累计产物前移。默认模式达到完整
入库目标，或显式来源耗尽模式取得完整耗尽证据并入库后，只清空累计摘要，不删除发现位置或候选记忆。通用控制面用
`crawl_discovery_seen_candidates` 保存视频、已有决定性权威证据的字段无效项和有效候选，跨轮在
详情、作者与媒体处理前跳过。详情请求、空响应或解析等可恢复失败必须记录
`runtime_failed`，保留原页/游标，且失败 ID 不进入 seen 集合。

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

结构化执行器先生成 JSONL 与图片 staging/manifest，再按正式 profile 过滤视频、去重、校验
权威正文、`content_detail_status/content_detail_source`、正文图/时间/作者/粉丝和互动字段。标题或搜索摘要
不能替代正文。根项目对有效集合逐帖核对 manifest 和文件；只有本轮完成模式、
行为/策略、字段和本地图片门禁同时成立才晋升并入库。导入报告区分处理、新增和更新；更新已有帖
时会优先匹配并保留仍有效的既有本地图片证据，新的整帖图片集合仍在同一事务重建。

`artifacts_verified` 对 `image_materialization` 的 manifest 哈希和计数等式负责；
`persistence_verified` 对 SQLite 行、`data/media` 文件、SHA/MIME/尺寸、连续图片序号、数据库完整性
负责。任一步失败，后续阶段保持 `frozen`，checkpoint 只停在最后安全前沿，不能用 URL-only 记录
推进。

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
