# 抓取架构

## 组件关系

```text
config/crawl_targets.json
  -> scripts/crawl_runner.py
      -> data/runtime/crawl_execution_states/<run_id>/<job>.json
      -> scripts/mediacrawler_crawl.py
          -> tools/MediaCrawler
          -> formal_validation
          -> web_posts / web_post_images
      -> scripts/ctf_resource_crawl.py
          -> ctf_captures / ctf_capture_images
          -> scripts/import_ctf_captures.py
          -> web_posts / web_post_images
```

`crawl_runner.py` 是唯一正式入口，负责选择任务、冻结计划、执行逐步门禁、分类失败和
生成运行报告。执行器不能绕过状态文件直接让调度器宣布完成。

## 冻结状态

调度器为每个任务冻结配置、执行契约、任务参数和命令。业务阶段固定为计划、命令、
产物、持久化和最终确认。每次进入下一阶段都重新读取磁盘状态并校验 SHA-256；失败
后的阶段保持冻结。

MediaCrawler 的自适应分页会向同一状态文件追加批次事件，包括实际候选、有效唯一数、
本批新增和连续停滞次数。状态事件是过程证据，最终成功仍以正式校验和数据库验证为准。

## 结构化平台

| 平台 | 入口 | 平台文档 |
|---|---|---|
| B站 article | `mediacrawler_crawl.py --platforms bilibili` | [B站](platforms/bilibili.md) |
| 微博 | MediaCrawler 搜索 | [微博](platforms/weibo.md) |
| 小红书 | MediaCrawler 搜索 + 作者主页 | [小红书](platforms/xhs.md) |
| 抖音 | MediaCrawler 搜索 + 图文作者主页 | [抖音](platforms/douyin.md) |
| 知乎 | MediaCrawler 搜索 | [知乎](platforms/zhihu.md) |

结构化执行器先生成 JSONL，再按正式 profile 过滤视频、去重、校验图片/时间/作者/粉丝
和互动字段。只有有效唯一集合达到目标才进入入库；导入报告区分处理、新增和更新。

## 页面证据平台

携程、去哪儿和豆瓣小组由 `ctf_resource_crawl.py` 抓取固定 URL。页面证据先进入
`ctf_captures`，符合内容条件的详情页再归一化到 `web_posts`。详细限制见
[页面证据平台](platforms/page-evidence.md)。

## 辅助入口

- `mediacrawler_login_warmup.py`：刷新 MediaCrawler 平台登录态。
- `ctf_login_warmup.py`：刷新页面证据平台登录态。
- `mediacrawler_batch_validate.py`：开发期严格字段验证。
- `info_collection_benchmark.py`：性能和容量评估。

辅助入口不创建完整正式阶段，不能替代 `crawl_runner.py`。
