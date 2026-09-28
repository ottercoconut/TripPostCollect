# 平台适配附录 C8：全量符号处置账 v0.7

> 本附录是[详细迁移规格](platform-adapter-specification.md) C 节的补充，逐个列出迁移闭包内每个
> 顶层函数、类及类方法的目标位置和处置。目标路径均未实现；当前运行仍遵循[正式契约](formal-crawl-contract.md)。

## 基线与生成方式

| 项 | 值 |
|---|---|
| 根源码 | `fe3e28ac7cc9575968e3279dd0e1c60ad0b7b1c1` |
| fork | `tools/MediaCrawler` @ `2bcde689cc9a1b50cbcc7255597e9da1ff1a1b30` |
| 上游对照 | `NanmiCoder/MediaCrawler` @ `380b426000aac3d612837ed72c99808347dc94c9`（2026-09-19）；与 fork 分叉点 `d6f7c5bb906b6dac40ddf343ef9e26438a3de092` |
| 生成方式 | 对下列文件做 AST 枚举（`def`/`async def`/`class` 及类内方法），逐项匹配处置规则；任何未匹配项使生成失败 |
| 规模 | 92 个文件、1234 个定义：迁 752、拆 93、并 7、薄 3、退 379 |

范围：执行器 C、私有桥 E、行为/人类流程、两个 warmup、四个被执行器导入的共享脚本（失败分类、节流策略、
浏览器运行时、执行状态）、页面证据就绪模块，以及 fork 中微博/抖音/知乎/小红书四站 `media_platform`、`store`、
`tools`、`model`、`cache`、`proxy`、`database`、`base`、`cmd_arg`、`main.py`、`recv_sms.py`。
不含：`config/*`、`constant/*`、`var.py`（无函数定义，按 D1 的逐键规则处置）；B站/快手/贴吧的上游平台目录（整体退出，
见 C7）；fork 的 `api/`、`webui/`、`docs/`、`tests/`（测试按 F 节迁移）；根项目 `src/trippostcollect` 现有模块（原位保留，见 C0）。

## 处置含义

| 处置 | 含义 | 删除旧定义的前提 |
|---|---|---|
| 迁 | 原样移动到目标模块；调用顺序、次数、异常、wire 不变，只改导入路径 | 所有调用方切到新位置，旧位置无静态/动态引用 |
| 拆 | 同一定义里的纯逻辑、IO、全局 config/env 读取或 monkeypatch 分别归位 | 新显式出口的负例测试先通过，再删旧 hook |
| 并 | 与另一处同名同义定义合并为唯一实现 | 逐项比对两份实现一致；不一致时保留差异为显式参数，不擅自取其一 |
| 薄 | 保留外部命令名，函数体只转发到包内入口 | 外部 CLI 与冻结命令兼容 |
| 退 | 不进入目标包 | 下文“退出切片的调用点切断”中列出的所有活跃调用点先改为显式不可达或删除；T12 静态/动态引用检查为零，T14 才删除文件 |

目标路径均相对 `src/trippostcollect/`，`scripts/...` 除外。“卡”指详细规格 G 的任务卡，“测”指 F 节的测试责任。

## 本附录对详细规格的新增约束

以下各项由全量枚举发现，已同步写入详细规格 B/C/D；此处集中列出，便于核对。

| 编号 | 发现 | 约束 |
|---|---|---|
| X1 | `row_for_record`、`inject_materialized_images` 需要 `artifacts.image_materialization.MaterializedImage` 类型 | `records/formal.py` 只在 `TYPE_CHECKING` 下导入该类型，不形成运行期 records→artifacts 依赖；依赖矩阵登记为唯一例外 |
| X2 | 四站 store 投影直接调用 `image_manifest` 的 `*_source_asset_key`、`normalize_image_url` 与 `upsert_manifest_rows_atomic` | 稳定键函数归各站 parser，`normalize_image_url` 归 runtime/helpers；staging 写出经 `contracts.ImageStager` 端口，平台不直接 import artifacts |
| X3 | 四站 `*_store_media.py` 是同构的整帖 staging 实现 | T04 先做四份逐行差分，一致部分合并为 `artifacts/image_staging.py` 单实现，差异项（source_key、asset key 函数）作为参数 |
| X4 | `should_reseed_douyin_frontier` 由抖音 core 调用，决定是否开启新的游标纪元 | 归 `application/candidates.py`，经 `CandidateDecisions` 端口新增只读方法供抖音调用；判定条件逐字不变 |
| X5 | `AdaptiveAccumulator.from_environment` 对微博使用 `stagnation_basis="candidate_identity"` | 构造时显式传入；停滞计数仍不作停止条件，但事件字段 `stagnation_basis` 值不变 |
| X6 | `env_int`、各站 `_env_float` 在操作起点读 env | 统一由 `application/worker_inputs.py` 解析为零参 reader，读取时刻与解析失败回默认值的行为不变 |
| X7 | C 与 W 各有 `profile_dir_for`/`cookie_snapshot_path`/`required_cookie_names`，平台代号字典键名不同（`mediacrawler` vs `code`） | 合并前核对五站代号一致；合并后 C/W 共用 `core.paths` 与 `runtime/cookies.py` |
| X8 | `repair_bilibili_articles.py` 从 C 导入 6 个符号；`mediacrawler_login_warmup.py` 导入 `discover_cdp_browser_path` | T08/T10 删除 C 旧函数前，两脚本改为从包内新位置导入 |
| X9 | `XhsRuntimeSupervisionError` 被执行器事务代码按类型捕获并回滚 | 类型定义进 `application/contracts.py`，`db/content.py` 与 `xhs/supervision.py` 都从 contracts 导入，避免 db→xhs 依赖 |
| X10 | C 的 `is_retryable_image_error`/`is_runtime_blocking_image_error` 与 fork `image_download_retry` 同名同常量 | 合并为 `runtime/image_retry.py` 单一实现；合并前比对两组错误码集合 |
| X11 | `repair_runtime_stop_reason` 无生产调用，仅 2 个测试引用 | T00 决定保留或退出；退出前先迁移测试中的保护语义 |
| X12 | 知乎指定详情遇 zvideo 时调用 `get_video_info`/`extract_zvideo_content_from_html` | 照迁以保持现行行为；视频仍由根 `is_video_record` 后置过滤，不在迁移中改为提前跳过 |
| X13 | 上游 2026-09-18/19 修复了抖音 detail 接口的 `uifid/verifyFp/fp` 参数与 `x-tt-argus` 请求头；fork 在公共参数中已有前者，没有后者 | 首期机械迁移不引入；若正式运行出现 “Blocked by ArgusSecurityPlugin”，按独立行为变更处理，见 H/R06 |

## 退出切片的调用点切断

被标为“退”的定义仍有来自保留代码的静态引用，全部位于正式运行关闭的分支（评论开关、代理开关、creator 模式、
上游基类/工厂、词云/Excel）。迁移时必须在保留代码中删除这些分支或显式替换，**不能只删文件**。下表为 AST 检出的完整列表：

| 退出符号 | 保留代码中的引用位置 | 切断方式 |
|---|---|---|
| `AbstractApiClient` | media_platform/douyin/client.py:DouYinClient, media_platform/xhs/client.py:XiaoHongShuClient, media_platform/zhihu/client.py:ZhiHuClient | 解除继承，改为本站类型或 D4 端口 |
| `AbstractCrawler` | media_platform/douyin/core.py:DouYinCrawler, media_platform/weibo/core.py:WeiboCrawler, media_platform/xhs/core.py:XiaoHongShuCrawler, media_platform/zhihu/core.py:ZhihuCrawler | 解除继承，改为本站类型或 D4 端口 |
| `AbstractLogin` | media_platform/douyin/login.py:DouYinLogin, media_platform/weibo/login.py:WeiboLogin, media_platform/xhs/login.py:XiaoHongShuLogin, media_platform/zhihu/login.py:ZhiHuLogin | 解除继承，改为本站类型或 D4 端口 |
| `AbstractStore` | store/douyin/_store_impl.py:DouyinJsonlStoreImplement, store/weibo/_store_impl.py:WeiboJsonlStoreImplement, store/xhs/_store_impl.py:XhsJsonlStoreImplement, store/zhihu/_store_impl.py:ZhihuJsonlStoreImplement | 解除继承，改为本站类型或 D4 端口 |
| `AbstractStoreImage` | store/douyin/douyin_store_media.py:DouYinImage, store/weibo/weibo_store_media.py:WeiboStoreImage, store/xhs/xhs_store_media.py:XiaoHongShuImage, store/zhihu/zhihu_store_media.py:ZhihuStoreImage | 解除继承，改为本站类型或 D4 端口 |
| `AsyncWordCloudGenerator` | tools/async_file_writer.py:AsyncFileWriter, tools/async_file_writer.py:AsyncFileWriter.__init__ | 删除 `ENABLE_GET_WORDCLOUD`/Excel 分支 |
| `CacheFactory` | media_platform/douyin/login.py:DouYinLogin, media_platform/douyin/login.py:DouYinLogin.login_by_mobile | 改为直接调用本站 JSONL 出口/MEMORY 缓存 |
| `CrawlerFactory` | main.py:main | 改为直接调用本站 JSONL 出口/MEMORY 缓存 |
| `CreatorUrlInfo` | media_platform/xhs/core.py:XiaoHongShuCrawler | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `DouyinStoreFactory` | store/douyin/__init__.py:update_douyin_aweme | 改为直接调用本站 JSONL 出口/MEMORY 缓存 |
| `IpInfoModel` | media_platform/douyin/core.py:DouYinCrawler, media_platform/douyin/core.py:DouYinCrawler.start, media_platform/weibo/core.py:WeiboCrawler, media_platform/weibo/core.py:WeiboCrawler.start, media_platform/xhs/core.py:XiaoHongShuCrawler | 删除 `ENABLE_IP_PROXY` 分支；client 去掉 Mixin，保持 `proxy=None` 无操作 |
| `ProxyRefreshMixin` | media_platform/douyin/client.py:DouYinClient, media_platform/weibo/client.py:WeiboClient, media_platform/xhs/client.py:XiaoHongShuClient, media_platform/zhihu/client.py:ZhiHuClient | 删除 `ENABLE_IP_PROXY` 分支；client 去掉 Mixin，保持 `proxy=None` 无操作 |
| `WeibostoreFactory` | store/weibo/__init__.py:update_weibo_note | 改为直接调用本站 JSONL 出口/MEMORY 缓存 |
| `XhsStoreFactory` | store/xhs/__init__.py:update_xhs_note | 改为直接调用本站 JSONL 出口/MEMORY 缓存 |
| `ZhihuComment` | media_platform/zhihu/client.py:ZhiHuClient, media_platform/zhihu/help.py:ZhihuExtractor | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `ZhihuStoreFactory` | store/zhihu/__init__.py:update_zhihu_content | 改为直接调用本站 JSONL 出口/MEMORY 缓存 |
| `_extract_comment` | media_platform/zhihu/help.py:ZhihuExtractor | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `_flush_excel_if_needed` | main.py:main | 删除 `ENABLE_GET_WORDCLOUD`/Excel 分支 |
| `_generate_wordcloud_if_needed` | main.py:main | 删除 `ENABLE_GET_WORDCLOUD`/Excel 分支 |
| `_inject_init_db_default` | cmd_arg/arg.py:parse_cmd | 删除 init_db/贴吧参数归一 |
| `_normalize_tieba_creator_url` | cmd_arg/arg.py:parse_cmd | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `_normalize_tieba_note_id` | cmd_arg/arg.py:parse_cmd | 删除 init_db/贴吧参数归一 |
| `_refresh_proxy_if_expired` | media_platform/douyin/client.py:DouYinClient, media_platform/douyin/client.py:DouYinClient.request, media_platform/weibo/client.py:WeiboClient, media_platform/weibo/client.py:WeiboClient.request, media_platform/xhs/client.py:XiaoHongShuClient | 删除 `ENABLE_IP_PROXY` 分支；client 去掉 Mixin，保持 `proxy=None` 无操作 |
| `batch_get_content_comments` | media_platform/zhihu/core.py:ZhihuCrawler, media_platform/zhihu/core.py:ZhihuCrawler.get_specified_notes, media_platform/zhihu/core.py:ZhihuCrawler.search | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `batch_get_note_comments` | media_platform/douyin/core.py:DouYinCrawler, media_platform/douyin/core.py:DouYinCrawler.get_specified_awemes, media_platform/douyin/core.py:DouYinCrawler.search, media_platform/xhs/core.py:XiaoHongShuCrawler, media_platform/xhs/core.py:XiaoHongShuCrawler.get_specified_notes | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `batch_get_notes_comments` | media_platform/weibo/core.py:WeiboCrawler, media_platform/weibo/core.py:WeiboCrawler.get_specified_notes, media_platform/weibo/core.py:WeiboCrawler.search | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `batch_get_notes_full_text` | media_platform/weibo/core.py:WeiboCrawler | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `batch_update_dy_aweme_comments` | media_platform/douyin/core.py:DouYinCrawler | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `batch_update_weibo_note_comments` | media_platform/weibo/core.py:WeiboCrawler | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `batch_update_weibo_notes` | media_platform/weibo/core.py:WeiboCrawler | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `batch_update_xhs_note_comments` | media_platform/xhs/core.py:XiaoHongShuCrawler | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `batch_update_zhihu_contents` | media_platform/zhihu/core.py:ZhihuCrawler | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `batch_update_zhihu_note_comments` | media_platform/zhihu/core.py:ZhihuCrawler | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `create_cache` | media_platform/douyin/login.py:DouYinLogin, media_platform/douyin/login.py:DouYinLogin.login_by_mobile | 改为直接调用本站 JSONL 出口/MEMORY 缓存 |
| `create_crawler` | main.py:main | 改为直接调用本站 JSONL 出口/MEMORY 缓存 |
| `create_ip_pool` | media_platform/douyin/core.py:DouYinCrawler, media_platform/douyin/core.py:DouYinCrawler.start, media_platform/weibo/core.py:WeiboCrawler, media_platform/weibo/core.py:WeiboCrawler.start, media_platform/xhs/core.py:XiaoHongShuCrawler | 删除 `ENABLE_IP_PROXY` 分支；client 去掉 Mixin，保持 `proxy=None` 无操作 |
| `create_store` | store/douyin/__init__.py:update_douyin_aweme, store/weibo/__init__.py:update_weibo_note, store/xhs/__init__.py:update_xhs_note, store/zhihu/__init__.py:update_zhihu_content | 改为直接调用本站 JSONL 出口/MEMORY 缓存 |
| `extract_comments` | media_platform/zhihu/client.py:ZhiHuClient | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `extract_content_list_from_creator` | media_platform/zhihu/client.py:ZhiHuClient | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `extract_creator` | media_platform/zhihu/client.py:ZhiHuClient | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `extract_offset` | media_platform/zhihu/client.py:ZhiHuClient | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `fetch_creator_notes_detail` | media_platform/xhs/core.py:XiaoHongShuCrawler | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `fetch_creator_video_detail` | media_platform/douyin/core.py:DouYinCrawler | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `format_proxy_info` | media_platform/douyin/core.py:DouYinCrawler, media_platform/douyin/core.py:DouYinCrawler.start, media_platform/weibo/core.py:WeiboCrawler, media_platform/weibo/core.py:WeiboCrawler.start, media_platform/xhs/core.py:XiaoHongShuCrawler | 删除 `ENABLE_IP_PROXY` 分支；client 去掉 Mixin，保持 `proxy=None` 无操作 |
| `generate_word_frequency_and_cloud` | tools/async_file_writer.py:AsyncFileWriter | 删除 `ENABLE_GET_WORDCLOUD`/Excel 分支 |
| `get_all_anwser_by_creator` | media_platform/zhihu/core.py:ZhihuCrawler | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `get_all_notes_by_creator` | media_platform/xhs/core.py:XiaoHongShuCrawler | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `get_all_notes_by_creator_id` | media_platform/weibo/core.py:WeiboCrawler | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `get_all_user_aweme_posts` | media_platform/douyin/core.py:DouYinCrawler | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `get_aweme_all_comments` | media_platform/douyin/core.py:DouYinCrawler | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `get_aweme_comments` | media_platform/douyin/client.py:DouYinClient | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `get_child_comments` | media_platform/zhihu/client.py:ZhiHuClient | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `get_comments` | media_platform/douyin/core.py:DouYinCrawler, media_platform/xhs/core.py:XiaoHongShuCrawler, media_platform/zhihu/core.py:ZhihuCrawler | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `get_comments_all_sub_comments` | media_platform/weibo/client.py:WeiboClient, media_platform/xhs/client.py:XiaoHongShuClient, media_platform/zhihu/client.py:ZhiHuClient | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `get_creator_answers` | media_platform/zhihu/client.py:ZhiHuClient | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `get_creator_articles` | media_platform/zhihu/client.py:ZhiHuClient | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `get_creator_info_by_id` | media_platform/weibo/core.py:WeiboCrawler | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `get_creator_videos` | media_platform/zhihu/client.py:ZhiHuClient | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `get_creators_and_notes` | media_platform/weibo/core.py:WeiboCrawler, media_platform/weibo/core.py:WeiboCrawler.start, media_platform/xhs/core.py:XiaoHongShuCrawler, media_platform/xhs/core.py:XiaoHongShuCrawler._run_browser_session, media_platform/zhihu/core.py:ZhihuCrawler | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `get_creators_and_videos` | media_platform/douyin/core.py:DouYinCrawler, media_platform/douyin/core.py:DouYinCrawler.start | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `get_note_all_comments` | media_platform/weibo/core.py:WeiboCrawler, media_platform/xhs/core.py:XiaoHongShuCrawler, media_platform/zhihu/core.py:ZhihuCrawler | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `get_note_comments` | media_platform/weibo/client.py:WeiboClient, media_platform/weibo/core.py:WeiboCrawler, media_platform/xhs/client.py:XiaoHongShuClient | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `get_note_sub_comments` | media_platform/xhs/client.py:XiaoHongShuClient | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `get_notes_by_creator` | media_platform/weibo/client.py:WeiboClient, media_platform/xhs/client.py:XiaoHongShuClient | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `get_proxy` | media_platform/douyin/core.py:DouYinCrawler, media_platform/douyin/core.py:DouYinCrawler.start, media_platform/weibo/core.py:WeiboCrawler, media_platform/weibo/core.py:WeiboCrawler.start, media_platform/xhs/core.py:XiaoHongShuCrawler | 删除 `ENABLE_IP_PROXY` 分支；client 去掉 Mixin，保持 `proxy=None` 无操作 |
| `get_root_comments` | media_platform/zhihu/client.py:ZhiHuClient | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `get_sub_comments` | media_platform/douyin/client.py:DouYinClient | 删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false） |
| `get_user_aweme_posts` | media_platform/douyin/client.py:DouYinClient | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `init_db` | cmd_arg/arg.py:parse_cmd, main.py:main | 删除 init_db/贴吧参数归一 |
| `init_proxy_pool` | media_platform/douyin/client.py:DouYinClient, media_platform/douyin/client.py:DouYinClient.__init__, media_platform/weibo/client.py:WeiboClient, media_platform/weibo/client.py:WeiboClient.__init__, media_platform/xhs/client.py:XiaoHongShuClient | 删除 `ENABLE_IP_PROXY` 分支；client 去掉 Mixin，保持 `proxy=None` 无操作 |
| `parse_creator_info_from_url` | media_platform/douyin/core.py:DouYinCrawler, media_platform/xhs/core.py:XiaoHongShuCrawler | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `save_creator` | media_platform/douyin/core.py:DouYinCrawler, media_platform/weibo/core.py:WeiboCrawler, media_platform/xhs/core.py:XiaoHongShuCrawler | 删除 `CRAWLER_TYPE == "creator"` 分支 |
| `update_dy_aweme_video` | media_platform/douyin/core.py:DouYinCrawler | 删除视频分支（正式禁用视频） |

`batch_get_notes_full_text` 与 `batch_update_weibo_notes` 等只在 creator 链调用，按 creator 切断；`get_comments`、
`get_note_all_comments` 等同名方法在多站出现时按各站分别切断。

## 全量账

每行：`行号 定义 → 目标 ｜处置｜卡｜测｜备注`。同一文件内按源码顺序排列。

### `scripts/mediacrawler_crawl.py`（147；迁138、并5、拆3、薄1）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 224 | `XhsParentNetworkPauseClock` | `runtime/process.py` | 迁 | T02 | F06 | XHS父侧网络暂停计时，随run_command |
| 227 | `XhsParentNetworkPauseClock.__init__` | `runtime/process.py` | 迁 | T02 | F06 | XHS父侧网络暂停计时，随run_command |
| 237 | `XhsParentNetworkPauseClock.observe` | `runtime/process.py` | 迁 | T02 | F06 | XHS父侧网络暂停计时，随run_command |
| 269 | `BilibiliArticleDetailError` | `platforms/bilibili/models.py` | 迁 | T08 | F14 | retryable/code/attempts/waits元数据不变；B修复脚本反向导入DetailError |
| 271 | `BilibiliArticleDetailError.__init__` | `platforms/bilibili/models.py` | 迁 | T08 | F14 | retryable/code/attempts/waits元数据不变；B修复脚本反向导入DetailError |
| 289 | `BilibiliFollowerFetchError` | `platforms/bilibili/models.py` | 迁 | T08 | F14 | retryable/code/attempts/waits元数据不变；B修复脚本反向导入DetailError |
| 291 | `BilibiliFollowerFetchError.__init__` | `platforms/bilibili/models.py` | 迁 | T08 | F14 | retryable/code/attempts/waits元数据不变；B修复脚本反向导入DetailError |
| 305 | `BilibiliRuntimeBlocked` | `platforms/bilibili/models.py` | 迁 | T08 | F14 | retryable/code/attempts/waits元数据不变；B修复脚本反向导入DetailError |
| 307 | `BilibiliRuntimeBlocked.__init__` | `platforms/bilibili/models.py` | 迁 | T08 | F14 | retryable/code/attempts/waits元数据不变；B修复脚本反向导入DetailError |
| 312 | `is_retryable_image_error` | `runtime/image_retry.py` | 并 | T04 | F05 | 与M/tools/image_download_retry同名同常量；合并前逐项比对两组错误码集合 |
| 316 | `is_runtime_blocking_image_error` | `runtime/image_retry.py` | 并 | T04 | F05 | 与M/tools/image_download_retry同名同常量；合并前逐项比对两组错误码集合 |
| 425 | `parse_args` | `application/inputs.py` | 迁 | T02 | F10 | D2全部参数与默认值不变 |
| 519 | `utc_stamp` | `runtime/helpers.py` | 迁 | T02 | F06 |  |
| 523 | `selected_platforms` | `application/inputs.py` | 迁 | T02 | F10 | W:90为独立同名实现，不合并 |
| 530 | `load_zhihu_detail_urls` | `application/inputs.py` | 迁 | T10 | F08 | 文件输入解析；带token URL不写普通摘要 |
| 560 | `load_xhs_detail_urls` | `application/inputs.py` | 迁 | T10 | F08 | 文件输入解析；带token URL不写普通摘要 |
| 588 | `load_xhs_repair_target_ids` | `application/inputs.py` | 迁 | T10 | F08 | 文件输入解析；带token URL不写普通摘要 |
| 602 | `load_post_repair_fallbacks` | `application/repair.py` | 迁 | T10 | F08 |  |
| 688 | `load_post_repair_targets` | `application/repair.py` | 迁 | T10 | F08 |  |
| 777 | `ensure_prerequisites` | `core/resources.py` | 拆 | T01 | F10 | fork文件存在检查改为包资源检查；uv/运行环境检查留入口 |
| 782 | `discover_cdp_browser_path` | `runtime/browser_launcher.py` | 迁 | T01 | F07 | W反向导入，T10同批切换 |
| 816 | `profile_dir_for` | `core/paths.py` | 并 | T01 | F10 | 与W:103/118重复；PLATFORMS代号映射一致后唯一定义 |
| 821 | `cookie_snapshot_path` | `core/paths.py` | 并 | T01 | F10 | 与W:103/118重复；PLATFORMS代号映射一致后唯一定义 |
| 825 | `platform_cookie_url` | `runtime/cookies.py` | 迁 | T03 | F07 |  |
| 835 | `required_cookie_names` | `runtime/cookies.py` | 并 | T03 | F07 | 与W:112重复 |
| 841 | `cookie_names_from_header` | `runtime/cookies.py` | 迁 | T03 | F07 |  |
| 852 | `cookies_to_header` | `runtime/cookies.py` | 迁 | T03 | F07 |  |
| 869 | `load_cookie_snapshot` | `runtime/cookies.py` | 迁 | T03 | F07/F08 | B修复反向导入 |
| 895 | `public_cookie_export` | `runtime/cookies.py` | 迁 | T03 | F07 |  |
| 899 | `export_profile_cookies` | `runtime/cookies.py` | 迁 | T07 | F07 | 见C4；知乎d_c0/z_c0要求不变 |
| 975 | `load_behavior_evidence` | `runtime/behavior.py` | 迁 | T04 | F07 |  |
| 992 | `behavior_environment` | `application/collection.py` | 拆 | T02 | F07/F10 | 父侧行为env构造；PROJECT_SCRIPTS在T12移除 |
| 1002 | `run_bilibili_behavior_session` | `platforms/bilibili/login.py` | 拆 | T08 | F07/F14 | 见C5；Context关闭后才走urllib |
| 1065 | `decode_text` | `runtime/process.py` | 迁 | T02 | F06 | 进程组、watchdog、管道净化 |
| 1071 | `tail` | `runtime/process.py` | 迁 | T02 | F06 | 进程组、watchdog、管道净化 |
| 1075 | `json_dump` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 1079 | `parse_int` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 1102 | `first_value` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 1110 | `timestamp_to_iso` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 1124 | `parse_datetime_text` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 1154 | `datetime_value_to_iso` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 1158 | `iter_nested_values_for_keys` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 1173 | `published_at_for_record` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 1186 | `merge_repair_fallback_metadata` | `records/formal.py` | 迁 | T04 | F08 | 含0被旧值覆盖分支，见H/R04，不顺手修 |
| 1253 | `is_video_record` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 1343 | `_diagnostic_timestamp` | `runtime/process.py` | 迁 | T09 | F06 | XHS网络诊断文件读取与净化 |
| 1355 | `_fresh_diagnostic_timestamp` | `runtime/process.py` | 迁 | T09 | F06 | XHS网络诊断文件读取与净化 |
| 1369 | `_sanitized_transport_reason` | `runtime/process.py` | 迁 | T09 | F06 | XHS网络诊断文件读取与净化 |
| 1380 | `xhs_network_state_from_diagnostics` | `runtime/process.py` | 迁 | T09 | F06 | XHS网络诊断文件读取与净化 |
| 1449 | `XhsRuntimeSupervisionError` | `application/contracts.py` | 迁 | T02 | F05/F06 | 类型移入contracts，xhs/supervision重导出；db/content仍需按该类型回滚 |
| 1453 | `XhsSupervisorRuntimeReporter` | `xhs/supervision.py` | 迁 | T09 | F06 | 认证心跳，不承担发现提交 |
| 1458 | `XhsSupervisorRuntimeReporter.__init__` | `xhs/supervision.py` | 迁 | T09 | F06 | 认证心跳，不承担发现提交 |
| 1483 | `XhsSupervisorRuntimeReporter.phase` | `xhs/supervision.py` | 迁 | T09 | F06 | 认证心跳，不承担发现提交 |
| 1486 | `XhsSupervisorRuntimeReporter._writer_identity_is_current` | `xhs/supervision.py` | 迁 | T09 | F06 | 认证心跳，不承担发现提交 |
| 1489 | `XhsSupervisorRuntimeReporter.checkpoint` | `xhs/supervision.py` | 迁 | T09 | F06 | 认证心跳，不承担发现提交 |
| 1545 | `XhsSupervisorRuntimeReporter.enter_finalizing` | `xhs/supervision.py` | 迁 | T09 | F06 | 认证心跳，不承担发现提交 |
| 1548 | `XhsSupervisorRuntimeReporter.snapshot` | `xhs/supervision.py` | 迁 | T09 | F06 | 认证心跳，不承担发现提交 |
| 1560 | `xhs_supervisor_runtime_reporter_from_context` | `xhs/supervision.py` | 迁 | T09 | F06 | 认证心跳，不承担发现提交 |
| 1639 | `_runtime_progress` | `runtime/helpers.py` | 迁 | T02 | F06 |  |
| 1644 | `_runtime_progress_if_due` | `runtime/helpers.py` | 迁 | T02 | F06 |  |
| 1659 | `progress_path_signature` | `runtime/process.py` | 迁 | T02 | F06 | 进程组、watchdog、管道净化 |
| 1694 | `process_group_exists` | `runtime/process.py` | 迁 | T02 | F06 | 进程组、watchdog、管道净化 |
| 1704 | `terminate_managed_process` | `runtime/process.py` | 迁 | T02 | F06 | 进程组、watchdog、管道净化 |
| 1744 | `runtime_watchdog_stop_detail` | `runtime/process.py` | 迁 | T02 | F06 | 进程组、watchdog、管道净化 |
| 1756 | `append_runtime_watchdog_stop_event` | `runtime/process.py` | 迁 | T02 | F06 | 写严格FrozenExecutionState，不走legacy事件 |
| 1821 | `run_command` | `runtime/process.py` | 迁 | T02 | F06 | 进程组、watchdog、管道净化 |
| 2219 | `skipped_command` | `runtime/process.py` | 迁 | T02 | F06 | 进程组、watchdog、管道净化 |
| 2243 | `item_type_from_path` | `application/reporting.py` | 迁 | T11 | F04/F10 | 摘要与样本在首次写出前净化 |
| 2254 | `truncate` | `application/reporting.py` | 迁 | T11 | F04/F10 | 摘要与样本在首次写出前净化 |
| 2259 | `clean_html_text` | `platforms/bilibili/parser.py` | 迁 | T08 | F14 | 仅B站两处调用，不设跨站共享 |
| 2266 | `extract_sample` | `application/reporting.py` | 迁 | T11 | F04/F10 | 摘要与样本在首次写出前净化 |
| 2274 | `summarize_jsonl` | `application/reporting.py` | 迁 | T11 | F04/F10 | 摘要与样本在首次写出前净化 |
| 2336 | `summarize_output` | `application/reporting.py` | 迁 | T11 | F04/F10 | 摘要与样本在首次写出前净化 |
| 2414 | `summarize_output_with_progress` | `application/reporting.py` | 迁 | T11 | F04/F10 | 摘要与样本在首次写出前净化 |
| 2428 | `write_json_with_progress` | `application/reporting.py` | 迁 | T11 | F04/F10 | 摘要与样本在首次写出前净化 |
| 2459 | `terminal_summary_envelope` | `application/reporting.py` | 迁 | T11 | F04/F10 | 摘要与样本在首次写出前净化 |
| 2476 | `ensure_web_schema` | `db/content.py` | 迁 | T11 | F05 | 事务与提交确定性探测不变 |
| 2480 | `platform_from_path` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 2498 | `post_id_for_record` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 2503 | `canonical_url_for_record` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 2531 | `content_body_for_record` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 2536 | `content_text_for_record` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 2540 | `inject_materialized_images` | `records/formal.py` | 迁 | T04 | F03/F05 | MaterializedImage仅TYPE_CHECKING导入，见B例外X1 |
| 2660 | `row_for_record` | `records/formal.py` | 迁 | T04 | F03/F05 | MaterializedImage仅TYPE_CHECKING导入，见B例外X1 |
| 2754 | `formal_record_identity` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 2762 | `formal_database_identities` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 2773 | `validate_formal_record` | `records/formal.py` | 迁 | T04 | F03 | 纯字段/时间/身份判定 |
| 2925 | `stable_douyin_search_id` | `application/collection.py` | 迁 | T11 | F01 | 抖音search_id提取保持父侧现位置，父进程不导入抖音包 |
| 2940 | `effective_discovery_checkpoint_event` | `application/collection.py` | 迁 | T11 | F01 | 抖音search_id提取保持父侧现位置，父进程不导入抖音包 |
| 2951 | `persist_discovery_checkpoint` | `application/collection.py` | 迁 | T11 | F01 | 抖音search_id提取保持父侧现位置，父进程不导入抖音包 |
| 3054 | `load_existing_formal_identities` | `db/discovery_read.py` | 迁 | T08 | F01/F14 | Bili与收集阶段各读一次 |
| 3076 | `load_pagination_evidence` | `application/collection.py` | 迁 | T11 | F01/F03/F05 |  |
| 3171 | `load_xhs_repair_report` | `application/repair.py` | 迁 | T10 | F08 |  |
| 3182 | `xhs_repair_pagination_evidence` | `application/repair.py` | 迁 | T10 | F08 |  |
| 3222 | `post_repair_pagination_evidence` | `application/repair.py` | 迁 | T10 | F08 |  |
| 3298 | `attach_skipped_candidate_evidence` | `application/collection.py` | 迁 | T11 | F01/F03/F05 |  |
| 3333 | `collect_formal_records` | `application/collection.py` | 迁 | T11 | F01/F03/F05 |  |
| 3597 | `resolve_media_root` | `artifacts/paths.py` | 迁 | T11 | F05 |  |
| 3616 | `_project_relative_evidence_path` | `artifacts/formal_images.py` | 迁 | T11 | F05 | 正式图片复验、晋升、回滚、媒体锁 |
| 3626 | `_load_manifest_with_evidence` | `artifacts/formal_images.py` | 迁 | T11 | F05 | 正式图片复验、晋升、回滚、媒体锁 |
| 3646 | `_staging_root_for_manifest_entry` | `artifacts/formal_images.py` | 迁 | T11 | F05 | 正式图片复验、晋升、回滚、媒体锁 |
| 3656 | `rollback_newly_promoted_images` | `artifacts/formal_images.py` | 迁 | T11 | F05 | 正式图片复验、晋升、回滚、媒体锁 |
| 3722 | `formal_media_persistence_lock` | `artifacts/formal_images.py` | 迁 | T11 | F05 | 正式图片复验、晋升、回滚、媒体锁 |
| 3750 | `_validated_manifest_rows_for_post` | `artifacts/formal_images.py` | 迁 | T11 | F05 | 正式图片复验、晋升、回滚、媒体锁 |
| 3896 | `materialize_formal_record_images` | `artifacts/formal_images.py` | 迁 | T11 | F05 | 正式图片复验、晋升、回滚、媒体锁 |
| 4250 | `find_existing_post` | `db/content.py` | 迁 | T11 | F05 | 事务与提交确定性探测不变 |
| 4268 | `upsert_web_post` | `db/content.py` | 迁 | T11 | F05 | 事务与提交确定性探测不变 |
| 4324 | `FormalImportBeforeCommitError` | `db/content.py` | 迁 | T11 | F05 | 事务与提交确定性探测不变 |
| 4326 | `FormalImportBeforeCommitError.__init__` | `db/content.py` | 迁 | T11 | F05 | 事务与提交确定性探测不变 |
| 4331 | `commit_formal_import` | `db/content.py` | 迁 | T11 | F05 | 事务与提交确定性探测不变 |
| 4335 | `import_valid_records` | `db/content.py` | 迁 | T11 | F05 | 事务与提交确定性探测不变 |
| 4413 | `import_valid_records_with_media_rollback` | `application/collection.py` | 迁 | T11 | F01/F03/F05 |  |
| 4470 | `normalize_bilibili_article_record` | `platforms/bilibili/parser.py` | 迁 | T08 | F14 | hydrate不覆盖搜索发布时间 |
| 4512 | `clean_bilibili_article_body` | `platforms/bilibili/parser.py` | 迁 | T08 | F14 | hydrate不覆盖搜索发布时间 |
| 4524 | `normalize_bilibili_detail_image_url` | `platforms/bilibili/parser.py` | 迁 | T08 | F14 | hydrate不覆盖搜索发布时间 |
| 4531 | `extract_bilibili_detail_images` | `platforms/bilibili/parser.py` | 迁 | T08 | F14 | hydrate不覆盖搜索发布时间 |
| 4591 | `bilibili_detail_headers` | `platforms/bilibili/client.py` | 迁 | T08 | F09/F14 | urllib不换；B修复反向导入detail |
| 4604 | `bilibili_image_headers` | `platforms/bilibili/client.py` | 迁 | T08 | F09/F14 | urllib不换；B修复反向导入detail |
| 4616 | `fetch_bilibili_image_bytes` | `platforms/bilibili/client.py` | 迁 | T08 | F09/F14 | urllib不换；B修复反向导入detail |
| 4630 | `download_bilibili_record_images` | `platforms/bilibili/core.py` | 迁 | T08 | F01/F05/F14 | 执行器进程内同步调用 |
| 4780 | `fetch_bilibili_article_detail` | `platforms/bilibili/client.py` | 迁 | T08 | F09/F14 | urllib不换；B修复反向导入detail |
| 4833 | `fetch_bilibili_article_detail_with_retry` | `platforms/bilibili/client.py` | 迁 | T08 | F09/F14 | urllib不换；B修复反向导入detail |
| 4861 | `hydrate_bilibili_article_record` | `platforms/bilibili/parser.py` | 迁 | T08 | F14 | hydrate不覆盖搜索发布时间 |
| 4917 | `fetch_bilibili_wbi_keys` | `platforms/bilibili/client.py` | 迁 | T08 | F09/F14 | urllib不换；B修复反向导入detail |
| 4936 | `sign_bilibili_wbi_params` | `platforms/bilibili/signer.py` | 迁 | T08 | F09 |  |
| 4949 | `fetch_bilibili_article_page` | `platforms/bilibili/client.py` | 迁 | T08 | F09/F14 | urllib不换；B修复反向导入detail |
| 4979 | `fetch_bilibili_follower_count` | `platforms/bilibili/client.py` | 迁 | T08 | F09/F14 | urllib不换；B修复反向导入detail |
| 5020 | `run_bilibili_article_search` | `platforms/bilibili/core.py` | 迁 | T08 | F01/F05/F14 | 执行器进程内同步调用 |
| 5601 | `_run_platform_without_policy` | `application/collection.py` | 迁 | T11 | F01/F03/F05 |  |
| 5920 | `effective_attempt_exit_code` | `application/collection.py` | 迁 | T11 | F01/F03/F05 |  |
| 5928 | `run_platform` | `application/collection.py` | 迁 | T11 | F01/F03/F05 |  |
| 6036 | `collect_behavior_validation` | `application/reporting.py` | 迁 | T11 | F04/F10 | 摘要与样本在首次写出前净化 |
| 6145 | `latest_platform_result_counts` | `application/reporting.py` | 迁 | T11 | F04/F10 | 摘要与样本在首次写出前净化 |
| 6164 | `write_markdown` | `application/reporting.py` | 迁 | T11 | F04/F10 | 摘要与样本在首次写出前净化 |
| 6289 | `apply_formal_completion_gates` | `application/collection.py` | 迁 | T11 | F01/F03/F05 |  |
| 6333 | `formal_import_gate_met` | `application/collection.py` | 迁 | T11 | F01/F03/F05 |  |
| 6339 | `formal_image_promotion_allowed` | `application/collection.py` | 迁 | T11 | F01/F03/F05 |  |
| 6348 | `repair_partial_child_execution_allowed` | `application/repair.py` | 迁 | T10 | F08 |  |
| 6369 | `repair_candidate_execution_completed` | `application/repair.py` | 迁 | T10 | F08 |  |
| 6420 | `runtime_blocker_stop_reason` | `application/failures.py` | 迁 | T11 | F06/F08 | 运行级阻断裁决 |
| 6428 | `runtime_blocker_from_terminal_event` | `application/failures.py` | 迁 | T11 | F06/F08 | 运行级阻断裁决 |
| 6451 | `runtime_blocker_from_pagination_evidence` | `application/failures.py` | 迁 | T11 | F06/F08 | 运行级阻断裁决 |
| 6494 | `latest_runtime_blocker` | `application/failures.py` | 迁 | T11 | F06/F08 | 运行级阻断裁决 |
| 6547 | `repair_runtime_stop_reason` | `application/repair.py` | 迁 | T10 | F08 | 生产代码无调用，仅2个测试引用；T00判定保留或退出，删除须先迁保护语义 |
| 6556 | `apply_runtime_blocker` | `application/failures.py` | 迁 | T11 | F06/F08 | 运行级阻断裁决 |
| 6574 | `_run_main` | `application/collection.py` | 迁 | T11 | F01/F03/F05 |  |
| 7200 | `main` | `scripts/mediacrawler_crawl.py` | 薄 | T11 | F10 | 外部命令名保留，只转发application |

### `scripts/mediacrawler_export_entrypoint.py`（17；迁5、拆12）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 29 | `_find_nested_platform_record` | `runtime/helpers.py` | 迁 | T05 | F08 | 微博/抖音详情共用纯查找 |
| 74 | `_douyin_detail_urls` | `platforms/douyin/client.py` | 迁 | T06 | F08 |  |
| 81 | `_find_douyin_detail` | `platforms/douyin/parser.py` | 迁 | T06 | F08 |  |
| 90 | `_find_weibo_detail` | `platforms/weibo/parser.py` | 迁 | T05 | F08 |  |
| 99 | `_weibo_detail_api_url` | `platforms/weibo/client.py` | 拆 | T05 | F08 |  |
| 103 | `sanitize_export_item` | `records/sanitization.py` | 拆 | T04 | F04 | 清理算法复用现有；hook改显式RecordSink |
| 117 | `install_export_hook` | `records/sanitization.py` | 拆 | T04 | F04 | 清理算法复用现有；hook改显式RecordSink |
| 143 | `install_batch_checkpoint_hook` | `application/events.py` | 拆 | T04 | F02 | append→publish显式；错误前缀与时点不变 |
| 176 | `_repair_exception_is_blocking` | `platforms/xhs/repair.py` | 拆 | T09 | F08 |  |
| 210 | `_xhs_repair_failure` | `platforms/xhs/repair.py` | 拆 | T09 | F08 |  |
| 243 | `_write_xhs_repair_report` | `artifacts/evidence.py` | 迁 | T09 | F04/F08 | 报告仍先净化 |
| 257 | `_xhs_repair_failure_scope` | `platforms/xhs/repair.py` | 拆 | T09 | F08 |  |
| 266 | `_xhs_repair_blocker` | `platforms/xhs/repair.py` | 拆 | T09 | F08 |  |
| 298 | `install_xhs_repair_resilience` | `platforms/xhs/repair.py` | 拆 | T09 | F08 |  |
| 446 | `install_douyin_browser_detail_fallback` | `platforms/douyin/client.py` | 拆 | T06 | F08 | monkeypatch改为显式repair分支 |
| 576 | `install_weibo_browser_detail_fallback` | `platforms/weibo/client.py` | 拆 | T05 | F08 |  |
| 724 | `main` | `platforms/entry.py` | 拆 | T02 | F10 | runpy桥退出，T14删E |

### `scripts/mediacrawler_behavior.py`（24；迁24）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 72 | `utc_now` | `runtime/helpers.py` | 迁 | T04 | F07 |  |
| 76 | `write_evidence` | `artifacts/evidence.py` | 迁 | T04 | F04/F07 |  |
| 84 | `runtime_fingerprint` | `runtime/behavior.py` | 迁 | T04 | F07 | run_page_behavior的xhs_guarded分支改注入callback |
| 105 | `visible_page_state` | `runtime/behavior.py` | 迁 | T04 | F07 | run_page_behavior的xhs_guarded分支改注入callback |
| 176 | `visible_challenge` | `runtime/behavior.py` | 迁 | T04 | F07 | run_page_behavior的xhs_guarded分支改注入callback |
| 194 | `record_xhs_platform_security_limit` | `platforms/xhs/behavior.py` | 迁 | T09 | F07/F15 | XHS特有动作，经窄callback注入runtime |
| 255 | `runtime_fingerprint_valid` | `runtime/behavior.py` | 迁 | T04 | F07 | run_page_behavior的xhs_guarded分支改注入callback |
| 271 | `effective_scroll_recorded` | `runtime/behavior.py` | 迁 | T04 | F07 | run_page_behavior的xhs_guarded分支改注入callback |
| 280 | `wait_for_xhs_search_ready` | `platforms/xhs/behavior.py` | 迁 | T09 | F07/F15 | XHS特有动作，经窄callback注入runtime |
| 466 | `dwell_on_list_with_checks` | `runtime/behavior.py` | 迁 | T04 | F07 | run_page_behavior的xhs_guarded分支改注入callback |
| 500 | `behavior_evidence_valid` | `runtime/behavior.py` | 迁 | T04 | F07 | run_page_behavior的xhs_guarded分支改注入callback |
| 531 | `run_guarded_request_pause` | `runtime/behavior.py` | 迁 | T04 | F07 | run_page_behavior的xhs_guarded分支改注入callback |
| 560 | `wait_for_xhs_continuity_verification` | `platforms/xhs/behavior.py` | 迁 | T09 | F07/F15 | XHS特有动作，经窄callback注入runtime |
| 698 | `run_xhs_api_captcha_verification` | `platforms/xhs/behavior.py` | 迁 | T09 | F07/F15 | XHS特有动作，经窄callback注入runtime |
| 872 | `persist_xhs_continuity_failure` | `platforms/xhs/behavior.py` | 迁 | T09 | F07/F15 | XHS特有动作，经窄callback注入runtime |
| 911 | `run_xhs_continuity_behavior` | `platforms/xhs/behavior.py` | 迁 | T09 | F07/F15 | XHS特有动作，经窄callback注入runtime |
| 1007 | `_first_visible_locator` | `platforms/xhs/behavior.py` | 迁 | T09 | F07/F15 | XHS特有动作，经窄callback注入runtime |
| 1020 | `_like_control_state` | `platforms/xhs/behavior.py` | 迁 | T09 | F07/F15 | XHS特有动作，经窄callback注入runtime |
| 1043 | `_looks_liked` | `platforms/xhs/behavior.py` | 迁 | T09 | F07/F15 | XHS特有动作，经窄callback注入runtime |
| 1059 | `_looks_unliked` | `platforms/xhs/behavior.py` | 迁 | T09 | F07/F15 | XHS特有动作，经窄callback注入runtime |
| 1074 | `_run_xhs_comment_scroll` | `platforms/xhs/behavior.py` | 迁 | T09 | F07/F15 | XHS特有动作，经窄callback注入runtime |
| 1091 | `_run_xhs_like_once` | `platforms/xhs/behavior.py` | 迁 | T09 | F07/F15 | XHS特有动作，经窄callback注入runtime |
| 1137 | `run_xhs_post_interaction` | `platforms/xhs/behavior.py` | 迁 | T09 | F07/F15 | XHS特有动作，经窄callback注入runtime |
| 1216 | `run_page_behavior` | `runtime/behavior.py` | 迁 | T04 | F07 | run_page_behavior的xhs_guarded分支改注入callback |

### `scripts/human_flow.py`（19；迁19）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 19 | `BehaviorProfile` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 96 | `load_behavior_profile` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 123 | `install_runtime_hints` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 133 | `_rand_range` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 138 | `_log` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 143 | `page_size` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 148 | `scroll_position` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 179 | `scroll_effect_observed` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 188 | `human_pause` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 201 | `random_mouse_moves` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 214 | `cdp_touch_scroll` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 259 | `wheel_scroll` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 278 | `human_scroll` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 319 | `dwell_on_list` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 327 | `_detail_bounds` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 336 | `maybe_visit_comments` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 358 | `dwell_on_detail` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 372 | `inter_detail_cooldown` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |
| 379 | `return_to_list` | `runtime/human_flow.py` | 迁 | T04 | F07 | profile数值与override语义见D1 |

### `scripts/mediacrawler_login_warmup.py`（20；迁13、并2、拆4、薄1）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 70 | `parse_args` | `application/warmup.py` | 迁 | T10 | F07 | 辅助登录判定不并入worker谓词 |
| 86 | `utc_stamp` | `application/warmup.py` | 迁 | T10 | F07 | 辅助登录判定不并入worker谓词 |
| 90 | `selected_platforms` | `application/warmup.py` | 迁 | T10 | F07 | 辅助登录判定不并入worker谓词 |
| 103 | `profile_dir_for` | `core/paths.py` | 并 | T01 | F10 | 与C同名合并 |
| 108 | `cookie_dict` | `runtime/cookies.py` | 拆 | T10 | F07 | snapshot chmod/重开验证不变；与C重复项合并 |
| 112 | `required_cookie_names` | `runtime/cookies.py` | 拆 | T10 | F07 | snapshot chmod/重开验证不变；与C重复项合并 |
| 118 | `cookie_snapshot_path` | `core/paths.py` | 并 | T01 | F10 | 与C同名合并 |
| 122 | `cookie_snapshot_info` | `runtime/cookies.py` | 拆 | T10 | F07 | snapshot chmod/重开验证不变；与C重复项合并 |
| 130 | `write_cookie_snapshot` | `runtime/cookies.py` | 拆 | T10 | F07 | snapshot chmod/重开验证不变；与C重复项合并 |
| 162 | `browser_path_for` | `application/warmup.py` | 迁 | T10 | F07 | 辅助登录判定不并入worker谓词 |
| 171 | `launch_login_context` | `application/warmup.py` | 迁 | T10 | F07 | 辅助登录判定不并入worker谓词 |
| 210 | `safe_local_storage` | `application/warmup.py` | 迁 | T10 | F07 | 辅助登录判定不并入worker谓词 |
| 218 | `zhihu_api_check` | `application/warmup.py` | 迁 | T10 | F07 | 辅助登录判定不并入worker谓词 |
| 248 | `weibo_api_check` | `application/warmup.py` | 迁 | T10 | F07 | 辅助登录判定不并入worker谓词 |
| 274 | `current_state` | `application/warmup.py` | 迁 | T10 | F07 | 辅助登录判定不并入worker谓词 |
| 330 | `weibo_desktop_login_state` | `application/warmup.py` | 迁 | T10 | F07 | 辅助登录判定不并入worker谓词 |
| 346 | `weibo_desktop_login_completed` | `application/warmup.py` | 迁 | T10 | F07 | 辅助登录判定不并入worker谓词 |
| 353 | `warmup_one` | `application/warmup.py` | 迁 | T10 | F07 | 辅助登录判定不并入worker谓词 |
| 512 | `main_async` | `application/warmup.py` | 迁 | T10 | F07 | 辅助登录判定不并入worker谓词 |
| 538 | `main` | `scripts/mediacrawler_login_warmup.py` | 薄 | T10 | F07 |  |

### `scripts/login_warmup.py`（11；迁10、薄1）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 31 | `parse_args` | `application/warmup.py` | 迁 | T10 | F07 |  |
| 48 | `selected_targets` | `application/warmup.py` | 迁 | T10 | F07 |  |
| 61 | `utc_iso` | `application/warmup.py` | 迁 | T10 | F07 |  |
| 65 | `utc_stamp` | `application/warmup.py` | 迁 | T10 | F07 |  |
| 69 | `implementation_args` | `application/warmup.py` | 迁 | T10 | F07 |  |
| 79 | `error_record` | `application/warmup.py` | 迁 | T10 | F07 |  |
| 96 | `run_target` | `application/warmup.py` | 迁 | T10 | F07 |  |
| 118 | `markdown_cell` | `application/warmup.py` | 迁 | T10 | F07 |  |
| 122 | `markdown_summary` | `application/warmup.py` | 迁 | T10 | F07 |  |
| 152 | `main_async` | `application/warmup.py` | 迁 | T10 | F07 |  |
| 197 | `main` | `scripts/login_warmup.py` | 薄 | T10 | F07 |  |

### `scripts/failure_classifier.py`（13；迁13）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 127 | `_xhs_stable_sms_terminal_reason` | `application/failures.py` | 迁 | T04 | F06/F07/F08 | 原tests/test_failure_classifier.py随迁 |
| 160 | `_xhs_sms_terminal_reason` | `application/failures.py` | 迁 | T04 | F06/F07/F08 | 原tests/test_failure_classifier.py随迁 |
| 176 | `is_xhs_sms_terminal_text` | `application/failures.py` | 迁 | T04 | F06/F07/F08 | 原tests/test_failure_classifier.py随迁 |
| 182 | `_platform_security_limit_reason` | `application/failures.py` | 迁 | T04 | F06/F07/F08 | 原tests/test_failure_classifier.py随迁 |
| 211 | `extract_stdout_json` | `application/failures.py` | 迁 | T04 | F06/F07/F08 | 原tests/test_failure_classifier.py随迁 |
| 223 | `_meta_markers` | `application/failures.py` | 迁 | T04 | F06/F07/F08 | 原tests/test_failure_classifier.py随迁 |
| 230 | `_text_blob` | `application/failures.py` | 迁 | T04 | F06/F07/F08 | 原tests/test_failure_classifier.py随迁 |
| 234 | `_without_false_security_markers` | `application/failures.py` | 迁 | T04 | F06/F07/F08 | 原tests/test_failure_classifier.py随迁 |
| 249 | `_stdout_without_json_payload` | `application/failures.py` | 迁 | T04 | F06/F07/F08 | 原tests/test_failure_classifier.py随迁 |
| 259 | `_terminal_stdout_payload` | `application/failures.py` | 迁 | T04 | F06/F07/F08 | 原tests/test_failure_classifier.py随迁 |
| 275 | `_strong_child_classification` | `application/failures.py` | 迁 | T04 | F06/F07/F08 | 原tests/test_failure_classifier.py随迁 |
| 295 | `_structured_runtime_blocker` | `application/failures.py` | 迁 | T04 | F06/F07/F08 | 原tests/test_failure_classifier.py随迁 |
| 316 | `classify_attempt` | `application/failures.py` | 迁 | T04 | F06/F07/F08 | 原tests/test_failure_classifier.py随迁 |

### `scripts/crawl_policy.py`（24；迁24）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 26 | `CrawlPolicyBlocked` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 28 | `CrawlPolicyBlocked.__init__` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 35 | `utc_now` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 39 | `isoformat` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 43 | `parse_iso_timestamp` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 56 | `policy_state_lock` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 67 | `_load_policy_state_unlocked` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 76 | `load_policy_state` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 82 | `_save_policy_state_unlocked` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 97 | `save_policy_state` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 103 | `clear_site_policy_state` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 121 | `site_policy_lock` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 131 | `jitter_window_seconds` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 138 | `positive_jitter_seconds` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 143 | `varied_wait_seconds` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 159 | `_current_daily_key` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 163 | `_ceil_positive_seconds` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 167 | `_seconds_until_next_utc_day` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 172 | `_automatic_session_cooldown_until` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 184 | `_normalize_entry` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 223 | `_blocked_event` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 246 | `_compute_pacing_wait` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 262 | `site_request_guard` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |
| 395 | `record_site_cooldown` | `application/policy.py` | 迁 | T04 | F07 | 文件锁/冷却/节流语义不变 |

### `scripts/browser_runtime.py`（3；迁3）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 15 | `browser_launch_environment` | `runtime/browser_runtime.py` | 迁 | T02 | F06/F10 |  |
| 25 | `browser_runtime_args` | `runtime/browser_runtime.py` | 迁 | T02 | F06/F10 |  |
| 34 | `xhs_window_size_value` | `runtime/browser_runtime.py` | 迁 | T02 | F06/F10 |  |

### `scripts/execution_state.py`（17；迁17）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 28 | `ExecutionStateError` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |
| 32 | `utc_iso` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |
| 36 | `canonical_json` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |
| 40 | `sha256_text` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |
| 44 | `sha256_file` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |
| 52 | `_atomic_write` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |
| 72 | `FrozenExecutionState` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |
| 73 | `FrozenExecutionState.__init__` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |
| 77 | `FrozenExecutionState.create` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |
| 130 | `FrozenExecutionState.load` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |
| 139 | `FrozenExecutionState._validate_frozen` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |
| 150 | `FrozenExecutionState.begin` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |
| 168 | `FrozenExecutionState.complete` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |
| 191 | `FrozenExecutionState.fail` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |
| 210 | `FrozenExecutionState.append_event` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |
| 219 | `FrozenExecutionState.finalize` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |
| 227 | `FrozenExecutionState.finalize_failure` | `core/execution_state.py` | 迁 | T04 | F02/F06 | 严格出口，不与legacy事件合并 |

### `scripts/ctf_browser_resilience.py`（7；迁7）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 27 | `is_douyin_target` | `application/page_evidence.py` | 迁 | T10 | F10 | 仅页面证据使用 |
| 32 | `clean_douyin_profile_cookies` | `application/page_evidence.py` | 迁 | T10 | F10 | 仅页面证据使用 |
| 72 | `clear_douyin_context_cookies` | `application/page_evidence.py` | 迁 | T10 | F10 | 仅页面证据使用 |
| 99 | `page_readiness_state` | `runtime/page_readiness.py` | 迁 | T10 | F10 |  |
| 134 | `wait_for_content_ready` | `runtime/page_readiness.py` | 迁 | T10 | F10 |  |
| 152 | `wait_for_content_enrichment` | `runtime/page_readiness.py` | 迁 | T10 | F10 |  |
| 183 | `navigate_with_commit_and_readiness` | `runtime/page_readiness.py` | 迁 | T10 | F10 |  |

### `M/base/base_crawler.py`（19；退19）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 26 | `AbstractCrawler` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 29 | `AbstractCrawler.start` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 36 | `AbstractCrawler.search` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 43 | `AbstractCrawler.launch_browser` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 54 | `AbstractCrawler.launch_browser_with_cdp` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 67 | `AbstractLogin` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 70 | `AbstractLogin.begin` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 74 | `AbstractLogin.login_by_qrcode` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 78 | `AbstractStore` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 81 | `AbstractStore.store_content` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 85 | `AbstractStore.store_comment` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 91 | `AbstractStore.store_creator` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 95 | `AbstractStoreImage` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 99 | `AbstractStoreImage.store_image` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 103 | `AbstractStoreVideo` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 107 | `AbstractStoreVideo.store_video` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 111 | `AbstractApiClient` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 114 | `AbstractApiClient.request` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |
| 118 | `AbstractApiClient.update_cookies` | — | 退 | T12 | F10 | 四站解除继承后退出；职责由本站类型与D4端口承接 |

### `M/cache/abs_cache.py`（4；迁4）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 31 | `AbstractCache` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 |  |
| 34 | `AbstractCache.get` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 |  |
| 44 | `AbstractCache.set` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 |  |
| 56 | `AbstractCache.keys` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 |  |

### `M/cache/cache_factory.py`（2；退2）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 28 | `CacheFactory` | — | 退 | T06 | F07 | 改为直接构造MEMORY实现；Redis支路退出 |
| 34 | `CacheFactory.create_cache` | — | 退 | T06 | F07 | 改为直接构造MEMORY实现；Redis支路退出 |

### `M/cache/local_cache.py`（9；迁9）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 34 | `ExpiringLocalCache` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | 保留清理task生命周期 |
| 36 | `ExpiringLocalCache.__init__` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | 保留清理task生命周期 |
| 48 | `ExpiringLocalCache.__del__` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | 保留清理task生命周期 |
| 56 | `ExpiringLocalCache.get` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | 保留清理task生命周期 |
| 73 | `ExpiringLocalCache.set` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | 保留清理task生命周期 |
| 83 | `ExpiringLocalCache.keys` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | 保留清理task生命周期 |
| 98 | `ExpiringLocalCache._schedule_clear` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | 保留清理task生命周期 |
| 112 | `ExpiringLocalCache._clear` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | 保留清理task生命周期 |
| 121 | `ExpiringLocalCache._start_clear_cron` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | 保留清理task生命周期 |

### `M/cache/redis_cache.py`（6；退6）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 37 | `RedisCache` | — | 退 | T12 | — |  |
| 39 | `RedisCache.__init__` | — | 退 | T12 | — |  |
| 44 | `RedisCache._connet_redis` | — | 退 | T12 | — |  |
| 56 | `RedisCache.get` | — | 退 | T12 | — |  |
| 67 | `RedisCache.set` | — | 退 | T12 | — |  |
| 77 | `RedisCache.keys` | — | 退 | T12 | — |  |

### `M/cmd_arg/arg.py`（12；拆9、退3）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 40 | `PlatformEnum` | `application/worker_inputs.py` | 拆 | T02 | F10 | 只保留父侧生成参数与原str2bool语义 |
| 52 | `LoginTypeEnum` | `application/worker_inputs.py` | 拆 | T02 | F10 | 只保留父侧生成参数与原str2bool语义 |
| 60 | `CrawlerTypeEnum` | `application/worker_inputs.py` | 拆 | T02 | F10 | 只保留父侧生成参数与原str2bool语义 |
| 68 | `SaveDataOptionEnum` | `application/worker_inputs.py` | 拆 | T02 | F10 | 只保留父侧生成参数与原str2bool语义 |
| 81 | `InitDbOptionEnum` | `application/worker_inputs.py` | 拆 | T02 | F10 | 只保留父侧生成参数与原str2bool语义 |
| 89 | `_to_bool` | `application/worker_inputs.py` | 拆 | T02 | F10 | 只保留父侧生成参数与原str2bool语义 |
| 95 | `_coerce_enum` | `application/worker_inputs.py` | 拆 | T02 | F10 | 只保留父侧生成参数与原str2bool语义 |
| 115 | `_normalize_argv` | `application/worker_inputs.py` | 拆 | T02 | F10 | 只保留父侧生成参数与原str2bool语义 |
| 121 | `_inject_init_db_default` | — | 退 | T12 | F10 |  |
| 139 | `_normalize_tieba_note_id` | — | 退 | T12 | F10 |  |
| 146 | `_normalize_tieba_creator_url` | — | 退 | T12 | F10 |  |
| 154 | `parse_cmd` | `application/worker_inputs.py` | 拆 | T02 | F10 | 只保留父侧生成参数与原str2bool语义 |

### `M/database/db.py`（3；退3）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 35 | `init_table_schema` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 46 | `init_db` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 49 | `close` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |

### `M/database/db_session.py`（4；退4）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 31 | `create_database_if_not_exists` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 53 | `get_async_engine` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 77 | `create_tables` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 88 | `get_session` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |

### `M/database/models.py`（15；退15）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 28 | `BilibiliVideo` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 52 | `BilibiliVideoComment` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 67 | `BilibiliUpDynamic` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 82 | `DouyinAweme` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 113 | `DouyinAwemeComment` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 129 | `KuaishouVideo` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 148 | `KuaishouVideoComment` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 161 | `WeiboNote` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 187 | `WeiboNoteComment` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 203 | `XhsNote` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 243 | `XhsNoteComment` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 259 | `TiebaNote` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 278 | `TiebaComment` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 296 | `ZhihuContent` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 318 | `ZhihuComment` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |

### `M/database/mongodb_store_base.py`（13；退13）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 27 | `MongoDBConnection` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 34 | `MongoDBConnection.__new__` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 39 | `MongoDBConnection.get_client` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 47 | `MongoDBConnection.get_db` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 55 | `MongoDBConnection._connect` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 79 | `MongoDBConnection.close` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 88 | `MongoDBStoreBase` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 91 | `MongoDBStoreBase.__init__` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 99 | `MongoDBStoreBase.get_collection` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 105 | `MongoDBStoreBase.save_or_update` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 115 | `MongoDBStoreBase.find_one` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 124 | `MongoDBStoreBase.find_many` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 136 | `MongoDBStoreBase.create_index` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |

### `M/main.py`（6；退4、拆1、迁1）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 49 | `CrawlerFactory` | — | 退 | T14 | F10 | 全平台注册与关闭的Excel/词云 |
| 61 | `CrawlerFactory.create_crawler` | — | 退 | T14 | F10 | 全平台注册与关闭的Excel/词云 |
| 72 | `_flush_excel_if_needed` | — | 退 | T14 | F10 | 全平台注册与关闭的Excel/词云 |
| 85 | `_generate_wordcloud_if_needed` | — | 退 | T14 | F10 | 全平台注册与关闭的Excel/词云 |
| 99 | `main` | `platforms/entry.py` | 拆 | T02 | F10 | 选站延迟装配 |
| 122 | `async_cleanup` | `runtime/worker.py` | 迁 | T02 | F06 | 含内嵌_force_stop与信号回收 |

### `M/media_platform/douyin/client.py`（23；迁18、退5）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 59 | `DouYinClient` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 61 | `DouYinClient.__init__` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 91 | `DouYinClient._schedule_browser_search_response` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 98 | `DouYinClient.capture_browser_search_response` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 126 | `DouYinClient._take_observed_search_response` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 176 | `DouYinClient._scroll_for_observed_search_response` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 224 | `DouYinClient._build_visible_first_page_fallback` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 306 | `DouYinClient.__process_req_params` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 392 | `DouYinClient.request` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 413 | `DouYinClient.get` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 421 | `DouYinClient.post` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 426 | `DouYinClient.pong` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 437 | `DouYinClient.update_cookies` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 445 | `DouYinClient.search_info_by_keyword` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 538 | `DouYinClient.get_video_by_id` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 550 | `DouYinClient.get_aweme_comments` | — | 退 | T12 | F12 | 评论/creator批采 |
| 562 | `DouYinClient.get_sub_comments` | — | 退 | T12 | F12 | 评论/creator批采 |
| 580 | `DouYinClient.get_aweme_all_comments` | — | 退 | T12 | F12 | 评论/creator批采 |
| 639 | `DouYinClient.get_user_info` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 648 | `DouYinClient.get_user_aweme_posts` | — | 退 | T12 | F12 | 评论/creator批采 |
| 659 | `DouYinClient.get_all_user_aweme_posts` | — | 退 | T12 | F12 | 评论/creator批采 |
| 674 | `DouYinClient.get_aweme_media` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |
| 692 | `DouYinClient.resolve_short_url` | `platforms/douyin/client.py` | 迁 | T06 | F09/F12 |  |

### `M/media_platform/douyin/core.py`（20；迁12、退5、拆3）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 67 | `DouyinImageDownloadError` | `platforms/douyin/models.py` | 迁 | T06 | F09 |  |
| 70 | `DouyinImageDownloadError.__init__` | `platforms/douyin/models.py` | 迁 | T06 | F09 |  |
| 81 | `DouYinCrawler` | `platforms/douyin/core.py` | 迁 | T06 | F01/F12 | get_aweme_detail由get_specified_awemes:761调用 |
| 87 | `DouYinCrawler.__init__` | `platforms/douyin/core.py` | 迁 | T06 | F01/F12 | get_aweme_detail由get_specified_awemes:761调用 |
| 102 | `DouYinCrawler.start` | `platforms/douyin/core.py` | 迁 | T06 | F01/F12 | get_aweme_detail由get_specified_awemes:761调用 |
| 180 | `DouYinCrawler.search` | `platforms/douyin/core.py` | 迁 | T06 | F01/F12 | get_aweme_detail由get_specified_awemes:761调用 |
| 652 | `DouYinCrawler.enrich_aweme_creator` | `platforms/douyin/core.py` | 迁 | T06 | F01/F12 | get_aweme_detail由get_specified_awemes:761调用 |
| 734 | `DouYinCrawler.get_specified_awemes` | `platforms/douyin/core.py` | 迁 | T06 | F01/F12 | get_aweme_detail由get_specified_awemes:761调用 |
| 770 | `DouYinCrawler.get_aweme_detail` | `platforms/douyin/core.py` | 迁 | T06 | F01/F12 | get_aweme_detail由get_specified_awemes:761调用 |
| 789 | `DouYinCrawler.batch_get_note_comments` | — | 退 | T12 | F12 | 评论/creator批采/视频 |
| 805 | `DouYinCrawler.get_comments` | — | 退 | T12 | F12 | 评论/creator批采/视频 |
| 825 | `DouYinCrawler.get_creators_and_videos` | — | 退 | T12 | F12 | 评论/creator批采/视频 |
| 851 | `DouYinCrawler.fetch_creator_video_detail` | — | 退 | T12 | F12 | 评论/creator批采/视频 |
| 864 | `DouYinCrawler.create_douyin_client` | `platforms/douyin/core.py` | 迁 | T06 | F01/F12 | get_aweme_detail由get_specified_awemes:761调用 |
| 886 | `DouYinCrawler.launch_browser` | `platforms/douyin/core.py` | 拆 | T06 | F07 | 浏览器生命周期交runtime |
| 914 | `DouYinCrawler.launch_browser_with_cdp` | `platforms/douyin/core.py` | 拆 | T06 | F07 | 浏览器生命周期交runtime |
| 948 | `DouYinCrawler.close` | `platforms/douyin/core.py` | 拆 | T06 | F07 | 浏览器生命周期交runtime |
| 958 | `DouYinCrawler.get_aweme_media` | `platforms/douyin/core.py` | 迁 | T06 | F01/F12 | get_aweme_detail由get_specified_awemes:761调用 |
| 970 | `DouYinCrawler.get_aweme_images` | `platforms/douyin/core.py` | 迁 | T06 | F01/F12 | get_aweme_detail由get_specified_awemes:761调用 |
| 1056 | `DouYinCrawler.get_aweme_video` | — | 退 | T12 | F12 | 评论/creator批采/视频 |

### `M/media_platform/douyin/exception.py`（4；迁3、退1）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 24 | `DataFetchError` | `platforms/douyin/models.py` | 迁 | T06 | F09 |  |
| 28 | `SearchResponseError` | `platforms/douyin/models.py` | 迁 | T06 | F09 |  |
| 31 | `SearchResponseError.__init__` | `platforms/douyin/models.py` | 迁 | T06 | F09 |  |
| 36 | `IPBlockError` | — | 退 | T12 | — | 闭包内无引用，T00核 |

### `M/media_platform/douyin/field.py`（3；迁3）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 24 | `SearchChannelType` | `platforms/douyin/models.py` | 迁 | T06 | F09 |  |
| 32 | `SearchSortType` | `platforms/douyin/models.py` | 迁 | T06 | F09 |  |
| 38 | `PublishTimeType` | `platforms/douyin/models.py` | 迁 | T06 | F09 |  |

### `M/media_platform/douyin/help.py`（6；迁4、退2）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 39 | `get_web_id` | `platforms/douyin/client.py` | 迁 | T06 | F09 | 注入随机源 |
| 61 | `get_a_bogus` | `platforms/douyin/signer.py` | 迁 | T06 | F09/F10 | 模块导入期编译douyin.js时点不变 |
| 67 | `get_a_bogus_from_js` | `platforms/douyin/signer.py` | 迁 | T06 | F09/F10 | 模块导入期编译douyin.js时点不变 |
| 85 | `get_a_bogus_from_playwright` | — | 退 | T12 | — |  |
| 101 | `parse_video_info_from_url` | `platforms/douyin/parser.py` | 迁 | T06 | F08 |  |
| 141 | `parse_creator_info_from_url` | — | 退 | T12 | — |  |

### `M/media_platform/douyin/login.py`（10；迁10）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 37 | `DouYinLogin` | `platforms/douyin/login.py` | 迁 | T06 | F07 |  |
| 39 | `DouYinLogin.__init__` | `platforms/douyin/login.py` | 迁 | T06 | F07 |  |
| 53 | `DouYinLogin.begin` | `platforms/douyin/login.py` | 迁 | T06 | F07 |  |
| 92 | `DouYinLogin.check_login_state` | `platforms/douyin/login.py` | 迁 | T06 | F07 |  |
| 111 | `DouYinLogin.popup_login_dialog` | `platforms/douyin/login.py` | 迁 | T06 | F07 |  |
| 124 | `DouYinLogin.login_by_qrcode` | `platforms/douyin/login.py` | 迁 | T06 | F07 |  |
| 139 | `DouYinLogin.login_by_mobile` | `platforms/douyin/login.py` | 迁 | T06 | F07 |  |
| 171 | `DouYinLogin.check_page_display_slider` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 |  |
| 213 | `DouYinLogin.move_slider` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 |  |
| 266 | `DouYinLogin.login_by_cookies` | `platforms/douyin/login.py` | 迁 | T06 | F07 |  |

### `M/media_platform/douyin/search_safety.py`（4；迁4）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 32 | `decode_douyin_json_body` | `platforms/douyin/parser.py` | 迁 | T06 | F12 |  |
| 107 | `validate_douyin_search_response` | `platforms/douyin/parser.py` | 迁 | T06 | F12 |  |
| 142 | `classify_empty_first_page` | `platforms/douyin/parser.py` | 迁 | T06 | F12 |  |
| 150 | `inspect_empty_first_page` | `platforms/douyin/client.py` | 迁 | T06 | F12 | DOM检查 |

### `M/media_platform/weibo/client.py`（18；迁11、退7）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 54 | `weibo_image_request_urls` | `platforms/weibo/client.py` | 迁 | T05 | F09/F11 |  |
| 82 | `WeiboClient` | `platforms/weibo/client.py` | 迁 | T05 | F09/F11 |  |
| 84 | `WeiboClient.__init__` | `platforms/weibo/client.py` | 迁 | T05 | F09/F11 |  |
| 105 | `WeiboClient.request` | `platforms/weibo/client.py` | 迁 | T05 | F09/F11 |  |
| 144 | `WeiboClient.get` | `platforms/weibo/client.py` | 迁 | T05 | F09/F11 |  |
| 154 | `WeiboClient.post` | `platforms/weibo/client.py` | 迁 | T05 | F09/F11 |  |
| 158 | `WeiboClient.pong` | `platforms/weibo/client.py` | 迁 | T05 | F09/F11 |  |
| 174 | `WeiboClient.update_cookies` | `platforms/weibo/client.py` | 迁 | T05 | F09/F11 |  |
| 192 | `WeiboClient.get_note_by_keyword` | `platforms/weibo/client.py` | 迁 | T05 | F09/F11 |  |
| 214 | `WeiboClient.get_note_comments` | — | 退 | T12 | F11 | 评论/creator批采 |
| 235 | `WeiboClient.get_note_all_comments` | — | 退 | T12 | F11 | 评论/creator批采 |
| 271 | `WeiboClient.get_comments_all_sub_comments` | — | 退 | T12 | F11 | 评论/creator批采 |
| 299 | `WeiboClient.get_note_info_by_id` | `platforms/weibo/client.py` | 迁 | T05 | F09/F11 |  |
| 332 | `WeiboClient.get_note_image` | `platforms/weibo/client.py` | 迁 | T05 | F09/F11 |  |
| 388 | `WeiboClient.get_creator_container_info` | — | 退 | T12 | F11 | 评论/creator批采 |
| 406 | `WeiboClient.get_creator_info_by_id` | — | 退 | T12 | F11 | 评论/creator批采 |
| 426 | `WeiboClient.get_notes_by_creator` | — | 退 | T12 | F11 | 评论/creator批采 |
| 452 | `WeiboClient.get_all_notes_by_creator_id` | — | 退 | T12 | F11 | 评论/creator批采 |

### `M/media_platform/weibo/core.py`（20；迁13、退4、拆3）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 66 | `WeiboImageDownloadError` | `platforms/weibo/models.py` | 迁 | T05 | F09/F11 |  |
| 69 | `WeiboImageDownloadError.__init__` | `platforms/weibo/models.py` | 迁 | T05 | F09/F11 |  |
| 80 | `WeiboFullTextFetchError` | `platforms/weibo/models.py` | 迁 | T05 | F09/F11 |  |
| 83 | `WeiboFullTextFetchError.__init__` | `platforms/weibo/models.py` | 迁 | T05 | F09/F11 |  |
| 100 | `WeiboCrawler` | `platforms/weibo/core.py` | 迁 | T05 | F01/F11 |  |
| 106 | `WeiboCrawler.__init__` | `platforms/weibo/core.py` | 迁 | T05 | F01/F11 |  |
| 115 | `WeiboCrawler.start` | `platforms/weibo/core.py` | 迁 | T05 | F01/F11 |  |
| 207 | `WeiboCrawler.search` | `platforms/weibo/core.py` | 迁 | T05 | F01/F11 |  |
| 430 | `WeiboCrawler.get_specified_notes` | `platforms/weibo/core.py` | 迁 | T05 | F01/F11 |  |
| 455 | `WeiboCrawler.get_note_info_task` | `platforms/weibo/core.py` | 迁 | T05 | F01/F11 |  |
| 482 | `WeiboCrawler.batch_get_notes_comments` | — | 退 | T12 | F11 | 评论开关关闭/creator批采；batch_get_notes_full_text仅creator链调用 |
| 500 | `WeiboCrawler.get_note_comments` | — | 退 | T12 | F11 | 评论开关关闭/creator批采；batch_get_notes_full_text仅creator链调用 |
| 526 | `WeiboCrawler.get_note_images` | `platforms/weibo/core.py` | 迁 | T05 | F01/F11 |  |
| 620 | `WeiboCrawler.get_creators_and_notes` | — | 退 | T12 | F11 | 评论开关关闭/creator批采；batch_get_notes_full_text仅creator链调用 |
| 656 | `WeiboCrawler.create_weibo_client` | `platforms/weibo/core.py` | 迁 | T05 | F09 | 装配client |
| 678 | `WeiboCrawler.launch_browser` | `platforms/weibo/core.py` | 拆 | T05 | F07 | 浏览器生命周期交runtime，保留现有CDP失败回退 |
| 707 | `WeiboCrawler.launch_browser_with_cdp` | `platforms/weibo/core.py` | 拆 | T05 | F07 | 浏览器生命周期交runtime，保留现有CDP失败回退 |
| 738 | `WeiboCrawler.get_note_full_text` | `platforms/weibo/core.py` | 迁 | T05 | F01/F11 |  |
| 796 | `WeiboCrawler.batch_get_notes_full_text` | — | 退 | T12 | F11 | 评论开关关闭/creator批采；batch_get_notes_full_text仅creator链调用 |
| 808 | `WeiboCrawler.close` | `platforms/weibo/core.py` | 拆 | T05 | F07 | 浏览器生命周期交runtime，保留现有CDP失败回退 |

### `M/media_platform/weibo/exception.py`（4；迁3、退1）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 29 | `DataFetchError` | `platforms/weibo/models.py` | 迁 | T05 | F09 |  |
| 33 | `PlatformRuntimeError` | `platforms/weibo/models.py` | 迁 | T05 | F09 |  |
| 36 | `PlatformRuntimeError.__init__` | `platforms/weibo/models.py` | 迁 | T05 | F09 |  |
| 41 | `IPBlockError` | — | 退 | T12 | — | 闭包内无引用（星号导入不计），T00核 |

### `M/media_platform/weibo/field.py`（1；迁1）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 28 | `SearchType` | `platforms/weibo/models.py` | 迁 | T05 | F09 |  |

### `M/media_platform/weibo/help.py`（1；迁1）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 29 | `filter_search_result_card` | `platforms/weibo/parser.py` | 迁 | T05 | F11 |  |

### `M/media_platform/weibo/login.py`（7；迁7）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 40 | `WeiboLogin` | `platforms/weibo/login.py` | 迁 | T05 | F07 | login_by_mobile空实现照迁，不宣称可用 |
| 41 | `WeiboLogin.__init__` | `platforms/weibo/login.py` | 迁 | T05 | F07 | login_by_mobile空实现照迁，不宣称可用 |
| 55 | `WeiboLogin.begin` | `platforms/weibo/login.py` | 迁 | T05 | F07 | login_by_mobile空实现照迁，不宣称可用 |
| 70 | `WeiboLogin.check_login_state` | `platforms/weibo/login.py` | 迁 | T05 | F07 | login_by_mobile空实现照迁，不宣称可用 |
| 85 | `WeiboLogin.login_by_qrcode` | `platforms/weibo/login.py` | 迁 | T05 | F07 | login_by_mobile空实现照迁，不宣称可用 |
| 121 | `WeiboLogin.login_by_mobile` | `platforms/weibo/login.py` | 迁 | T05 | F07 | login_by_mobile空实现照迁，不宣称可用 |
| 124 | `WeiboLogin.login_by_cookies` | `platforms/weibo/login.py` | 迁 | T05 | F07 | login_by_mobile空实现照迁，不宣称可用 |

### `M/media_platform/xhs/client.py`（26；迁19、退7）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 63 | `unwrap_xhs_request_failure` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 74 | `is_recoverable_xhs_transport_failure` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 88 | `XiaoHongShuClient` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 90 | `XiaoHongShuClient.__init__` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 124 | `XiaoHongShuClient._get_manual_wait_budget` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 131 | `XiaoHongShuClient._pre_headers` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 175 | `XiaoHongShuClient.request` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 272 | `XiaoHongShuClient._build_query_string` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 280 | `XiaoHongShuClient.get` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 302 | `XiaoHongShuClient.post` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 322 | `XiaoHongShuClient.get_note_media` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 346 | `XiaoHongShuClient.query_self` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 392 | `XiaoHongShuClient.pong` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 414 | `XiaoHongShuClient.update_cookies` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 430 | `XiaoHongShuClient.get_note_by_keyword` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 462 | `XiaoHongShuClient.get_note_by_id` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 499 | `XiaoHongShuClient.get_note_comments` | — | 退 | T12 | F15 | 评论/creator批采/无调用短链 |
| 525 | `XiaoHongShuClient.get_note_sub_comments` | — | 退 | T12 | F15 | 评论/creator批采/无调用短链 |
| 557 | `XiaoHongShuClient.get_note_all_comments` | — | 退 | T12 | F15 | 评论/creator批采/无调用短链 |
| 606 | `XiaoHongShuClient.get_comments_all_sub_comments` | — | 退 | T12 | F15 | 评论/creator批采/无调用短链 |
| 689 | `XiaoHongShuClient.get_creator_info` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |
| 714 | `XiaoHongShuClient.extract_creator_info_from_html` | `platforms/xhs/client.py` | 迁 | T09 | F15 | 委托parser |
| 718 | `XiaoHongShuClient.get_notes_by_creator` | — | 退 | T12 | F15 | 评论/creator批采/无调用短链 |
| 749 | `XiaoHongShuClient.get_all_notes_by_creator` | — | 退 | T12 | F15 | 评论/creator批采/无调用短链 |
| 811 | `XiaoHongShuClient.get_note_short_url` | — | 退 | T12 | F15 | 评论/creator批采/无调用短链 |
| 831 | `XiaoHongShuClient.get_note_by_id_from_html` | `platforms/xhs/client.py` | 迁 | T09 | F09/F15 | _build_query_string保证签名与发送编码一致 |

### `M/media_platform/xhs/core.py`（87；迁81、拆1、退5）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 145 | `xhs_cdp_lifecycle_stop_detail` | `platforms/xhs/errors.py` | 迁 | T09 | F09/F15 |  |
| 152 | `is_recoverable_xhs_navigation_failure` | `platforms/xhs/errors.py` | 迁 | T09 | F09/F15 |  |
| 176 | `XHSImageDownloadError` | `platforms/xhs/errors.py` | 迁 | T09 | F09/F15 |  |
| 179 | `XHSImageDownloadError.__init__` | `platforms/xhs/errors.py` | 迁 | T09 | F09/F15 |  |
| 190 | `XHSNoteDetailUnavailable` | `platforms/xhs/errors.py` | 迁 | T09 | F09/F15 |  |
| 193 | `XHSNoteDetailUnavailable.__init__` | `platforms/xhs/errors.py` | 迁 | T09 | F09/F15 |  |
| 202 | `XHSCreatorProfileUnavailable` | `platforms/xhs/errors.py` | 迁 | T09 | F09/F15 |  |
| 205 | `XHSCreatorProfileUnavailable.__init__` | `platforms/xhs/errors.py` | 迁 | T09 | F09/F15 |  |
| 214 | `XHSNetworkRecoveryTimeout` | `platforms/xhs/errors.py` | 迁 | T09 | F09/F15 |  |
| 217 | `XHSNetworkRecoveryTimeout.__init__` | `platforms/xhs/errors.py` | 迁 | T09 | F09/F15 |  |
| 226 | `XiaoHongShuCrawler` | `platforms/xhs/core.py` | 迁 | T09 | F01/F15 | search/session编排 |
| 232 | `XiaoHongShuCrawler.__init__` | `platforms/xhs/core.py` | 迁 | T09 | F01/F15 | search/session编排 |
| 254 | `XiaoHongShuCrawler._env_float` | `application/worker_inputs.py` | 拆 | T09 | F09 | 各操作起点读取改零参reader |
| 261 | `XiaoHongShuCrawler._validate_login_contract` | `application/worker_inputs.py` | 迁 | T09 | F07 |  |
| 271 | `XiaoHongShuCrawler._page_is_closed` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 278 | `XiaoHongShuCrawler._popup_monotonic` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 282 | `XiaoHongShuCrawler._popup_sleep` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 285 | `XiaoHongShuCrawler._get_manual_wait_budget` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 292 | `XiaoHongShuCrawler._install_new_page_guard` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 305 | `XiaoHongShuCrawler._on_browser_page` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 309 | `XiaoHongShuCrawler._register_new_page` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 320 | `XiaoHongShuCrawler._hold_new_page` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 342 | `XiaoHongShuCrawler._new_guarded_page` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 354 | `XiaoHongShuCrawler._wait_before_page_close` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 383 | `XiaoHongShuCrawler._wait_for_all_new_page_guards` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 406 | `XiaoHongShuCrawler._prepare_browser_shutdown` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 438 | `XiaoHongShuCrawler._guarded_pause` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 451 | `XiaoHongShuCrawler._navigation_target_matches` | `platforms/xhs/navigation.py` | 迁 | T09 | F07/F15 | 诊断写出经artifacts/evidence |
| 463 | `XiaoHongShuCrawler._goto_with_deadline` | `platforms/xhs/navigation.py` | 迁 | T09 | F07/F15 | 诊断写出经artifacts/evidence |
| 580 | `XiaoHongShuCrawler._utc_now` | `platforms/xhs/navigation.py` | 迁 | T09 | F07/F15 | 诊断写出经artifacts/evidence |
| 583 | `XiaoHongShuCrawler._install_navigation_observers` | `platforms/xhs/navigation.py` | 迁 | T09 | F07/F15 | 诊断写出经artifacts/evidence |
| 619 | `XiaoHongShuCrawler._navigation_diagnostics_path` | `platforms/xhs/navigation.py` | 迁 | T09 | F07/F15 | 诊断写出经artifacts/evidence |
| 629 | `XiaoHongShuCrawler._write_navigation_diagnostics` | `platforms/xhs/navigation.py` | 迁 | T09 | F07/F15 | 诊断写出经artifacts/evidence |
| 653 | `XiaoHongShuCrawler._page_render_diagnostic` | `platforms/xhs/navigation.py` | 迁 | T09 | F07/F15 | 诊断写出经artifacts/evidence |
| 683 | `XiaoHongShuCrawler._record_navigation_diagnostic` | `platforms/xhs/navigation.py` | 迁 | T09 | F07/F15 | 诊断写出经artifacts/evidence |
| 707 | `XiaoHongShuCrawler._wait_for_visible_page_shell` | `platforms/xhs/navigation.py` | 迁 | T09 | F07/F15 | 诊断写出经artifacts/evidence |
| 750 | `XiaoHongShuCrawler._open_behavior_search_page` | `platforms/xhs/navigation.py` | 迁 | T09 | F07/F15 | 诊断写出经artifacts/evidence |
| 805 | `XiaoHongShuCrawler._close_page_with_deadline` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 820 | `XiaoHongShuCrawler._browser_identity_headers` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 879 | `XiaoHongShuCrawler._maybe_run_post_interaction` | `platforms/xhs/behavior.py` | 迁 | T09 | F07 | 一次互动latch |
| 934 | `XiaoHongShuCrawler._profile_dir` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 940 | `XiaoHongShuCrawler._assert_primary_page_alive` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 947 | `XiaoHongShuCrawler._is_target_closed_error` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 955 | `XiaoHongShuCrawler._run_human_behavior_on_primary_page` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 967 | `XiaoHongShuCrawler._open_behavior_search_page_on_primary_page` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 979 | `XiaoHongShuCrawler._assert_cdp_lifecycle_alive` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 986 | `XiaoHongShuCrawler._run_qrcode_login` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 1053 | `XiaoHongShuCrawler._single_page_for_login` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 1077 | `XiaoHongShuCrawler._profile_ui_visible` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 1101 | `XiaoHongShuCrawler._wait_for_initial_page_settle` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 1119 | `XiaoHongShuCrawler._request_failure_exception` | `platforms/xhs/errors.py` | 迁 | T09 | F09/F15 |  |
| 1130 | `XiaoHongShuCrawler._request_failure_attempts` | `platforms/xhs/errors.py` | 迁 | T09 | F09/F15 |  |
| 1138 | `XiaoHongShuCrawler._is_login_expired_failure` | `platforms/xhs/errors.py` | 迁 | T09 | F09/F15 |  |
| 1154 | `XiaoHongShuCrawler._classify_visible_terminal` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 1196 | `XiaoHongShuCrawler._popup_checkpoint_state` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 1271 | `XiaoHongShuCrawler._raise_for_terminal_popup_state` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 1288 | `XiaoHongShuCrawler._assert_network_recovery_session` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 1328 | `XiaoHongShuCrawler._record_network_recovery_event` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 1354 | `XiaoHongShuCrawler._pause_for_network_recovery` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 1441 | `XiaoHongShuCrawler._finish_network_recovery` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 1466 | `XiaoHongShuCrawler._abort_network_recovery` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 1492 | `XiaoHongShuCrawler._run_with_network_recovery` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 1526 | `XiaoHongShuCrawler._pong_with_network_recovery` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 1534 | `XiaoHongShuCrawler._wait_for_midrun_login_recovery` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 1662 | `XiaoHongShuCrawler.start` | `platforms/xhs/core.py` | 迁 | T09 | F01/F15 | search/session编排 |
| 1702 | `XiaoHongShuCrawler._run_browser_session` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 1809 | `XiaoHongShuCrawler.search` | `platforms/xhs/core.py` | 迁 | T09 | F01/F15 | search/session编排 |
| 2370 | `XiaoHongShuCrawler.get_creators_and_notes` | — | 退 | T12 | F15 | creator批采/评论/视频 |
| 2416 | `XiaoHongShuCrawler.fetch_creator_notes_detail` | — | 退 | T12 | F15 | creator批采/评论/视频 |
| 2440 | `XiaoHongShuCrawler.get_specified_notes` | `platforms/xhs/detail.py` | 迁 | T09 | F08/F15 |  |
| 2475 | `XiaoHongShuCrawler.enrich_note_creator` | `platforms/xhs/author.py` | 迁 | T09 | F15 |  |
| 2528 | `XiaoHongShuCrawler._raise_for_creator_page_terminal` | `platforms/xhs/author.py` | 迁 | T09 | F15 |  |
| 2559 | `XiaoHongShuCrawler._get_creator_info_from_browser` | `platforms/xhs/author.py` | 迁 | T09 | F15 |  |
| 2616 | `XiaoHongShuCrawler._recover_creator_login_on_primary_page` | `platforms/xhs/author.py` | 迁 | T09 | F15 |  |
| 2624 | `XiaoHongShuCrawler._wait_for_creator_profile_verification` | `platforms/xhs/author.py` | 迁 | T09 | F15 |  |
| 2679 | `XiaoHongShuCrawler.is_video_note` | `platforms/xhs/parser.py` | 迁 | T09 | F15 |  |
| 2684 | `XiaoHongShuCrawler.note_detail_summaries` | `platforms/xhs/parser.py` | 迁 | T09 | F15 |  |
| 2710 | `XiaoHongShuCrawler.get_note_detail_async_task` | `platforms/xhs/detail.py` | 迁 | T09 | F08/F15 |  |
| 2804 | `XiaoHongShuCrawler.batch_get_note_comments` | — | 退 | T12 | F15 | creator批采/评论/视频 |
| 2821 | `XiaoHongShuCrawler.get_comments` | — | 退 | T12 | F15 | creator批采/评论/视频 |
| 2842 | `XiaoHongShuCrawler.create_xhs_client` | `platforms/xhs/core.py` | 迁 | T09 | F01/F15 | search/session编排 |
| 2873 | `XiaoHongShuCrawler.launch_browser` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 2900 | `XiaoHongShuCrawler.launch_browser_with_cdp` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 2930 | `XiaoHongShuCrawler.close` | `platforms/xhs/session.py` | 迁 | T09 | F07/F15 | 页守卫、弹窗、网络恢复、主Page存活、浏览器生命周期 |
| 2980 | `XiaoHongShuCrawler.get_notice_media` | `platforms/xhs/media.py` | 迁 | T09 | F05/F15 | 视频分支保持禁用 |
| 2987 | `XiaoHongShuCrawler.get_note_images` | `platforms/xhs/media.py` | 迁 | T09 | F05/F15 | 视频分支保持禁用 |
| 3072 | `XiaoHongShuCrawler.get_notice_video` | — | 退 | T12 | F15 | creator批采/评论/视频 |

### `M/media_platform/xhs/exception.py`（5；迁5）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 24 | `DataFetchError` | `platforms/xhs/errors.py` | 迁 | T09 | F09 |  |
| 28 | `IPBlockError` | `platforms/xhs/errors.py` | 迁 | T09 | F09 |  |
| 32 | `PlatformRuntimeError` | `platforms/xhs/errors.py` | 迁 | T09 | F09 |  |
| 35 | `PlatformRuntimeError.__init__` | `platforms/xhs/errors.py` | 迁 | T09 | F09 |  |
| 40 | `NoteNotFoundError` | `platforms/xhs/errors.py` | 迁 | T09 | F09 |  |

### `M/media_platform/xhs/extractor.py`（4；迁4）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 27 | `XiaoHongShuExtractor` | `platforms/xhs/parser.py` | 迁 | T09 | F15 |  |
| 28 | `XiaoHongShuExtractor.__init__` | `platforms/xhs/parser.py` | 迁 | T09 | F15 |  |
| 31 | `XiaoHongShuExtractor.extract_note_detail_from_html` | `platforms/xhs/parser.py` | 迁 | T09 | F15 |  |
| 52 | `XiaoHongShuExtractor.extract_creator_info_from_html` | `platforms/xhs/parser.py` | 迁 | T09 | F15 |  |

### `M/media_platform/xhs/field.py`（5；退3、迁2）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 25 | `FeedType` | — | 退 | T12 | — | 闭包内无引用 |
| 50 | `NoteType` | — | 退 | T12 | — | 闭包内无引用 |
| 55 | `SearchSortType` | `platforms/xhs/models.py` | 迁 | T09 | F09 |  |
| 65 | `SearchNoteType` | `platforms/xhs/models.py` | 迁 | T09 | F09 |  |
| 75 | `Note` | — | 退 | T12 | — | 闭包内无引用 |

### `M/media_platform/xhs/help.py`（15；退12、迁3）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 31 | `sign` | — | 退 | T12 | F09 | 旧签名算法/仅__main__或creator链调用 |
| 62 | `get_b3_trace_id` | — | 退 | T12 | F09 | 旧签名算法/仅__main__或creator链调用 |
| 71 | `mrc` | — | 退 | T12 | F09 | 旧签名算法/仅__main__或creator链调用 |
| 197 | `tripletToBase64` | — | 退 | T12 | F09 | 旧签名算法/仅__main__或creator链调用 |
| 206 | `encodeChunk` | — | 退 | T12 | F09 | 旧签名算法/仅__main__或creator链调用 |
| 215 | `b64Encode` | — | 退 | T12 | F09 | 旧签名算法/仅__main__或creator链调用 |
| 235 | `encodeUtf8` | — | 退 | T12 | F09 | 旧签名算法/仅__main__或creator链调用 |
| 252 | `base36encode` | `platforms/xhs/parser.py` | 迁 | T09 | F15 | search_id的时钟/随机由core注入 |
| 274 | `base36decode` | — | 退 | T12 | F09 | 旧签名算法/仅__main__或creator链调用 |
| 278 | `get_search_id` | `platforms/xhs/parser.py` | 迁 | T09 | F15 | search_id的时钟/随机由core注入 |
| 291 | `get_img_url_by_trace_id` | — | 退 | T12 | F09 | 旧签名算法/仅__main__或creator链调用 |
| 295 | `get_img_urls_by_trace_id` | — | 退 | T12 | F09 | 旧签名算法/仅__main__或creator链调用 |
| 299 | `get_trace_id` | — | 退 | T12 | F09 | 旧签名算法/仅__main__或creator链调用 |
| 304 | `parse_note_info_from_note_url` | `platforms/xhs/parser.py` | 迁 | T09 | F15 | search_id的时钟/随机由core注入 |
| 319 | `parse_creator_info_from_url` | — | 退 | T12 | F09 | 旧签名算法/仅__main__或creator链调用 |

### `M/media_platform/xhs/login.py`（24；迁24）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 36 | `XiaoHongShuLogin` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 128 | `XiaoHongShuLogin.__init__` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 147 | `XiaoHongShuLogin._get_manual_wait_budget` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 154 | `XiaoHongShuLogin._new_login_page` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 159 | `XiaoHongShuLogin._close_extra_login_page` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 175 | `XiaoHongShuLogin._single_login_page` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 198 | `XiaoHongShuLogin._login_pages` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 212 | `XiaoHongShuLogin._selector_is_visible` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 227 | `XiaoHongShuLogin._any_selector_is_visible` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 238 | `XiaoHongShuLogin._visible_page_text` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 266 | `XiaoHongShuLogin._matching_markers` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 275 | `XiaoHongShuLogin.classify_terminal_state` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 355 | `XiaoHongShuLogin._checkpoint_kind` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 410 | `XiaoHongShuLogin._remember_observation` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 422 | `XiaoHongShuLogin.terminal_context` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 444 | `XiaoHongShuLogin.terminal_failure_type` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 451 | `XiaoHongShuLogin._page_login_observation` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 532 | `XiaoHongShuLogin._login_observation` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 596 | `XiaoHongShuLogin._is_pure_qr_observation` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 610 | `XiaoHongShuLogin._is_pure_expired_qr_observation` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 624 | `XiaoHongShuLogin._click_expired_qr_component_refresh` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 658 | `XiaoHongShuLogin._check_login_state_once` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 707 | `XiaoHongShuLogin.begin` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |
| 713 | `XiaoHongShuLogin.login_by_qrcode` | `platforms/xhs/login.py` | 迁 | T09 | F07/F15 |  |

### `M/media_platform/xhs/manual_wait.py`（23；迁23）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 19 | `XHSManualWaitBudgetExhausted` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 22 | `XHSManualWaitBudgetExhausted.__init__` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 29 | `XHSManualWaitTicket` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 32 | `XHSManualWaitTicket.__init__` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 39 | `XHSManualWaitTicket.remaining_seconds` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 43 | `XHSManualWaitTicket.manual_elapsed_seconds` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 46 | `XHSManualWaitTicket.raise_if_exhausted` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 50 | `XHSManualWaitTicket.paused` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 62 | `XHSManualWaitTicket.close` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 68 | `XHSManualWaitTicket.__enter__` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 71 | `XHSManualWaitTicket.__exit__` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 75 | `XHSManualWaitBudget` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 78 | `XHSManualWaitBudget.__init__` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 98 | `XHSManualWaitBudget.from_environment` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 115 | `XHSManualWaitBudget.now` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 118 | `XHSManualWaitBudget._is_charging` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 124 | `XHSManualWaitBudget._transition` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 132 | `XHSManualWaitBudget.manual_elapsed_seconds` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 139 | `XHSManualWaitBudget.remaining_seconds` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 142 | `XHSManualWaitBudget.raise_if_exhausted` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 146 | `XHSManualWaitBudget.start` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 153 | `XHSManualWaitBudget._set_paused` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |
| 169 | `XHSManualWaitBudget._close` | `platforms/xhs/manual_wait.py` | 迁 | T09 | F07/F15 | 全轮单实例 |

### `M/media_platform/xhs/playwright_sign.py`（1；迁1）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 33 | `sign_with_xhshow` | `platforms/xhs/signer.py` | 迁 | T09 | F09 | 延迟import xhshow，每次新建实例 |

### `M/media_platform/xhs/xhs_sign.py`（7；退6、迁1）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 79 | `_right_shift_unsigned` | — | 退 | T12 | F09 | 旧算法，无调用 |
| 86 | `mrc` | — | 退 | T12 | F09 | 旧算法，无调用 |
| 94 | `_triplet_to_base64` | — | 退 | T12 | F09 | 旧算法，无调用 |
| 104 | `_encode_chunk` | — | 退 | T12 | F09 | 旧算法，无调用 |
| 113 | `encode_utf8` | — | 退 | T12 | F09 | 旧算法，无调用 |
| 128 | `b64_encode` | — | 退 | T12 | F09 | 旧算法，无调用 |
| 150 | `get_trace_id` | `platforms/xhs/signer.py` | 迁 | T09 | F09 | x-b3-traceid随机回退 |

### `M/media_platform/zhihu/client.py`（24；迁13、退11）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 52 | `ZhiHuClient` | `platforms/zhihu/client.py` | 迁 | T07 | F09/F13 |  |
| 54 | `ZhiHuClient.__init__` | `platforms/zhihu/client.py` | 迁 | T07 | F09/F13 |  |
| 73 | `ZhiHuClient._pre_headers` | `platforms/zhihu/client.py` | 迁 | T07 | F09/F13 |  |
| 91 | `ZhiHuClient.request` | `platforms/zhihu/client.py` | 迁 | T07 | F09/F13 |  |
| 140 | `ZhiHuClient.get` | `platforms/zhihu/client.py` | 迁 | T07 | F09/F13 |  |
| 157 | `ZhiHuClient.pong` | `platforms/zhihu/client.py` | 迁 | T07 | F09/F13 |  |
| 177 | `ZhiHuClient.update_cookies` | `platforms/zhihu/client.py` | 迁 | T07 | F09/F13 |  |
| 193 | `ZhiHuClient.get_current_user_info` | `platforms/zhihu/client.py` | 迁 | T07 | F09/F13 |  |
| 202 | `ZhiHuClient.get_content_image` | `platforms/zhihu/client.py` | 迁 | T07 | F09/F13 |  |
| 246 | `ZhiHuClient.get_note_by_keyword` | `platforms/zhihu/client.py` | 迁 | T07 | F09/F13 |  |
| 288 | `ZhiHuClient.get_root_comments` | — | 退 | T12 | F13 | 评论/creator批采 |
| 319 | `ZhiHuClient.get_child_comments` | — | 退 | T12 | F13 | 评论/creator批采 |
| 345 | `ZhiHuClient.get_note_all_comments` | — | 退 | T12 | F13 | 评论/creator批采 |
| 390 | `ZhiHuClient.get_comments_all_sub_comments` | — | 退 | T12 | F13 | 评论/creator批采 |
| 443 | `ZhiHuClient.get_creator_info` | — | 退 | T12 | F13 | 评论/creator批采 |
| 456 | `ZhiHuClient.get_creator_answers` | — | 退 | T12 | F13 | 评论/creator批采 |
| 478 | `ZhiHuClient.get_creator_articles` | — | 退 | T12 | F13 | 评论/creator批采 |
| 499 | `ZhiHuClient.get_creator_videos` | — | 退 | T12 | F13 | 评论/creator批采 |
| 519 | `ZhiHuClient.get_all_anwser_by_creator` | — | 退 | T12 | F13 | 评论/creator批采 |
| 549 | `ZhiHuClient.get_all_articles_by_creator` | — | 退 | T12 | F13 | 评论/creator批采 |
| 583 | `ZhiHuClient.get_all_videos_by_creator` | — | 退 | T12 | F13 | 评论/creator批采 |
| 617 | `ZhiHuClient.get_answer_info` | `platforms/zhihu/client.py` | 迁 | T07 | F09/F13 |  |
| 635 | `ZhiHuClient.get_article_info` | `platforms/zhihu/client.py` | 迁 | T07 | F09/F13 |  |
| 648 | `ZhiHuClient.get_video_info` | `platforms/zhihu/client.py` | 迁 | T07 | F13 | 指定详情遇zvideo时调用；照迁，视频由根后置过滤 |

### `M/media_platform/zhihu/core.py`（23；迁14、拆6、退3）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 65 | `ZhihuImageDownloadError` | `platforms/zhihu/models.py` | 迁 | T07 | F09/F13 |  |
| 68 | `ZhihuImageDownloadError.__init__` | `platforms/zhihu/models.py` | 迁 | T07 | F09/F13 |  |
| 79 | `ZhihuDetailFetchError` | `platforms/zhihu/models.py` | 迁 | T07 | F09/F13 |  |
| 82 | `ZhihuDetailFetchError.__init__` | `platforms/zhihu/models.py` | 迁 | T07 | F09/F13 |  |
| 99 | `ZhihuCrawler` | `platforms/zhihu/core.py` | 迁 | T07 | F13 |  |
| 105 | `ZhihuCrawler.__init__` | `platforms/zhihu/core.py` | 迁 | T07 | F13 |  |
| 115 | `ZhihuCrawler._env_float` | `application/worker_inputs.py` | 拆 | T07 | F09 | settle值改零参reader，读点时刻不变 |
| 124 | `ZhihuCrawler._activate_latest_zhihu_page` | `platforms/zhihu/core.py` | 拆 | T07 | F07 | 浏览器生命周期交runtime |
| 134 | `ZhihuCrawler._close_stale_pages` | `platforms/zhihu/core.py` | 拆 | T07 | F07 | 浏览器生命周期交runtime |
| 155 | `ZhihuCrawler._wait_for_initial_login_settle` | `platforms/zhihu/core.py` | 迁 | T07 | F13 |  |
| 173 | `ZhihuCrawler.enrich_search_content_detail` | `platforms/zhihu/core.py` | 迁 | T07 | F13 |  |
| 233 | `ZhihuCrawler.start` | `platforms/zhihu/core.py` | 迁 | T07 | F13 |  |
| 352 | `ZhihuCrawler.search` | `platforms/zhihu/core.py` | 迁 | T07 | F13 |  |
| 550 | `ZhihuCrawler.batch_get_content_comments` | — | 退 | T12 | F13 |  |
| 574 | `ZhihuCrawler.get_comments` | — | 退 | T12 | F13 |  |
| 601 | `ZhihuCrawler.get_creators_and_notes` | — | 退 | T12 | F13 |  |
| 655 | `ZhihuCrawler.get_note_detail` | `platforms/zhihu/core.py` | 迁 | T07 | F13 |  |
| 775 | `ZhihuCrawler.get_specified_notes` | `platforms/zhihu/core.py` | 迁 | T07 | F13 |  |
| 835 | `ZhihuCrawler.get_content_images` | `platforms/zhihu/core.py` | 迁 | T07 | F13 |  |
| 919 | `ZhihuCrawler.create_zhihu_client` | `platforms/zhihu/core.py` | 迁 | T07 | F13 |  |
| 948 | `ZhihuCrawler.launch_browser` | `platforms/zhihu/core.py` | 拆 | T07 | F07 | 浏览器生命周期交runtime |
| 982 | `ZhihuCrawler.launch_browser_with_cdp` | `platforms/zhihu/core.py` | 拆 | T07 | F07 | 浏览器生命周期交runtime |
| 1015 | `ZhihuCrawler.close` | `platforms/zhihu/core.py` | 拆 | T07 | F07 | 浏览器生命周期交runtime |

### `M/media_platform/zhihu/exception.py`（5；迁3、退2）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 24 | `DataFetchError` | `platforms/zhihu/models.py` | 迁 | T07 | F09 |  |
| 28 | `PlatformRuntimeError` | `platforms/zhihu/models.py` | 迁 | T07 | F09 |  |
| 31 | `PlatformRuntimeError.__init__` | `platforms/zhihu/models.py` | 迁 | T07 | F09 |  |
| 36 | `IPBlockError` | — | 退 | T12 | — | 闭包内无引用，T00核 |
| 39 | `ForbiddenError` | — | 退 | T12 | — | 闭包内无引用，T00核 |

### `M/media_platform/zhihu/field.py`（3；迁3）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 27 | `SearchTime` | `platforms/zhihu/models.py` | 迁 | T07 | F09 |  |
| 40 | `SearchType` | `platforms/zhihu/models.py` | 迁 | T07 | F09 |  |
| 50 | `SearchSort` | `platforms/zhihu/models.py` | 迁 | T07 | F09 |  |

### `M/media_platform/zhihu/help.py`（28；迁21、退7）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 71 | `_first_non_empty` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 78 | `_normalize_image_url` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 87 | `_find_target_content_entity` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 131 | `_detail_json_payloads` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 147 | `extract_image_urls_from_html` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 174 | `extract_zhihu_content_text` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 219 | `merge_search_content_detail` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 241 | `sign` | `platforms/zhihu/signer.py` | 迁 | T07 | F09/F10 | 首次签名编译后复用 |
| 259 | `ZhihuExtractor` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 260 | `ZhihuExtractor.__init__` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 264 | `ZhihuExtractor._apply_author_info` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 277 | `ZhihuExtractor.extract_contents_from_search` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 294 | `ZhihuExtractor._extract_content_list` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 318 | `ZhihuExtractor._extract_answer_content` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 347 | `ZhihuExtractor._extract_article_content` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 376 | `ZhihuExtractor._extract_zvideo_content` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 407 | `ZhihuExtractor._extract_content_or_comment_author` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 447 | `ZhihuExtractor.extract_comments` | — | 退 | T12 | F13 | 评论/creator/zvideo详情 |
| 466 | `ZhihuExtractor._extract_comment` | — | 退 | T12 | F13 | 评论/creator/zvideo详情 |
| 494 | `ZhihuExtractor._extract_comment_ip_location` | — | 退 | T12 | F13 | 评论/creator/zvideo详情 |
| 513 | `ZhihuExtractor.extract_offset` | — | 退 | T12 | F13 | 评论/creator/zvideo详情 |
| 533 | `ZhihuExtractor._foramt_gender_text` | — | 退 | T12 | F13 | 评论/creator/zvideo详情 |
| 550 | `ZhihuExtractor.extract_creator` | — | 退 | T12 | F13 | 评论/creator/zvideo详情 |
| 596 | `ZhihuExtractor.extract_content_list_from_creator` | — | 退 | T12 | F13 | 评论/creator/zvideo详情 |
| 613 | `ZhihuExtractor.extract_answer_content_from_html` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 644 | `ZhihuExtractor.extract_article_content_from_html` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 675 | `ZhihuExtractor.extract_zvideo_content_from_html` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |
| 704 | `judge_zhihu_url` | `platforms/zhihu/parser.py` | 迁 | T07 | F13 | _extract_zvideo_content仍在搜索列表分支中调用，照迁，后由根视频过滤 |

### `M/media_platform/zhihu/login.py`（7；迁7）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 36 | `ZhiHuLogin` | `platforms/zhihu/login.py` | 迁 | T07 | F07 |  |
| 38 | `ZhiHuLogin.__init__` | `platforms/zhihu/login.py` | 迁 | T07 | F07 |  |
| 52 | `ZhiHuLogin.check_login_state` | `platforms/zhihu/login.py` | 迁 | T07 | F07 |  |
| 65 | `ZhiHuLogin.begin` | `platforms/zhihu/login.py` | 迁 | T07 | F07 |  |
| 77 | `ZhiHuLogin.login_by_mobile` | `platforms/zhihu/login.py` | 迁 | T07 | F07 |  |
| 81 | `ZhiHuLogin.login_by_qrcode` | `platforms/zhihu/login.py` | 迁 | T07 | F07 |  |
| 115 | `ZhiHuLogin.login_by_cookies` | `platforms/zhihu/login.py` | 迁 | T07 | F07 |  |

### `M/model/m_douyin.py`（2；迁1、退1）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 26 | `VideoUrlInfo` | `platforms/douyin/models.py` | 迁 | T06 | F08/F12 | 图文仍复用 |
| 32 | `CreatorUrlInfo` | — | 退 | T12 | — | creator批采/评论/未选平台模型 |

### `M/model/m_xiaohongshu.py`（2；迁1、退1）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 27 | `NoteUrlInfo` | `platforms/xhs/models.py` | 迁 | T09 | F08 |  |
| 33 | `CreatorUrlInfo` | — | 退 | T12 | — | creator批采/评论/未选平台模型 |

### `M/model/m_zhihu.py`（3；迁2、退1）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 25 | `ZhihuContent` | `platforms/zhihu/models.py` | 迁 | T07 | F03/F13 |  |
| 66 | `ZhihuComment` | — | 退 | T12 | — | creator批采/评论/未选平台模型 |
| 84 | `ZhihuCreator` | `platforms/zhihu/models.py` | 迁 | T07 | F03/F13 |  |

### `M/proxy/base_proxy.py`（7；退7）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 38 | `IpGetError` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 42 | `ProxyProvider` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 44 | `ProxyProvider.get_proxy` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 54 | `IpCache` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 55 | `IpCache.__init__` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 58 | `IpCache.set_ip` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 68 | `IpCache.load_all_ip` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |

### `M/proxy/providers/jishu_http_proxy.py`（4；退4）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 36 | `JiSuHttpProxy` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 38 | `JiSuHttpProxy.__init__` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 57 | `JiSuHttpProxy.get_proxy` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 99 | `new_jisu_http_proxy` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |

### `M/proxy/providers/kuaidl_proxy.py`（6；退6）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 41 | `KuaidailiProxyModel` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 47 | `parse_kuaidaili_proxy` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 72 | `KuaiDaiLiProxy` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 73 | `KuaiDaiLiProxy.__init__` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 96 | `KuaiDaiLiProxy.get_proxy` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 150 | `new_kuai_daili_proxy` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |

### `M/proxy/providers/wandou_http_proxy.py`（4；退4）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 36 | `WanDouHttpProxy` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 38 | `WanDouHttpProxy.__init__` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 52 | `WanDouHttpProxy.get_proxy` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 110 | `new_wandou_http_proxy` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |

### `M/proxy/proxy_ip_pool.py`（11；退11）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 44 | `ProxyIpPool` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 46 | `ProxyIpPool.__init__` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 63 | `ProxyIpPool.load_proxies` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 71 | `ProxyIpPool._is_valid_proxy` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 100 | `ProxyIpPool.get_proxy` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 118 | `ProxyIpPool.is_current_proxy_expired` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 130 | `ProxyIpPool.get_or_refresh_proxy` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 146 | `ProxyIpPool._reload_proxies` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 155 | `StaticProxyProvider` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 156 | `StaticProxyProvider.get_proxy` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 198 | `create_ip_pool` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |

### `M/proxy/proxy_mixin.py`（3；退3）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 34 | `ProxyRefreshMixin` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 49 | `ProxyRefreshMixin.init_proxy_pool` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 57 | `ProxyRefreshMixin._refresh_proxy_if_expired` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |

### `M/proxy/types.py`（3；退3）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 32 | `ProviderNameEnum` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 38 | `IpInfoModel` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |
| 48 | `IpInfoModel.is_expired` | — | 退 | T12 | F09/F10 | 正式proxy=false；四站client解除ProxyRefreshMixin时保持pool为None的无操作等价 |

### `M/recv_sms.py`（4；退4）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 38 | `SmsNotification` | — | 退 | T12 | F07 | 独立短信接收服务；T00核实无正式/辅助phone登录用法 |
| 46 | `extract_verification_code` | — | 退 | T12 | F07 | 独立短信接收服务；T00核实无正式/辅助phone登录用法 |
| 56 | `receive_sms_notification` | — | 退 | T12 | F07 | 独立短信接收服务；T00核实无正式/辅助phone登录用法 |
| 83 | `not_found` | — | 退 | T12 | F07 | 独立短信接收服务；T00核实无正式/辅助phone登录用法 |

### `M/store/douyin/__init__.py`（20；迁11、退7、拆2）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 35 | `_first_nonempty` | `platforms/douyin/parser.py` | 迁 | T06 | F03/F04 | 纯投影；写出经RecordSink |
| 42 | `_nested_value` | `platforms/douyin/parser.py` | 迁 | T06 | F03/F04 | 纯投影；写出经RecordSink |
| 55 | `_creator_user_profile` | `platforms/douyin/parser.py` | 迁 | T06 | F03/F04 | 纯投影；写出经RecordSink |
| 67 | `_author_metric` | `platforms/douyin/parser.py` | 迁 | T06 | F03/F04 | 纯投影；写出经RecordSink |
| 94 | `_normalized_author_stats` | `platforms/douyin/parser.py` | 迁 | T06 | F03/F04 | 纯投影；写出经RecordSink |
| 135 | `DouyinStoreFactory` | — | 退 | T12 | F12 | 评论/creator/工厂/视频 |
| 148 | `DouyinStoreFactory.create_store` | — | 退 | T12 | F12 | 评论/creator/工厂/视频 |
| 155 | `_extract_note_image_list` | `platforms/douyin/parser.py` | 迁 | T06 | F03/F04 | 纯投影；写出经RecordSink |
| 168 | `_extract_note_image_assets` | `platforms/douyin/parser.py` | 迁 | T06 | F03/F04 | 纯投影；写出经RecordSink |
| 207 | `_extract_comment_image_list` | — | 退 | T12 | F12 | 评论/creator/工厂/视频 |
| 231 | `_extract_content_cover_url` | `platforms/douyin/parser.py` | 迁 | T06 | F03/F04 | 纯投影；写出经RecordSink |
| 251 | `_extract_video_download_url` | `platforms/douyin/parser.py` | 迁 | T06 | F03/F04 | 纯投影；写出经RecordSink |
| 271 | `_extract_music_download_url` | `platforms/douyin/parser.py` | 迁 | T06 | F03/F04 | 纯投影；写出经RecordSink |
| 287 | `update_douyin_aweme` | `platforms/douyin/parser.py` | 迁 | T06 | F03/F04 | 纯投影；写出经RecordSink |
| 332 | `batch_update_dy_aweme_comments` | — | 退 | T12 | F12 | 评论/creator/工厂/视频 |
| 339 | `update_dy_aweme_comment` | — | 退 | T12 | F12 | 评论/creator/工厂/视频 |
| 365 | `save_creator` | — | 退 | T12 | F12 | 评论/creator/工厂/视频 |
| 370 | `update_dy_aweme_images` | `platforms/douyin/core.py` | 拆 | T06 | F05 | 图片写出改经ImageStager端口 |
| 376 | `record_dy_aweme_image_failure` | `platforms/douyin/core.py` | 拆 | T06 | F05 | 图片写出改经ImageStager端口 |
| 382 | `update_dy_aweme_video` | — | 退 | T12 | F12 | 评论/creator/工厂/视频 |

### `M/store/douyin/_store_impl.py`（27；退24、拆3）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 43 | `DouyinCsvStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 44 | `DouyinCsvStoreImplement.__init__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 50 | `DouyinCsvStoreImplement.store_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 64 | `DouyinCsvStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 78 | `DouyinCsvStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 93 | `DouyinDbStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 94 | `DouyinDbStoreImplement.store_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 115 | `DouyinDbStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 135 | `DouyinDbStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 140 | `DouyinJsonStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 141 | `DouyinJsonStoreImplement.__init__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 147 | `DouyinJsonStoreImplement.store_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 161 | `DouyinJsonStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 175 | `DouyinJsonStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 191 | `DouyinJsonlStoreImplement` | `artifacts/jsonl.py` | 拆 | T06 | F04/F09 | 仅JSONL内容出口 |
| 192 | `DouyinJsonlStoreImplement.__init__` | `artifacts/jsonl.py` | 拆 | T06 | F04/F09 | 仅JSONL内容出口 |
| 198 | `DouyinJsonlStoreImplement.store_content` | `artifacts/jsonl.py` | 拆 | T06 | F04/F09 | 仅JSONL内容出口 |
| 204 | `DouyinJsonlStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 210 | `DouyinJsonlStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 217 | `DouyinSqliteStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 221 | `DouyinMongoStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 224 | `DouyinMongoStoreImplement.__init__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 227 | `DouyinMongoStoreImplement.store_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 244 | `DouyinMongoStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 261 | `DouyinMongoStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 266 | `DouyinExcelStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 269 | `DouyinExcelStoreImplement.__new__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |

### `M/store/douyin/douyin_store_media.py`（9；拆4、退5）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 38 | `DouYinImage` | `artifacts/image_staging.py` | 拆 | T06 | F05 | 四站实现逐行差分；一致部分合并，差异作显式参数 |
| 39 | `DouYinImage.__init__` | `artifacts/image_staging.py` | 拆 | T06 | F05 | 四站实现逐行差分；一致部分合并，差异作显式参数 |
| 48 | `DouYinImage.store_post_images` | `artifacts/image_staging.py` | 拆 | T06 | F05 | 四站实现逐行差分；一致部分合并，差异作显式参数 |
| 74 | `DouYinImage.record_failure` | `artifacts/image_staging.py` | 拆 | T06 | F05 | 四站实现逐行差分；一致部分合并，差异作显式参数 |
| 94 | `DouYinVideo` | — | 退 | T12 | — | 视频 |
| 95 | `DouYinVideo.__init__` | — | 退 | T12 | — | 视频 |
| 101 | `DouYinVideo.store_video` | — | 退 | T12 | — | 视频 |
| 113 | `DouYinVideo.make_save_file_name` | — | 退 | T12 | — | 视频 |
| 126 | `DouYinVideo.save_video` | — | 退 | T12 | — | 视频 |

### `M/store/excel_store_base.py`（14；退14）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 52 | `ExcelStoreBase` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 64 | `ExcelStoreBase.get_instance` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 82 | `ExcelStoreBase.flush_all` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 96 | `ExcelStoreBase.__init__` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 147 | `ExcelStoreBase._apply_header_style` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 171 | `ExcelStoreBase._auto_adjust_column_width` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 193 | `ExcelStoreBase._write_headers` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 206 | `ExcelStoreBase._write_row` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 237 | `ExcelStoreBase.store_content` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 259 | `ExcelStoreBase.store_comment` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 279 | `ExcelStoreBase.store_creator` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 299 | `ExcelStoreBase.store_contact` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 323 | `ExcelStoreBase.store_dynamic` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |
| 347 | `ExcelStoreBase.flush` | — | 退 | T12 | F10 | 上游DB/Mongo/Excel存储 |

### `M/store/weibo/__init__.py`（14；迁6、退6、拆2）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 35 | `_first_present` | `platforms/weibo/parser.py` | 迁 | T05 | F03/F04 | 纯投影；写出经RecordSink |
| 42 | `_weibo_pic_url` | `platforms/weibo/parser.py` | 迁 | T05 | F03/F04 | 纯投影；写出经RecordSink |
| 58 | `_weibo_pic_urls` | `platforms/weibo/parser.py` | 迁 | T05 | F03/F04 | 纯投影；写出经RecordSink |
| 73 | `_weibo_pic_assets` | `platforms/weibo/parser.py` | 迁 | T05 | F03/F04 | 纯投影；写出经RecordSink |
| 99 | `WeibostoreFactory` | — | 退 | T12 | F11 | 评论/creator/工厂/视频 |
| 112 | `WeibostoreFactory.create_store` | — | 退 | T12 | F11 | 评论/creator/工厂/视频 |
| 119 | `batch_update_weibo_notes` | — | 退 | T12 | F11 | 评论/creator/工厂/视频 |
| 134 | `persisted_weibo_content_text` | `platforms/weibo/parser.py` | 迁 | T05 | F03/F04 | 纯投影；写出经RecordSink |
| 140 | `update_weibo_note` | `platforms/weibo/parser.py` | 迁 | T05 | F03/F04 | 纯投影；写出经RecordSink |
| 208 | `batch_update_weibo_note_comments` | — | 退 | T12 | F11 | 评论/creator/工厂/视频 |
| 224 | `update_weibo_note_comment` | — | 退 | T12 | F11 | 评论/creator/工厂/视频 |
| 261 | `update_weibo_note_images` | `platforms/weibo/core.py` | 拆 | T05 | F05 | 图片写出改经ImageStager端口 |
| 275 | `record_weibo_note_image_failure` | `platforms/weibo/core.py` | 拆 | T05 | F05 | 图片写出改经ImageStager端口 |
| 281 | `save_creator` | — | 退 | T12 | F11 | 评论/creator/工厂/视频 |

### `M/store/weibo/_store_impl.py`（28；退25、拆3）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 46 | `calculate_number_of_files` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 61 | `WeiboCsvStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 62 | `WeiboCsvStoreImplement.__init__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 66 | `WeiboCsvStoreImplement.store_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 77 | `WeiboCsvStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 88 | `WeiboCsvStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 101 | `WeiboDbStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 103 | `WeiboDbStoreImplement.store_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 129 | `WeiboDbStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 160 | `WeiboDbStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 173 | `WeiboJsonStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 174 | `WeiboJsonStoreImplement.__init__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 178 | `WeiboJsonStoreImplement.store_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 189 | `WeiboJsonStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 200 | `WeiboJsonStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 213 | `WeiboJsonlStoreImplement` | `artifacts/jsonl.py` | 拆 | T05 | F04/F09 | 仅JSONL内容出口 |
| 214 | `WeiboJsonlStoreImplement.__init__` | `artifacts/jsonl.py` | 拆 | T05 | F04/F09 | 仅JSONL内容出口 |
| 218 | `WeiboJsonlStoreImplement.store_content` | `artifacts/jsonl.py` | 拆 | T05 | F04/F09 | 仅JSONL内容出口 |
| 221 | `WeiboJsonlStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 224 | `WeiboJsonlStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 229 | `WeiboSqliteStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 236 | `WeiboMongoStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 239 | `WeiboMongoStoreImplement.__init__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 242 | `WeiboMongoStoreImplement.store_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 259 | `WeiboMongoStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 276 | `WeiboMongoStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 286 | `WeiboExcelStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 289 | `WeiboExcelStoreImplement.__new__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |

### `M/store/weibo/weibo_store_media.py`（4；拆4）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 39 | `WeiboStoreImage` | `artifacts/image_staging.py` | 拆 | T05 | F05 | 四站实现逐行差分；一致部分合并，差异作显式参数 |
| 40 | `WeiboStoreImage.__init__` | `artifacts/image_staging.py` | 拆 | T05 | F05 | 四站实现逐行差分；一致部分合并，差异作显式参数 |
| 49 | `WeiboStoreImage.store_post_images` | `artifacts/image_staging.py` | 拆 | T05 | F05 | 四站实现逐行差分；一致部分合并，差异作显式参数 |
| 75 | `WeiboStoreImage.record_failure` | `artifacts/image_staging.py` | 拆 | T05 | F05 | 四站实现逐行差分；一致部分合并，差异作显式参数 |

### `M/store/xhs/__init__.py`（18；迁9、退7、拆2）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 37 | `_xhs_image_assets` | `platforms/xhs/parser.py` | 迁 | T09 | F03/F04 | 纯投影；写出经RecordSink |
| 74 | `_first_nonempty` | `platforms/xhs/parser.py` | 迁 | T09 | F03/F04 | 纯投影；写出经RecordSink |
| 81 | `_nested_value` | `platforms/xhs/parser.py` | 迁 | T09 | F03/F04 | 纯投影；写出经RecordSink |
| 94 | `_interaction_count` | `platforms/xhs/parser.py` | 迁 | T09 | F03/F04 | 纯投影；写出经RecordSink |
| 114 | `_creator_basic_info` | `platforms/xhs/parser.py` | 迁 | T09 | F03/F04 | 纯投影；写出经RecordSink |
| 119 | `_creator_metric` | `platforms/xhs/parser.py` | 迁 | T09 | F03/F04 | 纯投影；写出经RecordSink |
| 128 | `_creator_profile_url` | `platforms/xhs/parser.py` | 迁 | T09 | F03/F04 | 纯投影；写出经RecordSink |
| 137 | `_normalized_creator_item` | `platforms/xhs/parser.py` | 迁 | T09 | F03/F04 | 纯投影；写出经RecordSink |
| 157 | `XhsStoreFactory` | — | 退 | T12 | F15 | 评论/creator/工厂/视频 |
| 170 | `XhsStoreFactory.create_store` | — | 退 | T12 | F15 | 评论/creator/工厂/视频 |
| 177 | `get_video_url_arr` | — | 退 | T12 | F15 | 评论/creator/工厂/视频 |
| 211 | `update_xhs_note` | `platforms/xhs/parser.py` | 迁 | T09 | F03/F04 | 纯投影；写出经RecordSink |
| 298 | `batch_update_xhs_note_comments` | — | 退 | T12 | F15 | 评论/creator/工厂/视频 |
| 314 | `update_xhs_note_comment` | — | 退 | T12 | F15 | 评论/creator/工厂/视频 |
| 345 | `save_creator` | — | 退 | T12 | F15 | 评论/creator/工厂/视频 |
| 361 | `update_xhs_note_images` | `platforms/xhs/core.py` | 拆 | T09 | F05 | 图片写出改经ImageStager端口 |
| 367 | `record_xhs_note_image_failure` | `platforms/xhs/core.py` | 拆 | T09 | F05 | 图片写出改经ImageStager端口 |
| 373 | `update_xhs_note_video` | — | 退 | T12 | F15 | 评论/creator/工厂/视频 |

### `M/store/xhs/_store_impl.py`（40；退36、拆4）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 42 | `XhsCsvStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 43 | `XhsCsvStoreImplement.__init__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 47 | `XhsCsvStoreImplement.store_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 55 | `XhsCsvStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 64 | `XhsCsvStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 67 | `XhsCsvStoreImplement.flush` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 71 | `XhsJsonStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 72 | `XhsJsonStoreImplement.__init__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 76 | `XhsJsonStoreImplement.store_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 84 | `XhsJsonStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 92 | `XhsJsonStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 95 | `XhsJsonStoreImplement.flush` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 104 | `XhsJsonlStoreImplement` | `artifacts/jsonl.py` | 拆 | T09 | F04/F09 | 仅JSONL内容出口 |
| 105 | `XhsJsonlStoreImplement.__init__` | `artifacts/jsonl.py` | 拆 | T09 | F04/F09 | 仅JSONL内容出口 |
| 109 | `XhsJsonlStoreImplement.store_content` | `artifacts/jsonl.py` | 拆 | T09 | F04/F09 | 仅JSONL内容出口 |
| 112 | `XhsJsonlStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 115 | `XhsJsonlStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 118 | `XhsJsonlStoreImplement.flush` | `artifacts/jsonl.py` | 拆 | T09 | F04/F09 | 仅JSONL内容出口 |
| 122 | `XhsDbStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 123 | `XhsDbStoreImplement.__init__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 126 | `XhsDbStoreImplement.store_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 136 | `XhsDbStoreImplement.add_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 179 | `XhsDbStoreImplement.update_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 212 | `XhsDbStoreImplement.content_is_exist` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 217 | `XhsDbStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 229 | `XhsDbStoreImplement.add_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 248 | `XhsDbStoreImplement.update_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 259 | `XhsDbStoreImplement.comment_is_exist` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 264 | `XhsDbStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 268 | `XhsDbStoreImplement.get_all_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 274 | `XhsDbStoreImplement.get_all_comments` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 281 | `XhsSqliteStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 282 | `XhsSqliteStoreImplement.__init__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 286 | `XhsMongoStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 289 | `XhsMongoStoreImplement.__init__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 293 | `XhsMongoStoreImplement.store_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 310 | `XhsMongoStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 327 | `XhsMongoStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 337 | `XhsExcelStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 340 | `XhsExcelStoreImplement.__new__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |

### `M/store/xhs/xhs_store_media.py`（9；拆4、退5）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 42 | `XiaoHongShuImage` | `artifacts/image_staging.py` | 拆 | T09 | F05 | 四站实现逐行差分；一致部分合并，差异作显式参数 |
| 43 | `XiaoHongShuImage.__init__` | `artifacts/image_staging.py` | 拆 | T09 | F05 | 四站实现逐行差分；一致部分合并，差异作显式参数 |
| 52 | `XiaoHongShuImage.store_post_images` | `artifacts/image_staging.py` | 拆 | T09 | F05 | 四站实现逐行差分；一致部分合并，差异作显式参数 |
| 78 | `XiaoHongShuImage.record_failure` | `artifacts/image_staging.py` | 拆 | T09 | F05 | 四站实现逐行差分；一致部分合并，差异作显式参数 |
| 96 | `XiaoHongShuVideo` | — | 退 | T12 | — | 视频 |
| 97 | `XiaoHongShuVideo.__init__` | — | 退 | T12 | — | 视频 |
| 103 | `XiaoHongShuVideo.store_video` | — | 退 | T12 | — | 视频 |
| 115 | `XiaoHongShuVideo.make_save_file_name` | — | 退 | T12 | — | 视频 |
| 128 | `XiaoHongShuVideo.save_video` | — | 退 | T12 | — | 视频 |

### `M/store/zhihu/__init__.py`（10；迁2、退6、拆2）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 41 | `zhihu_content_image_assets` | `platforms/zhihu/parser.py` | 迁 | T07 | F03/F04 | 纯投影；写出经RecordSink |
| 74 | `ZhihuStoreFactory` | — | 退 | T12 | F13 | 评论/creator/工厂/视频 |
| 87 | `ZhihuStoreFactory.create_store` | — | 退 | T12 | F13 | 评论/creator/工厂/视频 |
| 93 | `batch_update_zhihu_contents` | — | 退 | T12 | F13 | 评论/creator/工厂/视频 |
| 108 | `update_zhihu_content` | `platforms/zhihu/parser.py` | 迁 | T07 | F03/F04 | 纯投影；写出经RecordSink |
| 129 | `update_zhihu_content_images` | `platforms/zhihu/core.py` | 拆 | T07 | F05 | 图片写出改经ImageStager端口 |
| 135 | `record_zhihu_content_image_failure` | `platforms/zhihu/core.py` | 拆 | T07 | F05 | 图片写出改经ImageStager端口 |
| 142 | `batch_update_zhihu_note_comments` | — | 退 | T12 | F13 | 评论/creator/工厂/视频 |
| 158 | `update_zhihu_content_comment` | — | 退 | T12 | F13 | 评论/creator/工厂/视频 |
| 173 | `save_creator` | — | 退 | T12 | F13 | 评论/creator/工厂/视频 |

### `M/store/zhihu/_store_impl.py`（28；退25、拆3）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 45 | `calculate_number_of_files` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 60 | `ZhihuCsvStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 61 | `ZhihuCsvStoreImplement.__init__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 65 | `ZhihuCsvStoreImplement.store_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 76 | `ZhihuCsvStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 87 | `ZhihuCsvStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 92 | `ZhihuDbStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 93 | `ZhihuDbStoreImplement.store_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 121 | `ZhihuDbStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 143 | `ZhihuDbStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 148 | `ZhihuJsonStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 149 | `ZhihuJsonStoreImplement.__init__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 153 | `ZhihuJsonStoreImplement.store_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 164 | `ZhihuJsonStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 175 | `ZhihuJsonStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 180 | `ZhihuJsonlStoreImplement` | `artifacts/jsonl.py` | 拆 | T07 | F04/F09 | 仅JSONL内容出口 |
| 181 | `ZhihuJsonlStoreImplement.__init__` | `artifacts/jsonl.py` | 拆 | T07 | F04/F09 | 仅JSONL内容出口 |
| 185 | `ZhihuJsonlStoreImplement.store_content` | `artifacts/jsonl.py` | 拆 | T07 | F04/F09 | 仅JSONL内容出口 |
| 188 | `ZhihuJsonlStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 191 | `ZhihuJsonlStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 196 | `ZhihuSqliteStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 203 | `ZhihuMongoStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 206 | `ZhihuMongoStoreImplement.__init__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 209 | `ZhihuMongoStoreImplement.store_content` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 226 | `ZhihuMongoStoreImplement.store_comment` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 243 | `ZhihuMongoStoreImplement.store_creator` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 248 | `ZhihuExcelStoreImplement` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |
| 251 | `ZhihuExcelStoreImplement.__new__` | — | 退 | T12 | F10 | CSV/DB/JSON/SQLite/Mongo/Excel及评论/creator出口 |

### `M/store/zhihu/zhihu_store_media.py`（4；拆4）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 20 | `ZhihuStoreImage` | `artifacts/image_staging.py` | 拆 | T07 | F05 | 四站实现逐行差分；一致部分合并，差异作显式参数 |
| 21 | `ZhihuStoreImage.__init__` | `artifacts/image_staging.py` | 拆 | T07 | F05 | 四站实现逐行差分；一致部分合并，差异作显式参数 |
| 29 | `ZhihuStoreImage.store_post_images` | `artifacts/image_staging.py` | 拆 | T07 | F05 | 四站实现逐行差分；一致部分合并，差异作显式参数 |
| 55 | `ZhihuStoreImage.record_failure` | `artifacts/image_staging.py` | 拆 | T07 | F05 | 四站实现逐行差分；一致部分合并，差异作显式参数 |

### `M/tools/app_runner.py`（1；迁1）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 32 | `run` | `runtime/worker.py` | 迁 | T02 | F06 |  |

### `M/tools/async_file_writer.py`（7；迁4、退3）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 30 | `AsyncFileWriter` | `artifacts/jsonl.py` | 迁 | T04 | F04/F09 | 实例锁、当日路径、逐条追加不变 |
| 31 | `AsyncFileWriter.__init__` | `artifacts/jsonl.py` | 迁 | T04 | F04/F09 | 实例锁、当日路径、逐条追加不变 |
| 37 | `AsyncFileWriter._get_file_path` | `artifacts/jsonl.py` | 迁 | T04 | F04/F09 | 实例锁、当日路径、逐条追加不变 |
| 46 | `AsyncFileWriter.write_to_csv` | — | 退 | T12 | F10 | CSV/单JSON/词云出口 |
| 56 | `AsyncFileWriter.write_to_jsonl` | `artifacts/jsonl.py` | 迁 | T04 | F04/F09 | 实例锁、当日路径、逐条追加不变 |
| 62 | `AsyncFileWriter.write_single_item_to_json` | — | 退 | T12 | F10 | CSV/单JSON/词云出口 |
| 82 | `AsyncFileWriter.generate_wordcloud_from_comments` | — | 退 | T12 | F10 | CSV/单JSON/词云出口 |

### `M/tools/browser_launcher.py`（14；迁14）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 34 | `BrowserLauncher` | `runtime/browser_launcher.py` | 迁 | T02 | F06/F07 |  |
| 40 | `BrowserLauncher.__init__` | `runtime/browser_launcher.py` | 迁 | T02 | F06/F07 |  |
| 57 | `BrowserLauncher.cleanup_requested` | `runtime/browser_launcher.py` | 迁 | T02 | F06/F07 |  |
| 62 | `BrowserLauncher._process_pid` | `runtime/browser_launcher.py` | 迁 | T02 | F06/F07 |  |
| 69 | `BrowserLauncher._process_returncode` | `runtime/browser_launcher.py` | 迁 | T02 | F06/F07 |  |
| 79 | `BrowserLauncher._record_process_exit` | `runtime/browser_launcher.py` | 迁 | T02 | F06/F07 |  |
| 103 | `BrowserLauncher.process_status` | `runtime/browser_launcher.py` | 迁 | T02 | F06/F07 |  |
| 139 | `BrowserLauncher.xhs_window_size_argument` | `runtime/browser_launcher.py` | 迁 | T02 | F06/F07 |  |
| 152 | `BrowserLauncher.detect_browser_paths` | `runtime/browser_launcher.py` | 迁 | T02 | F06/F07 |  |
| 211 | `BrowserLauncher.find_available_port` | `runtime/browser_launcher.py` | 迁 | T02 | F06/F07 |  |
| 226 | `BrowserLauncher.launch_browser` | `runtime/browser_launcher.py` | 迁 | T02 | F06/F07 |  |
| 367 | `BrowserLauncher.wait_for_browser_ready` | `runtime/browser_launcher.py` | 迁 | T02 | F06/F07 |  |
| 406 | `BrowserLauncher.get_browser_info` | `runtime/browser_launcher.py` | 迁 | T02 | F06/F07 |  |
| 433 | `BrowserLauncher.cleanup` | `runtime/browser_launcher.py` | 迁 | T02 | F06/F07 |  |

### `M/tools/cdp_browser.py`（28；迁28）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 39 | `CDPBrowserLifecycleError` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 42 | `CDPBrowserLifecycleError.__init__` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 54 | `CDPBrowserManager` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 59 | `CDPBrowserManager.__init__` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 77 | `CDPBrowserManager.mark_planned_cleanup` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 83 | `CDPBrowserManager._planned_close_reason` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 92 | `CDPBrowserManager.lifecycle_snapshot` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 106 | `CDPBrowserManager._capture_lifecycle_event` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 147 | `CDPBrowserManager._observe_browser` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 168 | `CDPBrowserManager._observe_browser_context` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 187 | `CDPBrowserManager.assert_alive` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 247 | `CDPBrowserManager._register_cleanup_handlers` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 299 | `CDPBrowserManager.launch_and_connect` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 353 | `CDPBrowserManager._connect_existing_browser` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 411 | `CDPBrowserManager._get_browser_path` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 441 | `CDPBrowserManager._test_cdp_connection` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 464 | `CDPBrowserManager._clean_session_restore_tabs` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 500 | `CDPBrowserManager._launch_browser` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 552 | `CDPBrowserManager._get_browser_websocket_url` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 577 | `CDPBrowserManager._connect_via_cdp` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 627 | `CDPBrowserManager._create_browser_context` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 667 | `CDPBrowserManager.add_stealth_script` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 680 | `CDPBrowserManager.add_cookies` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 691 | `CDPBrowserManager.get_cookies` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 704 | `CDPBrowserManager._record_cancelled_cleanup` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 748 | `CDPBrowserManager.cleanup` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 897 | `CDPBrowserManager.is_connected` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |
| 903 | `CDPBrowserManager.get_browser_info` | `runtime/browser.py` | 迁 | T02 | F06/F07 |  |

### `M/tools/crawler_util.py`（12；迁10、退2）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 43 | `find_login_qrcode` | `runtime/login_helpers.py` | 迁 | T02 | F07 |  |
| 66 | `find_qrcode_img_from_canvas` | `runtime/login_helpers.py` | 迁 | T02 | F07 |  |
| 88 | `show_qrcode` | `runtime/login_helpers.py` | 迁 | T02 | F07 |  |
| 105 | `get_user_agent` | `runtime/helpers.py` | 迁 | T03 | F09 | 随机UA显式随机源；URL参数解析供DY/XHS |
| 131 | `get_mobile_user_agent` | `runtime/helpers.py` | 迁 | T03 | F09 | 随机UA显式随机源；URL参数解析供DY/XHS |
| 138 | `convert_cookies` | `runtime/cookies.py` | 迁 | T03 | F09 |  |
| 148 | `convert_browser_context_cookies` | `runtime/cookies.py` | 迁 | T03 | F09 |  |
| 159 | `convert_str_cookie_to_dict` | `runtime/cookies.py` | 迁 | T03 | F09 |  |
| 177 | `match_interact_info_count` | — | 退 | T12 | F10 | 闭包内无调用 |
| 189 | `format_proxy_info` | — | 退 | T12 | F10 | 仅ENABLE_IP_PROXY分支；随proxy切片退出 |
| 215 | `extract_text_from_html` | `runtime/helpers.py` | 迁 | T03 | F09 | 随机UA显式随机源；URL参数解析供DY/XHS |
| 226 | `extract_url_params_to_dict` | `runtime/helpers.py` | 迁 | T03 | F09 | 随机UA显式随机源；URL参数解析供DY/XHS |

### `M/tools/easing.py`（7；迁7）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 32 | `ease_in_quad` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | 仅滑块轨迹 |
| 36 | `ease_out_quad` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | 仅滑块轨迹 |
| 40 | `ease_out_quart` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | 仅滑块轨迹 |
| 44 | `ease_out_expo` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | 仅滑块轨迹 |
| 51 | `ease_out_bounce` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | 仅滑块轨迹 |
| 67 | `ease_out_elastic` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | 仅滑块轨迹 |
| 77 | `get_tracks` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | 仅滑块轨迹 |

### `M/tools/file_header_manager.py`（8；退8）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 53 | `get_file_relative_path` | — | 退 | T12 | — | 上游开发工具 |
| 67 | `generate_copyright_header` | — | 退 | T12 | — | 上游开发工具 |
| 90 | `has_copyright_header` | — | 退 | T12 | — | 上游开发工具 |
| 104 | `has_disclaimer` | — | 退 | T12 | — | 上游开发工具 |
| 117 | `find_insert_position` | — | 退 | T12 | — | 上游开发工具 |
| 147 | `process_file` | — | 退 | T12 | — | 上游开发工具 |
| 213 | `find_python_files` | — | 退 | T12 | — | 上游开发工具 |
| 240 | `main` | — | 退 | T12 | — | 上游开发工具 |

### `M/tools/httpx_util.py`（1；迁1）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 6 | `make_async_client` | `runtime/http.py` | 迁 | T03 | F09 | verify在每次构造读取 |

### `M/tools/image_download_retry.py`（7；迁7）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 21 | `ImageDownloadFetchError` | `runtime/image_retry.py` | 迁 | T04 | F05/F09 | 四站client使用classified_http_image_error |
| 24 | `ImageDownloadFetchError.__init__` | `runtime/image_retry.py` | 迁 | T04 | F05/F09 | 四站client使用classified_http_image_error |
| 39 | `ImageDownloadFetchError.with_attempts` | `runtime/image_retry.py` | 迁 | T04 | F05/F09 | 四站client使用classified_http_image_error |
| 49 | `classified_http_image_error` | `runtime/image_retry.py` | 迁 | T04 | F05/F09 | 四站client使用classified_http_image_error |
| 77 | `is_retryable_image_error` | `runtime/image_retry.py` | 迁 | T04 | F05/F09 | 四站client使用classified_http_image_error |
| 83 | `is_runtime_blocking_image_error` | `runtime/image_retry.py` | 迁 | T04 | F05/F09 | 四站client使用classified_http_image_error |
| 89 | `fetch_image_bytes_with_retry` | `runtime/image_retry.py` | 迁 | T04 | F05/F09 | 四站client使用classified_http_image_error |

### `M/tools/image_manifest.py`（17；迁17）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 38 | `ImageStagingError` | `artifacts/image_staging.py` | 迁 | T04 | F05 | 经contracts.ImageStager端口供平台调用 |
| 41 | `ImageStagingError.__init__` | `artifacts/image_staging.py` | 迁 | T04 | F05 | 经contracts.ImageStager端口供平台调用 |
| 48 | `ImageAsset` | `artifacts/image_staging.py` | 迁 | T04 | F05 | 经contracts.ImageStager端口供平台调用 |
| 58 | `InspectedImage` | `artifacts/image_staging.py` | 迁 | T04 | F05 | 经contracts.ImageStager端口供平台调用 |
| 67 | `looks_like_supported_raster` | `artifacts/image_staging.py` | 迁 | T04 | F05 | 经contracts.ImageStager端口供平台调用 |
| 77 | `normalize_image_url` | `runtime/helpers.py` | 迁 | T04 | F05 | DY/ZH投影共用纯函数 |
| 97 | `weibo_source_asset_key` | `platforms/weibo/parser.py` | 迁 | T05 | F05/F11 | 本站图片稳定键 |
| 106 | `xhs_source_asset_key` | `platforms/xhs/parser.py` | 迁 | T09 | F05/F15 |  |
| 117 | `zhihu_source_asset_key` | `platforms/zhihu/parser.py` | 迁 | T07 | F05/F13 |  |
| 131 | `douyin_source_asset_key` | `platforms/douyin/parser.py` | 迁 | T06 | F05/F12 |  |
| 139 | `inspect_image_bytes` | `artifacts/image_staging.py` | 迁 | T04 | F05 | 经contracts.ImageStager端口供平台调用 |
| 186 | `_safe_component` | `artifacts/image_staging.py` | 迁 | T04 | F05 | 经contracts.ImageStager端口供平台调用 |
| 195 | `_fsync_directory` | `artifacts/image_staging.py` | 迁 | T04 | F05 | 经contracts.ImageStager端口供平台调用 |
| 203 | `_manifest_payload` | `artifacts/image_staging.py` | 迁 | T04 | F05 | 经contracts.ImageStager端口供平台调用 |
| 220 | `upsert_manifest_rows_atomic` | `artifacts/image_staging.py` | 迁 | T04 | F05 | 经contracts.ImageStager端口供平台调用 |
| 260 | `failed_manifest_row` | `artifacts/image_staging.py` | 迁 | T04 | F05 | 经contracts.ImageStager端口供平台调用 |
| 294 | `stage_post_images` | `artifacts/image_staging.py` | 迁 | T04 | F05 | 经contracts.ImageStager端口供平台调用 |

### `M/tools/slider_util.py`（9；迁9）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 34 | `Slide` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | cv2/numpy仅在此处保留 |
| 39 | `Slide.__init__` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | cv2/numpy仅在此处保留 |
| 55 | `Slide.check_is_img_path` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | cv2/numpy仅在此处保留 |
| 84 | `Slide.clear_white` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | cv2/numpy仅在此处保留 |
| 108 | `Slide.template_match` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | cv2/numpy仅在此处保留 |
| 126 | `Slide.image_edge_detection` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | cv2/numpy仅在此处保留 |
| 130 | `Slide.discern` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | cv2/numpy仅在此处保留 |
| 145 | `get_track_simple` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | cv2/numpy仅在此处保留 |
| 178 | `get_tracks` | `platforms/douyin/login_support.py` | 迁 | T06 | F07 | cv2/numpy仅在此处保留 |

### `M/tools/time_util.py`（10；迁4、退6）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 30 | `get_current_timestamp` | `runtime/helpers.py` | 迁 | T05 | F03/F11 | 时钟显式注入 |
| 38 | `get_current_time` | — | 退 | T12 | — | 闭包内无调用 |
| 45 | `get_current_time_hour` | — | 退 | T12 | — | 闭包内无调用 |
| 52 | `get_current_date` | `artifacts/jsonl.py` | 迁 | T04 | F04 | JSONL当日文件名 |
| 60 | `get_time_str_from_unix_time` | — | 退 | T12 | — | 闭包内无调用 |
| 71 | `get_date_str_from_unix_time` | — | 退 | T12 | — | 闭包内无调用 |
| 82 | `get_unix_time_from_time_str` | — | 退 | T12 | — | 闭包内无调用 |
| 97 | `get_unix_timestamp` | — | 退 | T12 | — | 闭包内无调用 |
| 101 | `rfc2822_to_china_datetime` | `runtime/helpers.py` | 迁 | T05 | F03/F11 | 时钟显式注入 |
| 113 | `rfc2822_to_timestamp` | `runtime/helpers.py` | 迁 | T05 | F03/F11 | 时钟显式注入 |

### `M/tools/trippostcollect_adaptive.py`（17；拆3、迁14）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 15 | `env_int` | `application/worker_inputs.py` | 拆 | T03 | F01/F09 | 四站core读TOP_REFRESH/RESUME值；解析失败回默认值不变 |
| 24 | `_utc_iso` | `application/events.py` | 迁 | T04 | F02/F09 | legacy宽容出口 |
| 28 | `existing_platform_identities` | `db/discovery_read.py` | 拆 | T03 | F01/F09 | env读取改显式输入；错误吞并子集不变 |
| 131 | `append_execution_event` | `application/events.py` | 迁 | T04 | F02/F09 | legacy宽容出口 |
| 148 | `AdaptiveAccumulator` | `application/candidates.py` | 迁 | T03 | F01/F09 |  |
| 177 | `AdaptiveAccumulator.from_environment` | `application/candidates.py` | 拆 | T03 | F01 | 保留微博stagnation_basis=candidate_identity，其余valid_new |
| 187 | `AdaptiveAccumulator.can_continue` | `application/candidates.py` | 迁 | T03 | F01/F09 |  |
| 190 | `AdaptiveAccumulator.begin_batch` | `application/candidates.py` | 迁 | T03 | F01/F09 |  |
| 195 | `AdaptiveAccumulator.is_known` | `application/candidates.py` | 迁 | T03 | F01/F09 |  |
| 205 | `AdaptiveAccumulator.consider` | `application/candidates.py` | 迁 | T03 | F01/F09 |  |
| 216 | `AdaptiveAccumulator.skip_candidate_failure` | `application/candidates.py` | 迁 | T03 | F01/F09 |  |
| 259 | `AdaptiveAccumulator._record_source` | `application/candidates.py` | 迁 | T03 | F01/F09 |  |
| 286 | `AdaptiveAccumulator.finish_batch` | `application/candidates.py` | 迁 | T03 | F01/F09 |  |
| 356 | `AdaptiveAccumulator.mark_source_exhausted` | `application/candidates.py` | 迁 | T03 | F01/F09 |  |
| 390 | `AdaptiveAccumulator.mark_runtime_failed` | `application/candidates.py` | 迁 | T03 | F01/F09 |  |
| 417 | `AdaptiveAccumulator.summary` | `application/candidates.py` | 迁 | T03 | F01/F09 |  |
| 450 | `should_reseed_douyin_frontier` | `application/candidates.py` | 迁 | T06 | F01/F12 | 抖音core:611调用；前沿纪元判定属application，经注入端口调用 |

### `M/tools/trippostcollect_behavior.py`（10；拆10）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 14 | `_enabled` | `application/worker_inputs.py` | 拆 | T04 | F07 |  |
| 18 | `project_browser_args` | `runtime/behavior.py` | 拆 | T04 | F07/F10 | 删除sys.path注入 |
| 29 | `install_project_runtime_hints` | `runtime/behavior.py` | 拆 | T04 | F07/F10 | 删除sys.path注入 |
| 43 | `run_required_human_behavior` | `runtime/behavior.py` | 拆 | T04 | F07/F10 | 删除sys.path注入 |
| 66 | `run_required_request_pause` | `platforms/xhs/behavior.py` | 拆 | T09 | F07/F15 | 仅XHS调用的桥入口，改注入callback |
| 88 | `run_required_continuity_behavior` | `platforms/xhs/behavior.py` | 拆 | T09 | F07/F15 | 仅XHS调用的桥入口，改注入callback |
| 108 | `run_required_api_captcha_verification` | `platforms/xhs/behavior.py` | 拆 | T09 | F07/F15 | 仅XHS调用的桥入口，改注入callback |
| 139 | `inspect_visible_page_state` | `platforms/xhs/behavior.py` | 拆 | T09 | F07/F15 | 仅XHS调用的桥入口，改注入callback |
| 152 | `record_platform_security_limit` | `platforms/xhs/behavior.py` | 拆 | T09 | F07/F15 | 仅XHS调用的桥入口，改注入callback |
| 180 | `run_requested_post_interaction` | `platforms/xhs/behavior.py` | 拆 | T09 | F07/F15 | 仅XHS调用的桥入口，改注入callback |

### `M/tools/user_hash.py`（2；迁2）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 11 | `anonymize_user_id` | `records/identity.py` | 迁 | T04 | F04 |  |
| 22 | `mask_nickname` | `records/identity.py` | 迁 | T04 | F04 |  |

### `M/tools/utils.py`（2；拆1、迁1）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 29 | `init_loging_config` | `runtime/worker.py` | 拆 | T04 | F04 | logger经净化出口 |
| 47 | `str2bool` | `application/worker_inputs.py` | 迁 | T02 | F10 | 输入集合不变 |

### `M/tools/words.py`（5；退5）

| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |
|---:|---|---|---|---|---|---|
| 36 | `AsyncWordCloudGenerator` | — | 退 | T12 | — | 词云关闭 |
| 37 | `AsyncWordCloudGenerator.__init__` | — | 退 | T12 | — | 词云关闭 |
| 46 | `AsyncWordCloudGenerator.load_stop_words` | — | 退 | T12 | — | 词云关闭 |
| 50 | `AsyncWordCloudGenerator.generate_word_frequency_and_cloud` | — | 退 | T12 | — | 词云关闭 |
| 67 | `AsyncWordCloudGenerator.generate_word_cloud` | — | 退 | T12 | — | 词云关闭 |

## 维护规则

- 本附录由 AST 枚举与处置规则生成，不手工增删行。源码基线变化后，T00 必须按新基线重新枚举并解释每处差异。
- 新增、删除或改名的定义没有规则时，枚举失败即视为账目不完整，不得进入对应实施卡。
- 处置从“退”改为保留，或从保留改为“退”，属于行为范围变更，须同时修改详细规格对应 C 小节与 G 任务卡。
