# B站 article

B站正式能力只抓取 article/专栏图文。来源耗尽、候选失败、图片重试、事务和 checkpoint 的共享语义
见[正式抓取执行契约](../formal-crawl-contract.md)；本文只记录 B站差异。

## 入口

- 正式入口：`crawl_runner.py` 调用 `mediacrawler_crawl.py --platforms bilibili`。
- 配置仍使用 `job_kind=mediacrawler_search`，实际分派到项目自有 `run_bilibili_article_search()`，
  不进入第三方视频搜索或历史修复入口。
- 单页 Opus 页面证据不代表 B站正式平台轮次。
- 发现 checkpoint、seen、人工排除和跨次累计复用通用控制面。

只冻结当前长期 B站 job：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --job-key mc_bilibili_qingdao_laoshan_guide_article
```

计划必须包含 `--platforms bilibili`、正式字段 profile、正文图片参数和来源耗尽策略。

## 详情与字段来源

搜索接口只负责发现 article ID 和保存预览证据。搜索 `desc`、标题与预览 `image_urls` 不能作为完整
正文或正文图片。每个未知 ID 必须执行 article 详情和作者关系统计补全：

- 正文来自成功观察的 article/Opus 详情，保存
  `content_detail_status=detail_observed`、`content_detail_source=article_view_api`。
- 正文规范化保留段落换行，不使用摘要清洗器把全文压成单行。
- 正文图按 `opus.content.paragraphs[].pic.pics`、详情 HTML、`content_pic_list` 顺序合并去重；
  仅当这些来源都未提取到图片时，才使用详情响应的 `origin_image_urls` / `image_urls`。
- 搜索预览图、Opus 封面、作者头像与视频资源均不属于正文图。
- 作者粉丝来自 `mid` 关系统计，要求数值、`followers_observed=true` 和
  `author_followers_source=relation_stat`。
- 正式搜索记录的发布时间来自搜索响应原始 `pubdate` / `pub_time`；详情补全保留该值，
  不以详情时间覆盖，也不以抓取时间补缺。
- 去重键为 article 内容 ID。

正式记录使用 `image_post_with_followers_v1`。详情已观察且平台明确证明删除、私密或字段永久不符时，
可以形成决定性无效结果；超时、空响应、HTTP 5xx 或解析失败必须先有限重试，耗尽后形成
`candidate_skipped(failure_scope=post)`，不能伪装为永久无效。

## 图片资产差异

只有详情观察后的正文 `image_urls` 生成 `ImageCandidate(role=content)`，来源键为 `image_urls`。
BFS/CDN URL 去除已知变换后缀后形成稳定资产键，避免协议或变换参数制造重复资产。图片请求复用行为
阶段导出的 B站 Cookie 和 article Referer。

平台层写本轮 staging 与 `image_manifest.jsonl`；根项目负责字节复验、同帖 SHA-256 去重、晋升和
SQLite 事务。共享错误码和重试规则不在本文重复，见[正式契约](../formal-crawl-contract.md)与
[数据持久化](../data-persistence.md)。

## 搜索、阻断与安全前沿

article 搜索前必须在持久 profile 完成共享行为阶段，并让搜索、详情和关系统计请求复用同一会话
Cookie。HTTP 401/403/429、业务码 `-101/-509/-412/-352`、登录、授权、频控或风控属于运行级
阻断；保留当前页且不把该 ID 写入 seen。

首次从第 1 页开始；有 checkpoint 时先刷新顶部，再从 `resume_page` 继续。数据库、累计摘要、seen、
人工排除和本轮已处理集合中的 ID 在详情前跳过。完整深层页保存下一页；页中途停止保留当前页。

批次仍按是否发现未知 article ID 记录停滞诊断，而不是按有效图文数量；该诊断不触发停止。详情或
图片候选级失败形成 `candidate_skipped` 后可作为已处理候选继续跨页，但不增加有效记录数。排障时
同时查看新 ID、详情成功、候选跳过和有效记录数，正式完成仍只接受来源耗尽。

## 历史事件

2026-08-02 发现旧正式分支曾把搜索摘要当完整正文。事件范围、历史数量、校验和最终处置只在
[B站正文完整性事件](../incidents/2026-08-02-bilibili-article-completeness.md)记录。历史回填入口不属于
正常抓取流程，其执行计划从 Git 历史查询。

## 防回归测试

- `tests/test_bilibili_formal_route.py`：配置、runner 路由和 child 命令。
- `tests/test_bilibili_article_detail.py`：摘要拒绝、详情正文/图片、重试和阻断。
- `tests/test_discovery_checkpoints.py`：已知 ID 过滤和安全前沿。
